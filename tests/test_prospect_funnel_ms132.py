"""Prospect (打診) funnel tests — ms-132 e-4502.

The attack-list's per-prospect phase is a *configurable* funnel, a third one
alongside account / opportunity: seeded into a sales project, editable via
``beacon phase prospect ...``, and baked into a new attack-list's enum column so a
row's phase is validated against the project's own vocabulary. Mirrors the ms-116
funnel-edit harness (in-process ``commands.cmd_phase_*`` against a temp project).
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
    return _project(tmp_path, monkeypatch, data)


def _set(monkeypatch, **kv):
    for k in ("BEACON_FUNNEL_KIND", "BEACON_PHASE_NAME", "BEACON_PHASE_INDEX",
              "BEACON_PHASE_OLD", "BEACON_PHASE_NEW"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kv.items():
        monkeypatch.setenv(k, str(v))


def _prospect_names(cwd):
    return [p["name"] for p in _read(cwd)["prospect_phases"]]


# --- seed + accessors ------------------------------------------------------

def test_seed_has_prospect_funnel():
    data = se.build_sales_project("S", "o")
    assert _names(data["prospect_phases"]) == ["未接触", "連絡済", "返信あり", "アポ"]


def test_accessors_read_and_default():
    data = se.build_sales_project("S", "o")
    assert se.default_prospect_phase(data) == "未接触"
    # unset funnel → falls back to the shipped seed's first name
    assert se.default_prospect_phase({}) == "未接触"
    assert se.prospect_phases({}) == []


# --- phase list surfaces prospect -----------------------------------------

def test_phase_list_json_includes_prospect(sales_cwd, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_phase_list()
    out = json.loads(capsys.readouterr().out)
    assert _names(out["prospect_phases"]) == ["未接触", "連絡済", "返信あり", "アポ"]


# --- e-4405 (ms-129): sales project w/ no configured funnel shows the built-in
# default (which entity creation actually uses), NOT "not a sales project" -----

def _unseeded_sales(tmp_path, monkeypatch):
    """A sales project whose funnels were never seeded (e.g. init-created), so
    the accessors read empty but the built-in DEFAULT_* are what's in effect."""
    data = se.build_sales_project("S", "o")
    for k in ("account_phases", "opportunity_phases", "prospect_phases"):
        data.pop(k, None)
    return _project(tmp_path, monkeypatch, data)


def test_phase_list_sales_no_config_shows_builtin_default_json(tmp_path, monkeypatch, capsys):
    _unseeded_sales(tmp_path, monkeypatch)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_phase_list()
    out = json.loads(capsys.readouterr().out)
    assert out["using_builtin_default"] is True
    assert _names(out["account_phases"]) == ["未接触", "リード", "未成約顧客", "成約顧客"]
    assert _names(out["prospect_phases"]) == ["未接触", "連絡済", "返信あり", "アポ"]
    assert out["opportunity_phases"][0]["name"] == "商談準備"
    # per-funnel source (AX review): every funnel is the built-in default here.
    assert out["phases_source"] == {"account": "builtin_default",
                                    "opportunity": "builtin_default",
                                    "prospect": "builtin_default"}


def test_effective_phases_reports_source_per_funnel(tmp_path, monkeypatch):
    # AX review: a mixed project (custom account funnel, default商談/打診) must
    # report source per funnel, not a blanket boolean — so a read→add workflow
    # knows which funnel is already customised.
    data = se.build_sales_project("S", "o")
    for k in ("opportunity_phases", "prospect_phases"):
        data.pop(k, None)                       # only account_phases stays configured
    eff = se.effective_phases(data)
    assert eff["source"]["account"] == "configured"
    assert eff["source"]["opportunity"] == "builtin_default"
    assert eff["source"]["prospect"] == "builtin_default"
    assert eff["using_builtin_default"] is True


def test_effective_phases_non_sales_is_empty(tmp_path, monkeypatch):
    eff = se.effective_phases({"profession": "dev"})
    assert eff["account"] == [] and eff["opportunity"] == [] and eff["prospect"] == []
    assert eff["using_builtin_default"] is False
    assert set(eff["source"].values()) == {"none"}


def test_phase_list_sales_no_config_text_omits_not_a_sales_project(tmp_path, monkeypatch, capsys):
    _unseeded_sales(tmp_path, monkeypatch)
    monkeypatch.delenv("BEACON_JSON", raising=False)
    commands.cmd_phase_list()
    out = capsys.readouterr().out
    assert "not a sales project" not in out       # the misleading line is gone
    assert "デフォルト" in out                      # "(組み込みデフォルト使用中)" note shown
    assert "未接触" in out and "商談準備" in out    # effective funnel is displayed


def test_phase_list_non_sales_still_says_not_a_sales_project(tmp_path, monkeypatch, capsys):
    data = {"profession": "dev", "name": "D", "milestones": [], "members": [],
            "documents": [], "deployments": [], "releases": []}
    _project(tmp_path, monkeypatch, data)
    monkeypatch.delenv("BEACON_JSON", raising=False)
    commands.cmd_phase_list()
    assert "not a sales project" in capsys.readouterr().out


# --- editing the prospect funnel ------------------------------------------

def test_add_appends_prospect_stage(sales_cwd, monkeypatch):
    _set(monkeypatch, BEACON_FUNNEL_KIND="prospect", BEACON_PHASE_NAME="商談化")
    commands.cmd_phase_add()
    assert _prospect_names(sales_cwd)[-1] == "商談化"


def test_rename_prospect_stage(sales_cwd, monkeypatch):
    _set(monkeypatch, BEACON_FUNNEL_KIND="prospect",
         BEACON_PHASE_OLD="未接触", BEACON_PHASE_NEW="新規")
    commands.cmd_phase_rename()
    assert "新規" in _prospect_names(sales_cwd)
    assert "未接触" not in _prospect_names(sales_cwd)


def test_move_prospect_stage(sales_cwd, monkeypatch):
    _set(monkeypatch, BEACON_FUNNEL_KIND="prospect", BEACON_PHASE_NAME="アポ",
         BEACON_PHASE_INDEX="0")
    commands.cmd_phase_move()
    assert _prospect_names(sales_cwd)[0] == "アポ"


def test_remove_prospect_stage_not_blocked(sales_cwd, monkeypatch):
    # A prospect stage is config-only (attack-lists are self-describing table-docs,
    # not project.json rows), so removal is never blocked by "occupants".
    _set(monkeypatch, BEACON_FUNNEL_KIND="prospect", BEACON_PHASE_NAME="アポ")
    commands.cmd_phase_remove()
    assert "アポ" not in _prospect_names(sales_cwd)


# --- ownership guard -------------------------------------------------------

def test_dev_project_cannot_edit_prospect_funnel(tmp_path, monkeypatch, capsys):
    dev = {"name": "Dev", "profession": "dev", "milestones": []}
    _project(tmp_path, monkeypatch, dev)
    _set(monkeypatch, BEACON_FUNNEL_KIND="prospect", BEACON_PHASE_NAME="X")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_phase_add()
    assert ei.value.code == 1
    # prospect maps to the acquisition target-class for the ownership check.
    assert "dev" in capsys.readouterr().err


# --- attach-list bakes the configured funnel ------------------------------

def test_attach_list_bakes_configured_funnel(sales_cwd, monkeypatch, capsys):
    data = _read(sales_cwd)
    acq = se.acquisition_add(data, "獲得A")
    _write(sales_cwd, data)
    # Edit the funnel first: rename its entry stage.
    _set(monkeypatch, BEACON_FUNNEL_KIND="prospect",
         BEACON_PHASE_OLD="未接触", BEACON_PHASE_NEW="新規")
    commands.cmd_phase_rename()
    capsys.readouterr()  # drain the rename message so only the attach JSON remains
    # A newly attached list must reflect the edited funnel, not the shipped default.
    for k in ("BEACON_ACQ_ID", "BEACON_ACQ_LIST_TITLE", "BEACON_ACQ_LIST_PHASES",
              "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_ACQ_ID", acq)
    monkeypatch.setenv("BEACON_ACQ_LIST_TITLE", "攻略リスト")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_acquisition_attach_list()
    out = json.loads(capsys.readouterr().out)
    phase_col = {c["key"]: c for c in out["columns"]}["phase"]
    assert phase_col["values"] == ["新規", "連絡済", "返信あり", "アポ"]


def _names(phases):
    return [p.get("name") for p in phases]


# --- funnel-kind registry consistency (guards the 5-site sync) --------------

def test_funnel_kind_dicts_are_consistent():
    """Adding a funnel kind must touch every site or the guard fails here.

    A kind present in the CLI aliases but missing from the ownership map or the
    data-key map would let the CLI accept a funnel it can't own/persist. This
    asserts the three canonical-kind sets agree, so a future 4th funnel can't
    silently skip a site (ms-132 e-4502 保守性レビュー finding)."""
    import commands
    kinds_from_aliases = set(commands._FUNNEL_KIND_ALIASES.values())
    assert kinds_from_aliases == set(commands._FUNNEL_OWNING_CLASS)
    assert kinds_from_aliases == set(se._FUNNEL_KEYS)
    # every owning class must be a real sales target-class (ownership check input)
    import occupation
    sales_owned = set(occupation.PROFESSION_ADAPTER_KINDS["sales"])
    assert set(commands._FUNNEL_OWNING_CLASS.values()) <= sales_owned
