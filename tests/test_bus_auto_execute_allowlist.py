"""Tests for the bus auto-execute allowlist (ms-54 / e-1145).

The allowlist is the receiver-side, channel-level opt-in for delivery=auto-execute.
Without it, the inbox hook can already downgrade unsafe events to propose-to-ai
as a fallback, but there's no project-level way to say "yes, I really do want
this channel to auto-execute". These tests lock in the three pieces:

  * CLI: `bus auto-execute add/remove/list` round-trips through project.json
    with idempotent semantics and structural defaults (empty list).

  * Inbox hook: `auto-execute` events are downgraded to `propose-to-ai` when
    the channel is NOT in the allowlist, and pass through unchanged when it
    IS. Both behaviors hit identical events except for the channel allowlist
    state — that's the structural guarantee.

  * Safety: a missing or corrupted project.json field is treated as "no
    channels armed" (fail-closed), matching the bus_budget gate posture.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


# ---------------------------------------------------------------------------
# CLI fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A bare beacon project so allowlist add/remove/list have somewhere to
    persist. Same shape as test_bus_budget.py's fixture."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "test", "milestones": []}))
    monkeypatch.setenv(
        "BEACON_PROJECT_FILE",
        str(beacon_dir / "project.json"),
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _clear_ae_env(monkeypatch):
    for k in ("BEACON_BUS_AUTO_EXEC_CHANNEL", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

def test_list_when_empty_explains_secure_default(project_dir, monkeypatch,
                                                   capsys):
    """A missing field MUST print the safe-default explanation, not silently
    show nothing. Silence in a security feature is a confusion vector."""
    _clear_ae_env(monkeypatch)
    commands.cmd_bus_auto_execute_list()
    out = capsys.readouterr().out
    assert "empty" in out
    assert "downgraded" in out


def test_add_persists_channel_to_project_json(project_dir, monkeypatch,
                                                capsys):
    _clear_ae_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_AUTO_EXEC_CHANNEL", "ops")
    commands.cmd_bus_auto_execute_add()
    data = json.loads(
        (project_dir / ".beacon" / "project.json").read_text())
    assert data["bus_auto_execute_channels"] == ["ops"]


def test_add_is_idempotent(project_dir, monkeypatch, capsys):
    """Adding a channel twice MUST NOT duplicate it. Idempotency is the
    contract for safety-critical config: re-running the same CLI from a
    deploy script never explodes the list."""
    _clear_ae_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_AUTO_EXEC_CHANNEL", "ops")
    commands.cmd_bus_auto_execute_add()
    commands.cmd_bus_auto_execute_add()
    data = json.loads(
        (project_dir / ".beacon" / "project.json").read_text())
    assert data["bus_auto_execute_channels"] == ["ops"]


def test_remove_drops_channel(project_dir, monkeypatch, capsys):
    _clear_ae_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_AUTO_EXEC_CHANNEL", "ops")
    commands.cmd_bus_auto_execute_add()
    commands.cmd_bus_auto_execute_remove()
    data = json.loads(
        (project_dir / ".beacon" / "project.json").read_text())
    assert data.get("bus_auto_execute_channels", []) == []


def test_remove_nonexistent_is_idempotent(project_dir, monkeypatch, capsys):
    _clear_ae_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_AUTO_EXEC_CHANNEL", "ops")
    commands.cmd_bus_auto_execute_remove()  # must not crash; end state matches
    data = json.loads(
        (project_dir / ".beacon" / "project.json").read_text())
    assert data.get("bus_auto_execute_channels", []) == []


def test_add_rejects_empty_channel(project_dir, monkeypatch, capsys):
    _clear_ae_env(monkeypatch)
    with pytest.raises(SystemExit):
        commands.cmd_bus_auto_execute_add()


def test_list_json_mode(project_dir, monkeypatch, capsys):
    _clear_ae_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_AUTO_EXEC_CHANNEL", "ops")
    commands.cmd_bus_auto_execute_add()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_bus_auto_execute_list()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed == {"channels": ["ops"]}


def test_corrupt_field_treated_as_empty(project_dir, monkeypatch, capsys):
    """If `bus_auto_execute_channels` is hand-edited to a non-list (string,
    object, etc.) the reader must coerce to []. We don't want a stray
    project.json edit to silently let auto-execute through."""
    _clear_ae_env(monkeypatch)
    data = json.loads(
        (project_dir / ".beacon" / "project.json").read_text())
    data["bus_auto_execute_channels"] = "not-a-list"
    (project_dir / ".beacon" / "project.json").write_text(json.dumps(data))
    commands.cmd_bus_auto_execute_list()
    out = capsys.readouterr().out
    assert "empty" in out


# ---------------------------------------------------------------------------
# Inbox hook tests — the safety net
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def hook_module():
    spec = importlib.util.spec_from_file_location("bus_inbox_hook", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bus_inbox_hook"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    """Beacon project with `bus_auto_execute_channels` configurable via the
    helper below. Each test sets the allowlist before invoking the hook."""
    root = tmp_path / "proj"
    (root / ".beacon").mkdir(parents=True)
    # The hook reads the project file directly (no validation), so we keep
    # the schema minimal but always include name+milestones so a future
    # validate call would still succeed.
    (root / ".beacon" / "project.json").write_text(json.dumps(
        {"name": "test", "milestones": []}))
    (root / ".beacon" / "session.json").write_text(
        json.dumps({"session_id": "sess-X"}))
    (root / ".beacon" / "cloud.json").write_text(
        json.dumps({"api_url": "https://api.test", "project_id": "proj-1"}))
    creds_dir = tmp_path / "config" / "beacon"
    creds_dir.mkdir(parents=True)
    (creds_dir / "credentials.json").write_text(
        json.dumps({"id_token": "fake-token"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    return SimpleNamespace(root=root, tmp=tmp_path)


def _set_allowlist(root: Path, channels: list[str]) -> None:
    pj = root / ".beacon" / "project.json"
    data = json.loads(pj.read_text())
    data["bus_auto_execute_channels"] = channels
    pj.write_text(json.dumps(data))


def _make_event(eid: str, *, channel="ops", delivery="auto-execute",
                sender="other-sess", payload=None,
                created_at=None) -> dict:
    return {
        "event_id": eid,
        "channel": channel,
        "delivery": delivery,
        "sender_session_id": sender,
        "payload": payload or {},
        "created_at": created_at or "2026-06-07T01:30:00.000000Z",
    }


def _run_hook(hook_module, fake_project, events, monkeypatch, capsys,
              hook_event_name="UserPromptSubmit"):
    def fake_list_unread(api_url, project_id, recipient_id, token, limit=50):
        return list(events)

    def fake_ack(api_url, project_id, recipient_id, last_seen_at, token):
        pass

    monkeypatch.setattr(hook_module, "_list_unread", fake_list_unread)
    monkeypatch.setattr(hook_module, "_ack_cursor", fake_ack)
    monkeypatch.setattr(hook_module, "_load_id_token", lambda: "fake-token")

    payload = json.dumps({
        "cwd": str(fake_project.root),
        "session_id": "sess-X",
        "hook_event_name": hook_event_name,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    hook_module.main()
    return capsys.readouterr()


def test_hook_downgrades_auto_execute_when_channel_not_in_allowlist(
        hook_module, fake_project, monkeypatch, capsys):
    """The structural promise of the allowlist: an event sent with
    delivery=auto-execute on a channel that the receiver hasn't armed gets
    forced to propose-to-ai, with an audit-visible marker in the inject."""
    _set_allowlist(fake_project.root, [])  # nothing armed
    events = [_make_event("ev-1", channel="ops", delivery="auto-execute")]
    out = _run_hook(hook_module, fake_project, events, monkeypatch, capsys)
    assert out.out.strip()
    payload = json.loads(out.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "ev-1" in ctx
    # The downgrade marker is visible inline.
    assert "auto-execute downgraded" in ctx
    # delivery in the inject is the post-downgrade value.
    assert "delivery=propose-to-ai" in ctx
    # The summary count is also surfaced separately so a human scanning the
    # top of the inject sees "1 件 downgraded".
    assert "降格" in ctx


def test_hook_passes_through_auto_execute_when_channel_in_allowlist(
        hook_module, fake_project, monkeypatch, capsys):
    """Mirror image of the previous test — same event, same shape, but the
    channel IS armed. delivery stays auto-execute and no per-event downgrade
    marker appears. This is what makes the allowlist meaningful: opt-in
    flips the structural answer. We isolate to the event block (after the
    `[ev-1]` marker, before the static guide section) so the word "downgrade"
    in the unrelated usage docs doesn't spuriously fail the test."""
    _set_allowlist(fake_project.root, ["ops"])
    events = [_make_event("ev-1", channel="ops", delivery="auto-execute")]
    out = _run_hook(hook_module, fake_project, events, monkeypatch, capsys)
    payload = json.loads(out.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "ev-1" in ctx
    assert "delivery=auto-execute" in ctx
    # Per-event downgrade marker (added inline by _format_event when the
    # downgrade fires) must NOT appear here. The static guide section
    # explaining what downgrade means stays out of scope of this assertion.
    event_block = ctx.split("ev-1", 1)[1].split("--- 取り扱いガイド ---")[0]
    assert "auto-execute downgraded" not in event_block
    # The summary count line ("X 件 降格") also must not appear when nothing
    # was downgraded.
    assert "件 — channel が" not in ctx


def test_hook_records_downgrade_in_inbox_log(hook_module, fake_project,
                                                monkeypatch, capsys):
    """Audit trail: a downgrade is appended to .beacon/bus-inbox.log with the
    original event so a human can review what was forced into propose-to-ai
    after the fact. The AI sees the inline marker; the log has the durable
    record."""
    _set_allowlist(fake_project.root, [])
    events = [_make_event("ev-audit", channel="ops",
                           delivery="auto-execute",
                           payload={"action": "rm -rf /"})]
    _run_hook(hook_module, fake_project, events, monkeypatch, capsys)
    log_path = fake_project.root / ".beacon" / "bus-inbox.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "ev-audit" in content
    assert "_downgraded_from" in content


def test_hook_downgrade_mixed_with_pass_through(hook_module, fake_project,
                                                  monkeypatch, capsys):
    """Two events on different channels, one armed one not. Only the not-armed
    event gets the downgrade marker — the armed one is untouched. Each event
    is evaluated against the allowlist independently."""
    _set_allowlist(fake_project.root, ["ops"])
    events = [
        _make_event("ev-armed", channel="ops", delivery="auto-execute"),
        _make_event("ev-unarmed", channel="random", delivery="auto-execute",
                     created_at="2026-06-07T01:31:00.000000Z"),
    ]
    out = _run_hook(hook_module, fake_project, events, monkeypatch, capsys)
    ctx = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    # Both events appear in inject (auto-execute is never sent to log, only
    # downgraded or passed through).
    assert "ev-armed" in ctx
    assert "ev-unarmed" in ctx
    # Find each event's block and check delivery.
    armed_block = ctx.split("ev-armed", 1)[1].split("ev-unarmed")[0]
    unarmed_block = ctx.split("ev-unarmed", 1)[1]
    assert "delivery=auto-execute" in armed_block
    assert "downgraded" not in armed_block
    assert "delivery=propose-to-ai" in unarmed_block
    assert "downgraded" in unarmed_block


def test_hook_with_corrupt_allowlist_field_treats_as_empty(hook_module,
                                                              fake_project,
                                                              monkeypatch,
                                                              capsys):
    """A hand-edited project.json with the wrong shape MUST be treated as
    "no channels armed" (fail-closed). The downgrade still fires."""
    pj = fake_project.root / ".beacon" / "project.json"
    data = json.loads(pj.read_text())
    data["bus_auto_execute_channels"] = "not-a-list"
    pj.write_text(json.dumps(data))
    events = [_make_event("ev-1", channel="ops", delivery="auto-execute")]
    out = _run_hook(hook_module, fake_project, events, monkeypatch, capsys)
    ctx = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert "auto-execute downgraded" in ctx
    assert "delivery=propose-to-ai" in ctx


def test_hook_propose_to_ai_events_untouched_by_allowlist(hook_module,
                                                            fake_project,
                                                            monkeypatch,
                                                            capsys):
    """The allowlist is auto-execute-specific. propose-to-ai events must not
    be reshaped or annotated by it — that would be scope creep and break
    `test_bus_inbox_hook.test_main_injects_propose_to_ai_events`."""
    _set_allowlist(fake_project.root, [])
    events = [_make_event("ev-p", channel="ops", delivery="propose-to-ai")]
    out = _run_hook(hook_module, fake_project, events, monkeypatch, capsys)
    ctx = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert "ev-p" in ctx
    # The per-event downgrade marker must not appear in the event block
    # (the static guide section can still discuss downgrades in general).
    event_block = ctx.split("ev-p", 1)[1].split("--- 取り扱いガイド ---")[0]
    assert "downgraded" not in event_block
    # And the summary count line must be absent.
    assert "件 — channel が" not in ctx


def test_render_context_surfaces_downgraded_count(hook_module):
    """Direct test of the render layer — the summary count must surface so
    a quick glance at the inject reveals "the safety net fired 2 times"."""
    ev = {"event_id": "x", "channel": "c", "delivery": "propose-to-ai",
           "sender_session_id": "s", "payload": {}, "created_at": "ts",
           "_downgraded_from": "auto-execute"}
    out = hook_module._render_context([ev], notify_only_count=0,
                                        monitor_suggested=False,
                                        budget=None,
                                        auto_execute_downgraded_count=2)
    assert "2 件" in out
    assert "降格" in out
