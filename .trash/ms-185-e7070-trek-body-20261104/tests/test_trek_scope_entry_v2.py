"""Unit tests for Trek scope entry v2 schema (ms-99 / e-2828).

Pins:
- ``mint_slot_id`` shape and distinctness (matches ``mint_trek_id`` pattern)
- ``normalize_scope_entry`` legacy inputs still round-trip legacy shape
  (= backward compat guarantee for tests in ``test_trek_schema.py`` and
  every caller reading pre-v2 on-disk data)
- v2 output branch fires only when a v2 marker is present on input or the
  caller passes ``mint_slot=True``
- Identity match (``_scope_entry_identity_matches``) is narrowing-only,
  so ``add_scope_entry`` / ``remove_scope_entry`` treat a legacy row and
  a v2-shaped row that point at the same slot as duplicates
- ``add_pending_scope_op`` uses the same identity match for both
  scope_add (duplicate reject) and scope_remove (not-found reject)

Companion SPEC: ``lgdevK2ZMDVNiR5LwgaF`` (ms-99 SPEC: Trek slot schema
v2 — inventory 真値源化).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402


# ---------------------------------------------------------------------------
# mint_slot_id
# ---------------------------------------------------------------------------

def test_mint_slot_id_shape():
    sid = trek.mint_slot_id()
    assert sid.startswith("sl-")
    assert len(sid) == 3 + 8  # "sl-" + 8 hex
    assert all(c in "0123456789abcdef" for c in sid[3:])


def test_mint_slot_id_distinct():
    ids = {trek.mint_slot_id() for _ in range(100)}
    assert len(ids) == 100


# ---------------------------------------------------------------------------
# normalize_scope_entry — legacy inputs stay legacy (backward compat)
# ---------------------------------------------------------------------------

def test_normalize_legacy_project_plus_ms_stays_legacy_shape():
    # No v2 marker on input → output MUST equal the pre-v2 shape verbatim.
    # This is the pin that keeps ``test_trek_schema.py`` and every legacy
    # caller/reader unchanged after the v2 schema lands.
    out = trek.normalize_scope_entry({"project": "p-1", "milestone": "ms-3"})
    assert out == {"project": "p-1", "milestone": "ms-3"}
    for k in trek.V2_SCOPE_KEYS:
        assert k not in out


def test_normalize_legacy_project_plus_task_stays_legacy_shape():
    out = trek.normalize_scope_entry({"project": "p-1", "task": "e-42"})
    assert out == {"project": "p-1", "task": "e-42"}
    for k in trek.V2_SCOPE_KEYS:
        assert k not in out


def test_normalize_legacy_project_plus_op_stays_legacy_shape():
    out = trek.normalize_scope_entry(
        {"project": "p-1", "operation": "op-2"}
    )
    assert out == {"project": "p-1", "operation": "op-2"}
    for k in trek.V2_SCOPE_KEYS:
        assert k not in out


def test_normalize_legacy_permissive_project_only_stays_legacy_shape():
    # strict=False + no narrowing → grandfathered project-wide row.
    # Still no v2 fields because no v2 marker on input.
    out = trek.normalize_scope_entry({"project": "p-1"}, strict=False)
    assert out == {"project": "p-1"}
    for k in trek.V2_SCOPE_KEYS:
        assert k not in out


# ---------------------------------------------------------------------------
# normalize_scope_entry — v2 branch (marker on input or mint_slot=True)
# ---------------------------------------------------------------------------

def test_normalize_mint_slot_fills_v2_defaults():
    # mint_slot=True → fresh slot_id + derived target_kind/target_id.
    # included_task_ids / claim_session_id / claimed_at not on input → absent.
    out = trek.normalize_scope_entry(
        {"project": "p-1", "milestone": "ms-3"}, mint_slot=True
    )
    assert out["project"] == "p-1"
    assert out["milestone"] == "ms-3"
    assert out["slot_id"].startswith("sl-")
    assert len(out["slot_id"]) == 3 + 8
    assert out["target_kind"] == "milestone"
    assert out["target_id"] == "ms-3"
    # Fields not carried in on input are not synthesised (= keep output
    # minimal, avoid asserting semantics the caller didn't request).
    assert "included_task_ids" not in out
    assert "claim_session_id" not in out
    assert "claimed_at" not in out


def test_normalize_preserves_incoming_slot_id():
    out = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3", "slot_id": "sl-deadbeef",
    })
    assert out["slot_id"] == "sl-deadbeef"
    assert out["target_kind"] == "milestone"
    assert out["target_id"] == "ms-3"


def test_normalize_legacy_slot_id_empty_string_when_v2_marker_but_no_id():
    # Input carries a v2 marker (included_task_ids) but no slot_id and
    # mint_slot=False → slot_id="" (= Phase 3 interactive backfill per
    # director e-2828 decision, no silent hash backfill).
    out = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3", "included_task_ids": None,
    })
    assert out["slot_id"] == ""
    assert out["included_task_ids"] is None  # legacy all-include semantics
    assert out["target_kind"] == "milestone"
    assert out["target_id"] == "ms-3"


def test_normalize_included_task_ids_list_preserved():
    out = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3",
        "included_task_ids": ["e-A", "e-B", "e-C"],
    })
    assert out["included_task_ids"] == ["e-A", "e-B", "e-C"]
    # Ensure we returned a copy, not the caller's list reference.
    out["included_task_ids"].append("e-D")
    out2 = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3",
        "included_task_ids": ["e-A", "e-B", "e-C"],
    })
    assert out2["included_task_ids"] == ["e-A", "e-B", "e-C"]


def test_normalize_included_task_ids_none_marks_legacy_all_include():
    # SPEC AC4: included_task_ids=None on a MS slot = legacy semantics
    # (all children auto-include). This is the marker Phase 2's
    # materialize_slots reads to preserve behaviour on grandfathered data.
    out = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3", "included_task_ids": None,
    })
    assert out["included_task_ids"] is None


def test_normalize_claim_session_id_preserved():
    out = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3",
        "claim_session_id": "sv-abcd-1",
    })
    assert out["claim_session_id"] == "sv-abcd-1"


def test_normalize_claim_session_id_empty_string_when_key_present():
    # Passing an empty claim_session_id is the "unclaim" gesture; must
    # round-trip so callers can express it.
    out = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3", "claim_session_id": "",
    })
    assert out["claim_session_id"] == ""


def test_normalize_claimed_at_preserved():
    out = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3",
        "claim_session_id": "sv-1",
        "claimed_at": "2026-07-03T09:00:00Z",
    })
    assert out["claimed_at"] == "2026-07-03T09:00:00Z"


def test_normalize_claimed_at_none_when_empty_string_given():
    # None / "" both mean "not stamped yet"; canonicalise to None so
    # downstream readers don't have to check both.
    out = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3", "claimed_at": "",
    })
    assert out["claimed_at"] is None


def test_normalize_v2_target_kind_task():
    out = trek.normalize_scope_entry(
        {"project": "p-1", "task": "e-99"}, mint_slot=True
    )
    assert out["target_kind"] == "task"
    assert out["target_id"] == "e-99"


def test_normalize_v2_target_kind_operation():
    out = trek.normalize_scope_entry(
        {"project": "p-1", "operation": "op-7"}, mint_slot=True
    )
    assert out["target_kind"] == "operation"
    assert out["target_id"] == "op-7"


def test_normalize_v2_drops_unknown_keys():
    # Same tightness contract as legacy: unknown keys never survive.
    out = trek.normalize_scope_entry({
        "project": "p-1", "milestone": "ms-3", "slot_id": "sl-a",
        "spurious": "value", "rabbit": True,
    })
    assert "spurious" not in out
    assert "rabbit" not in out


# ---------------------------------------------------------------------------
# _scope_entry_identity_matches — narrowing tuple only
# ---------------------------------------------------------------------------

def test_identity_matches_same_project_and_milestone():
    a = {"project": "p-1", "milestone": "ms-3"}
    b = {"project": "p-1", "milestone": "ms-3"}
    assert trek._scope_entry_identity_matches(a, b) is True


def test_identity_matches_ignores_v2_attributes():
    # Same slot (project + milestone), different v2 attributes → still identical.
    a = {"project": "p-1", "milestone": "ms-3"}
    b = {
        "project": "p-1", "milestone": "ms-3",
        "slot_id": "sl-abcd0001",
        "target_kind": "milestone", "target_id": "ms-3",
        "included_task_ids": ["e-A"],
        "claim_session_id": "sv-1",
        "claimed_at": "2026-07-03T00:00:00Z",
    }
    assert trek._scope_entry_identity_matches(a, b) is True


def test_identity_differs_by_narrowing_key():
    a = {"project": "p-1", "milestone": "ms-3"}
    b = {"project": "p-1", "milestone": "ms-4"}
    assert trek._scope_entry_identity_matches(a, b) is False


def test_identity_differs_by_project():
    a = {"project": "p-1", "milestone": "ms-3"}
    b = {"project": "p-2", "milestone": "ms-3"}
    assert trek._scope_entry_identity_matches(a, b) is False


def test_identity_ms_vs_task_same_id_string_differs():
    # A milestone entry ms-3 and a task entry ms-3 (hypothetical id
    # collision) are structurally different slots.
    a = {"project": "p-1", "milestone": "ms-3"}
    b = {"project": "p-1", "task": "ms-3"}
    assert trek._scope_entry_identity_matches(a, b) is False


def test_identity_project_only_legacy_row_matches_project_only():
    # Grandfathered project-wide row identity.
    a = {"project": "p-1"}
    b = {"project": "p-1"}
    assert trek._scope_entry_identity_matches(a, b) is True


# ---------------------------------------------------------------------------
# add_scope_entry / remove_scope_entry — cross-shape identity
# ---------------------------------------------------------------------------

def _fresh_trek_doc() -> dict:
    return {"scope": [], "updated_at": ""}


def test_add_scope_entry_rejects_v2_dup_of_legacy_row():
    # Existing scope has a legacy entry; incoming v2-shaped add for the
    # same slot must be rejected as a duplicate.
    doc = {"scope": [{"project": "p-1", "milestone": "ms-3"}],
           "updated_at": ""}
    with pytest.raises(ValueError, match="already exists"):
        trek.add_scope_entry(doc, entry={
            "project": "p-1", "milestone": "ms-3",
            "slot_id": "sl-newnew11", "included_task_ids": None,
        })


def test_add_scope_entry_rejects_legacy_dup_of_v2_row():
    # Existing scope has a v2 entry; incoming legacy-shaped add for the
    # same slot must be rejected as a duplicate.
    doc = {"scope": [{
        "project": "p-1", "milestone": "ms-3",
        "slot_id": "sl-old00001",
        "target_kind": "milestone", "target_id": "ms-3",
        "included_task_ids": ["e-A"],
    }], "updated_at": ""}
    with pytest.raises(ValueError, match="already exists"):
        trek.add_scope_entry(doc, entry={
            "project": "p-1", "milestone": "ms-3",
        })


def test_add_scope_entry_appends_when_narrowing_differs():
    doc = _fresh_trek_doc()
    trek.add_scope_entry(doc, entry={"project": "p-1", "milestone": "ms-3"})
    trek.add_scope_entry(doc, entry={"project": "p-1", "milestone": "ms-4"})
    assert len(doc["scope"]) == 2


def test_remove_scope_entry_matches_legacy_row_from_v2_request():
    # Persisted row is legacy; request phrased in v2 shape still matches.
    doc = {"scope": [{"project": "p-1", "milestone": "ms-3"}],
           "updated_at": ""}
    trek.remove_scope_entry(doc, entry={
        "project": "p-1", "milestone": "ms-3",
        "slot_id": "sl-anything", "included_task_ids": None,
    })
    assert doc["scope"] == []


def test_remove_scope_entry_matches_v2_row_from_legacy_request():
    # Persisted row is v2; request phrased legacy still matches.
    doc = {"scope": [{
        "project": "p-1", "milestone": "ms-3",
        "slot_id": "sl-persisted",
        "target_kind": "milestone", "target_id": "ms-3",
        "claim_session_id": "sv-1",
    }], "updated_at": ""}
    trek.remove_scope_entry(doc, entry={
        "project": "p-1", "milestone": "ms-3",
    })
    assert doc["scope"] == []


def test_remove_scope_entry_raises_when_narrowing_missing():
    doc = {"scope": [{"project": "p-1", "milestone": "ms-3"}],
           "updated_at": ""}
    with pytest.raises(ValueError, match="not found"):
        trek.remove_scope_entry(doc, entry={
            "project": "p-1", "milestone": "ms-4",
        })


# ---------------------------------------------------------------------------
# add_pending_scope_op — identity match on both branches
# ---------------------------------------------------------------------------

def test_pending_scope_add_rejects_v2_dup_of_legacy_row():
    doc = {"scope": [{"project": "p-1", "milestone": "ms-3"}],
           "updated_at": "", trek.PENDING_SCOPE_OPS_KEY: []}
    with pytest.raises(ValueError, match="already present"):
        trek.add_pending_scope_op(
            doc, action=trek.PENDING_SCOPE_ACTION_ADD,
            entry={
                "project": "p-1", "milestone": "ms-3",
                "slot_id": "sl-newnew11",
            },
            requested_by_session_id="sv-x",
        )


def test_pending_scope_remove_matches_legacy_row_from_v2_request():
    doc = {"scope": [{"project": "p-1", "milestone": "ms-3"}],
           "updated_at": "", trek.PENDING_SCOPE_OPS_KEY: []}
    rec = trek.add_pending_scope_op(
        doc, action=trek.PENDING_SCOPE_ACTION_REMOVE,
        entry={
            "project": "p-1", "milestone": "ms-3",
            "slot_id": "sl-whatever",
        },
        requested_by_session_id="sv-x",
    )
    assert rec["action"] == trek.PENDING_SCOPE_ACTION_REMOVE
    assert doc[trek.PENDING_SCOPE_OPS_KEY][0]["pending_id"].startswith("sp-")


def test_pending_scope_remove_matches_v2_row_from_legacy_request():
    # Persisted row is v2; remove request phrased legacy still stages.
    doc = {"scope": [{
        "project": "p-1", "milestone": "ms-3",
        "slot_id": "sl-persist7",
        "target_kind": "milestone", "target_id": "ms-3",
    }], "updated_at": "", trek.PENDING_SCOPE_OPS_KEY: []}
    trek.add_pending_scope_op(
        doc, action=trek.PENDING_SCOPE_ACTION_REMOVE,
        entry={"project": "p-1", "milestone": "ms-3"},
        requested_by_session_id="sv-x",
    )
    assert len(doc[trek.PENDING_SCOPE_OPS_KEY]) == 1
