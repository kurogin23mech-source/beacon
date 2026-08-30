"""Every migrated canonical Skill under ``shared/skills/<name>/`` must be a
valid canonical Skill AND stay in sync with its legacy ``skills/<name>.md``.

Background (ms-93 Skill migration — e-3145 pathfinder + cohort PRs)
==================================================================
The migration moves each Skill into the canonical ``shared/skills/<name>/``
layout (``SKILL.md`` + ``clients/*.yaml``) that ``lib/skill_converter.py``
renders into the Claude and Codex client formats. During the migration BOTH
copies exist: the legacy flat file ``skills/<name>.md`` and the canonical
``shared/skills/<name>/``.

That dual source of truth is a drift hazard. On 2026-07-10 a doc fix applied
to ``skills/beacon-session-start.md`` had to be re-applied by hand to the
shared copy, because nothing pinned the two together. This test makes any
single-sided edit fail: the canonical body and description must match the
legacy file. It auto-covers each new cohort as skills are migrated, and can
be retired once the legacy files are removed (source-of-truth consolidation,
deferred per the cohort plan).

The check is on **body + description**, not byte-for-byte frontmatter: the
migration intentionally normalizes frontmatter (e.g. trigger phrases move to
``clients/claude.yaml``, comments are dropped), so a strict frontmatter
comparison would flag expected, benign formatting changes. The body is the
substance and the exact thing that drifted, so that is what we pin.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import skill_converter as sc  # noqa: E402

SHARED = REPO / "shared" / "skills"
LEGACY = REPO / "skills"


def _migrated_names() -> list[str]:
    if not SHARED.is_dir():
        return []
    return sorted(
        p.name
        for p in SHARED.iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file()
    )


def _legacy_body(text: str) -> str:
    """Return the markdown body after the leading ``---`` frontmatter block."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1:])
    return text


MIGRATED = _migrated_names()


def _distributable_legacy_names() -> list[str]:
    """Legacy ``skills/<name>.md`` that represent real, distributable Skills.

    Excludes ``_``-prefixed files — those are methodology includes referenced by
    other Skills (``_beacon-*-methodology``), not invocable Skills with triggers,
    so they are intentionally never migrated / distributed to the Codex plugin.
    """
    if not LEGACY.is_dir():
        return []
    return sorted(
        p.stem for p in LEGACY.glob("*.md") if not p.stem.startswith("_")
    )


def test_no_distributable_skill_is_legacy_only():
    """ms-160 e-5801/e-5802: the parity guard is BIDIRECTIONAL. Before, it only
    walked migrated → legacy, so a Skill that lived ONLY as ``skills/<name>.md``
    (never migrated to ``shared/skills/``) was invisible: it silently dropped out
    of the Codex plugin build (which ships only canonical skills). ``beacon-review-run``
    — the post-commit review wake target — was exactly such a Codex gap. This test
    fails if any distributable legacy Skill has no canonical counterpart."""
    legacy_only = [n for n in _distributable_legacy_names() if n not in set(MIGRATED)]
    assert not legacy_only, (
        "these Skills exist only as legacy skills/<name>.md and are NOT in "
        "shared/skills/<name>/, so the Codex plugin build (canonical-only) drops "
        f"them: {legacy_only}. Migrate them to shared/skills/ (SKILL.md + "
        "clients/claude.yaml) and run scripts/build-codex-plugin-skills.py."
    )


def test_at_least_the_pathfinder_is_migrated():
    """Guards the discovery glob itself: if it silently returns nothing, the
    parametrized parity tests below would vacuously pass and hide a regression.
    """
    assert "beacon-session-start" in MIGRATED, (
        "shared/skills/beacon-session-start/ is the e-3145 pathfinder and must "
        "exist; an empty MIGRATED list means the discovery path is broken."
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_canonical_skill_is_valid(name):
    """read_canonical_skill must accept the migrated Skill and name must match
    the directory (converter's own invariant, exercised on the real tree)."""
    skill = sc.read_canonical_skill(SHARED / name)
    assert skill.name == name


@pytest.mark.parametrize("name", MIGRATED)
def test_canonical_matches_legacy_body_and_description(name):
    """The canonical copy and the legacy skills/<name>.md must agree while both
    exist. Body drift (the 2026-07-10 failure mode) fails here."""
    skill = sc.read_canonical_skill(SHARED / name)
    legacy = LEGACY / f"{name}.md"
    assert legacy.is_file(), (
        f"legacy skills/{name}.md is missing while shared/skills/{name}/ exists. "
        "During migration both copies must exist and agree; remove this parity "
        "test's coverage for a skill only when its legacy file is retired "
        "(source-of-truth consolidation)."
    )
    legacy_txt = legacy.read_text(encoding="utf-8")
    assert skill.body.strip() == _legacy_body(legacy_txt).strip(), (
        f"shared/skills/{name}/SKILL.md and skills/{name}.md bodies have drifted. "
        "Edit BOTH copies (or retire the legacy file). ms-93 cohort migration."
    )
    assert skill.description and skill.description in legacy_txt, (
        f"description drift between shared/skills/{name}/ and skills/{name}.md."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
