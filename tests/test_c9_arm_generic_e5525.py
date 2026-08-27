"""ms-109 e-5525 (C9 arm-name coupling): 投影 / read 層の profession-concrete な
kind / arm 参照を記述子駆動へ寄せた不変条件を pin する。

対象 3 サイト:
  - occupation.claim_target_kinds — claimable kind 列挙を manifest 由来に
  - cmd_claim._canonical_target_kind — --target <kind> 検証を記述子 prefix 駆動に
  - scenario_bisect._all_communications — 擬似着信検出を iter_evidence 経由に

いずれも「新 target-class (記述子 occupation) を足したとき取りこぼさない」ことが芯。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation as occ  # noqa: E402
import scenario_bisect as sb  # noqa: E402


# A data-defined occupation whose Target carries an EXPLICIT evidence arm
# ("messages") so the generic evidence spine walks it without any code edit.
TICKET = {
    "kind": "ticket", "label": "チケット", "profession": "backoffice",
    "type": "single-shot", "id_prefix": "tkt-", "collection": "tickets",
    "decomposition": {"id_field": "id", "arms": ["replies", "messages"]},
    "work_item_arm": {"arm": "replies", "item_type": None, "kind": "work_item"},
    "evidence_arms": [{"arm": "messages", "item_type": None}],
}


def _ticket_project():
    return {"profession": "backoffice", "target_classes": [TICKET],
            "tickets": []}


# --------------------------------------------------------------------------
# occupation.claim_target_kinds
# --------------------------------------------------------------------------

def test_claim_target_kinds_includes_operation():
    """The old hardcoded error text said operation was NOT claimable, yet
    build_claim_views walks it via claim_target_collections — the derived kind
    list must include it (the drift the fix closes)."""
    kinds = occ.claim_target_kinds({})
    assert "operation" in kinds
    assert "milestone" in kinds and "opportunity" in kinds and "account" in kinds


def test_claim_target_kinds_covers_non_aggregatable_acquisition():
    """acquisition is claimable (secondary) though aggregatable=False; its kind
    must still resolve (it is absent from the aggregatable-only _COLLECTION_KIND)."""
    assert "acquisition" in occ.claim_target_kinds({})


def test_claim_target_kinds_includes_descriptor_kind():
    """A data-defined occupation's claimable kind lights up with no edit here."""
    assert "ticket" in occ.claim_target_kinds(_ticket_project())


# --------------------------------------------------------------------------
# occupation.canonical_claim_kind (--target <kind>:<id> validation vocabulary)
# --------------------------------------------------------------------------

def test_canonical_claim_kind_builtin_shorthand_and_canonical():
    for tok, want in [("ms", "milestone"), ("milestone", "milestone"),
                      ("opp", "opportunity"), ("acc", "account"),
                      ("op", "operation")]:
        assert occ.canonical_claim_kind(tok, {}) == want


def test_canonical_claim_kind_descriptor_aware():
    """A descriptor kind AND its id-prefix shorthand both canonicalise — the
    ms/opp/acc hardcode could not."""
    data = _ticket_project()
    assert occ.canonical_claim_kind("ticket", data) == "ticket"
    assert occ.canonical_claim_kind("tkt", data) == "ticket"


def test_canonical_claim_kind_unknown_is_empty():
    """Unknown token → "" so the caller SKIPS validation (never a false reject)."""
    assert occ.canonical_claim_kind("bogus", {}) == ""
    assert occ.canonical_claim_kind("", {}) == ""


def test_canonical_claim_kind_covers_acquisition():
    """PR#684 review finding 1: acquisition is advertised as claimable but was
    dropped by the narrowing-prefix validation (acq- not in that seed), so an
    `acquisition:ms-1` mismatch passed silently. The claimable-axis accessor must
    resolve BOTH the canonical name and the acq- shorthand."""
    assert occ.canonical_claim_kind("acquisition", {}) == "acquisition"
    assert occ.canonical_claim_kind("acq", {}) == "acquisition"


def test_claim_target_kinds_subset_of_validatable():
    """The reviewer's structural invariant: every ADVERTISED claimable kind
    (claim_target_kinds) must be VALIDATABLE (canonicalises to itself). No kind can
    be advertised yet skip validation — the acquisition split that finding 1 caught
    cannot recur for any class, built-in or descriptor."""
    for data in ({}, _ticket_project()):
        for kind in occ.claim_target_kinds(data):
            assert occ.canonical_claim_kind(kind, data) == kind, kind


# --------------------------------------------------------------------------
# scenario_bisect._all_communications
# --------------------------------------------------------------------------

def test_all_communications_covers_sales_both_grains():
    """Preserves the built-in sales walk: target-level + nested under work items."""
    store = {
        "profession": "sales",
        "opportunities": [{"id": "opp-1",
                           "communications": [{"id": "c1"}],
                           "activities": [{"id": "act-1",
                                           "communications": [{"id": "c2"}]}]}],
        "accounts": [{"id": "acc-1",
                      "communications": [{"id": "c3"}],
                      "nurturings": [{"id": "nrt-1",
                                      "communications": [{"id": "c4"}]}]}],
    }
    ids = sorted(c.get("id") for c in sb._all_communications(store))
    assert ids == ["c1", "c2", "c3", "c4"]


def test_injected_detection_generic_over_descriptor_class():
    """A 擬似着信 (source.injected) in a DESCRIPTOR-defined class's evidence arm is
    now detected — the hardcoded opportunities/accounts walk would have missed it."""
    store = _ticket_project()
    store["tickets"] = [{"id": "tkt-1",
                         "messages": [{"id": "m1", "source": {"injected": True}}]}]
    assert sb._has_injected_communication(store) is True


def test_injected_detection_false_on_dev_store_no_crash():
    """Dev commits never carry source.injected → False, and iter_evidence over a
    dev store does not raise."""
    dev = {"profession": "dev",
           "milestones": [{"id": "ms-1", "entries": [
               {"id": "e-1", "type": "task"},
               {"id": "e-2", "type": "commit", "meta": {"hash": "abc"}}]}]}
    assert sb._has_injected_communication(dev) is False
