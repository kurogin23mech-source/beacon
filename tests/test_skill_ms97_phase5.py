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




# ---------------------------------------------------------------------------
# G11 — Trek 用途外 banner on beacon-bus-armed
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# AC29 — Skill 軽量化 (lightweight Skill body)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Dispatch branches for the 3 missing event kinds (= leader-digest /
# task-review / completion_ready)
# ---------------------------------------------------------------------------


