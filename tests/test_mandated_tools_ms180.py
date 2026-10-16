"""ms-180 e-7005 — a tool the repo MANDATES must actually ship with the repo.

``CLAUDE.md`` ordered every session to use ``/review`` for PR reviews, while the
Skill existed only in the authoring machine's ``~/.claude/skills/``. A clean
install on another machine was instructed to use a tool that was not there, and
nothing detected it: a doc referencing a MILESTONE is checked by the capability
ledger's dependency invariant, a doc referencing a TOOL was checked by nothing.

``scripts/check-mandated-tools.py`` is that missing check. What these tests pin is
not "the script runs" but that it **goes red on the real defect and green on the
real fix** — a guard that cannot fail is a false sense of safety, and that is
worse than no guard, because a green guard gets trusted.

So the suite has three parts:
  1. the live repository passes (the fix is in),
  2. removing ``/review`` from the repo — reproducing the exact pre-ms-180 state —
     makes it fail, naming ``/review``,
  3. inversion: the detector's parts are checked for over- and under-reach, so a
     version that reported everything (noise) or nothing (vacuous green) is caught.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-mandated-tools.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_mandated_tools", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mt = _load()


def _run(repo: Path = REPO):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--json",
                           "--repo", str(repo)],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 1. The live repository.
# ---------------------------------------------------------------------------

def test_live_repo_has_every_mandated_tool():
    rc, result = _run()
    assert rc == 0, f"mandated tools missing: {result['missing']}"
    assert result["checked"] > 0, (
        "zero tools checked means the extractor found no mandates at all — the "
        "guard would then be vacuously green no matter what is missing")
    assert "CLAUDE.md" in result["scanned"]


def test_the_review_skill_is_now_shipped_by_the_repo():
    """The concrete fix: both copies the migration convention requires."""
    assert (REPO / "shared" / "skills" / "review" / "SKILL.md").is_file()
    assert (REPO / "skills" / "review.md").is_file()
    assert mt.skill_exists("review")


def test_claude_md_mandates_review_so_the_check_actually_covers_it():
    """If the mandate were ever reworded out of the closed marker set, the guard
    would silently stop covering the very tool it was built for."""
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "review" in mt.extract_mandates(text)["skills"]


# ---------------------------------------------------------------------------
# 2. Red on the real pre-fix state.
# ---------------------------------------------------------------------------

def _clone_repo_skeleton(dest: Path) -> Path:
    """A minimal copy of the repo the checker needs: instructions, the skill
    trees, and lib/ (for the CLI surface)."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("CLAUDE.md", "AGENTS.md"):
        src = REPO / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    shutil.copytree(REPO / "skills", dest / "skills")
    shutil.copytree(REPO / "shared" / "skills", dest / "shared" / "skills")
    (dest / "lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "lib" / "commands.py", dest / "lib" / "commands.py")
    return dest


def test_removing_the_review_skill_reproduces_the_failure(tmp_path):
    """This is the pre-ms-180 state, byte for byte: CLAUDE.md still mandates
    /review, the repo no longer ships it."""
    repo = _clone_repo_skeleton(tmp_path / "repo")
    shutil.rmtree(repo / "shared" / "skills" / "review")
    (repo / "skills" / "review.md").unlink()
    rc, result = _run(repo)
    assert rc == 1
    assert [row["ref"] for row in result["missing"]] == ["/review"]
    assert result["missing"][0]["kind"] == "skill"
    assert result["missing"][0]["file"] == "CLAUDE.md"


def test_only_one_of_the_two_copies_is_enough_to_satisfy_existence(tmp_path):
    """The two layouts spell the file differently (``shared/skills/x/SKILL.md``
    vs ``skills/x.md``), which is precisely why a name-based grep misses one."""
    repo = _clone_repo_skeleton(tmp_path / "repo")
    shutil.rmtree(repo / "shared" / "skills" / "review")
    assert _run(repo)[0] == 0          # legacy copy alone satisfies it
    repo2 = _clone_repo_skeleton(tmp_path / "repo2")
    (repo2 / "skills" / "review.md").unlink()
    assert _run(repo2)[0] == 0         # canonical copy alone satisfies it


def test_a_mandated_but_absent_skill_in_a_fresh_instruction_file_is_caught(tmp_path):
    repo = _clone_repo_skeleton(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text(
        "- デプロイ前には必ず `/totally-nonexistent-skill` を実行する。\n",
        encoding="utf-8")
    rc, result = _run(repo)
    assert rc == 1
    assert [r["ref"] for r in result["missing"]] == ["/totally-nonexistent-skill"]


def test_a_mandated_but_absent_cli_verb_is_caught(tmp_path):
    repo = _clone_repo_skeleton(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text(
        "- 記録は必ず `beacon notarealnoun record` で行う。\n", encoding="utf-8")
    rc, result = _run(repo)
    assert rc == 1
    assert result["missing"][0]["kind"] == "cli"
    assert result["missing"][0]["ref"] == "beacon notarealnoun"


# ---------------------------------------------------------------------------
# 3. Inversion: over-reach and under-reach.
# ---------------------------------------------------------------------------

def test_a_passing_mention_is_not_treated_as_a_mandate():
    """Over-reach check. Failing on every mention would make the guard noisy and
    it would be turned off — so a non-mandate line must contribute nothing."""
    text = "You can also use `/some-made-up-skill` if you like.\n"
    assert mt.extract_mandates(text) == {"skills": [], "cli": []}


def test_a_mandate_line_with_no_tool_reference_contributes_nothing():
    text = "- 曖昧なときは必ず user に確認する。\n"
    assert mt.extract_mandates(text) == {"skills": [], "cli": []}


def test_a_path_fragment_is_not_mistaken_for_a_skill():
    """Under-reach's mirror image: ``lib/review`` must not be read as ``/review``
    (and so accidentally satisfy or accidentally fail the check)."""
    text = "必ず lib/commands.py と a/b/c を読むこと。\n"
    assert mt.extract_mandates(text)["skills"] == []


@pytest.mark.parametrize("line,expected", [
    ("必ず `/beacon-log` を実行する", ["beacon-log"]),
    ("MUST run `/beacon-note` first", ["beacon-note"]),
    ("Never skip `/review`", ["review"]),
    ("`/beacon-spec` を使ってはならない", ["beacon-spec"]),
])
def test_each_mandate_marker_is_recognised(line, expected):
    assert mt.extract_mandates(line + "\n")["skills"] == expected


def test_cli_noun_resolution_accepts_a_subaction_key():
    """``beacon pr approve`` is dispatch key ``pr_approve``; a mandate naming the
    NOUN must resolve through any key under it."""
    verbs = {"pr_approve", "pr_list", "status"}
    assert mt.cli_verb_exists("pr", verbs) is True
    assert mt.cli_verb_exists("status", verbs) is True
    assert mt.cli_verb_exists("nonexistent", verbs) is False


def test_cli_noun_resolution_does_not_match_on_a_bare_prefix():
    """``beacon pr`` must not be satisfied by an unrelated key that merely starts
    with the same letters — that is the coincidental match this guard avoids."""
    assert mt.cli_verb_exists("pr", {"process_thing", "print_help"}) is False


def test_skill_existence_is_a_path_resolution_not_a_text_search(tmp_path, monkeypatch):
    """If existence were decided by grepping text for the name, a file that merely
    MENTIONS the skill would satisfy it."""
    monkeypatch.setattr(mt, "_REPO", str(tmp_path))
    (tmp_path / "skills").mkdir(parents=True)
    (tmp_path / "skills" / "unrelated.md").write_text(
        "this file mentions ghost-skill many times\n", encoding="utf-8")
    assert mt.skill_exists("ghost-skill") is False
    (tmp_path / "skills" / "ghost-skill.md").write_text("x", encoding="utf-8")
    assert mt.skill_exists("ghost-skill") is True


def test_the_marker_set_is_closed_so_widening_it_is_deliberate():
    """A regex-based "looks like a requirement" detector would drift silently.
    The marker list being data means adding one is a visible edit."""
    assert isinstance(mt.MANDATE_MARKERS, tuple)
    assert "必ず" in mt.MANDATE_MARKERS
    assert mt.extract_mandates("`/x-skill` をたぶん使う\n") == {"skills": [],
                                                              "cli": []}
