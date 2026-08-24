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
    assert "'id_prefix'" in joined
    assert "'collection'" in joined


def test_profession_is_optional_provenance_not_required():
    # e-5375: ``profession`` is a PROVENANCE tag, not a required part of a
    # well-formed descriptor. A class with everything BUT profession is valid.
    problems = td.validate_descriptor({
        "kind": "x", "label": "X", "id_prefix": "x-", "collection": "xs",
        "type": "single-shot", "phases": [{"key": "a"}]})
    joined = " ".join(problems)
    assert "'profession'" not in joined, "profession must not be required"
    assert problems == [], problems


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


def test_phase_field_required_is_now_accepted():
    # ms-124 e-4090: a required PHASE field now has an enforcement path
    # (advance_target enforces it when entering the phase), so validation no
    # longer rejects it — the ms-122 fence is closed.
    desc = dict(CONTRACT, phases=[
        {"key": "p1", "label": "P1",
         "fields": [{"key": "x", "label": "X", "required": True}]},
    ])
    problems = td.validate_descriptor(desc)
    assert problems == []


def test_base_field_required_still_allowed():
    # base field required is fine (enforced at create_target).
    assert td.validate_descriptor(CONTRACT) == []  # counterparty is required base


def test_validation_never_raises_on_garbage():
    # Loaders and validators must survive arbitrary shapes.
    assert td.validate_descriptor("not a dict")
    assert td.validate_descriptor(123)
    td.validate_target_classes({"target_classes": ["x", 1, None, {}]})


# ---------------------------------------------------------------------------
# Authoring — build + append a descriptor (ms-124 e-4091 no-code onboarding).
# ---------------------------------------------------------------------------

def test_build_descriptor_shape_and_default_arms():
    desc = td.build_descriptor(
        kind="ringi", label="稟議", profession="legal", dtype="single-shot",
        id_prefix="rg-", collection="ringis",
        fields=[{"key": "amount", "label": "金額", "type": "money"}],
        phases=[{"key": "draft", "label": "起案"},
                {"key": "approved", "label": "決裁", "terminal": True}])
    assert desc["kind"] == "ringi"
    assert desc["type"] == "single-shot"
    # authored classes inherit the thick-frame arms so their targets get
    # WorkItems / Evidence like the built-in seed
    assert desc["decomposition"]["arms"] == ["work_items", "evidence"]
    assert td.validate_descriptor(desc) == []


def test_append_descriptor_writes_and_is_readable():
    data = {"name": "t", "profession": "legal"}
    desc = td.build_descriptor(
        kind="ringi", label="稟議", profession="legal", dtype="single-shot",
        id_prefix="rg-", collection="ringis")
    problems = td.append_descriptor(data, desc)
    assert problems == []
    assert td.descriptor_kinds(data) == ["ringi"]
    assert td.get_descriptor(data, "ringi")["label"] == "稟議"


def test_append_descriptor_rejects_invalid_without_writing():
    data = {"name": "t"}
    bad = td.build_descriptor(kind="", label="x", profession="legal",
                              dtype="bogus", id_prefix="rg", collection="")
    problems = td.append_descriptor(data, bad)
    assert problems  # kind/type/id_prefix/collection all flagged
    assert td.load_descriptors(data) == []   # nothing written


def test_append_descriptor_rejects_duplicate_kind_and_prefix():
    data = {"name": "t"}
    first = td.build_descriptor(kind="ringi", label="稟議", profession="legal",
                                dtype="single-shot", id_prefix="rg-",
                                collection="ringis")
    assert td.append_descriptor(data, first) == []
    dup_kind = td.build_descriptor(kind="ringi", label="別", profession="legal",
                                   dtype="single-shot", id_prefix="rg2-",
                                   collection="ringis2")
    assert any("kind" in p for p in td.append_descriptor(data, dup_kind))
    dup_prefix = td.build_descriptor(kind="other", label="別", profession="legal",
                                     dtype="single-shot", id_prefix="rg-",
                                     collection="others")
    assert any("id_prefix" in p for p in td.append_descriptor(data, dup_prefix))
    # only the first descriptor was actually written
    assert td.descriptor_kinds(data) == ["ringi"]


# ---------------------------------------------------------------------------
# Phase adjacency graph (ms-152 e-5480) — cycle-permitted descriptor-side
# declaration of "which phase can follow which". Implicit linear order is the
# backward-compatible default; an explicit ``next`` graph may cycle for a
# persistent class but must stay acyclic for a finite one.
# ---------------------------------------------------------------------------

# A persistent monitoring Operation modelled as a descriptor: its execution loop
# cycles idle → due → running → idle forever, so it declares an explicit graph
# with a back edge and carries NO terminal phase.
MONITOR = {
    "kind": "monitor",
    "label": "監視",
    "type": "persistent",
    "id_prefix": "mon-",
    "collection": "monitors",
    "phases": [
        {"key": "idle", "next": ["due"]},
        {"key": "due", "next": ["running"]},
        {"key": "running", "next": ["idle"]},
    ],
}


def test_implicit_linear_adjacency_is_the_default():
    # A descriptor with no `next` reads as the historical linear order.
    assert not td.has_explicit_adjacency(CONTRACT)
    assert td.phase_successors(CONTRACT, "drafting") == ["legal_review"]
    assert td.phase_successors(CONTRACT, "legal_review") == ["signed"]
    assert td.phase_successors(CONTRACT, "signed") == []      # last / terminal
    assert td.phase_adjacency(CONTRACT) == {
        "drafting": ["legal_review"],
        "legal_review": ["signed"],
        "signed": [],
    }


def test_implicit_graph_is_acyclic():
    assert td.phase_graph_has_cycle(CONTRACT) is False


def test_unknown_phase_has_no_successors():
    assert td.phase_successors(CONTRACT, "ghost") == []
    assert td.phase_successors({"kind": "x"}, "anything") == []


def test_explicit_cyclic_adjacency_declared_and_read():
    assert td.has_explicit_adjacency(MONITOR)
    assert td.phase_successors(MONITOR, "running") == ["idle"]   # back edge
    assert td.phase_adjacency(MONITOR) == {
        "idle": ["due"], "due": ["running"], "running": ["idle"]}
    assert td.phase_graph_has_cycle(MONITOR) is True


def test_explicit_dead_end_phase_has_no_successors():
    # In explicit mode a phase with no `next` is a dead end (not the linear next).
    desc = dict(MONITOR, phases=[
        {"key": "idle", "next": ["running"]},
        {"key": "running"},           # no `next` → dead end, not → idle
    ])
    assert td.phase_successors(desc, "running") == []


def test_persistent_cycle_is_valid():
    assert td.validate_descriptor(MONITOR) == []
    assert td.validate_target_classes(_project([MONITOR])) == {}


def test_finite_cycle_is_rejected():
    # Same cyclic graph but declared finite (single-shot) → the acyclic invariant
    # fires: a finite target must march toward a terminal, never loop.
    desc = dict(MONITOR, type="single-shot")
    problems = td.validate_descriptor(desc)
    assert any("循環" in p for p in problems)


def test_dangling_next_edge_flagged():
    desc = dict(MONITOR, phases=[
        {"key": "idle", "next": ["ghost"]},
        {"key": "due", "next": ["idle"]},
    ])
    problems = td.validate_descriptor(desc)
    assert any("ghost" in p and "宣言されていない" in p for p in problems)


def test_duplicate_next_edge_flagged():
    desc = dict(MONITOR, phases=[
        {"key": "idle", "next": ["due", "due"]},
        {"key": "due", "next": ["idle"]},
    ])
    problems = td.validate_descriptor(desc)
    assert any("重複" in p for p in problems)


def test_malformed_next_not_a_list_flagged():
    desc = dict(MONITOR, phases=[
        {"key": "idle", "next": "due"},       # string, not a list
        {"key": "due", "next": ["idle"]},
    ])
    problems = td.validate_descriptor(desc)
    assert any("リスト" in p for p in problems)
    # tolerant read: the malformed edge reads as no successors, never raises.
    assert td.phase_successors(desc, "idle") == []


def test_terminal_phase_cannot_declare_successors():
    desc = dict(CONTRACT, phases=[
        {"key": "drafting", "next": ["signed"]},
        {"key": "signed", "next": ["drafting"], "terminal": True},
    ])
    problems = td.validate_descriptor(desc)
    assert any("終端" in p for p in problems)


def test_release_default_descriptor_unaffected():
    # The built-in release descriptor (finite, implicit linear) still validates
    # and reads acyclic — no regression from the adjacency feature.
    assert td.validate_descriptor(td.RELEASE_DESCRIPTOR) == []
    assert td.phase_graph_has_cycle(td.RELEASE_DESCRIPTOR) is False
    assert td.phase_successors(td.RELEASE_DESCRIPTOR, "draft") == ["published"]


def test_is_legal_phase_transition_reads_the_graph():
    # ms-152 e-5481: pure legality predicate over the adjacency graph.
    assert td.is_legal_phase_transition(MONITOR, "idle", "due") is True
    assert td.is_legal_phase_transition(MONITOR, "running", "idle") is True   # cycle
    assert td.is_legal_phase_transition(MONITOR, "idle", "running") is False  # skip
    assert td.is_legal_phase_transition(MONITOR, "idle", "idle") is True      # no-op
    # implicit-linear CONTRACT: forward is a declared (implicit) edge, backward is not.
    assert td.is_legal_phase_transition(CONTRACT, "drafting", "legal_review") is True
    assert td.is_legal_phase_transition(CONTRACT, "signed", "drafting") is False
