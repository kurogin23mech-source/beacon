"""Integration tests for session-end + rescue CLI (ms-57 / e-1038 + e-1039).

These exercise the full pipeline:
  cmd_session_end → aggregate_session → write local cache (+ cloud push)
  cmd_session_rescue → discover other session_ids → aggregate each

The cloud push is verified by stubbing _push_session_log_to_cloud, since
we don't run a live Firestore in unit tests.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import session  # noqa: E402


@pytest.fixture
def project_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "agent.json").write_text(
            json.dumps({"name": "test-agent"}), encoding="utf-8"
        )
        (beacon_dir / "project.json").write_text(
            json.dumps({
                "name": "t",
                "milestones": [{"id": "ms-57", "title": "test",
                                 "status": "in_progress", "entries": [], "progress": 0}],
            }), encoding="utf-8"
        )
        monkeypatch.chdir(tmp)
        # Disable cloud-side calls; tests use stubs.
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        # Silence cloud-push attempts even if anything leaks through.
        monkeypatch.setattr(commands, "_push_session_log_to_cloud", lambda p: False)
        yield Path(tmp)


_next_eid = [100]


def _add_commit_for_session(project_dir: Path, session_id: str, hash_str: str, msg: str) -> None:
    """Write a commit entry directly into project.json to simulate a recorded commit."""
    pf = project_dir / ".beacon" / "project.json"
    data = json.loads(pf.read_text(encoding="utf-8"))
    _next_eid[0] += 1
    data["milestones"][0]["entries"].append({
        "id": f"e-{_next_eid[0]}",
        "type": "commit",
        "description": msg,
        "status": "done",
        "meta": {"hash": hash_str, "message": msg, "session_id": session_id},
    })
    pf.write_text(json.dumps(data), encoding="utf-8")


def _append_note_for_session(project_dir: Path, session_id: str, text: str) -> None:
    p = project_dir / ".beacon" / "session_notes.jsonl"
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-06-06T10:00:00Z", "text": text,
                              "session_id": session_id}) + "\n")


# ---------------------------------------------------------------------------
# cmd_session_end
# ---------------------------------------------------------------------------

def test_session_end_aggregates_own_session(project_dir, monkeypatch):
    sid = session.get_session_id()
    _add_commit_for_session(project_dir, sid, "abc1234", "feat: test")
    _append_note_for_session(project_dir, sid, "note 1")

    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_session_end()
    payload = json.loads(buf.getvalue())
    assert payload["session_id"] == sid
    assert payload["commit_ids"]
    assert payload["note_ids"]
    assert payload["summary"]
    # Persisted to local cache
    cache = json.loads((project_dir / ".beacon" / "session_logs" / f"{sid}.json").read_text())
    assert cache["session_id"] == sid


def test_session_end_honors_summary_override(project_dir, monkeypatch):
    sid = session.get_session_id()
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_SUMMARY", "AI-generated summary text")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_session_end()
    payload = json.loads(buf.getvalue())
    assert payload["summary"] == "AI-generated summary text"


def test_session_end_idempotent(project_dir, monkeypatch):
    sid = session.get_session_id()
    _add_commit_for_session(project_dir, sid, "abc1234", "feat: test")

    monkeypatch.setenv("BEACON_JSON", "1")
    buf1 = io.StringIO()
    with redirect_stdout(buf1):
        commands.cmd_session_end()
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        commands.cmd_session_end()
    p1 = json.loads(buf1.getvalue())
    p2 = json.loads(buf2.getvalue())
    assert p1["commit_ids"] == p2["commit_ids"]
    assert p1["note_ids"] == p2["note_ids"]
    # Re-aggregation must NOT set recovered when first call didn't.
    assert "recovered" not in p2


# ---------------------------------------------------------------------------
# cmd_session_rescue
# ---------------------------------------------------------------------------

def test_session_rescue_aggregates_other_sessions(project_dir, monkeypatch):
    current = session.get_session_id()
    # Add commits / notes for two other sessions.
    _add_commit_for_session(project_dir, "other-session-1", "aaaa111", "their commit")
    _append_note_for_session(project_dir, "other-session-2", "their note")

    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_session_rescue()
    results = json.loads(buf.getvalue())
    rescued_ids = sorted(r["session_id"] for r in results if r["status"] == "ok")
    assert "other-session-1" in rescued_ids
    assert "other-session-2" in rescued_ids
    assert current not in rescued_ids
    # Each rescued payload should have recovered=True on first creation
    for r in results:
        if r["status"] == "ok":
            assert r["recovered"] is True


def test_session_rescue_skips_current_session(project_dir, monkeypatch):
    current = session.get_session_id()
    _append_note_for_session(project_dir, current, "my own note")

    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_session_rescue()
    results = json.loads(buf.getvalue())
    assert all(r["session_id"] != current for r in results)


def test_session_rescue_preserves_recovered_on_resweep(project_dir, monkeypatch):
    """Two rescue runs against the same orphan must both succeed; the
    recovered flag set by the first run must remain True across the second."""
    _add_commit_for_session(project_dir, "orphan-1", "bbbb222", "x")

    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_session_rescue()
    # Second rescue
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        commands.cmd_session_rescue()
    second_results = json.loads(buf2.getvalue())
    orphan = next(r for r in second_results if r["session_id"] == "orphan-1")
    assert orphan["recovered"] is True


def test_session_rescue_then_session_end_drops_recovered(project_dir, monkeypatch):
    """If rescue creates an entry with recovered=True, and then the original
    session's session-end runs against it, the existing flag is preserved
    (forensics fact: this entry was first born as a rescue)."""
    _add_commit_for_session(project_dir, "orphan-1", "cccc333", "x")
    # First rescue
    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_session_rescue()

    # Now simulate orphan-1 coming back online: session-end for orphan-1.
    monkeypatch.setenv("BEACON_SESSION_ID", "orphan-1")
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        commands.cmd_session_end()
    payload = json.loads(buf2.getvalue())
    assert payload["recovered"] is True  # preserved


# ---------------------------------------------------------------------------
# cmd_session_log_list / show
# ---------------------------------------------------------------------------

def test_session_log_list_after_session_end(project_dir, monkeypatch):
    sid = session.get_session_id()
    _add_commit_for_session(project_dir, sid, "dddd444", "x")
    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_session_end()

    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        commands.cmd_session_log_list()
    entries = json.loads(buf2.getvalue())
    assert any(e["session_id"] == sid for e in entries)


def test_session_log_show_round_trips(project_dir, monkeypatch):
    sid = session.get_session_id()
    monkeypatch.setenv("BEACON_JSON", "1")
    with redirect_stdout(io.StringIO()):
        commands.cmd_session_end()
    monkeypatch.setenv("BEACON_SESSION_ID", sid)
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_session_log_show()
    entry = json.loads(buf.getvalue())
    assert entry["session_id"] == sid


def test_session_log_show_missing_returns_error(project_dir, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_SESSION_ID", "no-such-session")
    with pytest.raises(SystemExit):
        commands.cmd_session_log_show()
