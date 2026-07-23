"""ms-119 AX follow-ups — judge/model independence check (e-3988) + external
reference slot (e-3997).

e-3988: the judge model must differ from the implementer model (a shared model =
a shared blind spot). The bundle now carries `implementer_model` and a callable
check compares it to the chosen judge model, so the guard is structural, not
散文-only.

e-3997: external facts (e.g. an application-map index) may be shown to the judge
ONLY through the bundle's `external_references` slot — never via ad-hoc prior
injection of implementer-session context. This pins the slot exists and defaults
empty (so nothing leaks unless explicitly, mechanically added).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import review_spine  # noqa: E402


# --- e-3988: judge_model_independence ---

def test_same_model_flags_not_ok():
    v = review_spine.judge_model_independence("opus", "opus")
    assert v["ok"] is False
    assert "implementer" in v["reason"]


def test_different_model_is_ok():
    assert review_spine.judge_model_independence("opus", "fable")["ok"] is True


def test_case_and_whitespace_insensitive():
    assert review_spine.judge_model_independence("  Opus ", "OPUS")["ok"] is False


def test_unknown_implementer_does_not_hard_block():
    v = review_spine.judge_model_independence("", "fable")
    assert v["ok"] is True
    assert "cannot compare" in v["reason"]


def test_bundle_carries_implementer_model():
    b = review_spine.assemble_review_context(
        review_spine.REVIEW_AX, origin_id="x", origin_content="y",
        diff_text="z", mode="diff", target_ref="PR #1",
        known_judge_types={"ax"}, implementer_model="opus",
    )
    assert b["implementer_model"] == "opus"
    # attainment bundle carries it too
    a = review_spine.assemble_attainment_context(
        target_id="ms-1", spec_origin_id="d", spec_content="s",
        criteria=[], target_ref="ms-1", implementer_model="opus",
    )
    assert a["implementer_model"] == "opus"


# --- e-3997: external_references slot ---

def test_external_references_default_empty():
    b = review_spine.assemble_review_context(
        review_spine.REVIEW_AX, origin_id="x", origin_content="y",
        diff_text="z", mode="diff", target_ref="PR #1",
        known_judge_types={"ax"},
    )
    assert b["external_references"] == []


def test_external_references_carried_when_supplied():
    refs = [{"id": "application-map", "kind": "doc", "content": "surface index ..."}]
    b = review_spine.assemble_review_context(
        review_spine.REVIEW_AX, origin_id="x", origin_content="y",
        diff_text="z", mode="diff", target_ref="PR #1",
        known_judge_types={"ax"}, external_references=refs,
    )
    assert b["external_references"] == refs
    # still no implementer-narrative keys leaked
    forbidden = {"session", "narrative", "conversation", "l2_memory"}
    assert forbidden.isdisjoint(b.keys())
