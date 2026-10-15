"""ms-184 e-7018 — the 動線 must really reach the derivation.

The failure this prevents
========================
ms-166 spent 236 items sweeping out "the wiring exists but nothing actually
runs it". A Skill that tells the AI to run `beacon recap targets --window ...`
would look complete in review and fail at the moment a user asks "昨日って何
やったっけ" — the flag does not exist, and no existing guard notices: the CLI
help-drift checker compares the CLI's own surfaces to each other, and the
Skill-parity test compares the Skill's three copies to each other. Neither
crosses from the Skill to the CLI.

So this walks the Skill text, pulls out every `beacon recap …` invocation, and
checks each verb and flag against BOTH dispatch front-ends (bash `bin/beacon`
and Python `beacon_cli/dispatch.py`) — a flag present on only one front-end is
the exact drift that broke Windows cross-machine DM in 2026-06-07.
"""

from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKILL_COPIES = (
    "shared/skills/beacon-recap/SKILL.md",
    "skills/beacon-recap.md",
    "plugins/beacon/skills/beacon-recap/SKILL.md",
)

#: `beacon recap <sub> …` up to the end of the line / a closing backtick.
_INVOCATION = re.compile(r"beacon recap (projects|targets)([^\n`]*)")
_FLAG = re.compile(r"(--[a-z][a-z0-9-]*)")

#: Flags whose VALUE in the Skill text is a placeholder, not a literal — they
#: still have to exist, which is what this test checks.
EXPECTED_SUBVERBS = {"projects", "targets"}


def _read(rel: str) -> str:
    path = os.path.join(ROOT, rel)
    assert os.path.isfile(path), f"missing Skill copy: {rel}"
    return open(path, encoding="utf-8").read()


def _invocations(text: str):
    for sub, rest in _INVOCATION.findall(text):
        yield sub, sorted(set(_FLAG.findall(rest)))


def test_every_skill_copy_exists_and_mentions_both_verbs():
    """Three copies ship (canonical / legacy / Codex plugin) and each must
    carry the whole 動線 — a copy that lost a verb silently narrows the
    entry path for whichever client reads that copy."""
    for rel in SKILL_COPIES:
        text = _read(rel)
        subs = {sub for sub, _ in _invocations(text)}
        assert subs == EXPECTED_SUBVERBS, \
            f"{rel} invokes {subs or 'nothing'}; expected {EXPECTED_SUBVERBS}"


def _bash_flags(subverb: str) -> set:
    """Flags the bash front-end's `recap` arm-loop accepts."""
    src = _read("bin/beacon")
    start = src.index("\n    recap)\n")
    end = src.index("\n    doctor)\n", start)
    return set(_FLAG.findall(src[start:end]))


def _python_flags(subverb: str) -> set:
    """Flags the Python front-end's `recap <subverb>` subparser accepts.

    Collects every ``p_recap_<subverb>.add_argument(...)`` line wherever it
    appears, rather than scanning a contiguous block from the parser's
    construction. The block-scanning version broke the moment a comment was
    inserted between the parser and its first argument — it silently returned
    an EMPTY flag set, which would have read as "the Python front accepts
    nothing" and could just as easily have gone the other way and passed
    vacuously. Matching the uniquely-prefixed variable name has no such
    ordering assumption.
    """
    src = _read("beacon_cli/dispatch.py")
    var = f"p_recap_{subverb}"
    assert f'{var} = recap_sub.add_parser("{subverb}"' in src, \
        f"no Python subparser for `recap {subverb}`"
    lines = [ln for ln in src.splitlines()
             if ln.strip().startswith(f"{var}.add_argument(")]
    assert lines, f"`recap {subverb}` subparser declares no arguments"
    return set(_FLAG.findall("\n".join(lines)))


@pytest.mark.parametrize("subverb", sorted(EXPECTED_SUBVERBS))
def test_skill_flags_exist_on_both_frontends(subverb):
    """Every flag the Skill tells the AI to pass must parse on BOTH fronts."""
    used = set()
    for rel in SKILL_COPIES:
        for sub, flags in _invocations(_read(rel)):
            if sub == subverb:
                used.update(flags)
    assert used, f"the Skill passes no flags to `recap {subverb}`"

    bash = _bash_flags(subverb)
    python = _python_flags(subverb)
    missing_bash = sorted(used - bash)
    missing_python = sorted(used - python)
    assert not missing_bash, (
        f"Skill uses `beacon recap {subverb} {missing_bash}` but bin/beacon "
        "does not accept it")
    assert not missing_python, (
        f"Skill uses `beacon recap {subverb} {missing_python}` but "
        "beacon_cli/dispatch.py does not accept it (= Windows / pipx users "
        "would hit 'unrecognized arguments')")


def _recap_arm_code() -> str:
    """The bash `recap)` arm with comment lines REMOVED.

    The first version of the ordering check searched the raw arm and matched
    the word `ensure_project` inside the comment that EXPLAINS the ordering —
    measuring the prose, not the code. That reads as a guard while being
    satisfiable by a comment anywhere, in any order, which is strictly worse
    than no check. Comments are stripped so the assertion can only be about
    what runs.
    """
    src = _read("bin/beacon")
    start = src.index("\n    recap)\n")
    arm = src[start:src.index("\n    doctor)\n", start)]
    return "\n".join(ln for ln in arm.splitlines()
                      if not ln.strip().startswith("#"))


def test_both_frontends_refuse_the_misplaced_project_flag(subverb="projects"):
    """`--project` on the aggregate verb must be refused on BOTH front-ends.

    The first version of this guard checked only the bash arm. The Python
    parser did not declare `--project` at all, so the same misuse there fell
    through to argparse's generic "unrecognized arguments" against the
    TOP-LEVEL banner — losing both the reason it is refused and the redirect to
    `recap targets --project <id>`. An independent AX review found it; the
    guard that was supposed to cover this had enumerated one of the two paths
    and read as if it covered the rule.
    """
    bash = _read("bin/beacon")
    start = bash.index("\n    recap)\n")
    arm = bash[start:bash.index("\n    doctor)\n", start)]
    assert "recap projects: --project is not accepted" in arm, \
        "bash must refuse a --project passed to `recap projects`, not ignore it"

    dispatch = _read("beacon_cli/dispatch.py")
    assert "--project" in _python_flags("projects"), (
        "the Python parser must DECLARE --project on `projects` so the handler "
        "can refuse it with an explanation; omitting it yields a generic "
        "argparse error with no redirect")
    assert "recap projects: --project is not accepted" in dispatch, \
        "the Python front must refuse it with the same message bash gives"


def test_both_frontends_agree_on_the_no_project_help_path():
    """`beacon recap` with no subcommand must not need a Beacon project.

    Learning how to use a command is not a project operation. The bash arm ran
    `ensure_project` before it looked at help, so OUTSIDE a project the same
    invocation errored (rc=1) on bash and printed usage (rc=0) on Python — one
    command, two behaviours, split by OS. Pinned structurally: the bash arm
    must reach its usage block before `ensure_project`.
    """
    arm = _recap_arm_code()
    usage_at = arm.index('echo "usage:"')
    ensure_at = arm.index("ensure_project")
    assert ensure_at > usage_at, (
        "bin/beacon runs ensure_project before handling help for `recap`, so "
        "`beacon recap` outside a project errors on bash while the Python "
        "front prints usage")
    # ... and it must still gate the verbs that actually read project data.
    assert "targets|projects) ensure_project" in arm, \
        "the real verbs must still require a project"


def test_recap_projects_refuses_the_flag_that_belongs_to_targets():
    """`--project` on the aggregate verb is a user error worth catching.

    `recap projects` already spans every project you own, so `--project` there
    means the caller wanted depth and typed the breadth verb. Silently
    ignoring the flag would return a 12-project sweep that reads like an
    answer about the one project they named.
    """
    src = _read("bin/beacon")
    start = src.index("\n    recap)\n")
    end = src.index("\n    doctor)\n", start)
    arm = src[start:end]
    assert "recap projects: --project is not accepted" in arm, \
        "a --project passed to `recap projects` must be refused, not ignored"


def test_skill_does_not_tell_the_ai_to_compute_dates():
    """受入条件 7: the period must be passed as the user's own words.

    If the Skill had the AI resolve "yesterday" into a date itself, the answer
    would drift at month / week / timezone edges with nothing in the output to
    reveal it. The parser owns that conversion (`date_window.parse_since`).
    """
    text = _read("shared/skills/beacon-recap/SKILL.md")
    assert "日付を自分で計算してはならない" in text
    # The mapping table must name the natural spellings the parser accepts, so
    # the Skill and the parser cannot drift into different vocabularies.
    import date_window as dw

    for spelling in ("yesterday", "today", "this-week", "last-week",
                     "this-month"):
        assert spelling in text, \
            f"the Skill's period table omits `{spelling}`, which the parser accepts"
        dw.parse_since(spelling)      # raises if the parser lost it


def test_skill_requires_reporting_what_could_not_be_judged():
    """方針 8 reaches the Skill, not just the library.

    The library returns `unavailable` / `undated` / `sources_unavailable`; if
    the Skill does not oblige the AI to SAY them, the distinction between
    "nothing happened" and "we could not tell" dies at the last step — which
    is the only step the user sees.
    """
    text = _read("shared/skills/beacon-recap/SKILL.md")
    for key in ("unavailable", "undated", "sources_unavailable"):
        assert key in text, f"the Skill never mentions `{key}`"
    assert "読めなかった分を 0 件側に混ぜない" in text
    assert "横断したふりをしない" in text


# ---------------------------------------------------------------------------
# 受入条件 10 — one derivation, and a flag spelled like the write side's
# ---------------------------------------------------------------------------

def test_project_flag_matches_the_write_sides_spelling():
    """The read side must not invent a second spelling for "that project".

    The write side already says `--project <id>` (`beacon dm send --project`,
    `beacon dm audit --project`). `--as-project` exists too but means something
    ELSE — "what would project P be allowed to see", a disclosure view — so
    reusing it here would give one flag two meanings.
    """
    dispatch = _read("beacon_cli/dispatch.py")
    assert 'p_recap_targets.add_argument("--project", dest="recap_project"' in dispatch
    assert "--as-project" not in _python_flags("targets"), \
        "--as-project means a disclosure view, not 'read that project'"

    # The spelling is shared with the write side, in the same parser file.
    assert '.add_argument("--project", dest="dm_project_id"' in dispatch, \
        ("the write side's --project spelling moved; re-check that the read "
         "side still matches it rather than silently diverging")


def test_the_derivation_lives_in_exactly_one_module():
    """受入条件 10: no second copy of "progressed" for the cross-project path.

    Both verbs, and the server endpoint, must call ``progress_window`` rather
    than re-deriving. Checked structurally: the CLI family and the server
    router may not contain their own date comparison against the window
    arguments, and both handlers must reach ``derive``.
    """
    import ast

    recap = _read("lib/cmd_recap.py")
    tree = ast.parse(recap)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "derive" in called, "cmd_recap does not call progress_window.derive"
    assert "aggregate" in called, "cmd_recap does not call progress_window.aggregate"

    # A re-implementation would compare timestamps itself. The period bounds
    # only ever travel INTO progress_window / date_window from here.
    for module in ("lib/cmd_recap.py", "server/routers_me.py"):
        src = _read(module)
        for banned in ("created_at <", "created_at >", "< from_date", "> to_date"):
            assert banned not in src, (
                f"{module} compares timestamps itself ({banned!r}) — the period "
                "rule belongs to lib/date_window.py alone")


def test_server_and_cli_share_the_same_derivation_import():
    """Both sweep paths import the same module, so they cannot disagree."""
    assert "import progress_window" in _read("server/routers_me.py")
    assert "import progress_window" in _read("lib/cmd_recap.py")
