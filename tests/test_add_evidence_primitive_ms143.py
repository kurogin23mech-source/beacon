"""Unit + parity tests for the ms-143 profession-generic evidence primitive
(``occupation.add_evidence``, 設計判断 b 系統4 = 証跡追加).

A sales Communication (事後記録型の証跡) is recorded through the SAME primitive
whether it is addressed to a Target (opp-/acc-) or nested under the work item it
fulfilled (act-/nrt-), mirroring the dev commit↔task model. ``add_evidence`` is
the evidence-grain sibling of ``add_work_item``; ``record_target_entry`` no-ops on
a sales Target and so cannot carry this grain — that is the gap this fills.

Accounts / nurturings are deliberately NOT ``profession_manifest`` Target-classes
(that invariant stays milestones + opportunities), so ``_resolve_evidence_parent``
reaches the sales resolvers directly — occupation.py is the layer allowed to know
sales collections (ms-143 option A, human-confirmed 2026-08-10).

These harnesses PIN that the new path produces byte-identical records (incl. the
exact nest position, key set, and comm- global numbering) to the pre-refactor
``sales_entities.communication_add``, and that the frontend shim still delegates
identically. A drift is surfaced here, never silently absorbed.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import occupation      # noqa: E402
import sales_entities  # noqa: E402

FIXED = "2026-08-10T00:00:00Z"


def _sales():
    return {
        "id": "p", "profession": "sales",
        "opportunities": [
            {"id": "opp-1", "phase": "商談中",
             "activities": [{"id": "act-1", "description": "call"}],
             "communications": []},
        ],
        "accounts": [
            {"id": "acc-1", "phase": "接触",
             "nurturings": [{"id": "nrt-1", "description": "touch"}],
             "communications": []},
        ],
    }


def _add(data, parent_id, **kw):
    kw.setdefault("summary", "s")
    kw.setdefault("direction", "outbound")
    kw.setdefault("channel", "email")
    kw.setdefault("created_at", FIXED)
    return occupation.add_evidence(data, parent_id, **kw)


# --- grain + nesting -------------------------------------------------------

def test_opp_grain_records_at_target_level():
    data = _sales()
    cid = _add(data, "opp-1", source={"ref": "m1"})
    assert cid == "comm-1"
    opp = data["opportunities"][0]
    assert opp["activities"][0].get("communications", []) == []  # NOT nested
    assert opp["communications"] == [{
        "id": "comm-1", "direction": "outbound", "channel": "email",
        "summary": "s", "source": {"ref": "m1"}, "linked_id": "",
        "occurred_at": "", "created_at": FIXED, "created_in_phase": "商談中",
    }]


def test_activity_grain_nests_under_work_item():
    data = _sales()
    cid = _add(data, "act-1")
    opp = data["opportunities"][0]
    assert opp["communications"] == []                # NOT at target level
    assert opp["activities"][0]["communications"] == [{
        "id": cid, "direction": "outbound", "channel": "email",
        "summary": "s", "source": {}, "linked_id": "act-1",
        "occurred_at": "", "created_at": FIXED, "created_in_phase": "商談中",
    }]


def test_account_grain_records_at_target_level():
    data = _sales()
    _add(data, "acc-1", direction="inbound")
    acc = data["accounts"][0]
    assert acc["nurturings"][0].get("communications", []) == []
    rec = acc["communications"][0]
    assert rec["linked_id"] == "" and rec["created_in_phase"] == "接触"
    assert rec["direction"] == "inbound"


def test_nurturing_grain_nests_under_work_item():
    data = _sales()
    _add(data, "nrt-1")
    acc = data["accounts"][0]
    assert acc["communications"] == []
    rec = acc["nurturings"][0]["communications"][0]
    assert rec["linked_id"] == "nrt-1" and rec["created_in_phase"] == "接触"


def test_body_written_only_when_present():
    data = _sales()
    _add(data, "opp-1", body="  full text  ")
    assert data["opportunities"][0]["communications"][0]["body"] == "full text"
    _add(data, "opp-1", body="   ")
    assert "body" not in data["opportunities"][0]["communications"][1]


def test_created_in_phase_explicit_overrides_container_default():
    data = _sales()
    _add(data, "opp-1", created_in_phase="提案")
    assert data["opportunities"][0]["communications"][0]["created_in_phase"] == "提案"


# --- global-by-prefix numbering across opportunities + accounts ------------

def test_comm_ids_are_global_by_prefix_across_containers():
    data = _sales()
    assert _add(data, "opp-1") == "comm-1"
    assert _add(data, "acc-1") == "comm-2"   # numbering spans opp + acc
    assert _add(data, "act-1") == "comm-3"   # nested records count too


# --- error precedence + messages (parity with pre-refactor) ----------------

def test_unresolvable_parent_raises_first():
    data = _sales()
    with pytest.raises(ValueError, match="Communication target not found"):
        occupation.add_evidence(data, "opp-404", summary="", direction="bad")


def test_empty_summary_raises_before_direction():
    data = _sales()
    with pytest.raises(ValueError, match="summary is required"):
        occupation.add_evidence(data, "opp-1", summary="   ", direction="bad")


def test_invalid_direction_raises():
    data = _sales()
    with pytest.raises(ValueError, match="direction must be one of"):
        occupation.add_evidence(data, "opp-1", summary="s", direction="sideways")


# --- frontend shim delegates identically -----------------------------------

def test_communication_add_shim_matches_add_evidence():
    for parent in ("opp-1", "acc-1", "act-1", "nrt-1"):
        via_prim = _sales()
        occupation.add_evidence(via_prim, parent, summary="s",
                                direction="outbound", channel="email",
                                source={"ref": "x"}, created_at=FIXED)
        via_shim = _sales()
        sales_entities.communication_add(via_shim, parent, "s",
                                         direction="outbound", channel="email",
                                         source={"ref": "x"}, created_at=FIXED)
        assert via_prim == via_shim, parent
