"""Drift guard: the review 節目 command set has ONE source of truth (ms-119 /
e-4080).

Before this, the wake-hook grep patterns and the manifest of "which commands are
review 節目" lived in two files that were hand-synced. Renaming a command in one
place and not the other made the wake go silent — the exact drift the review
machinery exists to prevent, reappearing in the machinery. lib/review_nodes.py is
now authoritative; these tests fail the build if bin/beacon-post-commit-hook.sh
diverges from it, and if the debug label stops distinguishing the two review
節目 that share /beacon-review-run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "beacon-post-commit-hook.sh"
LIB = ROOT / "lib"

sys.path.insert(0, str(LIB))
import review_nodes  # noqa: E402
import review_spine  # noqa: E402


def _hook_text() -> str:
    return HOOK.read_text(encoding="utf-8")


def test_manifest_is_nonempty_and_wellformed():
    assert review_nodes.REVIEW_NODES, "review node manifest is empty"
    for n in review_nodes.REVIEW_NODES:
        assert n["id"] and n["grep_pattern"] and n["skill"]
        assert isinstance(n["fires"], list) and n["fires"]


def test_hook_greps_each_manifest_pattern_verbatim():
    """Every manifest grep_pattern must appear byte-identical in the hook, inside
    a `grep -qE '<pattern>'` invocation. This is the single-source drift guard:
    editing the hook pattern without updating review_nodes.py (or vice versa)
    breaks this test."""
    text = _hook_text()
    for node_id, pattern in review_nodes.grep_patterns():
        needle = f"grep -qE '{pattern}'"
        assert needle in text, (
            f"review node {node_id!r}: hook is missing the manifest grep pattern.\n"
            f"  expected substring: {needle!r}\n"
            f"  → keep bin/beacon-post-commit-hook.sh and lib/review_nodes.py in sync."
        )


def test_manifest_fires_matches_the_firing_authority():
    """`fires` is a declared expectation, not the firing authority — this ties it
    to the real authorities so it cannot silently go stale (e-4124 maint review):
      * pr-open   → the data-driven registry (fires_on == "pr-open")
      * target-close → review_bindings_for_completion (+ human-gated attainment)
    """
    pr_open = review_nodes.node_by_id("pr-open")
    registry_pr_open = {tid for tid, d in review_spine.judge_run_review_types().items()
                        if d.get("fires_on") == "pr-open"}
    assert set(pr_open["fires"]) == registry_pr_open, (
        "review_nodes.py pr-open `fires` drifted from the review-type registry "
        f"(manifest={sorted(pr_open['fires'])}, registry={sorted(registry_pr_open)})"
    )
    tclose = review_nodes.node_by_id("target-close")
    # philosophy must be a type review_bindings_for_completion can bind; the
    # human-gated attainment is always part of the completion pair.
    completion_reviews = {b["review"] for b in
                          review_spine.review_bindings_for_completion(has_spec=True)}
    assert review_spine.REVIEW_ATTAINMENT in set(tclose["fires"])
    assert set(tclose["fires"]) - {review_spine.REVIEW_ATTAINMENT} <= completion_reviews


def test_hook_sets_branch_specific_node_label():
    """The two review 節目 share SKILL=/beacon-review-run, so the hook must set a
    distinct NODE=<id> for each, and the debug label must interpolate it."""
    text = _hook_text()
    for n in review_nodes.REVIEW_NODES:
        if n["skill"] == "/beacon-review-run":
            assert f'NODE="{n["id"]}"' in text, (
                f"hook does not set NODE={n['id']!r} for review node {n['id']!r}"
            )
    assert 'matched:$SKILL${NODE:+:$NODE}' in text, (
        "hook debug label must append the review-node id (${NODE:+:$NODE})"
    )


@pytest.fixture
def beacon_project_dir(tmp_path):
    import json
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps(
        {"name": "ReviewNodeDriftTest", "summary": "", "milestones": []}))
    return tmp_path


def _run(cwd: Path, command: str, debug: bool = False):
    import json
    import os
    env = dict(os.environ)
    home = cwd / "_home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    if debug:
        env["BEACON_HOOK_DEBUG"] = "1"
    payload = {"tool_input": {"command": command, "cwd": str(cwd)}}
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, cwd=str(cwd), timeout=10, env=env,
    ), home


def test_hook_debug_label_distinguishes_pr_open_from_target_close(beacon_project_dir):
    """End-to-end: the hook-debug log records the branch-specific node id so a
    PR-open wake and a target-close wake are distinguishable (both wake the same
    skill, which is why the id is needed)."""
    _, home = _run(beacon_project_dir, "beacon pr create -m ms-1", debug=True)
    log = (home / ".beacon" / "hook-debug.log").read_text(encoding="utf-8")
    assert "matched:/beacon-review-run:pr-open" in log

    _, home2 = _run(beacon_project_dir, "beacon milestone observe ms-1 --reason x",
                    debug=True)
    log2 = (home2 / ".beacon" / "hook-debug.log").read_text(encoding="utf-8")
    assert "matched:/beacon-review-run:target-close" in log2
