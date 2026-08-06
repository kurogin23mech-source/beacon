"""ms-127 e-4824 — local-mode `beacon trek reconcile` regression.

`cmd_trek_reconcile` has a local-mode branch that reads the project task pool to
find entries the pool has marked ``done`` but whose Trek stamp is still a
non-terminal state, then mirrors them. Before e-4824 that branch called
``read_project()`` — a name that was never defined (a stray from the commands.py
era). The call sat inside ``try/except Exception``, so the ``NameError`` was
swallowed, ``data`` fell to ``None``, ``pool_status`` stayed empty, and the diff
was ALWAYS empty. Local reconcile silently did nothing.

The cloud branch (test_trek_api.py) was fine; only the local CLI path was dead.
These tests drive the local branch directly and assert the pool→trek diff is
actually computed. They FAIL on the old ``read_project()`` code (empty diff) and
pass once it calls the already-imported ``load_project()``.
"""

from __future__ import annotations

import json
import os
import sys
from io import StringIO

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_trek  # noqa: E402
import trek_store  # noqa: E402


def _project(entry_id: str, status: str) -> dict:
    """Minimal project with one task entry in ``status``."""
    return {
        "milestones": [
            {
                "id": "ms-1",
                "title": "M",
                "entries": [
                    {"id": entry_id, "type": "task", "status": status,
                     "description": "t"},
                ],
            }
        ],
        "operations": [],
    }


def _trek_doc(entry_id: str, state: str) -> dict:
    return {
        "trek_id": "tk-local",
        "title": "Local trek",
        "status": "active",
        "scope": [],
        "task_states": {entry_id: {"state": state}},
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("BEACON_TREK_", "BEACON_JSON")):
            monkeypatch.delenv(key, raising=False)


def _run_reconcile(monkeypatch, *, trek_doc, project, apply_=False):
    """Drive cmd_trek_reconcile in local mode, return parsed JSON result."""
    monkeypatch.setattr(cmd_trek, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(trek_store, "load_trek", lambda tid: trek_doc)
    monkeypatch.setattr(cmd_trek, "load_project", lambda: project)
    saved = {}
    monkeypatch.setattr(trek_store, "save_trek",
                        lambda t: saved.update({"doc": t}))
    monkeypatch.setenv("BEACON_TREK_ID", "tk-local")
    monkeypatch.setenv("BEACON_JSON", "1")
    if apply_:
        monkeypatch.setenv("BEACON_TREK_APPLY", "1")
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    cmd_trek.cmd_trek_reconcile()
    return json.loads(buf.getvalue()), saved


def test_unreadable_project_degrades_but_is_not_silent(monkeypatch):
    """Independent-review consensus (AX+maintainability): the except guard used to
    swallow failures silently — the exact class of bug e-4824 was. The degrade
    (data=None → empty diff) is preserved, but a warning must reach stderr so the
    caller knows reconcile ran in reduced mode instead of seeing a clean 'no diff'."""
    monkeypatch.setattr(cmd_trek, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(trek_store, "load_trek",
                        lambda tid: _trek_doc("e-1", "working"))

    def _boom():
        raise RuntimeError("project.json unreadable")
    monkeypatch.setattr(cmd_trek, "load_project", _boom)
    monkeypatch.setenv("BEACON_TREK_ID", "tk-local")
    monkeypatch.setenv("BEACON_JSON", "1")
    out, err = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    cmd_trek.cmd_trek_reconcile()
    result = json.loads(out.getvalue())
    assert result["diff"] == []                 # degrade preserved
    assert "Warning" in err.getvalue()          # but no longer silent
    assert "project.json unreadable" in err.getvalue()


def test_pool_done_but_trek_working_surfaces_in_diff(monkeypatch):
    """The core regression: pool=done + trek stamp=working → diff has the entry.

    Old read_project() code returned an EMPTY diff here (data=None)."""
    result, _ = _run_reconcile(
        monkeypatch,
        trek_doc=_trek_doc("e-1", "working"),
        project=_project("e-1", "done"),
    )
    assert result["applied"] is False  # dry-run
    ids = [d["entry_id"] for d in result["diff"]]
    assert "e-1" in ids, f"local reconcile lost the pool→trek diff: {result}"
    item = next(d for d in result["diff"] if d["entry_id"] == "e-1")
    assert item["pool_status"] == "done"
    assert item["would_change_to"] == "done"
    assert item["trek_state"] == "working"


def test_pool_not_done_yields_no_diff(monkeypatch):
    """Guard: a pool entry still in progress must NOT be mirrored."""
    result, _ = _run_reconcile(
        monkeypatch,
        trek_doc=_trek_doc("e-1", "working"),
        project=_project("e-1", "in_progress"),
    )
    assert result["diff"] == []


def test_apply_mirrors_pool_done_into_trek_stamp(monkeypatch):
    """--apply flips the trek stamp to done and persists via save_trek."""
    result, saved = _run_reconcile(
        monkeypatch,
        trek_doc=_trek_doc("e-1", "working"),
        project=_project("e-1", "done"),
        apply_=True,
    )
    assert result["applied"] is True
    assert result["applied_entry_ids"] == ["e-1"]
    assert saved["doc"]["task_states"]["e-1"]["state"] == "done"
