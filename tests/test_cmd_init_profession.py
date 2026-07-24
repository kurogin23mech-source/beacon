"""Tests for ``beacon init`` 職種テンプレート選択 (ms-106 ① AC1).

BEACON_PROFESSION picks the job-template. Default "dev" keeps the historical
schema (plus an explicit profession marker); "sales" emits the sales entity
schema. Both must pass the shared validator.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import commands  # noqa: E402
import core  # noqa: E402


@pytest.fixture
def project_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".beacon").mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_NAME", "test-proj")
    monkeypatch.setenv("BEACON_OBJECTIVE", "test obj")
    monkeypatch.setenv("BEACON_RETRO_DAY", "monday")
    monkeypatch.setattr(commands, "_maybe_prompt_initial_profile", lambda: None)
    monkeypatch.delenv("BEACON_SENSITIVITY", raising=False)
    monkeypatch.delenv("BEACON_PROFESSION", raising=False)
    return cwd


def _read(cwd: Path) -> dict:
    return json.loads((cwd / ".beacon" / "project.json").read_text())


def test_default_profession_is_dev(project_cwd):
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["profession"] == "dev"
    assert data["milestones"] == []
    assert "opportunities" not in data
    core.validate_project(data)


def test_sales_profession_emits_sales_schema(project_cwd, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_PROFESSION", "sales")
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["profession"] == "sales"
    assert data["opportunities"] == []
    assert data["accounts"] == []
    assert data["milestones"] == []  # validator-compat
    core.validate_project(data)
    out = capsys.readouterr().out
    assert "profession = sales" in out
    assert "beacon account add" in out


def test_sales_profession_case_insensitive(project_cwd, monkeypatch):
    monkeypatch.setenv("BEACON_PROFESSION", "SALES")
    commands.cmd_init()
    assert _read(project_cwd)["profession"] == "sales"


def test_data_defined_profession_creates_descriptor_skeleton(project_cwd,
                                                             monkeypatch,
                                                             capsys):
    # ms-124 e-4091: a profession is no longer a hardcoded enum. An unrecognised
    # name creates a data-defined occupation skeleton (empty target_classes),
    # which the owner fills with `beacon target-class add` — no code change.
    monkeypatch.setenv("BEACON_PROFESSION", "legal")
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["profession"] == "legal"
    assert data["milestones"] == []       # validator-compat
    assert data["target_classes"] == []   # targets come from descriptors
    core.validate_project(data)
    out = capsys.readouterr().out
    assert "profession = legal" in out
    assert "beacon target-class add" in out
