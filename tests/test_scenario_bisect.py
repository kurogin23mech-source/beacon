"""ms-136 e-4700 — dataflow-layer bisect tests.

Pins SPEC 方針8 達成基準: a deliberately-broken scenario is localized to the
right layer in one shot (not 6段の手探り). Also pins the leader 裁定 refinements:
differential is localization-only (never overrides SPEC authority), the
inbound/ingest failure is attributed correctly (who_has_the_ball-class), and the
bisect is non-invasive (imports no code-under-test).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import scenario_runner as sr  # noqa: E402
import scenario_bisect as sb  # noqa: E402


# --- 方針8 達成基準: deliberate bug → one-shot layer localization -------------

def test_deliberate_cli_gap_localizes_to_L_cli(tmp_path):
    # A real use-case gap: the persona does an operation the CLI now requires
    # more for (milestone add without the mandatory --priority) — exactly the
    # 顧客獲得タブ / milestone-priority class. Must localize to L_cli in one shot.
    scenario = {
        "seed": {"profession": "dev", "name": "D", "objective": "o"},
        "steps": [
            {"kind": "persona_cli", "argv": ["milestone", "add", "First MS"],
             "label": "MS を立てる (必須 --priority を欠く=use-case gap)"},
        ],
    }
    report = sr.run_scenario(scenario, workdir=str(tmp_path))
    assert report["passed"] is False
    diag = sb.diagnose_failure(scenario, report)
    assert diag["diagnosable"] is True
    assert diag["responsible_layer"] == sb.LAYER_CLI
    assert diag["expected_provenance"] == "journey-contract"
    assert "層 L_cli" in diag["summary"]


def test_passed_scenario_is_not_diagnosable(tmp_path):
    scenario = {
        "seed": {"profession": "sales", "name": "S", "objective": "o"},
        "steps": [{"kind": "persona_cli", "argv": ["opportunity", "add", "D"]}],
    }
    report = sr.run_scenario(scenario, workdir=str(tmp_path))
    assert report["passed"] is True
    diag = sb.diagnose_failure(scenario, report)
    assert diag["diagnosable"] is False


# --- who_has_the_ball-class: injected but observed wrong → L_engine (read/derive)

def _populate_workdir_with_injection(tmp_path):
    """Run a real journey through an inbound_stimulus so tmp_path's project.json
    genuinely contains a擬似着信 (source.injected=True)."""
    scenario = {
        "seed": {"profession": "sales", "name": "S", "objective": "o"},
        "steps": [
            {"kind": "persona_cli", "argv": ["opportunity", "add", "D"]},
            {"kind": "persona_cli",
             "argv": ["communication", "add", "opp-1", "提案", "--direction",
                      "outbound"]},
            {"kind": "inbound_stimulus", "target": "opp-1", "summary": "返信"},
        ],
    }
    report = sr.run_scenario(scenario, workdir=str(tmp_path))
    assert report["passed"] is True
    # confirm the injected comm really is in the raw store
    store = json.loads((tmp_path / ".beacon" / "project.json").read_text())
    assert sb._has_injected_communication(store)
    return report


def test_ingest_failure_attributed_to_engine_read_derive(tmp_path):
    # The store genuinely holds the擬似着信; we synthesize a FAILED ball assert
    # (as a buggy derive would produce) downstream of the inbound_stimulus. The
    # bisect must say: data is present → the read/derive (engine) is at fault,
    # not the data/store. This is the who_has_the_ball-class one-shot.
    _populate_workdir_with_injection(tmp_path)
    synthetic_report = {
        "passed": False,
        "workdir": str(tmp_path),
        "steps": [
            {"kind": "persona_cli", "label": "add"},
            {"kind": "persona_cli", "label": "outbound"},
            {"kind": "inbound_stimulus", "label": "返信"},
            {"kind": "assert", "label": "ball 観測", "ok": False,
             "reason": "'counterpart' != 'self'",
             "spec_source": "SPEC §6: 最新 inbound → ボールは自分",
             "observation_basis": "communication list --json の ball"},
        ],
        "failure": {"index": 3, "kind": "assert",
                    "reason": "'counterpart' != 'self'",
                    "spec_source": "SPEC §6: 最新 inbound → ボールは自分"},
    }
    diag = sb.diagnose_failure({}, synthetic_report)
    assert diag["responsible_layer"] == sb.LAYER_ENGINE
    assert "read/derive" in diag["boundary"]
    # final-boundary authority stays SPEC, never handed to differential
    assert diag["expected_provenance"] == "spec"


def test_inbound_persistence_failure_attributed_to_store(tmp_path):
    # inbound_stimulus step itself failed and the store has NO injected comm →
    # injection/persistence failed (store), not ingest processing.
    (tmp_path / ".beacon").mkdir(parents=True)
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({"profession": "sales", "opportunities": [
            {"id": "opp-1", "communications": []}]}), encoding="utf-8")
    report = {
        "passed": False, "workdir": str(tmp_path),
        "steps": [{"kind": "inbound_stimulus", "label": "返信", "ok": False,
                   "reason": "inject failed: target not found",
                   "inject": {}}],
        "failure": {"index": 0, "kind": "inbound_stimulus",
                    "reason": "inject failed: target not found"},
    }
    diag = sb.diagnose_failure({}, report)
    assert diag["responsible_layer"] == sb.LAYER_STORE


# --- differential is localization-only, never correctness authority ----------

def test_differential_is_hint_not_authority(tmp_path):
    _populate_workdir_with_injection(tmp_path)
    report = {
        "passed": False, "workdir": str(tmp_path),
        "steps": [
            {"kind": "inbound_stimulus", "label": "返信"},
            {"kind": "assert", "ok": False, "reason": "'counterpart' != 'self'",
             "spec_source": "SPEC §6: 最新 inbound → ボールは自分",
             "observation_basis": "ball"},
        ],
        "failure": {"index": 1, "kind": "assert",
                    "reason": "'counterpart' != 'self'",
                    "spec_source": "SPEC §6: 最新 inbound → ボールは自分"},
    }
    baseline = {"steps": [
        {"kind": "inbound_stimulus"},
        {"kind": "assert", "ok": True,
         "spec_source": "SPEC §6: 最新 inbound → ボールは自分"},
    ]}
    diag = sb.diagnose_failure({}, report, baseline_report=baseline)
    # the FINAL expected stays SPEC-sourced ...
    assert diag["expected_provenance"] == "spec"
    # ... and the differential rides alongside, explicitly labelled as a hint.
    assert diag["differential"]["expected_provenance"] == "differential"
    assert "baseline behaved thus" in diag["differential"]["note"]


# --- non-invasive: no code-under-test imports (方針1 黒箱忠実度) --------------

def test_bisect_does_not_import_code_under_test():
    src = (REPO / "lib" / "scenario_bisect.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # must not reach into the engine / store internals it is diagnosing
    forbidden = {"sales_entities", "commands", "inward_inject", "store_local",
                 "store", "api_client"}
    assert not (imported & forbidden), f"invasive import: {imported & forbidden}"
