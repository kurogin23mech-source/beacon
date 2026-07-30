"""Profession front door + descriptor-driven onboarding plan (ms-133 e-4648/e-4408).

`beacon init --profession sales` used to run the DEVELOPMENT onboarding (大目的 /
ターゲット / やらないこと) because the Skills hardcoded the dev questions, and the
--profession flag the README advertised did not exist. These pin the structural
fix (SPEC review high#1): the onboarding plan (WHAT to ask + the ROLE of the
objective/vision) is emitted from lib/occupation.py per occupation, so the Skill
renders it instead of encoding `if profession == "sales"`.

  * occupation.onboarding_plan(p) returns a render-only plan; dev is unchanged,
    sales/backoffice ask occupation-specific questions (NOT the dev vision), and
    an unknown (data-defined) occupation still gets a sane generic plan;
  * `init --profession X --plan` emits that plan as JSON and writes nothing;
  * `init --profession X` forwards the occupation to the engine on BOTH the bash
    and Python surfaces (parity), and an omitted --profession stays dev.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "lib")):
    if p not in sys.path:
        sys.path.insert(0, p)

occupation = importlib.import_module("occupation")
dispatch = importlib.import_module("beacon_cli.dispatch")


# ---- the plan itself (lib/occupation.py) ----------------------------------

def test_dev_plan_unchanged_shape():
    plan = occupation.onboarding_plan("dev")
    assert plan["profession"] == "dev"
    keys = [f["key"] for f in plan["ask"]]
    # dev keeps its product-vision framing: objective + target + non_goals.
    assert keys[0] == "objective"
    assert {"target", "non_goals"} <= set(keys)
    assert "北極星" in plan["vision_role"]


def test_sales_plan_does_not_ask_dev_vision():
    """AC2 core: a sales init must NOT surface the development vision fields."""
    plan = occupation.onboarding_plan("sales")
    keys = {f["key"] for f in plan["ask"]}
    assert "objective" in keys           # every occupation has a north star
    assert "target" not in keys          # dev-only vision field
    assert "non_goals" not in keys       # dev-only vision field
    assert "営業" in plan["vision_role"]


def test_objective_always_present_and_required():
    """Callers rely on `objective` existing for every occupation."""
    for prof in ("dev", "sales", "backoffice", "legal", "", None):
        plan = occupation.onboarding_plan(prof)
        obj = [f for f in plan["ask"] if f["key"] == "objective"]
        assert obj and obj[0]["required"] is True


def test_unknown_occupation_gets_generic_render_only_plan():
    """A data-defined occupation with no built-in entry still yields a usable
    plan (front door works with no code change), carrying its own name."""
    plan = occupation.onboarding_plan("legal")
    assert plan["profession"] == "legal"
    assert plan["ask"][0]["key"] == "objective"
    # doesn't leak dev vision fields into an arbitrary occupation.
    assert {f["key"] for f in plan["ask"]} == {"objective"}


def test_blank_profession_defaults_to_dev():
    assert occupation.onboarding_plan("")["profession"] == "dev"
    assert occupation.onboarding_plan(None)["profession"] == "dev"


def test_plan_is_a_copy_not_shared_state():
    """Mutating a returned plan must not corrupt the registry for the next call."""
    p1 = occupation.onboarding_plan("sales")
    p1["ask"].append({"key": "x"})
    p1["ask"][0]["label"] = "MUTATED"
    p2 = occupation.onboarding_plan("sales")
    assert len(p2["ask"]) == 2
    assert p2["ask"][0]["label"] != "MUTATED"


# ---- the CLI seam (dispatch --plan / --profession) ------------------------

def test_dispatch_init_plan_emits_json_and_writes_nothing(tmp_path, capfd, monkeypatch):
    # capfd (not capsys): the plan is printed by a spawned commands.py subprocess
    # via _run_commands_py, so it lands on the real fd, not Python-level sys.stdout.
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(tmp_path / ".beacon" / "project.json"))
    rc = dispatch.dispatch(ROOT, ["init", "--profession", "sales", "--plan"])
    assert rc == 0
    out = json.loads(capfd.readouterr().out)
    assert out["profession"] == "sales"
    assert [f["key"] for f in out["ask"]] == ["objective", "focus"]
    # read-only: no project created.
    assert not (tmp_path / ".beacon" / "project.json").exists()


def test_dispatch_init_forwards_profession(tmp_path, monkeypatch):
    """--profession sales must forward BEACON_PROFESSION to the engine so a sales
    schema (opportunities) is written, not a dev one."""
    pf = tmp_path / ".beacon" / "project.json"
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(pf))
    rc = dispatch.dispatch(ROOT, ["init", "--profession", "sales",
                                  "--name", "T", "--objective", "開拓"])
    assert rc == 0
    data = json.loads(pf.read_text())
    assert data["profession"] == "sales"
    assert "opportunities" in data


def test_dispatch_init_without_profession_stays_dev(tmp_path, monkeypatch):
    """AC2: an omitted --profession keeps the development schema."""
    pf = tmp_path / ".beacon" / "project.json"
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(pf))
    rc = dispatch.dispatch(ROOT, ["init", "--name", "T", "--objective", "作る"])
    assert rc == 0
    data = json.loads(pf.read_text())
    assert data["profession"] == "dev"
    assert data["milestones"] == []
    assert "opportunities" not in data
