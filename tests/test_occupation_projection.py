"""Unit tests for the ③ shared-frame occupation projection (ms-108 e-3269).

The shared frame (status / session-start) must read ONE Target shape whether
the project is development (Milestones) or sales (Opportunities). These tests
pin:

  * ``core.project_targets`` maps Milestones → the canonical Target shape.
  * ``sales_entities.project_targets`` maps Opportunities → the SAME shape.
  * ``occupation.project_targets`` dispatches by profession (dev / sales /
    unknown-fallback / missing-field default).
  * The generic core keys (id/label/status/kind/work_items_*) are identical
    across occupations; occupation semantics live only under ``detail``.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import core  # noqa: E402
import sales_entities as se  # noqa: E402
import occupation  # noqa: E402

GENERIC_KEYS = {"id", "label", "status", "kind",
                "work_items_total", "work_items_done", "detail"}


# --------------------------------------------------------------------------
# Development adapter
# --------------------------------------------------------------------------

def _dev_project():
    return {
        "name": "Dev",
        "profession": "dev",
        "milestones": [
            {"id": "ms-1", "title": "First", "status": "in_progress",
             "progress": 40, "entries": [
                 {"type": "task", "status": "done"},
                 {"type": "task", "status": "todo"},
             ]},
            {"id": "ms-2", "label": "Second", "status": "todo",
             "entries": []},
            {"id": "ms-3", "title": "Cancelled one", "status": "cancelled",
             "entries": []},
        ],
    }


def test_dev_project_targets_shape_and_counts():
    targets = core.project_targets(_dev_project())
    # cancelled excluded
    assert [t["id"] for t in targets] == ["ms-1", "ms-2"]
    t1 = targets[0]
    assert GENERIC_KEYS <= set(t1.keys())
    assert t1["kind"] == "milestone"
    assert t1["label"] == "First"          # tolerant read of legacy ``title``
    assert (t1["work_items_total"], t1["work_items_done"]) == (2, 1)
    assert t1["detail"]["progress"] == 40
    # ms-2 reads the canonical ``label`` key
    assert targets[1]["label"] == "Second"


# --------------------------------------------------------------------------
# Sales adapter
# --------------------------------------------------------------------------

def _sales_project_with_opp():
    data = se.build_sales_project("Acme Sales", "close deals")
    opp_id = se.opportunity_add(data, "Big Deal")
    opp = se.find_opportunity(data, opp_id)
    opp.setdefault("activities", []).extend([
        {"id": "act-1", "status": "done"},
        {"id": "act-2", "status": "todo"},
        {"id": "act-3", "status": "todo"},
    ])
    return data, opp_id


def test_sales_project_targets_shape_and_counts():
    data, opp_id = _sales_project_with_opp()
    targets = se.project_targets(data)
    assert len(targets) == 1
    t = targets[0]
    assert GENERIC_KEYS <= set(t.keys())
    assert t["id"] == opp_id
    assert t["kind"] == "opportunity"
    assert t["label"] == "Big Deal"
    assert (t["work_items_total"], t["work_items_done"]) == (3, 1)
    # sales semantics live under detail, not in the generic core
    assert "phase" in t["detail"]
    assert "who_has_the_ball" in t["detail"]
    assert "phase" not in t  # not leaked into the occupation-neutral core


def test_sales_cancelled_opportunity_excluded():
    data, opp_id = _sales_project_with_opp()
    se.find_opportunity(data, opp_id)["status"] = "cancelled"
    assert se.project_targets(data) == []


# --------------------------------------------------------------------------
# Registry dispatch
# --------------------------------------------------------------------------

def test_registry_dispatches_by_profession():
    dev = _dev_project()
    assert occupation.project_targets(dev)[0]["kind"] == "milestone"

    sales, _ = _sales_project_with_opp()
    assert occupation.project_targets(sales)[0]["kind"] == "opportunity"


def test_registry_defaults_and_fallback():
    # missing profession field → dev projection
    no_prof = {"milestones": [{"id": "ms-1", "title": "X", "status": "todo",
                               "entries": []}]}
    assert occupation.resolve_profession(no_prof) == "dev"
    assert occupation.project_targets(no_prof)[0]["kind"] == "milestone"

    # unknown profession → fail-open to dev projection (no crash)
    weird = dict(no_prof, profession="astronaut")
    assert occupation.project_targets(weird)[0]["kind"] == "milestone"


def test_generic_core_identical_across_occupations():
    dev_t = core.project_targets(_dev_project())[0]
    sales_t = se.project_targets(_sales_project_with_opp()[0])[0]
    # both expose exactly the same generic-core key set
    assert set(dev_t.keys()) == set(sales_t.keys()) == GENERIC_KEYS


# --------------------------------------------------------------------------
# Human-readable `beacon status` text output (増分B)
# --------------------------------------------------------------------------

import commands  # noqa: E402


def _run_status_text(monkeypatch, data, capsys):
    monkeypatch.setattr(commands, "load_project", lambda: data)
    monkeypatch.delenv("BEACON_JSON", raising=False)
    monkeypatch.delenv("BEACON_ALL", raising=False)
    monkeypatch.delenv("BEACON_MS_FILTER", raising=False)
    commands.cmd_milestone_list()
    return capsys.readouterr().out


def test_status_text_dev_unchanged(monkeypatch, capsys):
    out = _run_status_text(monkeypatch, _dev_project(), capsys)
    # development keeps its exact line format: [id] label (progress%)
    assert "[ms-1] First" in out
    assert "(40%)" in out
    assert "ACTIVE" in out           # ms-1 is in_progress
    # cancelled milestone is not listed
    assert "Cancelled one" not in out


def test_status_text_sales_lists_opportunities(monkeypatch, capsys):
    data, _ = _sales_project_with_opp()
    out = _run_status_text(monkeypatch, data, capsys)
    # a sales project's status is no longer blank — it lists the opportunity
    assert "[opp-1] Big Deal" in out
    assert "(open)" in out
    assert "1/3" in out  # 1 of 3 activities done (WorkItem counts)


# ms-108 e-3701 (fable review B-5) — drift guard: the dev status text still
# reads milestones directly (byte-for-byte), so pin that its progress agrees
# with the canonical targets[] projection. If someone changes one path, this
# fails rather than letting the two representations silently diverge.
def test_dev_text_progress_matches_target_projection():
    data = _dev_project()
    proj = {t["id"]: t["detail"]["progress"] for t in core.project_targets(data)}
    for ms in data["milestones"]:
        if ms["status"] == "cancelled":
            continue
        assert proj[ms["id"]] == ms.get("progress", 0)


# ms-108 e-3701 (fable review B-1) — the target-collection enumeration now
# lives in the occupation registry, not hardcoded in session_log.
def test_iter_target_records_is_occupation_neutral():
    dev = {"milestones": [{"id": "ms-1"}], "opportunities": []}
    sales = {"milestones": [], "opportunities": [{"id": "opp-1"}]}
    assert [t["id"] for t in occupation.iter_target_records(dev)] == ["ms-1"]
    assert [t["id"] for t in occupation.iter_target_records(sales)] == ["opp-1"]
    assert occupation.iter_target_records({}) == []
