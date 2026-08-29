"""Codex PostToolUse halt-check hook (= ms-160 e-5856).

The Codex parity of ``beacon_cli/hooks/halt_check.py``. It must, mid autonomous
loop (= no UserPromptSubmit between tool calls):

  * resolve the Codex sid from ``receive-loop.session.json`` (NOT
    ``.beacon/session.json``, which is Claude's marker),
  * turn a pending ``stop-signal`` inbox event into a ``halt-request.json``,
  * render that halt once as PostToolUse additionalContext and acknowledge it
    so a later fire does not re-surface the same halt,
  * never touch a plain DM (that belongs to the inbox hook).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "codex-halt-check-hook.py"
LIB = REPO / "lib"
sys.path.insert(0, str(LIB))
import stop_signal as ss  # noqa: E402

CODEX_SID = "codex-1787000000000-abcdef01"


def _setup(tmp_path: Path) -> Path:
    cwd = tmp_path / "proj"
    beacon = cwd / ".beacon"
    (beacon / "codex" / "inbox").mkdir(parents=True)
    (beacon / "project.json").write_text("{}")
    (beacon / "codex" / "receive-loop.session.json").write_text(
        json.dumps({"session_id": CODEX_SID, "project_id": "proj-1"}))
    return cwd


def _seed(cwd: Path, event: dict) -> None:
    (cwd / ".beacon" / "codex" / "inbox" / f"{event['event_id']}.json").write_text(
        json.dumps(event))


def _run(cwd: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--install-root", str(REPO)],
        input=json.dumps({"hook_event_name": "PostToolUse", "cwd": str(cwd)}),
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout or "{}")


def _stop_event() -> dict:
    payload = ss.build_stop_payload(
        scope="global", issued_by_session_id="other-sid",
        reason="remote STOP mid-loop")
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


def test_midloop_stop_writes_halt_and_renders(tmp_path):
    cwd = _setup(tmp_path)
    _seed(cwd, _stop_event())
    out = _run(cwd)

    # halt-request now on disk for THIS codex session.
    halt = ss.read_halt_request(CODEX_SID, beacon_dir=str(cwd / ".beacon"))
    assert halt is not None, "stop-signal did not produce a halt-request"

    # The AI is told to halt (parity render), not shown the stop as a DM.
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "STOP SIGNAL" in ctx, ctx
    assert "1787000001000-STOPev" not in ctx  # raw stop event id not surfaced

    # The stop event was archived so a later fire cannot re-process it.
    inbox = cwd / ".beacon" / "codex" / "inbox"
    assert not (inbox / "1787000001000-STOPev.json").is_file()
    assert (inbox / ".read" / "1787000001000-STOPev.json").is_file()


def test_second_fire_is_silent_after_acknowledge(tmp_path):
    cwd = _setup(tmp_path)
    _seed(cwd, _stop_event())
    first = _run(cwd)
    assert "STOP SIGNAL" in first.get("hookSpecificOutput", {}).get(
        "additionalContext", "")
    # A subsequent PostToolUse fire must not spam the same halt.
    second = _run(cwd)
    assert second == {}


def test_plain_dm_does_not_halt_or_archive(tmp_path):
    cwd = _setup(tmp_path)
    _seed(cwd, _dm_event())
    out = _run(cwd)
    assert out == {}
    halt = ss.read_halt_request(CODEX_SID, beacon_dir=str(cwd / ".beacon"))
    assert halt is None, "a plain DM must not create a halt-request"
    # The DM is left for the inbox hook — the halt hook does not touch it.
    inbox = cwd / ".beacon" / "codex" / "inbox"
    assert (inbox / "1787000002000-DMevnt.json").is_file()


def test_no_session_pointer_is_silent(tmp_path):
    cwd = _setup(tmp_path)
    (cwd / ".beacon" / "codex" / "receive-loop.session.json").unlink()
    _seed(cwd, _stop_event())
    out = _run(cwd)
    assert out == {}
    # No sid resolved → no halt-request written anywhere.
    assert not (cwd / ".beacon" / "sessions").exists()
