"""ms-164 e-5947: incident escalate targets ANY target class, not just a milestone.

The old ``core.incident_escalate`` hard-wired ``find_target_milestone``, so an
Incident could only be escalated onto a development milestone — a sales /
back-office project (or an escalation to an Operation) had no valid target. Now
the CLI resolves the target generically via ``occupation.resolve_target`` and the
core append lands on whatever Target record it resolved.
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
import core  # noqa: E402


def _incident_op(inc_id="e-100"):
    return {"id": "op-1", "type": "operation", "label": "watch", "status": "open",
            "entries": [{"id": inc_id, "type": "incident", "title": "boom",
                         "description": "prod down", "status": "open"}]}


@pytest.fixture
def project_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".beacon").mkdir()
        monkeypatch.chdir(tmp)
        for k in ("BEACON_JSON",):
            monkeypatch.delenv(k, raising=False)
        try:
            yield Path(tmp)
        finally:
            os.chdir(tempfile.gettempdir())


def _write(project_dir: Path, data: dict) -> None:
    (project_dir / ".beacon" / "project.json").write_text(
        json.dumps(data), encoding="utf-8")


def _read(project_dir: Path) -> dict:
    return json.loads(
        (project_dir / ".beacon" / "project.json").read_text(encoding="utf-8"))


# --- core: appends to whatever Target record it is handed (occupation-generic) ---

def test_core_escalate_appends_to_given_target():
    target = {"id": "opp-1", "entries": []}
    data = {"milestones": [], "operations": [_incident_op()],
            "opportunities": [target]}
    op, incident, task = core.incident_escalate(data, "e-100", target)
    assert target["entries"] == [task]
    assert task["type"] == "task"
    assert incident["linked_ms_task"] == task["id"]
    assert task["meta"]["escalated_from"] == "e-100"


# --- CLI: resolves the target generically ------------------------------------

def test_cli_escalate_to_milestone(project_dir, monkeypatch):
    _write(project_dir, {
        "name": "t",
        "milestones": [{"id": "ms-1", "status": "in_progress", "entries": []}],
        "operations": [_incident_op()],
    })
    monkeypatch.setenv("BEACON_INCIDENT_ID", "e-100")
    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    commands.cmd_incident_escalate()
    data = _read(project_dir)
    ms = data["milestones"][0]
    assert len(ms["entries"]) == 1 and ms["entries"][0]["type"] == "task"


def test_cli_escalate_to_opportunity(project_dir, monkeypatch):
    """The generalization: escalate onto a sales Opportunity (was impossible)."""
    _write(project_dir, {
        "name": "t",
        "opportunities": [{"id": "opp-1", "status": "in_progress", "entries": []}],
        "operations": [_incident_op()],
    })
    monkeypatch.setenv("BEACON_INCIDENT_ID", "e-100")
    monkeypatch.setenv("BEACON_MS_ID", "opp-1")  # -m accepts any target id now
    commands.cmd_incident_escalate()
    data = _read(project_dir)
    opp = data["opportunities"][0]
    assert len(opp["entries"]) == 1
    assert opp["entries"][0]["description"].startswith("[Incident]")


def test_cli_escalate_unknown_target_errors(project_dir, monkeypatch, capsys):
    _write(project_dir, {
        "name": "t",
        "milestones": [{"id": "ms-1", "status": "in_progress", "entries": []}],
        "operations": [_incident_op()],
    })
    monkeypatch.setenv("BEACON_INCIDENT_ID", "e-100")
    monkeypatch.setenv("BEACON_MS_ID", "ms-DOES-NOT-EXIST")
    with pytest.raises(SystemExit):
        commands.cmd_incident_escalate()
    assert "not found" in capsys.readouterr().out.lower()
