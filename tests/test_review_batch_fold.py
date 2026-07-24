"""節目 reviews fire as one batch, fold into one report (ms-119 / e-4125).

A 節目 fires a PAIR (PR-open → AX + maintainability). This must be emittable in
one call (so the Skill fans the judges out in parallel over one shared diff), and
their findings must fold into one deduped, consensus-scored report — a finding two
independent judges both raise is stronger than one raised alone.
"""

import json
import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import review_spine  # noqa: E402


# --- pure: which reviews bind to a node ------------------------------------

def test_batch_types_pr_open_is_data_driven():
    got = {d["review"] for d in review_spine.batch_review_types_for_node("pr-open")}
    assert "ax" in got and "maintainability" in got
    assert all(d["judge_run"] for d in review_spine.batch_review_types_for_node("pr-open"))


def test_batch_types_target_close_pairs_philosophy_and_attainment():
    got = review_spine.batch_review_types_for_node("target-close")
    ids = {d["review"]: d["judge_run"] for d in got}
    assert ids["philosophy"] is True         # judge-run
    assert ids["attainment"] is False        # human-gated verdict


def test_batch_types_unknown_node_empty():
    assert review_spine.batch_review_types_for_node("nope") == []


# --- pure: fold N reports into one -----------------------------------------

def test_aggregate_dedups_and_scores_consensus():
    reports = [
        {"review_type": "ax", "findings": [
            {"title": "silent no-op on bad flag", "file": "lib/commands.py", "line": 10},
            {"title": "AX-only issue", "file": "a.py", "line": 1},
        ]},
        {"review_type": "maintainability", "findings": [
            {"title": "Silent no-op on bad flag.", "file": "lib/commands.py", "line": 10,
             "severity": "high"},
            {"title": "maint-only issue", "file": "b.py", "line": 2},
        ]},
    ]
    agg = review_spine.aggregate_review_reports(reports)
    # the shared finding is merged into one with consensus 2, listed first.
    top = agg["findings"][0]
    assert top["consensus"] == 2
    assert set(top["raised_by"]) == {"ax", "maintainability"}
    assert top["severity"] == "high"  # non-empty severity preserved across merge
    # total distinct findings = 3 (1 shared + 2 unique)
    assert len(agg["findings"]) == 3
    assert agg["by_type"] == {"ax": 2, "maintainability": 2}
    assert agg["reviews"] == ["ax", "maintainability"]


def test_aggregate_empty():
    agg = review_spine.aggregate_review_reports([])
    assert agg["findings"] == [] and agg["reviews"] == []


# --- CLI: one call emits every judge-run bundle for the node ---------------

@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps(
        {"name": "t", "milestones": []}))
    monkeypatch.setenv("BEACON_PROJECT_FILE",
                       str(tmp_path / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_PR", "42")
    for k in ("BEACON_IMPLEMENTER_MODEL",):
        monkeypatch.delenv(k, raising=False)
    # gh pr diff → deterministic fake diff.
    real_run = subprocess.run

    def fake_run(argv, *a, **k):
        if argv[:3] == ["gh", "pr", "diff"]:
            return types.SimpleNamespace(returncode=0, stdout="@@ -1 +1 @@\n-x\n+y\n",
                                         stderr="")
        return real_run(argv, *a, **k)
    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    # no application-map in the store.
    monkeypatch.setattr(commands, "get_store",
                        lambda: types.SimpleNamespace(get_document=lambda _id: None))
    return tmp_path


def test_batch_context_emits_pair_over_shared_diff(proj, monkeypatch, capsys):
    commands.cmd_review_batch_context()
    out = json.loads(capsys.readouterr().out)
    assert out["node"] == "pr-open"
    assert out["target_ref"] == "PR #42"
    rtypes = {r["review_type"] for r in out["reviews"]}
    assert {"ax", "maintainability"} <= rtypes
    # every bundle shares the one diff and carries the independence contract.
    for r in out["reviews"]:
        assert "+y" in r["bundle"]["artifact"]["content"]
        assert r["bundle"]["independence_contract"]
