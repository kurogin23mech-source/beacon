"""Parity pin for ms-143: cmd_save's no-active-milestone behavior is unchanged
after routing through occupation.record_target_entry (leader 握り).

record_target_entry no-ops (recorded=False) when there is no milestone to record
onto, whereas the old core.save_entry RAISED "No active milestone". cmd_save (a dev
verb) re-raises to PRESERVE that observable error — abstraction lives in occupation,
the no-milestone UX stays in the frontend. This harness fixes that behavior so the
symbol-reach remediation (save→record_target_entry) stays a pure abstraction.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import commands  # noqa: E402


def _set_env(monkeypatch, **kw):
    for k in ("BEACON_DESCRIPTION", "BEACON_MS_ID", "BEACON_SOURCE", "BEACON_URL",
              "BEACON_REVISION_ID", "BEACON_HASH", "BEACON_PROGRESS",
              "BEACON_DATE", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_cmd_save_no_active_milestone_raises(monkeypatch):
    # empty ms_id + no in_progress milestone → the observable error is preserved.
    data = {"id": "p", "profession": "dev",
            "milestones": [{"id": "ms-1", "status": "done", "entries": []}]}
    monkeypatch.setattr(commands, "load_project", lambda: data)
    monkeypatch.setattr(commands, "save_project", lambda d, *a, **k: None)
    _set_env(monkeypatch, BEACON_SOURCE="manual", BEACON_DESCRIPTION="x")
    with pytest.raises(ValueError, match="No active milestone"):
        commands.cmd_save()
    # nothing recorded on the (done) milestone
    assert data["milestones"][0]["entries"] == []


def test_cmd_save_records_onto_active(monkeypatch, capsys):
    data = {"id": "p", "profession": "dev",
            "milestones": [{"id": "ms-1", "status": "in_progress", "entries": []}]}
    saved = {}
    monkeypatch.setattr(commands, "load_project", lambda: data)
    monkeypatch.setattr(commands, "save_project",
                        lambda d, *a, **k: saved.update(done=True))
    _set_env(monkeypatch, BEACON_SOURCE="manual", BEACON_DESCRIPTION="did a thing")
    commands.cmd_save()
    assert saved.get("done") is True
    # the side-effect entry landed on the active milestone via record_target_entry
    assert len(data["milestones"][0]["entries"]) == 1
    assert data["milestones"][0]["entries"][0]["description"] == "did a thing"


def test_cmd_save_bad_id_still_raises(monkeypatch):
    # explicit bad id RAISES identically (record_target_entry propagates
    # find_target_milestone's error, not the no-op path).
    data = {"id": "p", "profession": "dev",
            "milestones": [{"id": "ms-1", "status": "in_progress", "entries": []}]}
    monkeypatch.setattr(commands, "load_project", lambda: data)
    monkeypatch.setattr(commands, "save_project", lambda d, *a, **k: None)
    _set_env(monkeypatch, BEACON_SOURCE="manual", BEACON_DESCRIPTION="x",
             BEACON_MS_ID="ms-99")
    with pytest.raises(ValueError, match="not found"):
        commands.cmd_save()
