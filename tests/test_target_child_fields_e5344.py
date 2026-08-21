"""ms-146 e-5344: a data-defined target-class can declare the fields its CHILD
arms carry — WorkItems (業務) and Evidence (証跡) — and the engine enforces that
declaration on write.

WHY this exists: ``occupation.add_work_item`` (the entry point a dev task / sales
activity rides) accepts ``**extra``, so a code-defined profession can put a
priority / deadline on its work items. The descriptor path could not: it took a
description and nothing else. That asymmetry made "業務ごとの時間予算" and
"証跡ごとの 効いたか" impossible to express as data — precisely the kind of thing
descriptors exist to express. These tests pin the new symmetry AND pin that a
class declaring no child fields behaves exactly as before (the compat contract).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import target_descriptor as td  # noqa: E402
import target_engine as te  # noqa: E402


# A class whose child arms carry declarations — the executive "やること" shape
# (ms-146): a work item holds its time budget, an evidence holds whether the work
# actually moved the objective.
UNDERTAKING = {
    "kind": "undertaking",
    "label": "やること",
    "profession": "dev",
    "type": "single-shot",
    "id_prefix": "ut-",
    "collection": "undertakings",
    "fields": [{"key": "purpose", "label": "上位目的", "type": "string",
                "required": True}],
    "phases": [
        {"key": "not_started", "label": "やってない"},
        {"key": "started", "label": "着手"},
        {"key": "enough", "label": "十分やった", "terminal": True},
    ],
    "work_item_fields": [
        {"key": "budget_h", "label": "時間予算(時間)", "type": "number",
         "required": True},
        {"key": "note", "label": "備考", "type": "text"},
    ],
    "evidence_fields": [
        {"key": "moved", "label": "上位目的に効いたか", "type": "string",
         "required": True},
        {"key": "spent_h", "label": "消費時間", "type": "number"},
    ],
}

# A class with NO child declarations — the pre-e-5344 shape every existing
# descriptor (backoffice 契約 / 評価) has. Its behaviour must be untouched.
CONTRACT = {
    "kind": "contract",
    "label": "契約",
    "profession": "backoffice",
    "type": "single-shot",
    "id_prefix": "ctr-",
    "collection": "contracts",
    "fields": [{"key": "counterparty", "label": "相手方", "type": "string"}],
    "phases": [{"key": "drafting", "label": "起草"},
               {"key": "signed", "label": "締結", "terminal": True}],
}


def _with_target(desc, **fields):
    data = {"name": "t"}
    rec = te.create_target(data, desc, label="セミナー準備", fields=fields)
    return data, rec["id"]


# ---------------------------------------------------------------------------
# Descriptor accessors + validation.
# ---------------------------------------------------------------------------

def test_accessors_read_declared_child_fields():
    assert [f["key"] for f in td.work_item_fields(UNDERTAKING)] \
        == ["budget_h", "note"]
    assert [f["key"] for f in td.evidence_fields(UNDERTAKING)] \
        == ["moved", "spent_h"]


def test_accessors_are_tolerant_on_a_class_that_declares_none():
    """A descriptor written before e-5344 reads as "no child fields" — the
    additive-only / tolerant-read compat contract, so nothing needs migrating."""
    assert td.work_item_fields(CONTRACT) == []
    assert td.evidence_fields(CONTRACT) == []
    assert td.work_item_fields({}) == []
    assert td.evidence_fields({"work_item_fields": "not-a-list"}) == []


def test_validate_flags_a_malformed_child_field():
    bad = dict(UNDERTAKING, work_item_fields=[{"label": "key がない"}])
    problems = td.validate_descriptor(bad)
    assert problems, "a child field with no key must be reported"
    assert any("work_item" in p for p in problems), problems


def test_validate_flags_duplicate_child_field_keys():
    bad = dict(UNDERTAKING, evidence_fields=[
        {"key": "moved", "label": "a"}, {"key": "moved", "label": "b"}])
    problems = td.validate_descriptor(bad)
    assert any("evidence" in p for p in problems), problems


def test_a_well_formed_child_declaration_validates_clean():
    assert td.validate_descriptor(UNDERTAKING) == []


def test_build_descriptor_omits_the_keys_when_nothing_is_declared():
    """Byte-identical to the pre-e-5344 output, so every project.json that never
    declares child fields is unchanged."""
    desc = td.build_descriptor(kind="k", label="l", profession="dev",
                               dtype="single-shot", id_prefix="k-",
                               collection="ks")
    assert td.WORK_ITEM_FIELDS_KEY not in desc
    assert td.EVIDENCE_FIELDS_KEY not in desc


def test_build_descriptor_emits_declared_child_fields():
    desc = td.build_descriptor(
        kind="k", label="l", profession="dev", dtype="single-shot",
        id_prefix="k-", collection="ks",
        work_item_fields=[{"key": "budget_h", "label": "予算"}],
        evidence_fields=[{"key": "moved", "label": "効いたか"}])
    assert desc[td.WORK_ITEM_FIELDS_KEY] == [{"key": "budget_h",
                                             "label": "予算"}]
    assert desc[td.EVIDENCE_FIELDS_KEY] == [{"key": "moved",
                                            "label": "効いたか"}]


# ---------------------------------------------------------------------------
# WorkItem — the write path.
# ---------------------------------------------------------------------------

def test_work_item_stores_declared_field_values():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    item = te.add_work_item(data, UNDERTAKING, tid, "原稿を考える",
                            fields={"budget_h": "2", "note": "事例3本"})
    assert item["budget_h"] == "2"
    assert item["note"] == "事例3本"
    # and it is really on the record, not just the returned dict
    stored = te.list_work_items(te.find_target(data, UNDERTAKING, tid))
    assert stored[0]["budget_h"] == "2"


def test_work_item_rejects_an_undeclared_field():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    with pytest.raises(te.TargetEngineError) as e:
        te.add_work_item(data, UNDERTAKING, tid, "原稿", fields={"nope": "x"})
    assert "nope" in str(e.value)


def test_work_item_requires_a_declared_required_field():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    with pytest.raises(te.TargetEngineError) as e:
        te.add_work_item(data, UNDERTAKING, tid, "原稿", fields={"note": "x"})
    assert "budget_h" in str(e.value)


def test_a_rejected_work_item_leaves_no_partial_record():
    """Validate-before-mutate: the target must not gain a half-written child."""
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    with pytest.raises(te.TargetEngineError):
        te.add_work_item(data, UNDERTAKING, tid, "原稿", fields={"note": "x"})
    assert te.list_work_items(te.find_target(data, UNDERTAKING, tid)) == []


def test_zero_satisfies_a_required_numeric_field():
    """A 0-hour budget is a real answer, not a missing one."""
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    item = te.add_work_item(data, UNDERTAKING, tid, "原稿",
                            fields={"budget_h": 0})
    assert item["budget_h"] == 0


# ---------------------------------------------------------------------------
# Evidence — the write path.
# ---------------------------------------------------------------------------

def test_evidence_stores_declared_field_values():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    ev = te.add_evidence(data, UNDERTAKING, tid, summary="通し1本",
                         fields={"moved": "効いてない", "spent_h": "2"})
    assert ev["moved"] == "効いてない"
    assert ev["spent_h"] == "2"
    assert te.list_evidence(te.find_target(data, UNDERTAKING, tid))[0]["moved"] \
        == "効いてない"


def test_evidence_requires_its_declared_required_field():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    with pytest.raises(te.TargetEngineError) as e:
        te.add_evidence(data, UNDERTAKING, tid, summary="通し1本")
    assert "moved" in str(e.value)


def test_a_rejected_evidence_leaves_no_partial_record():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    with pytest.raises(te.TargetEngineError):
        te.add_evidence(data, UNDERTAKING, tid, summary="通し1本")
    assert te.list_evidence(te.find_target(data, UNDERTAKING, tid)) == []


def test_evidence_still_links_to_a_work_item_with_fields():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    item = te.add_work_item(data, UNDERTAKING, tid, "原稿",
                            fields={"budget_h": "2"})
    ev = te.add_evidence(data, UNDERTAKING, tid, summary="done",
                         linked_id=item["id"], fields={"moved": "効いた"})
    assert ev["linked_id"] == item["id"]
    assert ev["moved"] == "効いた"


# ---------------------------------------------------------------------------
# Back-compat — a class that declares no child fields is untouched.
# ---------------------------------------------------------------------------

def test_class_without_child_declarations_still_adds_work_items():
    data, tid = _with_target(CONTRACT, counterparty="A社")
    item = te.add_work_item(data, CONTRACT, tid, "条項を確認する")
    assert item["description"] == "条項を確認する"
    assert te.list_work_items(te.find_target(data, CONTRACT, tid)) == [item]


def test_class_without_child_declarations_still_adds_evidence():
    data, tid = _with_target(CONTRACT, counterparty="A社")
    ev = te.add_evidence(data, CONTRACT, tid, summary="法務OK")
    assert ev["summary"] == "法務OK"


def test_passing_a_field_to_an_undeclaring_class_says_why():
    """Not a silent drop: the caller is told the arm carries no declaration."""
    data, tid = _with_target(CONTRACT, counterparty="A社")
    with pytest.raises(te.TargetEngineError) as e:
        te.add_work_item(data, CONTRACT, tid, "確認", fields={"budget_h": "2"})
    assert "宣言していません" in str(e.value)


# ---------------------------------------------------------------------------
# Non-bypass pin — there must be exactly ONE write path for a descriptor class's
# work items, so the declaration this MS adds cannot be routed around.
# ---------------------------------------------------------------------------

def test_the_generic_work_item_path_cannot_reach_a_descriptor_class():
    """``occupation.add_work_item`` is the OTHER work-item writer (a dev task /
    sales activity rides it) and it does NOT enforce ``work_item_fields`` — it
    takes ``**extra`` verbatim. That is safe only while it cannot resolve a
    descriptor class at all: its kind resolver maps built-in id prefixes, not
    author-declared ones like ``ut-``.

    This test pins that boundary. If someone generalises the resolver so custom
    prefixes resolve, this test fails — which is the point: the required-field
    gate would then be bypassable through the generic path, and the validation
    has to move down to the shared primitive before that lands."""
    import occupation  # local import: only this pin needs it

    data = {"name": "t", "milestones": [], "target_classes": [UNDERTAKING],
            "undertakings": [{"id": "ut-1", "label": "x",
                              "kind": "undertaking", "phase": "started",
                              "status": "todo", "work_items": [],
                              "evidence": [], "phase_history": []}]}
    with pytest.raises(ValueError):
        occupation.add_work_item(data, "ut-1", description="bypass?")


# ---------------------------------------------------------------------------
# ms-146 e-5348 — cancelling a work item. A class whose point is deciding what
# NOT to do must be able to drop an item; ``done`` as the only exit contradicts
# the class.
# ---------------------------------------------------------------------------

def test_cancel_marks_the_item_cancelled_without_removing_it():
    """Soft cancel per data-immutability-principle: the record stays, its status
    changes, and who / why is stamped."""
    import work_model as wm

    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    item = te.add_work_item(data, UNDERTAKING, tid, "3本目の事例を足す",
                            fields={"budget_h": "1"})
    te.cancel_work_item(data, UNDERTAKING, tid, item["id"],
                        actor="me", reason="成約への寄与が薄いのでやらない")
    stored = te.list_work_items(te.find_target(data, UNDERTAKING, tid))
    assert len(stored) == 1, "cancel must not physically remove the record"
    assert wm.is_cancelled(stored[0])
    assert stored[0]["meta"]["cancel_reason"] == "成約への寄与が薄いのでやらない"
    assert stored[0]["meta"]["cancelled_by"] == "me"


def test_a_cancelled_item_is_no_longer_the_next_move():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    item = te.add_work_item(data, UNDERTAKING, tid, "やらないやつ",
                            fields={"budget_h": "1"})
    rec = te.find_target(data, UNDERTAKING, tid)
    assert "やらないやつ" in te.infer_next_move(UNDERTAKING, rec)
    te.cancel_work_item(data, UNDERTAKING, tid, item["id"], reason="やめる")
    assert "やらないやつ" not in te.infer_next_move(UNDERTAKING, rec)


def test_cancelling_twice_is_refused():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    item = te.add_work_item(data, UNDERTAKING, tid, "x",
                            fields={"budget_h": "1"})
    te.cancel_work_item(data, UNDERTAKING, tid, item["id"], reason="やめる")
    with pytest.raises(te.TargetEngineError) as e:
        te.cancel_work_item(data, UNDERTAKING, tid, item["id"], reason="again")
    assert "既に取り消し済み" in str(e.value)


def test_cancelling_an_unknown_item_raises():
    data, tid = _with_target(UNDERTAKING, purpose="成約3件")
    with pytest.raises(te.TargetEngineError):
        te.cancel_work_item(data, UNDERTAKING, tid, "ut-1-w9", reason="x")
