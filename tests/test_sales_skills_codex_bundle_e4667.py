"""The 14 sales skills are canonical and ship in the Codex plugin (ms-133 e-4667).

e-4644's audit found that `beacon-sales-*` skills lived only in legacy
`skills/*.md`, never migrated to canonical `shared/skills/`. Since the Codex
plugin build (`build-codex-plugin-skills.py`) ships only `shared/skills/*`, a
Codex sales user received no sales skills. e-4667 migrated them; these pin:

  * all 14 sales skills exist as canonical `shared/skills/<name>/` (SKILL.md +
    clients/claude.yaml) and parse via skill_converter;
  * they are materialized into the Codex plugin subtree;
  * the canonical SKILL.md carries no `profession:` field (bundle-all model —
    the profession gate stays in legacy skills/*.md for Claude only).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

SALES = sorted(p.stem.replace(".md", "")
               for p in (ROOT / "skills").glob("beacon-sales-*.md"))


def test_there_are_sales_skills_to_check():
    assert len(SALES) >= 14


@pytest.mark.parametrize("name", SALES)
def test_sales_skill_is_canonical_and_parses(name):
    import skill_converter as sc
    d = ROOT / "shared" / "skills" / name
    assert (d / "SKILL.md").is_file(), f"{name} not migrated to shared/skills/"
    assert (d / "clients" / "claude.yaml").is_file(), f"{name} missing claude.yaml"
    skill = sc.read_canonical_skill(d)  # raises on malformed
    assert skill.name == name


@pytest.mark.parametrize("name", SALES)
def test_sales_skill_shipped_in_codex_plugin(name):
    plugin_skill = ROOT / "plugins" / "beacon" / "skills" / name / "SKILL.md"
    assert plugin_skill.is_file(), (
        f"{name} not materialized into the Codex plugin — run "
        "python3 scripts/build-codex-plugin-skills.py"
    )


@pytest.mark.parametrize("name", SALES)
def test_canonical_skill_md_has_no_profession_field(name):
    """profession stays in legacy skills/*.md (Claude's gate); canonical
    frontmatter must not carry it (COMMON_FIELDS only) — else read_canonical_skill
    would reject the disallowed field."""
    skill_md = (ROOT / "shared" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    fm = skill_md.split("---", 2)[1]
    assert "profession:" not in fm
