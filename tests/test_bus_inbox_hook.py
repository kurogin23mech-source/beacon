"""Tests for the bus-inbox hook (ms-54 / e-1140).

The hook script is loaded as a module so its `_render_context`, `_format_event`,
and the delivery-mode dispatch in `main` can be exercised without spawning a
subprocess on every assertion. Network calls are patched out — these tests
lock in the **shape** of the inject (which event modes appear, how they're
formatted, whether the cursor advances), not the wire layer (covered by
test_bus_transport.py).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "bin" / "beacon-bus-inbox-hook.py"


@pytest.fixture(scope="module")
def hook_module():
    """Import the hook script as a module."""
    spec = importlib.util.spec_from_file_location("bus_inbox_hook", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bus_inbox_hook"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    """Minimal beacon project on disk so _find_beacon_root + _load_session_id
    succeed without hitting the real filesystem."""
    root = tmp_path / "proj"
    (root / ".beacon").mkdir(parents=True)
    (root / ".beacon" / "project.json").write_text("{}")
    (root / ".beacon" / "session.json").write_text(
        json.dumps({"session_id": "sess-X"}))
    (root / ".beacon" / "cloud.json").write_text(
        json.dumps({"api_url": "https://api.test", "project_id": "proj-1"}))

    # Force credentials lookup to a known path with an id_token.
    creds_dir = tmp_path / "config" / "beacon"
    creds_dir.mkdir(parents=True)
    (creds_dir / "credentials.json").write_text(
        json.dumps({"id_token": "fake-token"}))
    monkeypatch.setenv("HOME", str(tmp_path))

    return SimpleNamespace(root=root, tmp=tmp_path)


def _make_event(eid: str, *, channel="dm", delivery="propose-to-ai",
                sender="other-sess", payload=None, created_at=None) -> dict:
    return {
        "event_id": eid,
        "channel": channel,
        "delivery": delivery,
        "sender_session_id": sender,
        "payload": payload or {},
        "created_at": created_at or "2026-06-07T01:30:00.000000Z",
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def test_format_event_includes_id_channel_sender_payload(hook_module):
    ev = _make_event("ev-1", channel="dm", sender="alice",
                      payload={"text": "hi"})
    out = hook_module._format_event(ev)
    assert "ev-1" in out
    assert "channel=dm" in out
    assert "from=alice" in out
    assert "\"text\": \"hi\"" in out


def test_render_context_lists_all_inject_events(hook_module):
    events = [_make_event("ev-1"), _make_event("ev-2")]
    out = hook_module._render_context(events, notify_only_count=0,
                                       monitor_suggested=False)
    assert "ev-1" in out
    assert "ev-2" in out
    assert "AI コンテキスト inject 対象: 2 件" in out


def test_pending_approval_banner_surfaces_and_leads(hook_module):
    """ms-97 C3 (review H3): an event carrying ``pending_approval`` must
    render the [DM-PENDING-APPROVAL] banner, name the event_id, point at
    /beacon-dm-respond, and appear BEFORE the generic event list so the human
    decision is unmissable."""
    events = [
        _make_event("ev-normal"),
        _make_event("ev-gated", sender="attacker-sess"),
    ]
    events[1]["pending_approval"] = True
    out = hook_module._render_context(events, notify_only_count=0,
                                       monitor_suggested=False)
    assert "[DM-PENDING-APPROVAL]" in out
    assert "ev-gated" in out
    assert "attacker-sess" in out
    assert "/beacon-dm-respond" in out
    # Banner precedes the generic event list.
    assert out.index("[DM-PENDING-APPROVAL]") < out.index(
        "AI コンテキスト inject 対象")


def test_no_pending_banner_when_no_gated_events(hook_module):
    """No pending event → no banner (= zero-noise for the common path)."""
    out = hook_module._render_context([_make_event("ev-1")],
                                       notify_only_count=0,
                                       monitor_suggested=False)
    assert "[DM-PENDING-APPROVAL]" not in out


def test_render_context_mentions_notify_only_count_when_nonzero(hook_module):
    events = [_make_event("ev-1")]
    out = hook_module._render_context(events, notify_only_count=3,
                                       monitor_suggested=False)
    assert "notify-user-only" in out
    assert "3 件" in out
    # the notify-only events themselves must NOT appear in context — only
    # the count does (this is the privacy contract).
    assert "log にだけ流した" in out


def test_render_context_monitor_suggestion_only_at_session_start(hook_module):
    """Monitor advice is welcome at session-start (fresh session, user hasn't
    armed anything) but becomes noise on every prompt — the hook trims it."""
    out_with = hook_module._render_context(
        [_make_event("ev-1")], notify_only_count=0, monitor_suggested=True)
    out_without = hook_module._render_context(
        [_make_event("ev-1")], notify_only_count=0, monitor_suggested=False)
    assert "bus listen --auto-ack" in out_with
    assert "bus listen --auto-ack" not in out_without


def test_render_context_surfaces_budget_when_armed(hook_module):
    """The "残ターン" piece of the autonomous-DM vision — without surfacing
    the count the AI has no way to know it's about to be cut off."""
    budget = {"total": 5, "used": 2, "granted_at": "2026-06-07T01:50:00Z"}
    out = hook_module._render_context(
        [_make_event("ev-1")], notify_only_count=0,
        monitor_suggested=False, budget=budget)
    assert "BUDGET" in out
    assert "2/5 used" in out
    assert "3" in out  # remaining


def test_render_context_no_budget_line_when_not_armed(hook_module):
    """No budget file → no budget line. Prevents noisy output for ordinary
    one-off sends from CLI users who haven't opted into autonomous mode."""
    out = hook_module._render_context(
        [_make_event("ev-1")], notify_only_count=0,
        monitor_suggested=False, budget=None)
    assert "BUDGET" not in out


def test_render_context_marks_budget_exhausted(hook_module):
    """When the count is already 0 the inject must say so — even if events
    arrive, the AI can't reply until the budget is re-granted."""
    budget = {"total": 5, "used": 5}
    out = hook_module._render_context(
        [_make_event("ev-1")], notify_only_count=0,
        monitor_suggested=False, budget=budget)
    assert "exhausted" in out


# ---------------------------------------------------------------------------
# Delivery mode dispatch — full main() with HTTP patched
# ---------------------------------------------------------------------------

def _run_main(hook_module, fake_project, events, hook_event_name,
              monkeypatch, capsys, *, force_no_creds=False):
    """Drive main() with mocked network and supplied stdin."""
    acks: list[tuple[str, str, str]] = []  # (project_id, recipient_id, ts)

    def fake_list_unread(api_url, project_id, recipient_id, token, limit=50):
        return list(events)

    def fake_ack(api_url, project_id, recipient_id, last_seen_at, token):
        acks.append((project_id, recipient_id, last_seen_at))

    monkeypatch.setattr(hook_module, "_list_unread", fake_list_unread)
    monkeypatch.setattr(hook_module, "_ack_cursor", fake_ack)
    # Path.home() doesn't always honor monkeypatched HOME on macOS (see
    # CPython getpwuid path), so stub the credential lookup directly. The
    # credentials-missing path is exercised by force_no_creds below.
    if not force_no_creds:
        monkeypatch.setattr(hook_module, "_load_id_token", lambda: "fake-token")
    else:
        monkeypatch.setattr(hook_module, "_load_id_token", lambda: "")

    payload = json.dumps({
        "cwd": str(fake_project.root),
        "session_id": "sess-X",
        "hook_event_name": hook_event_name,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    hook_module.main()
    captured = capsys.readouterr()
    return captured.out, captured.err, acks


def test_main_injects_propose_to_ai_events(hook_module, fake_project,
                                            monkeypatch, capsys):
    events = [_make_event("ev-1", delivery="propose-to-ai")]
    out, _, acks = _run_main(hook_module, fake_project, events,
                              "UserPromptSubmit", monkeypatch, capsys)
    assert out.strip(), "must emit additionalContext for propose-to-ai events"
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "ev-1" in payload["hookSpecificOutput"]["additionalContext"]
    # Cursor advanced to the event's created_at — otherwise the next prompt
    # would replay the same event (regression for the "I keep seeing the
    # same DM" class of bug).
    assert acks and acks[0][2] == events[0]["created_at"]


def test_main_routes_notify_user_only_to_log_not_context(hook_module,
                                                          fake_project,
                                                          monkeypatch,
                                                          capsys):
    """notify-user-only payloads MUST NOT appear in additionalContext — that's
    the delivery-mode contract for keeping UI-only notifications out of the AI
    decision loop. The events still go to .beacon/bus-inbox.log so the user
    can review them from the terminal.

    ms-160 e-5799: when notify-only arrives ALONE the hook now emits a concise
    count + pointer block (previously it returned silently, swallowing the fact
    that anything arrived). The block is metadata only — the payload body / the
    raw event_id must still stay out of AI context."""
    events = [_make_event("notify-1", delivery="notify-user-only",
                          payload={"text": "user notice"})]
    out, _, acks = _run_main(hook_module, fake_project, events,
                              "UserPromptSubmit", monkeypatch, capsys)
    # A pointer IS surfaced now (not the old silent swallow) ...
    assert out.strip(), "notify-only-alone must surface a pointer"
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "1 件" in ctx and "bus-inbox.log" in ctx
    # ... but the payload body and raw event id must NOT leak into context.
    assert "user notice" not in ctx
    assert "notify-1" not in ctx
    inbox_log = fake_project.root / ".beacon" / "bus-inbox.log"
    assert inbox_log.exists()
    content = inbox_log.read_text()
    assert "notify-1" in content
    # Cursor still advanced — otherwise the same notify-only event would
    # accumulate in the log forever.
    assert acks and acks[0][2] == events[0]["created_at"]


def test_main_mixed_modes_split_correctly(hook_module, fake_project,
                                           monkeypatch, capsys):
    events = [
        _make_event("propose-1", delivery="propose-to-ai"),
        _make_event("notify-1", delivery="notify-user-only"),
        _make_event("auto-1", delivery="auto-execute"),
    ]
    out, _, _ = _run_main(hook_module, fake_project, events,
                           "UserPromptSubmit", monkeypatch, capsys)
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    # propose + auto-execute land in context (auto is treated as propose
    # until the opt-in enforcement lands)
    assert "propose-1" in ctx
    assert "auto-1" in ctx
    # notify-user-only is excluded from context
    assert "notify-1" not in ctx
    # but the count is mentioned
    assert "1 件" in ctx


def test_main_unknown_delivery_falls_back_to_propose(hook_module, fake_project,
                                                      monkeypatch, capsys):
    """Defense in depth: server coerces unknown modes to the default, but the
    hook must also tolerate a stale payload that slipped through."""
    events = [_make_event("ev-?", delivery="future-mode-not-yet-shipped")]
    out, _, _ = _run_main(hook_module, fake_project, events,
                           "UserPromptSubmit", monkeypatch, capsys)
    assert out.strip()
    assert "ev-?" in out


def test_main_silent_when_no_events(hook_module, fake_project, monkeypatch,
                                     capsys):
    out, err, acks = _run_main(hook_module, fake_project, [],
                                "UserPromptSubmit", monkeypatch, capsys)
    assert out == ""
    assert acks == [], "no events ⇒ no cursor advance"


def test_main_session_start_event_includes_monitor_advice(hook_module,
                                                           fake_project,
                                                           monkeypatch,
                                                           capsys):
    events = [_make_event("ev-1", delivery="propose-to-ai")]
    out, _, _ = _run_main(hook_module, fake_project, events, "SessionStart",
                           monkeypatch, capsys)
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "bus listen --auto-ack" in ctx


def test_main_user_prompt_submit_skips_monitor_advice(hook_module,
                                                       fake_project,
                                                       monkeypatch, capsys):
    events = [_make_event("ev-1", delivery="propose-to-ai")]
    out, _, _ = _run_main(hook_module, fake_project, events,
                           "UserPromptSubmit", monkeypatch, capsys)
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "bus listen --auto-ack" not in ctx


# ---------------------------------------------------------------------------
# Failure-mode guarantees — silence on every error path so the harness never
# blocks on bus issues.
# ---------------------------------------------------------------------------

def test_main_silent_when_no_beacon_project(hook_module, tmp_path,
                                              monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin",
                        io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    hook_module.main()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_silent_when_credentials_missing(hook_module, fake_project,
                                                monkeypatch, capsys):
    out, _, acks = _run_main(hook_module, fake_project,
                              [_make_event("ev-1")], "UserPromptSubmit",
                              monkeypatch, capsys, force_no_creds=True)
    assert out == ""
    assert acks == []


def test_main_silent_when_unread_call_raises(hook_module, fake_project,
                                              monkeypatch, capsys):
    monkeypatch.setattr(hook_module, "_load_id_token", lambda: "fake-token")

    def boom(*a, **kw):
        raise TimeoutError("simulated cloud hiccup")
    monkeypatch.setattr(hook_module, "_list_unread", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "cwd": str(fake_project.root), "session_id": "sess-X",
        "hook_event_name": "UserPromptSubmit",
    })))
    hook_module.main()
    captured = capsys.readouterr()
    assert captured.out == ""
    # The error went to stderr so it's visible in BEACON_HOOK_DEBUG-style
    # investigations but never gets surfaced to the AI / blocks the prompt.
    assert "simulated cloud hiccup" in captured.err


# ---------------------------------------------------------------------------
# ms-54 / e-1319: bus-directory heartbeat refresh retired
# ---------------------------------------------------------------------------
#
# Originally (e-1189) this hook fire-and-forget'd ``beacon session id`` on
# every SessionStart / UserPromptSubmit so .beacon/session.json.last_active
# stayed fresh, keeping long-running idle sessions visible in
# ``beacon bus directory --since-min 30 --live``.
#
# Post Option C (PR #111 / commit 78048b6 / e-1318) the bridge's poll loop
# is the truth source for both ``last_active`` and ``last_poll_at``. A
# parallel CLI write let a session appear alive while its bridge poll loop
# was dead — DMs accumulated server-side unread, motivating this cleanup.
#
# These tests now pin the inverse contract:
#   - ``_refresh_session_heartbeat`` is a no-op (kept as a tombstone for
#     external callers who may still import it).
#   - ``main()`` does NOT invoke it.
#   - the inbox fetch / cursor advance path is unchanged — only the
#     heartbeat side-channel was retired.


def test_refresh_session_heartbeat_is_a_no_op(
    hook_module, fake_project, monkeypatch
):
    """Post e-1319: the helper does not spawn any subprocess.

    We patch subprocess.Popen with a tripwire — if anything in the helper
    still calls into it, the test fails with a clear message about the
    regression.
    """
    popen_calls = []

    def tripwire(*a, **kw):
        popen_calls.append((a, kw))
        raise AssertionError(
            "_refresh_session_heartbeat spawned a subprocess — post-e-1319 "
            "it must be a no-op (bridge owns the heartbeat now)"
        )

    monkeypatch.setattr(__import__("subprocess"), "Popen", tripwire)

    # Returns cleanly without spawning anything.
    result = hook_module._refresh_session_heartbeat(fake_project.root)
    assert result is None
    assert popen_calls == []


def test_main_does_not_refresh_heartbeat_on_user_prompt(
    hook_module, fake_project, monkeypatch, capsys
):
    """Post e-1319: UserPromptSubmit must NOT trigger a CLI heartbeat.

    The bridge's per-iteration write is the truth source. If main() calls
    _refresh_session_heartbeat we've regressed to the dual-truth shape
    that this cleanup removed.
    """
    refresh_calls: list[Path] = []
    monkeypatch.setattr(hook_module, "_refresh_session_heartbeat",
                        lambda root: refresh_calls.append(root))

    _run_main(hook_module, fake_project, [], "UserPromptSubmit",
              monkeypatch, capsys)

    assert refresh_calls == [], (
        "UserPromptSubmit must not refresh the CLI heartbeat post e-1319 — "
        "the bridge owns last_active / last_poll_at structurally"
    )


def test_main_does_not_refresh_heartbeat_on_session_start(
    hook_module, fake_project, monkeypatch, capsys
):
    """SessionStart shape — also no CLI heartbeat post e-1319."""
    refresh_calls: list[Path] = []
    monkeypatch.setattr(hook_module, "_refresh_session_heartbeat",
                        lambda root: refresh_calls.append(root))

    _run_main(hook_module, fake_project, [], "SessionStart",
              monkeypatch, capsys)

    assert refresh_calls == [], (
        "SessionStart must not refresh the CLI heartbeat post e-1319"
    )


def test_main_proceeds_to_cloud_call_without_heartbeat(
    hook_module, fake_project, monkeypatch, capsys
):
    """The inbox fetch path is unchanged — only the heartbeat side-channel
    was retired. This test pins that ``main()`` still hits the unread fetch
    even when no heartbeat is in the picture, so the inbox flow keeps
    working in isolation.
    """
    fetch_calls: list = []
    monkeypatch.setattr(hook_module, "_refresh_session_heartbeat",
                        lambda root: None)

    def fake_fetch(*a, **kw):
        fetch_calls.append((a, kw))
        return []  # no unread events — clean exit

    monkeypatch.setattr(hook_module, "_list_unread", fake_fetch)
    monkeypatch.setattr(hook_module, "_load_id_token", lambda: "fake-token")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "cwd": str(fake_project.root), "session_id": "sess-X",
        "hook_event_name": "UserPromptSubmit",
    })))
    hook_module.main()

    assert len(fetch_calls) == 1, (
        "inbox fetch path regressed — e-1319 only retired the heartbeat, "
        "the cloud unread call must still fire"
    )


def test_simulation_pre_and_post_e1319(
    hook_module, fake_project, monkeypatch, capsys
):
    """Contrast pre-e-1319 (every prompt bumped the heartbeat via this hook)
    with post-e-1319 (the hook never touches the heartbeat). This is the
    regression net for re-introducing a CLI-side bump on the inbox path.

    State (pre): the heartbeat was called on every prompt — simulated by
                 manually invoking _refresh_session_heartbeat in the loop
                 to model the OLD shape that no longer exists in main().
    State (post): main() runs N times; the heartbeat counter stays at 0.
    """
    counter = {"refresh": 0}
    monkeypatch.setattr(hook_module, "_refresh_session_heartbeat",
                        lambda root: counter.__setitem__(
                            "refresh", counter["refresh"] + 1))
    for _ in range(5):
        _run_main(hook_module, fake_project, [], "UserPromptSubmit",
                  monkeypatch, capsys)

    assert counter["refresh"] == 0, (
        f"post-e-1319: 5 prompts must drive 0 heartbeat refreshes "
        f"(got {counter['refresh']}). The CLI-side heartbeat is retired; "
        "the bridge poll loop is the truth source."
    )
