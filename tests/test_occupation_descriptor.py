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
    rows = occ.project_targets(data)
    assert [r["id"] for r in rows] == ["ms-1"]
    assert occ.owned_target_classes(data, "dev") == ("milestone", "operation")
    # ms-142 e-5156 (T1): operations joined the Target-collection seed so a dev
    # Operation is enumerated as a first-class Target. project_targets / owned
    # classes are unchanged (this test's "unchanged" subject); the seed grows.
    assert occ.target_collections(data) == ("milestones", "opportunities", "operations")
    assert occ.target_collections() == ("milestones", "opportunities", "operations")


def test_builtin_narrowing_unchanged_without_data():
    # trek.py calls these with no data at import time — must be the seed only.
    assert occ.all_narrowing_kinds() == ("milestone", "operation", "task",
                                         "opportunity", "account")
    assert occ.narrowing_kind_for_ref("ms-1") == "milestone"
    assert "contract" not in occ.narrowing_id_prefixes()
