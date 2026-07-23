"""ms-120 / e-3909: disambiguate polymorphic / read-write-ambiguous CLI shapes.

Covers the four sub-parts:
  1. opportunity judge — the trailing positional is validated per verb
     (retry / terminal require it) instead of being an opaque positional.
  2. opportunity transition-date — <date> and --clear are mutually exclusive.
  3. opportunity rename — a post-creation title-edit path now exists.
  4. meeting list — <opp-id> optional (global list, symmetric with other list
     verbs); meeting `ended` (read) renamed to `list-ended` to stop colliding
     with the write verb `end` (alias kept, warns).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "lib"))

import sales_entities as se  # noqa: E402
import commands  # noqa: E402
from beacon_cli import dispatch, main as main_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Part 3 — opportunity rename (entity layer)
# ---------------------------------------------------------------------------

def test_opportunity_set_title_renames():
    data = se.build_sales_project("S", "obj")
    opp_id = se.opportunity_add(data, "old title")
    se.opportunity_set_title(data, opp_id, "new title")
    assert se.find_opportunity(data, opp_id)["title"] == "new title"


def test_opportunity_set_title_rejects_empty():
    data = se.build_sales_project("S", "obj")
    opp_id = se.opportunity_add(data, "old")
    with pytest.raises(ValueError):
        se.opportunity_set_title(data, opp_id, "   ")


def test_opportunity_set_title_rejects_unknown():
    data = se.build_sales_project("S", "obj")
    with pytest.raises(ValueError):
        se.opportunity_set_title(data, "opp-nope", "x")


# ---------------------------------------------------------------------------
# commands.py layer (project fixture)
# ---------------------------------------------------------------------------

@pytest.fixture
def sales_project(tmp_path, monkeypatch):
    data = se.build_sales_project("S", "obj")
    opp_id = se.opportunity_add(data, "deal one")
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps(data, ensure_ascii=False))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon / "project.json"))
    monkeypatch.chdir(tmp_path)
    for k in ("BEACON_OPP_ID", "BEACON_JUDGE_DECISION", "BEACON_JUDGE_ARG",
              "BEACON_PHASE_NOTE", "BEACON_MTG_OPP", "BEACON_TITLE", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    return {"opp_id": opp_id}


# Part 1 — judge positional validated per verb

def test_judge_retry_requires_date(sales_project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPP_ID", sales_project["opp_id"])
    monkeypatch.setenv("BEACON_JUDGE_DECISION", "retry")
    monkeypatch.setenv("BEACON_JUDGE_ARG", "")  # missing the required new date
    with pytest.raises(SystemExit) as exc:
        commands.cmd_opportunity_judge()
    assert exc.value.code == 1
    assert "retry" in capsys.readouterr().err


def test_judge_terminal_requires_phase(sales_project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPP_ID", sales_project["opp_id"])
    monkeypatch.setenv("BEACON_JUDGE_DECISION", "terminal")
    monkeypatch.setenv("BEACON_JUDGE_ARG", "")  # missing the required terminal phase
    with pytest.raises(SystemExit) as exc:
        commands.cmd_opportunity_judge()
    assert exc.value.code == 1
    assert "terminal" in capsys.readouterr().err


# Part 3 — rename via command

def test_cmd_opportunity_rename(sales_project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_OPP_ID", sales_project["opp_id"])
    monkeypatch.setenv("BEACON_TITLE", "renamed deal")
    commands.cmd_opportunity_rename()
    out = capsys.readouterr().out
    assert "renamed deal" in out
    reloaded = json.loads(
        (Path(".beacon") / "project.json").read_text())
    assert se.find_opportunity(reloaded, sales_project["opp_id"])["title"] == "renamed deal"


# Part 4 — meeting list global (no opp-id)

def test_meeting_list_without_opp_lists_all(sales_project, monkeypatch, capsys):
    # Add a second opportunity + a meeting on each, then list globally.
    data = json.loads((Path(".beacon") / "project.json").read_text())
    opp2_id = se.opportunity_add(data, "deal two")
    se.meeting_schedule(data, sales_project["opp_id"], "2026-08-01T10:00")
    se.meeting_schedule(data, opp2_id, "2026-08-02T10:00")
    (Path(".beacon") / "project.json").write_text(json.dumps(data, ensure_ascii=False))

    monkeypatch.setenv("BEACON_MTG_OPP", "")   # no scope → all opportunities
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_meeting_list()
    out = json.loads(capsys.readouterr().out)
    assert out["opportunity"] is None
    assert len(out["meetings"]) == 2


# ---------------------------------------------------------------------------
# Windows dispatch parity
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_call(monkeypatch):
    box: Dict[str, Any] = {"cmd": None, "env": None}

    def fake_call(cmd, env=None, **kwargs):
        box["cmd"] = list(cmd)
        box["env"] = dict(env) if env is not None else None
        return 0

    monkeypatch.setattr(dispatch.subprocess, "call", fake_call)
    return box


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text('{"name":"t","milestones":[]}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    yield tmp_path


@pytest.fixture
def no_bash(monkeypatch):
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)


def test_dispatch_transition_date_exclusive(project_dir, no_bash, captured_call, capsys):
    rc = main_mod.main([
        "opportunity", "transition-date", "opp-1", "2026-08-01", "--clear",
    ])
    assert rc == 2
    assert "排他" in capsys.readouterr().err
    assert captured_call["cmd"] is None  # never reached commands.py


def test_dispatch_opportunity_rename_routes(project_dir, no_bash, captured_call):
    rc = main_mod.main(["opportunity", "rename", "opp-1", "new name"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "opportunity_rename"
    assert captured_call["env"]["BEACON_TITLE"] == "new name"


def test_dispatch_meeting_list_ended_and_alias(project_dir, no_bash, captured_call, capsys):
    rc = main_mod.main(["meeting", "list-ended"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "meeting_ended"
    # `ended` alias still routes, with a deprecation nudge.
    rc = main_mod.main(["meeting", "ended"])
    assert rc == 0
    assert "list-ended" in capsys.readouterr().err


def test_dispatch_meeting_list_allows_no_opp(project_dir, no_bash, captured_call):
    rc = main_mod.main(["meeting", "list"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "meeting_list"
    assert captured_call["env"]["BEACON_MTG_OPP"] == ""
