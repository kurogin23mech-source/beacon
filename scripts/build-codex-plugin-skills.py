#!/usr/bin/env python3
"""Materialize canonical shared/skills/* into plugins/beacon/skills/* (Codex form).

Why (ms-93 / e-2512)
====================
The Codex plugin ships whatever lives under ``plugins/beacon/skills/`` — its
``.codex-plugin/plugin.json`` declares ``skills: ./skills/``. ``codex plugin
add`` copies that plugin subtree into the plugin cache verbatim; anything
OUTSIDE the subtree (e.g. the canonical ``shared/skills/``) is NOT included.
So a fresh Codex install only received the two plugin-only skills
(beacon-codex-bridge / beacon-codex-armed) and none of the 35 canonical
Beacon skills — they were migrated to ``shared/skills/`` but never delivered.

This script closes that gap deterministically: the source of truth stays
``shared/skills/*``, and each canonical skill is rendered INTO the plugin
subtree via ``skill_converter.render_codex`` (copies SKILL.md plus any
sub-resource dirs). Symlinks / ``../..`` references are deliberately avoided —
they break at the archive/cache boundary when the plugin is copied.

The two plugin-only Codex skills are hand-maintained and preserved (they are
not in ``shared/skills/``, so this script never touches them).

Profession gate policy (ms-133 e-4667)
======================================
The Codex plugin BUNDLES ALL canonical skills, including profession-specific
ones (the 14 ``beacon-sales-*`` skills migrated in e-4667). There is NO
per-project profession gate on the Codex side — unlike ``beacon skill install``
for Claude Code, which reads the legacy ``skills/*.md`` frontmatter
``profession:`` and installs only the current project's occupation. That gate
can't apply here: a Codex plugin is a static bundle copied at ``codex plugin
add`` time, with no project context. Bundling everything and letting each skill
activate only on its own triggers is the accepted model (a dev Codex user simply
never fires the sales triggers). ``profession`` therefore stays only in the
legacy ``skills/*.md`` (Claude's gate); the canonical ``SKILL.md`` frontmatter
has no ``profession`` field (COMMON_FIELDS = name/description/version).

Run this after any change under ``shared/skills/``. The CI test
``tests/test_codex_plugin_install.py`` pins that the materialized output is in
sync (byte-identical SKILL.md) and that the shipped set is exactly
canonical + plugin-only. After running, bump ``plugin.json`` version
(cachebuster) so ``codex plugin add`` picks up the new content.

Usage: python scripts/build-codex-plugin-skills.py [--check]
  (no args)  materialize and write
  --check    verify plugins/beacon/skills is in sync WITHOUT writing; exit 1 on drift
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import skill_converter as sc  # noqa: E402

SHARED = REPO / "shared" / "skills"
PLUGIN_SKILLS = REPO / "plugins" / "beacon" / "skills"
# Codex-only skills that live solely in the plugin (no canonical source).
PLUGIN_ONLY = {"beacon-codex-bridge", "beacon-codex-armed"}


def _canonical_names() -> list[str]:
    return sorted(
        p.name
        for p in SHARED.iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file()
    )


# NOTE: render_codex copies SKILL.md verbatim, so the on-disk canonical
# SKILL.md IS the codex artifact — check() compares against it directly.


def check() -> int:
    canonical = _canonical_names()
    drift = []
    for name in canonical:
        src = SHARED / name / "SKILL.md"
        dst = PLUGIN_SKILLS / name / "SKILL.md"
        if not dst.is_file():
            drift.append(f"missing: plugins/beacon/skills/{name}/SKILL.md")
        elif dst.read_bytes() != src.read_bytes():
            drift.append(f"stale: plugins/beacon/skills/{name}/SKILL.md")
    present = {p.name for p in PLUGIN_SKILLS.iterdir() if p.is_dir()}
    stray = present - set(canonical) - PLUGIN_ONLY
    for s in sorted(stray):
        drift.append(f"stray: plugins/beacon/skills/{s} (not canonical, not plugin-only)")
    for missing_only in PLUGIN_ONLY - present:
        drift.append(f"missing plugin-only: plugins/beacon/skills/{missing_only}")
    if drift:
        print("Codex plugin skills OUT OF SYNC:", file=sys.stderr)
        for d in drift:
            print(f"  - {d}", file=sys.stderr)
        print("Run: python scripts/build-codex-plugin-skills.py", file=sys.stderr)
        return 1
    print(f"in sync: {len(canonical)} canonical + {len(PLUGIN_ONLY)} plugin-only = "
          f"{len(canonical) + len(PLUGIN_ONLY)}")
    return 0


def build() -> int:
    canonical = _canonical_names()
    for name in canonical:
        sk = sc.read_canonical_skill(SHARED / name)
        sc.render_codex(sk, PLUGIN_SKILLS / name)
        print(f"materialized {name}")
    present = {p.name for p in PLUGIN_SKILLS.iterdir() if p.is_dir()}
    stray = present - set(canonical) - PLUGIN_ONLY
    if stray:
        print(
            f"WARNING: stray plugin skill dirs (not canonical, not plugin-only): "
            f"{sorted(stray)} — remove by hand (move to .trash) if a skill was renamed/dropped.",
            file=sys.stderr,
        )
    print(f"done: {len(canonical)} canonical + {len(PLUGIN_ONLY)} plugin-only = "
          f"{len(canonical) + len(PLUGIN_ONLY)} expected in plugins/beacon/skills/")
    return 0


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv[1:] else build())
