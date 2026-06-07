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
    """notify-user-only MUST NOT appear in additionalContext — that's the
    delivery-mode contract for keeping UI-only notifications out of the AI
    decision loop. The events still go to .beacon/bus-inbox.log so the user
    can review them from the terminal."""
    events = [_make_event("notify-1", delivery="notify-user-only",
                          payload={"text": "user notice"})]
    out, _, acks = _run_main(hook_module, fake_project, events,
                              "UserPromptSubmit", monkeypatch, capsys)
    assert out == "", "notify-user-only must NOT inject AI context"
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
