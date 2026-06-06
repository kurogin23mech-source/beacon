"""Tests for session_id on session notes (ms-57 / e-1036).

Mirrors the commit/PR tagging contract from e-1062: notes recorded via
``beacon note add`` carry the current session_id in the persisted record
so session-end / rescue aggregation can pull them via
``WHERE session_id == X``.

Empty session_id is the documented "no session" sentinel and is NOT
written — keeps the field present-or-absent so Firestore queries stay
clean. Forward-only: pre-ms-57 notes stay untagged.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
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
            json.dumps({"name": "t", "milestones": []}), encoding="utf-8"
        )
        monkeypatch.chdir(tmp)
        monkeypatch.delenv("BEACON_NOTE_TEXT", raising=False)
        monkeypatch.delenv("BEACON_NOTE_CONTEXT", raising=False)
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        try:
            yield Path(tmp)
        finally:
            # Windows can't remove a directory that is the process CWD, so the
            # TemporaryDirectory cleanup would raise WinError 32. Step out of the
            # temp project before cleanup (no-op on POSIX). ms-57.
            os.chdir(tempfile.gettempdir())


def _read_notes(project_dir: Path) -> list[dict]:
    path = project_dir / ".beacon" / "session_notes.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Local-mode tagging
# ---------------------------------------------------------------------------

def test_note_add_writes_session_id(project_dir, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_NOTE_TEXT", "first note")
    # No cloud config — cmd_note_add's _push_note_to_cloud is a no-op.
    commands.cmd_note_add()
    notes = _read_notes(project_dir)
    assert len(notes) == 1
    assert notes[0]["text"] == "first note"
    assert notes[0]["session_id"]
    # Same id that the session module would mint, so aggregation queries
    # match what session.get_session_id() returns.
    assert notes[0]["session_id"] == session.get_session_id()


def test_note_add_with_context_preserves_both_fields(project_dir, monkeypatch):
    monkeypatch.setenv("BEACON_NOTE_TEXT", "with context")
    monkeypatch.setenv("BEACON_NOTE_CONTEXT", "design-decision")
    commands.cmd_note_add()
    note = _read_notes(project_dir)[0]
    assert note["context"] == "design-decision"
    assert note["session_id"]


def test_note_add_omits_session_id_when_resolver_returns_empty(project_dir, monkeypatch):
    """A misbehaving session module must NOT inject an empty 'session_id'
    field — the field is present-or-absent, never an empty sentinel."""
    monkeypatch.setattr(commands, "_resolve_session_id", lambda: "")
    monkeypatch.setenv("BEACON_NOTE_TEXT", "no session")
    commands.cmd_note_add()
    note = _read_notes(project_dir)[0]
    assert "session_id" not in note


def test_note_add_multiple_notes_share_session_id(project_dir, monkeypatch):
    """Two notes in the same heartbeat window inherit the same session_id —
    the per-session aggregation depends on this invariant."""
    monkeypatch.setenv("BEACON_NOTE_TEXT", "one")
    commands.cmd_note_add()
    monkeypatch.setenv("BEACON_NOTE_TEXT", "two")
    commands.cmd_note_add()
    notes = _read_notes(project_dir)
    assert len(notes) == 2
    assert notes[0]["session_id"] == notes[1]["session_id"]


def test_note_add_swallows_session_module_failure(project_dir, monkeypatch):
    """Recording a note must succeed even if session.py is broken —
    note recording is more important than tagging it."""
    def _boom():
        raise RuntimeError("simulated session module failure")
    monkeypatch.setattr(session, "get_session_id", _boom)
    monkeypatch.setenv("BEACON_NOTE_TEXT", "still recorded")
    commands.cmd_note_add()
    note = _read_notes(project_dir)[0]
    assert note["text"] == "still recorded"
    assert "session_id" not in note
