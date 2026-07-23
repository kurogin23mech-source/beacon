"""Tests for lib/target_descriptor.py — data-defined target-class descriptors
(ms-122 e-3954). Covers tolerant loading, phase/field accessors (incl. the
per-phase field extension of SPEC §4), and validation."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import target_descriptor as td  # noqa: E402


# A well-formed back-office contract descriptor, used across tests. A contract
# is finite (single-shot); its 弁護士レビュー phase adds fields that appear only
# once that phase is reached (SPEC §4).
CONTRACT = {
    "kind": "contract",
    "label": "契約",
    "profession": "backoffice",
    "type": "single-shot",
    "id_prefix": "ctr-",
    "collection": "contracts",
    "decomposition": {"id_field": "id", "arms": []},
    "fields": [
        {"key": "counterparty", "label": "相手方", "type": "string",
         "required": True},
    ],
    "phases": [
        {"key": "drafting", "label": "起草"},
        {"key": "legal_review", "label": "弁護士レビュー",
         "fields": [
             {"key": "reviewer", "label": "レビュー依頼先", "type": "string"},
             {"key": "risk", "label": "想定リスク", "type": "text"},
         ]},
        {"key": "signed", "label": "締結", "terminal": True},
    ],
}


def _project(descriptors):
    return {"name": "T", "target_classes": descriptors}


# ---------------------------------------------------------------------------
# Tolerant loading — missing / malformed keys read as empty, never raise.
# ---------------------------------------------------------------------------

def test_load_absent_key_reads_empty():
    # A project.json written before this feature has no target_classes key.
    assert td.load_descriptors({"name": "legacy"}) == []
    assert td.descriptor_kinds({"name": "legacy"}) == []


def test_load_non_list_reads_empty():
    assert td.load_descriptors({"target_classes": "oops"}) == []
    assert td.load_descriptors({"target_classes": None}) == []


def test_load_and_get_descriptor():
    data = _project([CONTRACT])
    assert td.descriptor_kinds(data) == ["contract"]
    assert td.get_descriptor(data, "contract")["label"] == "契約"
    assert td.get_descriptor(data, "nonexistent") is None
    assert td.get_descriptor(data, "") is None


def test_descriptor_kinds_skips_malformed():
    data = _project([CONTRACT, {"label": "no kind"}, "not a dict", {"kind": ""}])
    assert td.descriptor_kinds(data) == ["contract"]


# ---------------------------------------------------------------------------
# Phase + field accessors.
# ---------------------------------------------------------------------------

def test_phase_keys_in_order():
    assert td.phase_keys(CONTRACT) == ["drafting", "legal_review", "signed"]


def test_phase_keys_empty_when_no_phases():
    assert td.phase_keys({"kind": "x"}) == []


def test_terminal_phase_keys():
    assert td.terminal_phase_keys(CONTRACT) == ["signed"]


def test_base_fields():
    keys = [f["key"] for f in td.base_fields(CONTRACT)]
    assert keys == ["counterparty"]


def test_fields_at_phase_adds_per_phase_extension():
    # SPEC §4: at the legal_review phase, the reviewer/risk fields appear on top
    # of the base counterparty field, base first.
    keys = [f["key"] for f in td.fields_at_phase(CONTRACT, "legal_review")]
    assert keys == ["counterparty", "reviewer", "risk"]


def test_fields_at_phase_without_extension_is_base_only():
    keys = [f["key"] for f in td.fields_at_phase(CONTRACT, "drafting")]
    assert keys == ["counterparty"]


def test_fields_at_unknown_phase_is_base_only():
    keys = [f["key"] for f in td.fields_at_phase(CONTRACT, "ghost")]
    assert keys == ["counterparty"]


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------

def test_valid_descriptor_has_no_problems():
    assert td.validate_descriptor(CONTRACT) == []
    assert td.validate_target_classes(_project([CONTRACT])) == {}


def test_missing_required_fields_flagged():
    problems = td.validate_descriptor({"kind": "x"})
    joined = " ".join(problems)
    assert "'label'" in joined
    assert "'profession'" in joined
    assert "'id_prefix'" in joined
    assert "'collection'" in joined


def test_invalid_type_flagged():
    desc = dict(CONTRACT, type="recurring")
    problems = td.validate_descriptor(desc)
    assert any("type" in p for p in problems)


def test_id_prefix_must_end_with_dash():
    desc = dict(CONTRACT, id_prefix="ctr")
    problems = td.validate_descriptor(desc)
    assert any("id_prefix" in p for p in problems)


def test_duplicate_phase_key_flagged():
    desc = dict(CONTRACT, phases=[
        {"key": "a", "label": "A"},
        {"key": "a", "label": "A dup"},
    ])
    problems = td.validate_descriptor(desc)
    assert any("重複" in p for p in problems)


def test_duplicate_field_key_flagged():
    desc = dict(CONTRACT, fields=[
        {"key": "dup", "label": "1"},
        {"key": "dup", "label": "2"},
    ])
    problems = td.validate_descriptor(desc)
    assert any("dup" in p and "重複" in p for p in problems)


def test_unknown_field_type_flagged():
    desc = dict(CONTRACT, fields=[{"key": "k", "type": "blob"}])
    problems = td.validate_descriptor(desc)
    assert any("blob" in p for p in problems)


def test_cross_descriptor_duplicate_kind_flagged():
    result = td.validate_target_classes(_project([CONTRACT, dict(CONTRACT)]))
    assert "(重複)" in result
    assert any("contract" in p for p in result["(重複)"])


def test_cross_descriptor_duplicate_prefix_flagged():
    other = dict(CONTRACT, kind="eval", collection="evals")  # same id_prefix
    result = td.validate_target_classes(_project([CONTRACT, other]))
    assert any("id_prefix" in p for p in result.get("(重複)", []))


def test_validation_never_raises_on_garbage():
    # Loaders and validators must survive arbitrary shapes.
    assert td.validate_descriptor("not a dict")
    assert td.validate_descriptor(123)
    td.validate_target_classes({"target_classes": ["x", 1, None, {}]})
