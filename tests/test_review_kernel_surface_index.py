"""Review kernel carries the surface index so the judge needs no repo reads
(ms-119 / e-4096).

A context-zero judge is stronger when it can see WHAT the product's surface is
(to spot a diff that duplicates an existing feature) without reaching into the
repo — reaching in would taint the instrument (AX 原典 §2). The application-map is
the sanctioned implementer-independent index; the kernel bundle now carries it as
an external reference, and records a gap when it is absent.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import review_spine  # noqa: E402


def test_build_surface_index_reference_shape():
    ref = review_spine.build_surface_index_reference(
        "# map\n- beacon milestone ...", updated_at="2026-07-21T00:00:00")
    assert ref["id"] == "application-map"
    assert ref["kind"] == "surface-index"
    assert "map" in ref["content"]
    assert ref["stale"] is False


def test_build_surface_index_reference_none_when_empty():
    assert review_spine.build_surface_index_reference("") is None
    assert review_spine.build_surface_index_reference("   ") is None


def test_stale_flag_carried():
    ref = review_spine.build_surface_index_reference("x", stale=True)
    assert ref["stale"] is True


def test_assemble_review_context_accepts_surface_reference():
    """The kernel bundle carries the surface index in external_references so it
    reaches the judge as sanctioned non-diff context."""
    ref = review_spine.build_surface_index_reference("# surface map")
    bundle = review_spine.assemble_review_context(
        review_spine.REVIEW_AX,
        origin_id="skills/ax-review/principles.md",
        origin_content="principles...",
        diff_text="@@ -1 +1 @@",
        mode="diff",
        target_ref="PR #1",
        known_judge_types={review_spine.REVIEW_AX},
        external_references=[ref],
    )
    ids = [r["id"] for r in bundle["external_references"]]
    assert "application-map" in ids
    # independence contract still present — the surface index does not replace it.
    assert bundle["independence_contract"]
