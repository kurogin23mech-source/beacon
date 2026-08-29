"""Codex inbox hook: operation-trigger imperative + auto-execute downgrade parity
with the Claude inbox hook (ms-160 e-5803).

Before this, the Codex inbox hook rendered every non-stop event as a generic DM:
it never surfaced the "run /beacon-operation-execute autonomously" imperative for
opted-in operation-trigger events, and never downgraded an auto-execute event on
a channel the project had not opted into. Both hooks now share
lib/bus_delivery.classify_auto_execute + format_operation_trigger_imperative.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "codex-inbox-hook.py"

CODEX_SID = "codex-1787000000000-abcdef01"
_T1_ENVELOPE = {"tier": "T1-system", "issuer": "beacon-system"}


def _setup(tmp_path: Path, *, auto_execute_channels) -> Path:
    cwd = tmp_path / "proj"
    beacon = cwd / ".beacon"
    (beacon / "codex" / "inbox").mkdir(parents=True)
    (beacon / "cloud.json").write_text(json.dumps({"project_id": "proj-1"}))
    (beacon / "project.json").write_text(json.dumps(
        {"bus_auto_execute_channels": auto_execute_channels}))
    (beacon / "codex" / "receive-loop.session.json").write_text(
        json.dumps({"session_id": CODEX_SID, "project_id": "proj-1"}))
    return cwd


def _seed(cwd: Path, event: dict) -> None:
    (cwd / ".beacon" / "codex" / "inbox" / f"{event['event_id']}.json").write_text(
        json.dumps(event))


def _run(cwd: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--cwd", str(cwd),
         "--no-archive", "--install-root", str(REPO)],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout or "{}")
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def _op_trigger_event(*, envelope=None) -> dict:
    e = {
        "event_id": "1787000010000-OPtrig",
        "channel": "operation-trigger",
        "delivery": "auto-execute",
        "sender_session_id": "other-sid",
        "created_at": "2026-08-29T07:00:00.000000Z",
        "payload": {"recipient_session_id": CODEX_SID,
                    "op_id": "op-9", "trigger_name": "daily-health"},
    }
    if envelope is not None:
        e["envelope"] = envelope
    return e


def test_opted_in_operation_trigger_emits_imperative(tmp_path):
    cwd = _setup(tmp_path, auto_execute_channels=["operation-trigger"])
    _seed(cwd, _op_trigger_event(envelope=_T1_ENVELOPE))
    ctx = _run(cwd)
    assert "AUTONOMOUS ACTION" in ctx
    assert "/beacon-operation-execute op-9" in ctx
    assert "安全側降格" not in ctx  # kept, not downgraded


def test_non_opted_in_auto_execute_is_downgraded_with_allowlist_reason(tmp_path):
    # bus_auto_execute_channels is empty → allowlist-miss downgrade; no imperative,
    # and the notice must name the allowlist / opt-in cause (not provenance).
    cwd = _setup(tmp_path, auto_execute_channels=[])
    _seed(cwd, _op_trigger_event(envelope=_T1_ENVELOPE))
    ctx = _run(cwd)
    assert "AUTONOMOUS ACTION" not in ctx
    assert "安全側降格" in ctx
    assert "opt-in 前" in ctx
    assert "T1-system" not in ctx  # e-5803 review AX-1: not a provenance failure


def test_opted_in_but_no_system_envelope_downgraded_with_provenance_reason(tmp_path):
    # In the allowlist, but the envelope is not T1-system → provenance downgrade.
    # e-5803 review (AX-1): the notice must say envelope/provenance, NOT tell the
    # AI to opt-in the channel (it already is opted in).
    cwd = _setup(tmp_path, auto_execute_channels=["operation-trigger"])
    _seed(cwd, _op_trigger_event(envelope=None))
    ctx = _run(cwd)
    assert "AUTONOMOUS ACTION" not in ctx
    assert "安全側降格" in ctx
    assert "T1-system" in ctx  # provenance-specific wording
    assert "opt-in 前" not in ctx


def test_unprocessed_stop_surfaces_a_degraded_warning(tmp_path):
    # e-5803 review (AX-1/AX-2): a STOP that can't be turned into a halt-request
    # (here: no receive-loop.session.json → session_id unresolved) must NOT vanish
    # silently on the kill-switch path — it must surface a fail-loud warning.
    cwd = _setup(tmp_path, auto_execute_channels=[])
    (cwd / ".beacon" / "codex" / "receive-loop.session.json").unlink()  # session_id ""
    _seed(cwd, {
        "event_id": "1787000099000-STOPx",
        "channel": "stop-signal",
        "sender_session_id": "other-sid",
        "created_at": "2026-08-29T07:00:09.000000Z",
        "payload": {"kind": "stop", "scope": "global",
                    "issued_by_session_id": "other-sid", "reason": "halt"},
    })
    ctx = _run(cwd)
    assert "STOP signal 受信" in ctx
    assert "停止処理に失敗" in ctx
    # and the stop is NOT rendered as a normal DM
    assert "1787000099000-STOPx" not in ctx


def test_plain_dm_has_no_imperative_or_downgrade(tmp_path):
    cwd = _setup(tmp_path, auto_execute_channels=["operation-trigger"])
    _seed(cwd, {
        "event_id": "1787000011000-DM",
        "channel": "dm",
        "sender_session_id": "other-sid",
        "created_at": "2026-08-29T07:00:01.000000Z",
        "payload": {"recipient_session_id": CODEX_SID, "text": "hello"},
    })
    ctx = _run(cwd)
    assert "hello" in ctx
    assert "AUTONOMOUS ACTION" not in ctx
    assert "安全側降格" not in ctx
