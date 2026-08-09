"""ms-136 e-4701 — cloud-ephemeral runner tests (mocked, no live cloud).

Per the leader-ratified verification boundary: dev tests mock the ApiClient and
never touch a live cloud; the real cloud-ephemeral run is user-verified. These
pin the orchestration — non-prod refusal, disposable_project usage, guaranteed
teardown (incl. on exception), residue naming caught by the orphan classifier,
inbound rejection, tier routing, and the L_api bisect layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import scenario_cloud as sc  # noqa: E402
import scenario_runner as sr  # noqa: E402
import scenario_bisect as sb  # noqa: E402


class _MockClient:
    """Records the disposable_project lifecycle calls without any network."""
    def __init__(self):
        self.created = []
        self.archived = []
        self.env_issued = []

    def create_project(self, pid, name, objective=""):
        self.created.append((pid, name, objective))
        return {"id": pid}

    def issue_bus_envelope(self, project_id, *, tier, actions_authorized):
        self.env_issued.append((project_id, tier, tuple(actions_authorized)))
        return {"tier": tier, "actions_authorized": actions_authorized}

    def archive_project(self, project_id, envelope):
        self.archived.append(project_id)
        return {"archived": True}


def _cloud_scenario():
    return {
        "name": "cloud smoke",
        "tier": "cloud-ephemeral",
        "spec_ref": "y2gy76tVfnzKVfFy4elM",
        "seed": {"profession": "dev", "name": "C", "objective": "o"},
        "steps": [{"kind": "persona_cli", "argv": ["status", "--json"]}],
    }


def _passing_steps(scenario, workdir, eid, beacon_bin, env):
    return {"steps": [{"index": 0, "kind": "persona_cli", "ok": True}],
            "failure": None, "passed": True}


# --- non-prod refusal (belt with guard_prod_project_write) -------------------

def test_refuses_production_endpoint():
    mc = _MockClient()
    with pytest.raises(sc.ScenarioError):
        sc.run_cloud_scenario(
            _cloud_scenario(),
            client_factory=lambda: (mc, "https://beacon-ai.dev"),
            _step_runner=_passing_steps)
    assert mc.created == []  # never even attempted to create on prod


# --- disposable_project usage + guaranteed teardown -------------------------

def test_creates_and_tears_down_ephemeral_project():
    mc = _MockClient()
    report = sc.run_cloud_scenario(
        _cloud_scenario(),
        client_factory=lambda: (mc, "https://nonprod.example.com"),
        now=lambda: 1786279000,
        _step_runner=_passing_steps)
    assert report["mode"] == "cloud"
    assert report["passed"] is True
    # created a non-prod project with the residue-prefixed id ...
    assert len(mc.created) == 1
    eid = mc.created[0][0]
    assert eid.startswith(sc.TEST_RESIDUE_PREFIX)
    assert report["ephemeral_project_id"] == eid
    # ... and tore it down (archived on exit)
    assert mc.archived == [eid]


def test_teardown_runs_even_on_journey_exception():
    mc = _MockClient()

    def boom(*a):
        raise RuntimeError("journey crashed mid-run")

    with pytest.raises(RuntimeError):
        sc.run_cloud_scenario(
            _cloud_scenario(),
            client_factory=lambda: (mc, "https://nonprod.example.com"),
            _step_runner=boom)
    # structural finally in disposable_project guarantees teardown
    assert len(mc.created) == 1
    assert mc.archived == [mc.created[0][0]]


# --- residue naming is caught by the orphan classifier (no silent leak) ------

def test_residue_prefix_is_flagged_by_orphan_classifier():
    import project_cleanup
    assert project_cleanup._matches_test_name(sc.TEST_RESIDUE_PREFIX + "1786279000")


# --- minimal start: inbound_stimulus rejected in cloud tier -----------------

def test_inbound_stimulus_rejected_in_cloud_tier():
    scenario = _cloud_scenario()
    scenario["steps"].append(
        {"kind": "inbound_stimulus", "target": "opp-1", "summary": "返信"})
    mc = _MockClient()
    with pytest.raises(sc.ScenarioError):
        sc.run_cloud_scenario(
            scenario,
            client_factory=lambda: (mc, "https://nonprod.example.com"),
            _step_runner=_passing_steps)
    assert mc.created == []  # rejected before touching the cloud


# --- tier routing + validation ----------------------------------------------

def test_run_scenario_routes_cloud_tier(monkeypatch):
    called = {}

    def fake_run_cloud(scenario, **kw):
        called["scenario"] = scenario
        return {"mode": "cloud", "passed": True}

    monkeypatch.setattr(sc, "run_cloud_scenario", fake_run_cloud)
    # scenario_runner imports scenario_cloud locally; patch the attribute it uses
    import scenario_cloud
    monkeypatch.setattr(scenario_cloud, "run_cloud_scenario", fake_run_cloud)
    report = sr.run_scenario(_cloud_scenario())
    assert report["mode"] == "cloud"
    assert called["scenario"]["tier"] == "cloud-ephemeral"


def test_unknown_tier_rejected():
    scenario = _cloud_scenario()
    scenario["tier"] = "quantum"
    with pytest.raises(sr.ScenarioError):
        sr.run_scenario(scenario)


# --- L_api bisect layer (cloud mode) ----------------------------------------

def test_cloud_failure_localizes_to_L_api():
    report = {
        "passed": False, "mode": "cloud", "workdir": "/nonexistent-cloud",
        "steps": [{"kind": "assert", "ok": False, "reason": "'x' != 'y'",
                   "spec_source": "SPEC: …", "observation_basis": "server field"}],
        "failure": {"index": 0, "kind": "assert", "reason": "'x' != 'y'",
                    "spec_source": "SPEC: …"},
    }
    diag = sb.diagnose_failure({}, report)
    assert diag["responsible_layer"] == sb.LAYER_API
    assert "cloud" in diag["boundary"]
