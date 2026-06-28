"""ms-97 Phase 5 — Skill 動線整理 structural tests.

Verifies the Phase-5 Skill changes are in place:

  * G8: ``skills/beacon-trek.md`` (= OLD generic Skill) is removed from
    the canonical Skill source. Archived under ``.trash/`` for
    archeology, not deleted.
  * G10 / AC28: ``skills/beacon-trek-execute.md`` carries the auto-show
    step for the ``trek-operating-manual`` CORE doc.
  * G11: ``skills/beacon-bus-armed.md`` carries the "Trek 用途外"
    banner so future users don't conflate armed mode with Trek
    autonomous loops.
  * AC29: ``skills/beacon-trek-execute.md`` is trimmed (= max ~250
    lines) and adds dispatch branches for trek-leader-digest /
    trek-task-review / completion_ready event kinds.

These tests are string-based: the Skill body is prose that AI
interprets, so structural removals are the regression we care about.
Wording can drift inside sections without breaking these tests.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"
TREK_EXECUTE = SKILLS / "beacon-trek-execute.md"
BUS_ARMED = SKILLS / "beacon-bus-armed.md"
LEGACY_BEACON_TREK = SKILLS / "beacon-trek.md"


# ---------------------------------------------------------------------------
# G8 — OLD beacon-trek Skill moved to .trash/
# ---------------------------------------------------------------------------


def test_legacy_beacon_trek_skill_removed_from_canonical_path():
    """The OLD generic ``beacon-trek`` Skill must not exist in the
    canonical Skill source path. It was a 526-line wrapper for the
    create/join/scope-add/archive subactions, superseded by direct
    ``beacon trek <verb>`` CLI usage and the trimmed
    ``beacon-trek-execute`` Skill. Phase 5 retires the wrapper.
    """
    assert not LEGACY_BEACON_TREK.exists(), (
        f"{LEGACY_BEACON_TREK} should be archived under .trash/ — "
        "the OLD generic beacon-trek Skill is retired in ms-97 Phase 5 "
        "(G8). If you need it back, restore from .trash/skills-archive-*"
    )


# ---------------------------------------------------------------------------
# G10 / AC28 — Trek manual auto-show step
# ---------------------------------------------------------------------------


def test_trek_execute_has_manual_auto_show_step():
    """The execute Skill must open with the trek-operating-manual
    auto-show step (= AC28 onboarding). Without this, executor sessions
    re-discover the manual every time instead of having it injected.
    """
    body = TREK_EXECUTE.read_text(encoding="utf-8")
    assert "Step 0: Trek manual" in body, (
        "skills/beacon-trek-execute.md must have a Step 0 that "
        "auto-shows the trek-operating-manual CORE doc"
    )
    assert "yfOufm7d2zkAhcm5QWES" in body, (
        "Step 0 must reference the actual doc_id of trek-operating-manual"
    )
    assert "AC28" in body, "self-reference the driving AC"
    assert "/tmp/.beacon-trek-manual-shown-" in body, (
        "the auto-show must dedupe via a /tmp sentinel file so the same "
        "session doesn't re-show the manual on every Skill invocation"
    )


# ---------------------------------------------------------------------------
# G11 — Trek 用途外 banner on beacon-bus-armed
# ---------------------------------------------------------------------------


def test_bus_armed_has_trek_out_of_scope_banner():
    """The armed Skill must carry a top banner that points Trek
    progress management at ``/beacon-trek-execute`` instead.

    Without this banner future users conflate armed mode (= generic DM
    debug) with Trek autonomous loops (= the actual production path)
    and pick the wrong tool.
    """
    body = BUS_ARMED.read_text(encoding="utf-8")
    assert "Trek 用途外" in body, (
        "skills/beacon-bus-armed.md must open with a 'Trek 用途外' banner "
        "(= ms-97 中心原則 6) so users don't conflate armed mode with "
        "Trek progress management"
    )
    assert "/beacon-trek-execute" in body, (
        "the banner must point at the canonical Trek autonomous Skill"
    )
    assert "ms-97" in body, "self-reference the driving milestone"


# ---------------------------------------------------------------------------
# AC29 — Skill 軽量化 (lightweight Skill body)
# ---------------------------------------------------------------------------


def test_trek_execute_skill_is_trimmed():
    """The execute Skill is trimmed to ~150-250 lines (= AC29). The
    pre-Phase-5 body was 468 lines of heavy structural self-check
    sections; the trimmed body keeps only the dispatch table + 1-2
    line reminders + the Step 1-9 autonomous loop skeleton.
    """
    body = TREK_EXECUTE.read_text(encoding="utf-8")
    line_count = body.count("\n") + 1
    assert line_count < 250, (
        f"beacon-trek-execute.md is {line_count} lines; AC29 targets "
        "~150-250 lines. Re-trim before adding more sections."
    )


# ---------------------------------------------------------------------------
# Dispatch branches for the 3 missing event kinds (= leader-digest /
# task-review / completion_ready)
# ---------------------------------------------------------------------------


def test_trek_execute_dispatch_table_covers_all_event_kinds():
    """The dispatch table must list every event kind the executor
    might receive. Phase 5 adds the 3 missing kinds (= trek-leader-
    digest / trek-task-review / completion_ready) so each branch has
    explicit guidance rather than falling through to the default
    autonomous loop.
    """
    body = TREK_EXECUTE.read_text(encoding="utf-8")
    for channel in (
        "trek-trigger",
        "trek-progress-check",
        "trek-leader-digest",
        "trek-task-review",
        "completion_ready",
    ):
        assert channel in body, (
            f"dispatch table must include '{channel}' branch with at "
            "least a 1-2 sentence guidance (= AC reference) so the "
            "executor knows what to do when the event arrives"
        )
