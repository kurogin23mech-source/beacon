"""Tests for profession-gated skill install (ms-108 e-3364).

`_install_skills` / `cmd_skill_install` must install ``core`` skills for every
project but only emit a profession's skills (e.g. ``beacon-sales-*``) when that
profession is in scope — so a dev project does not receive the sales toolkit and
vice versa. Install is additive across setups.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import commands  # noqa: E402


# ---------------------------------------------------------------------------
# _skill_profession_from_text
# ---------------------------------------------------------------------------

class TestSkillProfession:
    def test_no_frontmatter_is_core(self):
        assert commands._skill_profession_from_text("just a body") == "core"

    def test_frontmatter_without_profession_is_core(self):
        assert commands._skill_profession_from_text("---\nname: x\n---\nbody") == "core"

    def test_reads_profession_tag(self):
        assert commands._skill_profession_from_text(
            "---\nname: x\nprofession: sales\n---\n") == "sales"

    def test_case_insensitive(self):
        assert commands._skill_profession_from_text(
            "---\nprofession: Sales\n---\n") == "sales"

    def test_only_reads_frontmatter_block(self):
        # a `profession:` mention in the body must not count
        assert commands._skill_profession_from_text(
            "---\nname: x\n---\nprofession: sales in prose") == "core"


# ---------------------------------------------------------------------------
# _requested_professions
# ---------------------------------------------------------------------------

class TestRequestedProfessions:
    def test_always_includes_core(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BEACON_PROFESSION", raising=False)
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(tmp_path / "none.json"))
        assert commands._requested_professions() == {"core"}

    def test_env_override_adds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BEACON_PROFESSION", "sales")
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(tmp_path / "none.json"))
        assert commands._requested_professions() == {"core", "sales"}

    def test_reads_project_profession(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BEACON_PROFESSION", raising=False)
        pf = tmp_path / "project.json"
        pf.write_text('{"profession": "sales", "milestones": []}')
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(pf))
        assert commands._requested_professions() == {"core", "sales"}

    def test_dev_project_is_core_only(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BEACON_PROFESSION", raising=False)
        pf = tmp_path / "project.json"
        pf.write_text('{"profession": "dev", "milestones": []}')
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(pf))
        assert commands._requested_professions() == {"core", "dev"}


# ---------------------------------------------------------------------------
# _skill_is_installable
# ---------------------------------------------------------------------------

class TestSkillIsInstallable:
    def test_core_always(self, tmp_path):
        p = tmp_path / "c.md"
        p.write_text("---\nname: c\n---\n")
        assert commands._skill_is_installable(str(p), {"core"})

    def test_sales_gated_out_for_core(self, tmp_path):
        p = tmp_path / "s.md"
        p.write_text("---\nname: s\nprofession: sales\n---\n")
        assert not commands._skill_is_installable(str(p), {"core"})

    def test_sales_installs_when_requested(self, tmp_path):
        p = tmp_path / "s.md"
        p.write_text("---\nname: s\nprofession: sales\n---\n")
        assert commands._skill_is_installable(str(p), {"core", "sales"})

    def test_unreadable_fails_open(self, tmp_path):
        # a missing file must not silently hide a skill by erroring
        assert commands._skill_is_installable(str(tmp_path / "missing.md"), {"core"})


# ---------------------------------------------------------------------------
# integration: _install_skills gates the real sales skills
# ---------------------------------------------------------------------------

class TestInstallSkillsGate:
    def _home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(commands, "_user_home", lambda: str(home))
        return home / ".claude" / "skills"

    def test_dev_skips_sales(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BEACON_PROFESSION", raising=False)
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(tmp_path / "none.json"))
        dst = self._home(tmp_path, monkeypatch)
        commands._install_skills()
        assert (dst / "beacon-log").exists()          # core skill installed
        assert not (dst / "beacon-sales-card").exists()  # sales gated out

    def test_sales_includes_sales(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BEACON_PROFESSION", "sales")
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(tmp_path / "none.json"))
        dst = self._home(tmp_path, monkeypatch)
        commands._install_skills()
        assert (dst / "beacon-log").exists()
        assert (dst / "beacon-sales-card").exists()   # sales installed when in scope
