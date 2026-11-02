#!/usr/bin/env python3
"""Every Skill a producer names must exist on disk (ms-185 e-7079).

## Why this guard exists

PR #827 deleted four Skills (``beacon-trek-execute`` and siblings) from all
three places they live. Two *independent* reviewers then found the same high
defect: ``bin/beacon-bus-inbox-hook.py`` was still building prompt text that
told the receiving AI session to

    launch: `<the beacon-trek-execute Skill>`  (no confirmation prompt)

for a Skill that no longer existed anywhere — and the producers that trigger
that text (the server tick, the review endpoint) were still live. Nothing in
CI noticed, because no test cross-checked a Skill *reference* against Skill
*existence*. The reference lives in a Python f-string, the Skill in a Markdown
file: no compiler, no import, no type — nothing connects them.

That class of defect is worse than an ordinary dangling reference. The text is
injected into an AI's context as an instruction, with "run this autonomously
without asking the user first" attached. A human reading a broken link shrugs;
an AI told to invoke a command that returns nothing has no fallback and will
improvise.

## What it checks

For every production file (not tests, not changelogs, not archived copies),
find every ``/beacon-<name>`` slash-command reference and require that
``<name>`` resolves to a Skill that actually exists, in any of the three
places Skills are kept:

  * ``skills/<name>.md``                      (flat canonical copy)
  * ``shared/skills/<name>/SKILL.md``         (portable copy)
  * ``plugins/beacon/skills/<name>/SKILL.md`` (plugin copy)

The three-place lookup is deliberate: a Skill present in only one of them is
a *different* problem, already covered by
``tests/test_shared_skills_canonical_parity.py``. This guard answers one
question only — "does the thing we tell an AI to run exist at all?" — so that
a failure here is never ambiguous about its cause.

## Run modes

  --warn   (default) print findings, exit 0.  For pre-commit.
  --strict           print findings, exit 1.  For the CI gate.
  --json             machine-readable report on stdout.

Deliberately dependency-free (no pytest): ``ci-strict-drift-guards.sh`` is
also invoked from the lint-docs job, where pytest is not installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Where a Skill may live. A name resolving in ANY of these counts as existing.
SKILL_LOCATIONS = (
    "skills/{name}.md",
    "shared/skills/{name}/SKILL.md",
    "plugins/beacon/skills/{name}/SKILL.md",
)

# Production surfaces that can put a slash command in front of an AI.
#
# The Skill directories are in this list on purpose, and they are the reason the
# guard earns its keep: **a Skill telling an AI to run another Skill is the most
# common shape this defect takes**, not a Python f-string. e-7069 had to remove
# 9 such references by hand, in each of the three places Skills live (27 edits).
# A first version of this guard scanned only the code directories; an injection
# test then put `/beacon-<deleted>` into all three copies of one Skill and the
# whole strict-guard suite stayed green. Leaving them out would have made the
# green line ("every reference in production code resolves") read far wider than
# what was actually checked — and a green that overstates its coverage is worse
# than no guard, because people stop looking.
#
# Order is only for stable output.
SCAN_DIRS = (
    "bin", "lib", "server", "channel", "scripts", "beacon_cli", "desktop",
    # the three places a Skill lives; `shared` and `plugins` are walked whole
    # because everything under them is shipped instruction text
    "skills", "shared", "plugins",
)
SCAN_SUFFIXES = (".py", ".sh", ".mjs", ".js", ".md", ".html")

# Not production instruction surfaces:
#   tests/      — fixtures legitimately name removed Skills to pin behaviour
#   .trash/ .archive/ — local move-to-delete convention dirs
#   CHANGELOG.md — a historical record; past entries MUST keep old names
#   mocks/ mockups/ — static design artefacts, never executed
EXCLUDE_PARTS = {".git", ".trash", ".archive", "tests", "mocks", "mockups", "node_modules"}
EXCLUDE_NAMES = {"CHANGELOG.md"}

# ---------------------------------------------------------------------------
# Allowlist — references that are prose about history, not an invocation
# ---------------------------------------------------------------------------
#
# Keyed by (file, skill). Every row carries WHY, because the row itself is the
# thing a reviewer has to judge. Two rules make the list safe:
#
#   1. A row that no longer matches anything is reported as STALE and fails the
#      strict run. That is the specific failure this repo keeps paying for: an
#      exemption outliving the thing it exempted silently re-opens the hole it
#      covered. Same posture as KNOWN_GAPS in check-cli-env-parity.py.
#   2. Rows are exact pairs, never wildcards — a real regression in one of
#      these files on a DIFFERENT Skill still fails.
#
# The ``pending e-7070`` rows are deliberate: e-7069 removed the entry points
# while the body stayed, so the body still names the Skills it used to launch.
# When e-7070 deletes those files these rows go stale and this guard will say
# so — which is the breadcrumb an independent reviewer of PR #827 asked for
# (a chain reachable from the files a maintainer actually opens first).
KNOWN_PROSE_REFERENCES = {
    # --- Trek body, pending removal in e-7070 -----------------------------
    ("lib/trek.py", "beacon-trek-execute"):
        "pending e-7070: Trek body still describes the Skill it used to launch",
    ("lib/trek.py", "beacon-trek-pulse"):
        "pending e-7070: Trek body still describes the Skill it used to launch",
    ("lib/cmd_trek.py", "beacon-trek-execute"):
        "pending e-7070: Trek CLI handlers, orphaned by e-7069, not yet deleted",
    ("lib/cmd_trek.py", "beacon-trek-pulse"):
        "pending e-7070: Trek CLI handlers, orphaned by e-7069, not yet deleted",
    ("lib/cmd_trek.py", "beacon-trek-review"):
        "pending e-7070: Trek CLI handlers, orphaned by e-7069, not yet deleted",
    ("lib/trek_scheduler.py", "beacon-trek-execute"):
        "pending e-7070: scheduler text; the producer itself is stopped in e-7070",
    ("lib/trek_scheduler.py", "beacon-trek-review"):
        "pending e-7070: scheduler text; the producer itself is stopped in e-7070",
    ("server/routers_treks.py", "beacon-trek-execute"):
        "pending e-7070: Trek router, unreachable for creation since e-7079",
    ("server/routers_treks.py", "beacon-trek-pulse"):
        "pending e-7070: Trek router, unreachable for creation since e-7079",
    ("server/routers_treks.py", "beacon-trek-review"):
        "pending e-7070: Trek router, unreachable for creation since e-7079",

    # --- Skill prose, found when the scan widened to the Skill dirs -------
    # Two kinds, both genuinely prose rather than an instruction. Listed per
    # (file, skill) and per copy, because the three Skill copies are separate
    # files and a regression in only one of them must still fail.
    #
    #   (a) history: a migration note recording that the old dm-reply command
    #       was folded into this Skill on 2026-06-10. Removing the name would
    #       delete the only record of where it went — the opposite of helpful.
    #   (b) a Skill that is planned but not written (the bus-budget one, marked
    #       将来 in the text itself, so a reader is not misled). If it is ever
    #       created these rows go stale and the guard says so.
    #
    # Note the spelling here: these comments name the Skills WITHOUT a leading
    # slash on purpose. This file is itself scanned, so quoting the dangling
    # spelling would make the guard report its own documentation — it did, on
    # the first attempt. Same reason the inversion harness composes its fake
    # names at runtime instead of writing them as literals.
    ("skills/beacon-dm-send.md", "beacon-dm-reply"):
        "prose/history: migration note recording that beacon-dm-reply was "
        "folded into this Skill on 2026-06-10",
    ("skills/beacon-dm-send.md", "beacon-bus-budget"):
        "prose/planned: listed as 将来 in the text; budget is CLI-only today",
    ("shared/skills/beacon-dm-send/SKILL.md", "beacon-dm-reply"):
        "prose/history: migration note recording that beacon-dm-reply was "
        "folded into this Skill on 2026-06-10",
    ("shared/skills/beacon-dm-send/SKILL.md", "beacon-bus-budget"):
        "prose/planned: listed as 将来 in the text; budget is CLI-only today",
    ("plugins/beacon/skills/beacon-dm-send/SKILL.md", "beacon-dm-reply"):
        "prose/history: migration note recording that beacon-dm-reply was "
        "folded into this Skill on 2026-06-10",
    ("plugins/beacon/skills/beacon-dm-send/SKILL.md", "beacon-bus-budget"):
        "prose/planned: listed as 将来 in the text; budget is CLI-only today",
    # --- Pre-existing, found BY this guard on its first run ---------------
    # Not introduced by ms-185. Recorded rather than fixed here so the guard
    # can land green; fixing them is their own (small) change.
    ("beacon_cli/skills_helpers/__init__.py", "beacon-dm-reply"):
        "pre-existing: names a Skill that does not exist (reply folded into "
        "beacon-dm-send). Found by this guard's first run, not by ms-185.",
    ("beacon_cli/skills_helpers/dm_reply.py", "beacon-dm-reply"):
        "pre-existing: same dangling beacon-dm-reply name as __init__.py",
    ("server/routers_projects.py", "beacon-bus-inbox-hook"):
        "pre-existing: prose names the inbox-hook FILE with a leading slash, "
        "so it reads as a Skill. Harmless, but real drift — left as found.",
}


# A Skill slash-command reference, as it appears when something tells a human
# or an AI to RUN it: "`/beacon-foo`", "invoke /beacon-foo", "launch: /beacon-foo".
#
# The hard part is NOT finding "/beacon-" — it is excluding the three far more
# common spellings that are not commands at all. A first draft matched all of
# them and produced 120+ false positives, which is its own failure: a guard
# that cries wolf gets switched off, so a loose match is no safer than none.
#
#   URL host        https://beacon-ai.dev          -> "/" preceded by "/"
#   repo path       bin/beacon-bus-inbox-hook.py   -> "/" preceded by "n"
#   filesystem path /tmp/beacon-bus-channel.log    -> "/" preceded by "p"
#
# All three put a path character immediately before the slash, while a command
# is always at a token boundary (line start, space, backtick, quote, paren, …).
# So the discriminator is the character BEFORE the slash, plus "the name has no
# file extension" — a reference ending in .py/.sh/.log/.dev is a path by
# construction, whatever precedes it.
REF_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s`'\"(\[*=,:]))"      # token boundary, never a path char
    r"/(beacon-[a-z0-9]+(?:-[a-z0-9]+)*)"
    r"(?![A-Za-z0-9_-])"                      # whole token
    r"(?!\.[A-Za-z])"                         # not "<name>.py" / ".dev" / ".log"
)

# Placeholders in prose about the PATTERN ("skills/*.md -> '/beacon-xxx'"), not
# references to a Skill. Listed explicitly so a real Skill can never be
# silenced by a wildcard rule.
PLACEHOLDER_NAMES = {"beacon-xxx", "beacon-foo", "beacon-name", "beacon-skill"}


# A trailing-hyphen or wildcard spelling is prose about a FAMILY of Skills
# ("the /beacon-trek-* Skills are not invoked"), not a reference to one. Those
# are not resolvable by construction and are not what this guard is about.
def _is_family_prose(name: str) -> bool:
    return name.endswith("-") or "*" in name or name in PLACEHOLDER_NAMES


def _declared_trigger_aliases() -> set:
    """Alias names that Skills declare for themselves in their frontmatter.

    A Skill's frontmatter carries a ``triggers:`` list — the phrases that invoke
    it — and short aliases live there: ``beacon-session-start`` declares
    ``/beacon-start``, ``beacon-dm-respond`` declares ``/beacon-dm-approve`` and
    ``/beacon-dm-deny``. **These are declarations, not references.** There is no
    file named ``skills/beacon-start.md`` and there is not meant to be.

    So an alias is a real answer to this guard's question — "does the thing we
    tell an AI to run exist?" — and must count as existing, in frontmatter and
    in body prose alike ("サブコマンド hint (`/beacon-dm-approve`) で起動された
    場合…" is a Skill describing its own alias).

    Resolving them *by nature* rather than listing them in the allowlist matters:
    50 Skills declare aliases, so an allowlist would need dozens of rows that say
    nothing except "this one is fine", and each new alias would turn the guard
    red until someone added a row. A guard that cries wolf gets switched off,
    which is the same failure as no guard. (CORE doc: 免除とガードは「性質」で
    閉じる — 狭すぎても緩すぎても破れる.)
    """
    aliases = set()
    for loc in SKILL_LOCATIONS:
        pattern = loc.format(name="*")
        for path in REPO.glob(pattern):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not text.startswith("---"):
                continue
            end = text.find("\n---", 3)
            if end == -1:
                continue
            front = text[3:end]
            in_triggers = False
            for line in front.splitlines():
                if re.match(r"^\w[\w-]*:", line):
                    in_triggers = line.startswith("triggers:")
                    continue
                if not in_triggers:
                    continue
                m = re.match(r"\s*-\s*/(beacon-[a-z0-9][a-z0-9-]*)\s*$", line)
                if m:
                    aliases.add(m.group(1))
    return aliases


_ALIAS_CACHE: "set | None" = None


def skill_exists(name: str) -> bool:
    """True if ``name`` resolves to a Skill file OR to a declared alias."""
    if any((REPO / loc.format(name=name)).exists() for loc in SKILL_LOCATIONS):
        return True
    global _ALIAS_CACHE
    if _ALIAS_CACHE is None:
        _ALIAS_CACHE = _declared_trigger_aliases()
    return name in _ALIAS_CACHE


def iter_production_files():
    for d in SCAN_DIRS:
        base = REPO / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            if path.name in EXCLUDE_NAMES:
                continue
            if EXCLUDE_PARTS & set(path.relative_to(REPO).parts):
                continue
            yield path


def collect(repo_relative_only: bool = True) -> dict:
    """Return {ok, dangling:[{file,line,skill,text}], scanned, references}."""
    dangling: list[dict] = []
    allowed_seen = set()
    scanned = 0
    references = 0
    for path in iter_production_files():
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "/beacon-" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in REF_RE.finditer(line):
                name = match.group(1)
                if _is_family_prose(name):
                    continue
                references += 1
                if skill_exists(name):
                    continue
                rel = str(path.relative_to(REPO)) if repo_relative_only else str(path)
                key = (rel, name)
                if key in KNOWN_PROSE_REFERENCES:
                    allowed_seen.add(key)
                    continue
                dangling.append({
                    "file": rel,
                    "line": lineno,
                    "skill": name,
                    "text": line.strip()[:160],
                })
    # A row that matched nothing is stale: the reference it excused is gone, so
    # from now on it would excuse a future one silently. Report it as failure.
    stale = []
    for (f, s) in sorted(KNOWN_PROSE_REFERENCES):
        if (f, s) not in allowed_seen:
            stale.append({"file": f, "skill": s,
                          "reason": KNOWN_PROSE_REFERENCES[(f, s)]})
    return {
        "ok": not dangling and not stale,
        "dangling": dangling,
        "stale_allowlist": stale,
        "allowlisted": len(allowed_seen),
        "scanned_files": scanned,
        "references": references,
    }


def render(report: dict) -> str:
    if report["ok"]:
        return (
            "[skill-reference-drift] OK: every /beacon-<skill> reference in "
            "production code resolves to a Skill file or to an alias a Skill "
            "declares for itself "
            f"({report['references']} references across {report['scanned_files']} files; "
            f"{report['allowlisted']} allowlisted prose rows, all still matching; "
            "scanned bin/ lib/ server/ channel/ scripts/ beacon_cli/ desktop/ "
            "and all three Skill dirs — tests/, CHANGELOG.md, mocks/, mockups/ "
            "are excluded on purpose)."
        )
    lines = ["[skill-reference-drift] Drift detected:", ""]
    if report["dangling"]:
        lines.append("  Production code names Skills that do not exist:")
        lines.append("")
        for d in report["dangling"]:
            lines.append(f"  {d['file']}:{d['line']}  ->  /{d['skill']}")
            lines.append(f"      {d['text']}")
        lines.append("")
    if report.get("stale_allowlist"):
        lines.append("  KNOWN_PROSE_REFERENCES rows that no longer match anything")
        lines.append("  (= the reference they excused is gone; drop the row, or it")
        lines.append("   will silently excuse the NEXT one):")
        lines.append("")
        for s in report["stale_allowlist"]:
            lines.append(f"  {s['file']}  ->  /{s['skill']}")
            lines.append(f"      was: {s['reason']}")
        lines.append("")
    lines += [
        "",
        "  Each of these puts a slash command in front of a human or an AI that",
        "  cannot resolve. Fix it one of two ways:",
        "    * restore the Skill (skills/<name>.md + the shared/ and plugins/ copies), or",
        "    * stop naming it — if the instruction is gone, remove the instruction,",
        "      and keep only the disclosure (see _format_trek_retired_block in",
        "      the inbox hook for the shape).",
        "",
        "  tests/, CHANGELOG.md, mocks/ and mockups/ are excluded on purpose:",
        "  they pin or record past behaviour and must keep old names.",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on findings (CI gate)")
    ap.add_argument("--json", action="store_true",
                    help="emit the machine-readable report")
    args = ap.parse_args()

    report = collect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    if report["ok"]:
        return 0
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
