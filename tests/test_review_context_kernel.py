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


# --- CLI guard tests (ms-119 e-3947 dogfood): the review capability's own CLI
# must not ship the silent-no-op defects it exists to catch. Each guard fires
# BEFORE any git/store access, so these run without a project or a real diff. ---
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMANDS = os.path.join(REPO, "lib", "commands.py")


def _run_review_context(env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, COMMANDS, "review_context"],
        capture_output=True, text=True, env=env, cwd=REPO,
    )


def test_cli_rejects_pr_and_diff_ref_together():
    p = _run_review_context({"BEACON_REVIEW_TYPE": "ax", "BEACON_PR": "1",
                             "BEACON_DIFF_REF": "a...b"})
    assert p.returncode == 1
    assert "mutually exclusive" in p.stderr
    assert p.stdout.strip() == ""  # no bundle emitted — nothing to misparse as JSON


def test_cli_rejects_origin_doc_with_ax():
    # ms-119 / e-4009: ax is a repo-file-origin type (原典 fixed), so --origin-doc
    # is rejected. The message now names the fixed 原典 ref (the whitelist that
    # used to hardcode "philosophy" is gone — types are data-driven).
    p = _run_review_context({"BEACON_REVIEW_TYPE": "ax", "BEACON_PR": "1",
                             "BEACON_ORIGIN_DOC": "someDoc"})
    assert p.returncode == 1
    assert "--origin-doc" in p.stderr
    assert "skills/ax-review/principles.md" in p.stderr


def test_cli_full_surface_mode_now_supported():
    # ms-119 / e-3987: full-surface was a rejected follow-up; it now collects a
    # surface snapshot. --pr is ignored in this mode (surface, not change-scoped).
    p = _run_review_context({"BEACON_REVIEW_TYPE": "ax", "BEACON_PR": "1",
                             "BEACON_MODE": "full-surface"})
    assert p.returncode == 0, p.stderr
    import json as _json
    bundle = _json.loads(p.stdout)
    assert bundle["mode"] == "full-surface"
    assert bundle["artifact"]["kind"] == "surface-snapshot"


def test_cli_rejects_unknown_mode_cleanly_not_traceback():
    p = _run_review_context({"BEACON_REVIEW_TYPE": "ax", "BEACON_PR": "1",
                             "BEACON_MODE": "audit"})
    assert p.returncode == 1
    assert "Error:" in p.stderr
    assert "Traceback" not in p.stderr  # a clean CLI error, not a raw stack trace
