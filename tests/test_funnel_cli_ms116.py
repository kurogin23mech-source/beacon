"""CLI-layer tests for ms-116 phase-funnel editing (task e-3821 / e-3822).

Exercises the ``beacon phase add|rename|move|remove`` command functions through
``commands.cmd_phase_*`` against a temp project (honouring BEACON_PROJECT_FILE),
covering: the happy paths persist to project.json, rename follows references,
delete is blocked while a stage is non-empty (AC3), and — crucially — the ms-115
containment rule is enforced for funnel edits too: a dev project may not edit a
sales-owned funnel (e-3822).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import commands  # noqa: E402
import sales_entities as se  # noqa: E402


def _write(cwd: Path, data: dict) -> None:
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read(cwd: Path) -> dict:
    return json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))


def _project(tmp_path, monkeypatch, data: dict) -> Path:
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    _write(cwd, data)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


@pytest.fixture
def sales_cwd(tmp_path, monkeypatch):
    data = se.build_sales_project("Smoke Sales", "close deals")
    se.account_add(data, "顧客A", phase="リード")
    se.opportunity_add(data, "商談X", phase="提案準備")
    return _project(tmp_path, monkeypatch, data)


def _set(monkeypatch, **kv):
    for k in ("BEACON_FUNNEL_KIND", "BEACON_PHASE_NAME", "BEACON_PHASE_INDEX",
              "BEACON_PHASE_OLD", "BEACON_PHASE_NEW"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kv.items():
        monkeypatch.setenv(k, str(v))


def _acc_names(cwd):
    return [p["name"] for p in _read(cwd)["account_phases"]]


# --- happy paths persist ---------------------------------------------------

def test_add_appends(sales_cwd, monkeypatch):
    _set(monkeypatch, BEACON_FUNNEL_KIND="account", BEACON_PHASE_NAME="休眠")
    commands.cmd_phase_add()
    assert _acc_names(sales_cwd)[-1] == "休眠"


def test_add_at_index_inserts_at_head(sales_cwd, monkeypatch):
    _set(monkeypatch, BEACON_FUNNEL_KIND="account", BEACON_PHASE_NAME="最優先",
         BEACON_PHASE_INDEX="0")
    commands.cmd_phase_add()
    assert _acc_names(sales_cwd)[0] == "最優先"


def test_rename_follows_account_reference(sales_cwd, monkeypatch):
    _set(monkeypatch, BEACON_FUNNEL_KIND="account",
         BEACON_PHASE_OLD="リード", BEACON_PHASE_NEW="見込み")
    commands.cmd_phase_rename()
    data = _read(sales_cwd)
    assert "見込み" in _acc_names(sales_cwd) and "リード" not in _acc_names(sales_cwd)
    assert data["accounts"][0]["phase"] == "見込み"  # reference followed


def test_move_reorders(sales_cwd, monkeypatch):
    _set(monkeypatch, BEACON_FUNNEL_KIND="account", BEACON_PHASE_NAME="成約顧客",
         BEACON_PHASE_INDEX="0")
    commands.cmd_phase_move()
    assert _acc_names(sales_cwd)[0] == "成約顧客"


def test_remove_empty_ok(sales_cwd, monkeypatch):
    _set(monkeypatch, BEACON_FUNNEL_KIND="account", BEACON_PHASE_NAME="成約顧客")
    commands.cmd_phase_remove()
    assert "成約顧客" not in _acc_names(sales_cwd)


def test_opportunity_rename_follows_reference(sales_cwd, monkeypatch):
    _set(monkeypatch, BEACON_FUNNEL_KIND="opportunity",
         BEACON_PHASE_OLD="提案準備", BEACON_PHASE_NEW="提案")
    commands.cmd_phase_rename()
    assert _read(sales_cwd)["opportunities"][0]["phase"] == "提案"


# --- guards ----------------------------------------------------------------

def test_remove_non_empty_blocks(sales_cwd, monkeypatch, capsys):
    _set(monkeypatch, BEACON_FUNNEL_KIND="account", BEACON_PHASE_NAME="リード")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_phase_remove()
    assert ei.value.code == 1
    assert "cannot delete non-empty" in capsys.readouterr().err
    assert "リード" in _acc_names(sales_cwd)  # untouched


def test_unknown_funnel_rejected(sales_cwd, monkeypatch, capsys):
    _set(monkeypatch, BEACON_FUNNEL_KIND="milestone", BEACON_PHASE_NAME="X")
    with pytest.raises(SystemExit):
        commands.cmd_phase_add()
    assert "unknown funnel" in capsys.readouterr().err


def test_dev_project_cannot_edit_sales_funnel(tmp_path, monkeypatch, capsys):
    # e-3822: containment — a dev project must not edit a sales-owned funnel.
    dev = {"name": "Dev", "profession": "dev", "milestones": []}
    _project(tmp_path, monkeypatch, dev)
    _set(monkeypatch, BEACON_FUNNEL_KIND="account", BEACON_PHASE_NAME="X")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_phase_add()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "dev" in err and "account" in err  # guidance-rich block
