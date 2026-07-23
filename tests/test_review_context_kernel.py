"""Review-kernel bundle assembly — reviewer independence is structural (ms-119 e-3947).

The value of a review is its independence (AX 原典 §2, 計器の必然): the author's
context is maximal, so they cannot measure their own AX / philosophy drift. The
kernel enforces this structurally by handing the judge ONLY the 原典 (origin) and
a mechanically-collected diff (artifact) — never the implementer's session
narrative. These tests pin that shape so a future refactor cannot silently
smuggle implementer context back into the judge's input.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import review_spine  # noqa: E402


def test_bundle_carries_only_origin_and_diff():
    b = review_spine.assemble_review_context(
        review_spine.REVIEW_AX,
        origin_id="skills/ax-review/principles.md",
        origin_content="原則1 命名の一貫性 ...",
        diff_text="diff --git a/x b/x\n+foo",
        mode="diff",
        target_ref="PR #99",
    )
    # The judge's input is exactly origin + artifact + the independence contract.
    assert b["origin"]["content"] == "原則1 命名の一貫性 ..."
    assert b["artifact"]["content"] == "diff --git a/x b/x\n+foo"
    assert b["artifact"]["kind"] == "diff"
    assert b["review_type"] == "ax"
    assert b["mode"] == "diff"
    assert b["target_ref"] == "PR #99"
    # No key carries implementer narrative / intent / session context.
    forbidden = {"session", "intent", "narrative", "conversation", "author_notes"}
    assert forbidden.isdisjoint(b.keys())
    # The independence contract is present and names the taint it forbids.
    assert "計器" in b["independence_contract"]
    assert "文脈" in b["independence_contract"]


def test_philosophy_type_is_supported():
    b = review_spine.assemble_review_context(
        review_spine.REVIEW_PHILOSOPHY,
        origin_id="specDocId",
        origin_content="## 設計方針 ...",
        diff_text="+bar",
        mode="full-surface",
        target_ref="ms-119",
    )
    assert b["review_type"] == "philosophy"
    assert b["mode"] == "full-surface"


def test_attainment_is_rejected_not_a_judge_run():
    # 目的達成 is human-gated (beacon target approve), never a subagent judge.
    with pytest.raises(ValueError):
        review_spine.assemble_review_context(
            review_spine.REVIEW_ATTAINMENT,
            origin_id="x", origin_content="y", diff_text="z",
            mode="diff", target_ref="ms-1",
        )


def test_unknown_review_type_is_rejected():
    with pytest.raises(ValueError):
        review_spine.assemble_review_context(
            "bugs", origin_id="x", origin_content="y", diff_text="z",
            mode="diff", target_ref="ms-1",
        )


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        review_spine.assemble_review_context(
            review_spine.REVIEW_AX, origin_id="x", origin_content="y",
            diff_text="z", mode="bogus", target_ref="ms-1",
        )


def test_gaps_are_carried_for_missing_origin():
    # A SPEC-less philosophy review surfaces the missing 原典 as a gap, not a
    # crash (SPEC § 方針5: the missing origin is itself a gentle forcing function).
    b = review_spine.assemble_review_context(
        review_spine.REVIEW_PHILOSOPHY,
        origin_id="specDocId", origin_content="",
        diff_text="+bar", mode="diff", target_ref="ms-9",
        gaps=["原典が空です"],
    )
    assert b["gaps"] == ["原典が空です"]
