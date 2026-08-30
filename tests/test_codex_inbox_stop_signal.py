"""The Codex inbox hook must turn a remote-STOP into a halt-request (ms-160 e-5800).

Before e-5800 the Codex receive filter dropped stop-signal events entirely
(channel-not-allowed), so a Codex session could not be stopped at all. Now the
filter keeps stop-signal, and the inbox hook runs the shared
``process_inbox_events`` reducer to write a halt-request — while NOT rendering
the stop as a normal DM. These tests pin that behaviour end-to-end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "codex-inbox-hook.py"
LIB = REPO / "lib"
sys.path.insert(0, str(LIB))
import stop_signal as ss  # noqa: E402

CODEX_SID = "codex-1787000000000-abcdef01"


def _setup(tmp_path: Path, event: dict) -> Path:
    cwd = tmp_path / "proj"
    beacon = cwd / ".beacon"
    (beacon / "codex" / "inbox").mkdir(parents=True)
    (beacon / "cloud.json").write_text(json.dumps({"project_id": "proj-1"}))
    (beacon / "codex" / "receive-loop.session.json").write_text(
        json.dumps({"session_id": CODEX_SID, "project_id": "proj-1"}))
    (beacon / "codex" / "inbox" / f"{event['event_id']}.json").write_text(
        json.dumps(event))
    return cwd


def _run(cwd: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--cwd", str(cwd),
         "--no-archive", "--install-root", str(REPO)],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout or "{}")


def _stop_event() -> dict:
    payload = ss.build_stop_payload(
        scope="global", issued_by_session_id="other-sid",
        reason="remote STOP test")
    return {
        "event_id": "1787000001000-STOPev",
        "channel": "stop-signal",
        "sender_session_id": "other-sid",
        "created_at": "2026-08-29T06:00:00.000000Z",
        "payload": payload,
    }


def _dm_event() -> dict:
    return {
        "event_id": "1787000002000-DMevnt",
        "channel": "dm",
        "sender_session_id": "other-sid",
        "created_at": "2026-08-29T06:00:01.000000Z",
        "payload": {"recipient_session_id": CODEX_SID, "text": "SECRET_DM_BODY"},
    }


def test_stop_signal_writes_halt_request(tmp_path):
    cwd = _setup(tmp_path, _stop_event())
    out = _run(cwd)
    # A halt-request now exists for THIS codex session id.
    halt = ss.read_halt_request(CODEX_SID, beacon_dir=str(cwd / ".beacon"))
    assert halt is not None, "stop-signal did not produce a halt-request"
    # The AI is told a STOP arrived, but the stop is NOT rendered as a DM.
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "STOP signal received" in ctx, ctx
    assert "channel=stop-signal" not in ctx  # not rendered via _format_entry


def test_dm_still_renders_and_stop_is_separated(tmp_path):
    """A DM alongside a stop: the DM renders normally, the stop only surfaces as
    the halt notice (its raw event id is not shown as a DM entry)."""
    cwd = _setup(tmp_path, _dm_event())
    # add the stop event too
    stop = _stop_event()
    (cwd / ".beacon" / "codex" / "inbox" / f"{stop['event_id']}.json").write_text(
        json.dumps(stop))
    out = _run(cwd)
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "SECRET_DM_BODY" in ctx           # the real DM renders
    assert "STOP signal received" in ctx     # the stop surfaces as a notice
    assert "1787000001000-STOPev" not in ctx  # stop event id not shown as a DM


def test_plain_dm_only_does_not_write_halt(tmp_path):
    cwd = _setup(tmp_path, _dm_event())
    _run(cwd)
    halt = ss.read_halt_request(CODEX_SID, beacon_dir=str(cwd / ".beacon"))
    assert halt is None, "a plain DM must not create a halt-request"
