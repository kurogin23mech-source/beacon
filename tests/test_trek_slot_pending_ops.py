"""Unit tests for Trek slot pending ops (ms-99 / e-2829).

Pins the two new slot-id-based pending action verbs (``scope_amend`` /
``scope_claim``) that flow through the same ``pending_scope_ops`` queue
as ``scope_add`` / ``scope_remove`` so SPEC AC 15 ("all slot CLI ops
via staging") holds.

Covers:
- ``add_pending_slot_amend_op`` — staging, slot lookup, no-op refusal
- ``add_pending_slot_claim_op`` — staging, empty-session unclaim gesture
- ``_apply_slot_amend`` — legacy null → materialised list, add + remove
- ``_apply_slot_claim`` — set + unclaim behaviour
- ``approve_pending_scope_op`` routing for AMEND / CLAIM
- ``materialize_slot_view`` — legacy row vs v2 row projection
- Cross-verb interaction: add → approve → amend → approve chain
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_trek_with_v2_slot(slot_id: str = "sl-aaaa0001") -> dict:
    """Trek doc with a single v2-shape MS slot ready for amend / claim."""
    return {
        "trek_id": "tk-aaaaaaaa",
        "scope": [{
            "project": "p-1",
            "milestone": "ms-3",
            "slot_id": slot_id,
            "target_kind": "milestone",
            "target_id": "ms-3",
            "included_task_ids": None,  # legacy semantics marker
            "claim_session_id": "",
            "claimed_at": None,
        }],
        "pending_scope_ops": [],
        "updated_at": "",
    }


def _make_trek_with_legacy_slot() -> dict:
    """Trek doc with a single legacy (pre-v2) scope entry."""
    return {
        "trek_id": "tk-legacyaa",
        "scope": [{"project": "p-1", "milestone": "ms-3"}],
        "pending_scope_ops": [],
        "updated_at": "",
    }


# ---------------------------------------------------------------------------
# add_pending_slot_amend_op
# ---------------------------------------------------------------------------

def test_amend_op_stages_add_children():
    t = _make_trek_with_v2_slot()
    rec = trek.add_pending_slot_amend_op(
        t, slot_id="sl-aaaa0001",
        add_children=["e-A", "e-B"],
        requested_by_session_id="sv-req",
    )
    assert rec["pending_id"].startswith("sp-")
    assert rec["action"] == trek.PENDING_SCOPE_ACTION_AMEND
    assert rec["slot_id"] == "sl-aaaa0001"
    assert rec["add_children"] == ["e-A", "e-B"]
    assert rec["remove_children"] == []
    assert rec["requested_by_session_id"] == "sv-req"
    # Scope not mutated yet — staging only.
    assert t["scope"][0]["included_task_ids"] is None
    assert len(t["pending_scope_ops"]) == 1


def test_amend_op_stages_remove_children():
    t = _make_trek_with_v2_slot()
    t["scope"][0]["included_task_ids"] = ["e-X", "e-Y", "e-Z"]
    rec = trek.add_pending_slot_amend_op(
        t, slot_id="sl-aaaa0001",
        remove_children=["e-Y"],
    )
    assert rec["remove_children"] == ["e-Y"]
    assert rec["add_children"] == []


def test_amend_op_rejects_missing_slot():
    t = _make_trek_with_v2_slot()
    with pytest.raises(ValueError, match="slot not found"):
        trek.add_pending_slot_amend_op(
            t, slot_id="sl-noexist0", add_children=["e-A"],
        )
    assert t["pending_scope_ops"] == []


def test_amend_op_rejects_empty_slot_id():
    t = _make_trek_with_v2_slot()
    with pytest.raises(ValueError, match="slot_id is required"):
        trek.add_pending_slot_amend_op(t, slot_id="", add_children=["e-A"])


def test_amend_op_rejects_noop():
    # No-op amend has no meaning — surface early.
    t = _make_trek_with_v2_slot()
    with pytest.raises(ValueError, match="at least one"):
        trek.add_pending_slot_amend_op(t, slot_id="sl-aaaa0001")


# ---------------------------------------------------------------------------
# add_pending_slot_claim_op
# ---------------------------------------------------------------------------

def test_claim_op_stages_with_session_id():
    t = _make_trek_with_v2_slot()
    rec = trek.add_pending_slot_claim_op(
        t, slot_id="sl-aaaa0001", session_id="sv-claimer",
        requested_by_session_id="sv-req",
    )
    assert rec["action"] == trek.PENDING_SCOPE_ACTION_CLAIM
    assert rec["session_id"] == "sv-claimer"
    assert rec["slot_id"] == "sl-aaaa0001"
    # Slot untouched at stage time.
    assert t["scope"][0]["claim_session_id"] == ""
    assert t["scope"][0]["claimed_at"] is None


def test_claim_op_stages_unclaim_gesture():
    # Empty session_id = unclaim.
    t = _make_trek_with_v2_slot()
    t["scope"][0]["claim_session_id"] = "sv-someone"
    rec = trek.add_pending_slot_claim_op(
        t, slot_id="sl-aaaa0001", session_id="",
    )
    assert rec["session_id"] == ""


def test_claim_op_rejects_missing_slot():
    t = _make_trek_with_v2_slot()
    with pytest.raises(ValueError, match="slot not found"):
        trek.add_pending_slot_claim_op(
            t, slot_id="sl-noexist0", session_id="sv-x",
        )


def test_claim_op_rejects_empty_slot_id():
    t = _make_trek_with_v2_slot()
    with pytest.raises(ValueError, match="slot_id is required"):
        trek.add_pending_slot_claim_op(t, slot_id="", session_id="sv-x")


# ---------------------------------------------------------------------------
# approve_pending_scope_op — AMEND routing
# ---------------------------------------------------------------------------

def test_approve_amend_materialises_legacy_null_and_adds():
    # Legacy null (= all-include) is materialised to an explicit list at
    # first amend. This is the semantic pivot: after amend, the slot's
    # included_task_ids becomes an opt-in list.
    t = _make_trek_with_v2_slot()
    rec = trek.add_pending_slot_amend_op(
        t, slot_id="sl-aaaa0001", add_children=["e-A"],
    )
    trek.approve_pending_scope_op(t, pending_id=rec["pending_id"])
    assert t["scope"][0]["included_task_ids"] == ["e-A"]
    assert t["pending_scope_ops"] == []


def test_approve_amend_add_then_add_dedups():
    t = _make_trek_with_v2_slot()
    r1 = trek.add_pending_slot_amend_op(
        t, slot_id="sl-aaaa0001", add_children=["e-A"],
    )
    trek.approve_pending_scope_op(t, pending_id=r1["pending_id"])
    r2 = trek.add_pending_slot_amend_op(
        t, slot_id="sl-aaaa0001", add_children=["e-A", "e-B"],
    )
    trek.approve_pending_scope_op(t, pending_id=r2["pending_id"])
    # e-A added once even after two amends including it.
    assert t["scope"][0]["included_task_ids"] == ["e-A", "e-B"]


def test_approve_amend_remove_absent_is_noop():
    t = _make_trek_with_v2_slot()
    t["scope"][0]["included_task_ids"] = ["e-A", "e-B"]
    rec = trek.add_pending_slot_amend_op(
        t, slot_id="sl-aaaa0001", remove_children=["e-Z"],
    )
    trek.approve_pending_scope_op(t, pending_id=rec["pending_id"])
    # e-Z was never in the list; removal is a no-op.
    assert t["scope"][0]["included_task_ids"] == ["e-A", "e-B"]


def test_approve_amend_add_and_remove_together():
    t = _make_trek_with_v2_slot()
    t["scope"][0]["included_task_ids"] = ["e-A", "e-B"]
    rec = trek.add_pending_slot_amend_op(
        t, slot_id="sl-aaaa0001",
        add_children=["e-C"], remove_children=["e-A"],
    )
    trek.approve_pending_scope_op(t, pending_id=rec["pending_id"])
    assert t["scope"][0]["included_task_ids"] == ["e-B", "e-C"]


# ---------------------------------------------------------------------------
# approve_pending_scope_op — CLAIM routing
# ---------------------------------------------------------------------------

def test_approve_claim_stamps_session_and_timestamp():
    t = _make_trek_with_v2_slot()
    rec = trek.add_pending_slot_claim_op(
        t, slot_id="sl-aaaa0001", session_id="sv-claimer",
    )
    trek.approve_pending_scope_op(t, pending_id=rec["pending_id"])
    assert t["scope"][0]["claim_session_id"] == "sv-claimer"
    assert t["scope"][0]["claimed_at"] is not None
    assert isinstance(t["scope"][0]["claimed_at"], str)
    assert t["scope"][0]["claimed_at"].endswith("Z")


def test_approve_claim_leaves_task_state_untouched():
    # SPEC 方針 4: claim never changes task state. This is the split
    # that lets "I'll take this next" be visible before any state
    # transition. We assert on the persisted claim fields only.
    t = _make_trek_with_v2_slot()
    # Fake a task_states cache to make sure we don't clobber it.
    t["task_states"] = {"e-999": {"state": "working"}}
    rec = trek.add_pending_slot_claim_op(
        t, slot_id="sl-aaaa0001", session_id="sv-x",
    )
    trek.approve_pending_scope_op(t, pending_id=rec["pending_id"])
    assert t["task_states"] == {"e-999": {"state": "working"}}


def test_approve_unclaim_clears_session_and_timestamp():
    t = _make_trek_with_v2_slot()
    t["scope"][0]["claim_session_id"] = "sv-old"
    t["scope"][0]["claimed_at"] = "2026-01-01T00:00:00Z"
    rec = trek.add_pending_slot_claim_op(
        t, slot_id="sl-aaaa0001", session_id="",
    )
    trek.approve_pending_scope_op(t, pending_id=rec["pending_id"])
    assert t["scope"][0]["claim_session_id"] == ""
    assert t["scope"][0]["claimed_at"] is None


# ---------------------------------------------------------------------------
# add_pending_scope_op with mint_slot=True (for cmd_trek_slot_add path)
# ---------------------------------------------------------------------------

def test_pending_scope_add_with_mint_slot_gets_fresh_id():
    t = {"trek_id": "tk-fresh", "scope": [], "pending_scope_ops": [],
         "updated_at": ""}
    rec = trek.add_pending_scope_op(
        t,
        action=trek.PENDING_SCOPE_ACTION_ADD,
        entry={"project": "p-1", "milestone": "ms-5"},
        mint_slot=True,
    )
    entry = rec["entry"]
    assert entry["slot_id"].startswith("sl-")
    assert entry["target_kind"] == "milestone"
    assert entry["target_id"] == "ms-5"


def test_pending_scope_add_without_mint_slot_stays_legacy():
    # Existing beacon trek scope-add path — no v2 field synthesised.
    t = {"trek_id": "tk-fresh", "scope": [], "pending_scope_ops": [],
         "updated_at": ""}
    rec = trek.add_pending_scope_op(
        t,
        action=trek.PENDING_SCOPE_ACTION_ADD,
        entry={"project": "p-1", "milestone": "ms-5"},
    )
    entry = rec["entry"]
    assert entry == {"project": "p-1", "milestone": "ms-5"}


def test_pending_scope_op_rejects_slot_verbs_via_entry_helper():
    # Slot-id-based verbs must go through the dedicated helpers.
    t = {"trek_id": "tk", "scope": [], "pending_scope_ops": [],
         "updated_at": ""}
    with pytest.raises(ValueError, match="use add_pending_slot"):
        trek.add_pending_scope_op(
            t,
            action=trek.PENDING_SCOPE_ACTION_AMEND,
            entry={"project": "p-1"},
        )


# ---------------------------------------------------------------------------
# materialize_slot_view
# ---------------------------------------------------------------------------

def test_materialize_slot_view_projects_v2_row():
    t = _make_trek_with_v2_slot()
    t["scope"][0]["included_task_ids"] = ["e-A"]
    t["scope"][0]["claim_session_id"] = "sv-c"
    t["scope"][0]["claimed_at"] = "2026-07-03T00:00:00Z"
    rows = trek.materialize_slot_view(t)
    assert len(rows) == 1
    r = rows[0]
    assert r["slot_id"] == "sl-aaaa0001"
    assert r["target_kind"] == "milestone"
    assert r["target_id"] == "ms-3"
    assert r["project"] == "p-1"
    assert r["included_task_ids"] == ["e-A"]
    assert r["claim_session_id"] == "sv-c"
    assert r["claimed_at"] == "2026-07-03T00:00:00Z"


def test_materialize_slot_view_falls_back_to_legacy_narrowing():
    # Legacy on-disk row without v2 fields — view still surfaces the
    # narrowing tuple by falling back to milestone/task/operation keys.
    t = _make_trek_with_legacy_slot()
    rows = trek.materialize_slot_view(t)
    assert len(rows) == 1
    r = rows[0]
    assert r["slot_id"] == ""  # legacy row, no id assigned
    assert r["target_kind"] == "milestone"
    assert r["target_id"] == "ms-3"
    assert r["included_task_ids"] is None  # legacy semantics preserved
    assert r["claim_session_id"] == ""


def test_materialize_slot_view_empty_scope_returns_empty_list():
    t = {"trek_id": "tk", "scope": [], "pending_scope_ops": []}
    assert trek.materialize_slot_view(t) == []


# ---------------------------------------------------------------------------
# End-to-end: add → approve → amend → approve → claim → approve chain
# ---------------------------------------------------------------------------

def test_slot_add_amend_claim_full_chain():
    t = {"trek_id": "tk-chain", "scope": [], "pending_scope_ops": [],
         "updated_at": ""}
    # Add a MS slot with mint_slot=True (= new slot-add path).
    r_add = trek.add_pending_scope_op(
        t,
        action=trek.PENDING_SCOPE_ACTION_ADD,
        entry={"project": "p-1", "milestone": "ms-7"},
        mint_slot=True,
    )
    trek.approve_pending_scope_op(t, pending_id=r_add["pending_id"])
    assert len(t["scope"]) == 1
    slot_id = t["scope"][0]["slot_id"]
    assert slot_id.startswith("sl-")

    # Amend the slot's child list.
    r_amend = trek.add_pending_slot_amend_op(
        t, slot_id=slot_id, add_children=["e-1", "e-2"],
    )
    trek.approve_pending_scope_op(t, pending_id=r_amend["pending_id"])
    assert t["scope"][0]["included_task_ids"] == ["e-1", "e-2"]

    # Claim the slot.
    r_claim = trek.add_pending_slot_claim_op(
        t, slot_id=slot_id, session_id="sv-worker",
    )
    trek.approve_pending_scope_op(t, pending_id=r_claim["pending_id"])
    assert t["scope"][0]["claim_session_id"] == "sv-worker"
    assert t["scope"][0]["claimed_at"] is not None

    # Materialise view.
    rows = trek.materialize_slot_view(t)
    assert len(rows) == 1
    assert rows[0]["slot_id"] == slot_id
    assert rows[0]["included_task_ids"] == ["e-1", "e-2"]
    assert rows[0]["claim_session_id"] == "sv-worker"
