"""Tests for descriptor-aware occupation registry (ms-122 e-3957): a data-
defined target-class (a descriptor under project.json ``target_classes``)
contributes to the six occupation registries WITHOUT editing occupation.py, and
dev / sales behaviour is unchanged when no descriptors are present."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation as occ  # noqa: E402
import target_engine as te  # noqa: E402


CONTRACT = {
    "kind": "contract", "label": "契約", "profession": "backoffice",
    "type": "single-shot", "id_prefix": "ctr-", "collection": "contracts",
    "decomposition": {"id_field": "id", "arms": ["clauses"]},
    "fields": [{"key": "counterparty", "label": "相手方", "required": True}],
    "phases": [{"key": "drafting", "label": "起草"},
               {"key": "signed", "label": "締結", "terminal": True}],
}


def _backoffice():
    data = {"name": "bo", "profession": "backoffice", "milestones": [],
            "target_classes": [CONTRACT]}
    te.create_target(data, CONTRACT, label="A社 NDA",
                     fields={"counterparty": "A社"})
    return data


def _dev():
    return {"name": "d", "profession": "dev",
            "milestones": [{"id": "ms-1", "title": "M", "status": "in_progress",
                            "entries": []}]}


# ---------------------------------------------------------------------------
# A descriptor-defined occupation becomes visible via the registry.
# ---------------------------------------------------------------------------

def test_project_targets_includes_descriptor_targets():
    rows = occ.project_targets(_backoffice())
    assert len(rows) == 1
    assert rows[0]["id"] == "ctr-1"
    assert rows[0]["kind"] == "contract"
    assert rows[0]["detail"]["phase"] == "drafting"


def test_project_targets_excludes_cancelled():
    data = _backoffice()
    data["contracts"][0]["status"] = "cancelled"
    assert occ.project_targets(data) == []


def test_iter_target_records_includes_descriptor_collection():
    ids = [r["id"] for r in occ.iter_target_records(_backoffice())]
    assert "ctr-1" in ids


def test_owned_target_classes_includes_descriptor_kind():
    assert occ.owned_target_classes(_backoffice(), "backoffice") == ("contract",)


def test_target_collections_includes_descriptor_collection():
    assert "contracts" in occ.target_collections(_backoffice())


def test_target_decomposition_includes_descriptor():
    dec = occ.target_decomposition(_backoffice())
    assert dec["contracts"] == {"id_field": "id", "arms": ("clauses",)}
    assert "clauses" in occ.target_child_tables(_backoffice())


def test_narrowing_prefix_and_ref_for_descriptor():
    data = _backoffice()
    assert occ.narrowing_id_prefixes(data)["contract"] == "ctr-"
    assert occ.narrowing_kind_for_ref("ctr-1", data) == "contract"


def test_assert_target_class_owned_allows_descriptor_kind():
    occ.assert_target_class_owned(_backoffice(), "contract")  # no raise


def test_assert_target_class_owned_blocks_wrong_class():
    try:
        occ.assert_target_class_owned(_backoffice(), "milestone")
        assert False, "expected TargetClassProfessionError"
    except occ.TargetClassProfessionError:
        pass


def test_target_class_owner_resolves_descriptor_with_data():
    assert occ.target_class_owner("contract", _backoffice()) == "backoffice"
    # Without data, only built-ins are known.
    assert occ.target_class_owner("contract") == ""
    assert occ.target_class_owner("milestone") == "dev"


# ---------------------------------------------------------------------------
# dev / sales unchanged when no descriptors present (behaviour preservation).
# ---------------------------------------------------------------------------

def test_dev_project_unchanged():
    data = _dev()
    # ms-142 e-5161 (T6): release is dev's L3 built-in-as-data Target-class,
    # injected by occupation.effective_descriptors — so a dev project WITH a release
    # record projects it (below) and owned/collections now include release. A dev
    # project with NO release record is otherwise unchanged (only ms-1 projects).
    rows = occ.project_targets(data)
    assert [r["id"] for r in rows] == ["ms-1"]
    assert occ.owned_target_classes(data, "dev") == (
        "milestone", "operation", "release")
    # ms-142 e-5156 (T1): operations joined the Target-collection seed. e-5161 (T6):
    # release_targets joins via the dev profession-default descriptor (present with or
    # without ``data`` — the coverage-matrix floor depends on it surfacing no-data).
    assert occ.target_collections(data) == (
        "milestones", "opportunities", "operations", "release_targets")
    assert occ.target_collections() == (
        "milestones", "opportunities", "operations", "release_targets")


# ---------------------------------------------------------------------------
# Release (dev's L3 built-in-as-data class) + the generic bundle capability
# (ms-142 e-5161 / T6).
# ---------------------------------------------------------------------------

def test_release_is_a_dev_default_descriptor_but_not_in_raw_list():
    import target_descriptor as td
    data = _dev()
    # effective (registry-facing) sees release; raw (authoring) does not.
    assert occ.effective_get_descriptor(data, "release")["collection"] == "release_targets"
    assert td.get_descriptor(data, "release") is None
    assert "release" not in [d.get("kind") for d in td.load_descriptors(data)]
    # A dev project with a release record projects it as a first-class Target.
    data["release_targets"] = [
        {"id": "rel-1", "label": "v1", "kind": "release", "status": "in_progress",
         "phase": "draft", "who_has_the_ball": "self",
         "phase_history": [], "work_items": [], "evidence": []}]
    ids = [r["id"] for r in occ.project_targets(data)]
    assert "rel-1" in ids and "ms-1" in ids


def test_bundled_targets_resolves_references_without_owning():
    # A release bundles milestones by reference: bundled_targets returns the
    # referenced milestone RECORDS (still owned by data['milestones']); a dangling
    # id is skipped, not raised. Generic — any Target with a ``bundles`` field.
    data = _dev()
    data["milestones"].append(
        {"id": "ms-2", "title": "M2", "status": "done", "entries": []})
    data["release_targets"] = [
        {"id": "rel-1", "label": "v1", "kind": "release", "status": "in_progress",
         "phase": "draft", "bundled_target_ids": ["ms-1", "ms-2", "ms-404"],
         "who_has_the_ball": "self", "phase_history": [],
         "work_items": [], "evidence": []}]
    bundled = occ.bundled_targets(data, "rel-1")
    assert [t["id"] for t in bundled] == ["ms-1", "ms-2"]   # ms-404 dropped
    # Resolution, not ownership: the bundled milestone is unchanged in its own
    # collection (same object identity).
    assert bundled[0] is data["milestones"][0]
    # Accepts a record directly, and {"id": ...} ref shape.
    rec = data["release_targets"][0]
    rec["bundled_target_ids"] = [{"id": "ms-2"}]
    assert [t["id"] for t in occ.bundled_targets(data, rec)] == ["ms-2"]
    # No bundles → empty.
    assert occ.bundled_targets(data, "ms-1") == []


def test_builtin_narrowing_no_data_includes_dev_profession_defaults():
    # trek.py calls these with no data at import time. The result is the built-in
    # seed PLUS the dev profession-default (release, ms-142 e-5161): the no-data set
    # is intentionally NOT the immutable seed — a profession default expands it, and
    # release is a first-class dev Target-class (§7 全種類を同格に), Trek-narrowable too.
    assert occ.all_narrowing_kinds() == ("milestone", "operation", "task",
                                         "opportunity", "account", "release")
    assert occ.narrowing_kind_for_ref("ms-1") == "milestone"
    assert occ.narrowing_kind_for_ref("rel-1") == "release"
    assert "contract" not in occ.narrowing_id_prefixes()
    assert occ.narrowing_id_prefixes().get("release") == "rel-"
