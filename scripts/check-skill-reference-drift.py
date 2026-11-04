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
# ``.yaml`` is here for ``shared/skills/<name>/clients/*.yaml`` — a SECOND place
# where Skills declare their trigger aliases (49 files do). An independent AX
# review of PR #827 found that leaving it out broke the guard in both
# directions: a dangling reference planted in a client yaml was never found,
# and an alias declared only there would make every legitimate reference to it
# go red. Measured at the time: ``skills/*.md`` declared triggers in 50 files,
# ``shared|plugins/**/SKILL.md`` in **0**, ``clients/*.yaml`` in 49 — so the
# first version of this guard, which read only SKILL.md frontmatter, was in
# practice reading one of the two real declaration sites.
SCAN_SUFFIXES = (".py", ".sh", ".mjs", ".js", ".md", ".html", ".yaml")

# Not production instruction surfaces:
#   tests/      — fixtures legitimately name removed Skills to pin behaviour
#   .trash/ .archive/ — local move-to-delete convention dirs
#   CHANGELOG.md — a historical record; past entries MUST keep old names
#   mocks/ mockups/ — static design artefacts, never executed
EXCLUDE_PARTS = {".git", ".trash", ".archive", "tests", "mocks", "mockups", "node_modules"}
#   this file      — see below
EXCLUDE_NAMES = {"CHANGELOG.md", "check-skill-reference-drift.py"}

# Why this file excludes itself, and why that is not the usual rot-prone
# self-exemption: a guard that documents a defect class has to NAME the defects
# (every comment below explaining "the slash was deleted from ``launch:
# beacon-trek-execute``" is a reference by construction), and this file is not
# an instruction surface — nothing injects it into an AI's context. Writing the
# names in a mangled spelling instead, which an earlier version did, is worse:
# it makes the documentation harder to read AND models the exact move the AX
# review flagged ("change the spelling and the guard stops looking").
#
# The exemption is bounded and tested rather than trusted: it is keyed on this
# one filename, not on ``scripts/``, and the inversion harness's first mutation
# injects a dangling reference into a different file and requires RED — so
# "scripts/ is still scanned" cannot silently stop being true.

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
    # --- Trek body: 10 rows removed in e-7070 ------------------------------
    # 全 10 行が「e-7070 で消す予定」として置かれていた免除で、その e-7070 が
    # 実際に本体を落としたので対象を失った。この checker は一致しなくなった行を
    # stale として赤くする (= 次の 1 件を黙って免除してしまうため)。消えたのは
    # lib/trek.py / lib/cmd_trek.py / lib/trek_scheduler.py (本体ごと .trash/ へ)
    # と server/routers_treks.py (読み取り 3 ルートだけに縮小、Skill への言及なし)。

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
    r"(?!/)"                                  # not a path PREFIX: "/beacon-x/y"
)

# Two shapes the boundary rule above cannot tell apart from a command, found by
# an independent AX review that injected them and watched the guard report them:
#
#   markdown link target   [FAQ](/beacon-faq)        -> "(" before, no ".ext"
#   quoted absolute path   `/beacon-config/x.json`   -> "`" before, no ".ext"
#
# The second is handled by the ``(?!/)`` lookahead above (a command is never
# itself a path prefix). The first needs its own rule: a link target sits
# immediately inside ``](...)``, which a real invocation never does. Without
# this the guard cries wolf on ordinary bilingual docs — and a guard that cries
# wolf gets switched off, which is the same failure as no guard at all.
MARKDOWN_LINK_RE = re.compile(r"\]\(\s*/(beacon-[a-z0-9][a-z0-9-]*)")

# The tool-call shape. An AI does not only read "/beacon-x" — it is handed
# ``Skill(skill="beacon-x")`` by its harness, and config files carry
# ``skill: beacon-x``. Same hazard, no slash anywhere.
TOOLCALL_RE = re.compile(
    r"""skill\s*[=:]\s*["']?/?(beacon-[a-z0-9][a-z0-9-]*)""", re.IGNORECASE)

# The bare-name shape, which is how PR #827 slipped past the first version of
# this guard: the earlier review fix removed the leading slash from five
# comments in the inbox hook (``launch: beacon-trek-execute <id>``), leaving the
# imperative text intact and the guard silent. "Change the spelling and the
# guard stops looking" is exactly the false safety this guard exists to prevent.
#
# A bare ``beacon-<name>`` token on its own CANNOT be used — measured on this
# repo, 35 distinct unresolved tokens / 337 hits, and nearly all of them are not
# Skills at all (``beacon-find-root`` is an executable, ``beacon-review-gate`` a
# CI check, ``beacon-tooltip-icon`` a CSS class, ``beacon-hook-*`` hook ids).
# Reporting those would flood the report.
#
# So the discriminator is an IMPERATIVE on the same line: the hazard is text
# that tells someone to RUN the thing. With the imperative requirement the same
# scan yields 2 distinct names / 4 hits and **zero** false positives — and it
# catches both ``launch: beacon-trek-execute`` and ``force beacon-trek-review
# invoke``. ``run`` and ``Skill`` were deliberately left OUT of the imperative
# list: they matched ``gcloud run services update beacon-api-prod`` and a
# listing line ``[skill] beacon-backoffice-payroll``, i.e. they bought nothing
# but noise.
BARE_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s`'\"(\[*=,:]))"
    r"(beacon-[a-z0-9]+(?:-[a-z0-9]+){1,})"   # >= 2 segments: a Skill-shaped name
    r"(?![A-Za-z0-9_-])(?!\.[A-Za-z])(?!/)"
)
IMPERATIVE_RE = re.compile(
    r"(launch|invoke|invoking|起動|呼び出|即時|MUST)", re.IGNORECASE)

# An allowlist row in THIS file is a key, i.e. data — not an instruction. The
# rows necessarily spell out the dangling names they excuse, so without this the
# bare-name rule reports the allowlist table itself. Keyed on the row SHAPE
# rather than on this file's name, so a real imperative anywhere in scripts/ is
# still caught.
ALLOWLIST_ROW_RE = re.compile(r'^\s*\("[^"]+",\s*"beacon-[a-z0-9-]+"\):')

# Placeholders in prose about the PATTERN ("skills/*.md -> '/beacon-xxx'"), not
# references to a Skill. Listed explicitly so a real Skill can never be
# silenced by a wildcard rule.
PLACEHOLDER_NAMES = {"beacon-xxx", "beacon-foo", "beacon-name", "beacon-skill",
                     "beacon-x", "beacon-y"}


# A trailing-hyphen or wildcard spelling is prose about a FAMILY of Skills
# ("the /beacon-trek-* Skills are not invoked"), not a reference to one. Those
# are not resolvable by construction and are not what this guard is about.
def _is_family_prose(name: str) -> bool:
    return name.endswith("-") or "*" in name or name in PLACEHOLDER_NAMES


def _skill_own_files(skill: str) -> set:
    """Every path that belongs to one Skill (its three copies + its clients)."""
    own = {loc.format(name=skill) for loc in SKILL_LOCATIONS}
    own |= {str(q.relative_to(REPO))
            for q in REPO.glob(f"shared/skills/{skill}/**/*")
            if q.is_file()}
    own |= {str(q.relative_to(REPO))
            for q in REPO.glob(f"plugins/beacon/skills/{skill}/**/*")
            if q.is_file()}
    return own


def _declared_trigger_aliases() -> dict:
    """Alias -> {paths allowed to use it}. Built from both declaration sites.

    A Skill's frontmatter carries a ``triggers:`` list — the phrases that invoke
    it — and short aliases live there: ``beacon-session-start`` declares
    ``/beacon-start``, ``beacon-dm-respond`` declares ``/beacon-dm-approve`` and
    ``/beacon-dm-deny``. **These are declarations, not references.** There is no
    file named ``skills/beacon-start.md`` and there is not meant to be. So an
    alias is a real answer to this guard's question — "does the thing we tell an
    AI to run exist?" — and must count as existing.

    Resolving them *by nature* rather than listing them in the allowlist matters:
    50 Skills declare aliases, so an allowlist would need dozens of rows that say
    nothing except "this one is fine", and each new alias would turn the guard
    red until someone added a row. A guard that cries wolf gets switched off,
    which is the same failure as no guard. (CORE doc: 免除とガードは「性質」で
    閉じる — 狭すぎても緩すぎても破れる.)

    Two things an independent AX review of PR #827 made necessary here.

    **(1) The return value is scoped, not a flat set.** The first version
    returned ``set[str]`` and ``skill_exists`` answered ``name in aliases`` —
    a flat, global namespace. The reviewer demonstrated the consequence: adding
    one line to the ``triggers:`` of ANY unrelated Skill silently legitimised a
    genuinely dangling reference anywhere else in the repo, and the guard
    reported OK. That re-opens the very hazard this guard exists to close,
    through a different door. An alias now only answers for references inside
    the files of the Skill that declared it; used anywhere else it is reported
    (as its own category, so the message never confuses "this name does not
    exist" with "this name belongs to another Skill").

    **(2) Parsing is delegated to the one parser the repo already has.**
    ``commands_shared._parse_frontmatter`` returns block lists AND inline lists
    as ``list[str]`` and strips quotes, and was hardened once (PE dogfood
    2026-06-10) for exactly the "split on the first colon" class of bug. The
    hand-rolled line reader this replaced silently missed ``- "/beacon-x"`` —
    the quoted form, which is what all 49 ``clients/*.yaml`` files use — so a
    third frontmatter grammar would have shipped with a known blind spot.
    """
    sys.path.insert(0, str(REPO / "lib"))
    try:
        from commands_shared import _parse_frontmatter  # type: ignore
    except Exception:                                    # pragma: no cover
        # lint-docs runs this script with no deps; if lib is unavailable the
        # honest answer is "no aliases known", which errs toward REPORTING a
        # reference rather than silently excusing it.
        return {}

    registry: dict = {}

    def _add(skill: str, raw) -> None:
        if not isinstance(raw, list):
            return
        for item in raw:
            name = str(item).strip().strip('"').strip("'").lstrip("/")
            if not name.startswith("beacon-"):
                continue
            registry.setdefault(name, set()).update(_skill_own_files(skill))

    for loc in SKILL_LOCATIONS:
        for path in REPO.glob(loc.format(name="*")):
            skill = (path.stem if path.name.endswith(".md") and path.parent.name == "skills"
                     else path.parent.name)
            try:
                meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            _add(skill, meta.get("triggers"))

    # Second declaration site: shared/skills/<name>/clients/*.yaml. Not
    # frontmatter (no leading ---), so feed the parser a synthetic document.
    for path in REPO.glob("shared/skills/*/clients/*.yaml"):
        skill = path.parent.parent.name
        try:
            body = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        meta, _ = _parse_frontmatter("---\n" + body.rstrip() + "\n---\n")
        _add(skill, meta.get("triggers"))

    return registry


_ALIAS_CACHE: "dict | None" = None


def _aliases() -> dict:
    global _ALIAS_CACHE
    if _ALIAS_CACHE is None:
        _ALIAS_CACHE = _declared_trigger_aliases()
    return _ALIAS_CACHE


def skill_exists(name: str) -> bool:
    """True if ``name`` resolves to a Skill FILE (alias scoping is separate).

    Kept narrow on purpose: "is there a Skill of this name" and "may this file
    use this alias" are different questions, and collapsing them is what made
    the alias namespace global in the first version.
    """
    return any((REPO / loc.format(name=name)).exists() for loc in SKILL_LOCATIONS)


def resolve(name: str, referencing_file: str) -> str:
    """``"skill"`` | ``"alias"`` | ``"alias-out-of-scope"`` | ``"dangling"``."""
    if skill_exists(name):
        return "skill"
    owners = _aliases().get(name)
    if owners is None:
        return "dangling"
    return "alias" if referencing_file in owners else "alias-out-of-scope"


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


def _references_in_line(line: str):
    """Yield ``(name, shape)`` for every "run this Skill" reference on a line.

    Three shapes, because an instruction to run a Skill reaches an AI in three
    spellings and the hazard is identical in all of them (AX review of #827):
    the slash command a human types, the tool call a harness emits, and a bare
    name in imperative prose. The first version matched only the slash, which
    is how five imperative comments survived by having their slash deleted.
    """
    seen = set()
    link_targets = {(m.start(1), m.group(1)) for m in MARKDOWN_LINK_RE.finditer(line)}
    for m in REF_RE.finditer(line):
        if (m.start(1), m.group(1)) in link_targets:
            continue                      # markdown link target, not a command
        if (m.group(1), "slash") not in seen:
            seen.add((m.group(1), "slash"))
            yield m.group(1), "slash"
    for m in TOOLCALL_RE.finditer(line):
        if (m.group(1), "tool-call") not in seen:
            seen.add((m.group(1), "tool-call"))
            yield m.group(1), "tool-call"
    if IMPERATIVE_RE.search(line) and not ALLOWLIST_ROW_RE.match(line):
        for m in BARE_RE.finditer(line):
            if (m.group(1), "bare-imperative") not in seen:
                seen.add((m.group(1), "bare-imperative"))
                yield m.group(1), "bare-imperative"


def collect(repo_relative_only: bool = True) -> dict:
    """Return the report: dangling / alias-out-of-scope / stale allowlist rows."""
    dangling: list[dict] = []
    out_of_scope: list[dict] = []
    allowed_seen = set()
    scanned = 0
    references = 0
    for path in iter_production_files():
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "beacon-" not in text:
            continue
        rel = str(path.relative_to(REPO)) if repo_relative_only else str(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, shape in _references_in_line(line):
                if _is_family_prose(name):
                    continue
                references += 1
                verdict = resolve(name, rel)
                if verdict == "skill" or verdict == "alias":
                    continue
                key = (rel, name)
                if key in KNOWN_PROSE_REFERENCES:
                    allowed_seen.add(key)
                    continue
                row = {
                    "file": rel,
                    "line": lineno,
                    "skill": name,
                    "shape": shape,
                    "text": line.strip()[:160],
                }
                if verdict == "alias-out-of-scope":
                    row["declared_by"] = sorted(
                        {o.split("/")[-1].replace(".md", "")
                         for o in _aliases().get(name, set())
                         if o.startswith("skills/")})
                    out_of_scope.append(row)
                else:
                    dangling.append(row)
    # A row that matched nothing is stale: the reference it excused is gone, so
    # from now on it would excuse a future one silently. Report it as failure.
    stale = []
    for (f, sk) in sorted(KNOWN_PROSE_REFERENCES):
        if (f, sk) not in allowed_seen:
            stale.append({"file": f, "skill": sk,
                          "reason": KNOWN_PROSE_REFERENCES[(f, sk)]})
    return {
        "ok": not dangling and not out_of_scope and not stale,
        "dangling": dangling,
        "alias_out_of_scope": out_of_scope,
        "stale_allowlist": stale,
        "allowlisted": len(allowed_seen),
        "scanned_files": scanned,
        "references": references,
        "aliases_known": len(_aliases()),
    }


def render(report: dict) -> str:
    if report["ok"]:
        return (
            "[skill-reference-drift] OK: every reference checked resolves to a "
            "Skill file, or to an alias declared by the very Skill whose files "
            "contain the reference.\n"
            f"  checked {report['references']} references across "
            f"{report['scanned_files']} files "
            f"({report['aliases_known']} declared aliases, "
            f"{report['allowlisted']} allowlisted prose rows all still matching).\n"
            "  shapes checked: `/beacon-<name>` slash commands; "
            "`skill=`/`skill:` tool calls; bare `beacon-<name>` ON A LINE WITH "
            "AN IMPERATIVE (launch / invoke / 起動 / 呼び出 / 即時 / MUST).\n"
            "  NOT checked: a bare name in prose with no imperative on the line; "
            "any spelling in " + "/".join(x for x in SCAN_SUFFIXES) + " files "
            "outside " + " ".join(d + "/" for d in SCAN_DIRS) + "; and tests/, "
            "CHANGELOG.md, mocks/, mockups/, this script itself (excluded on "
            "purpose — see EXCLUDE_NAMES)."
        )
    lines = ["[skill-reference-drift] Drift detected:", ""]
    if report["dangling"]:
        lines.append("  Production code names Skills that do not exist:")
        lines.append("")
        for d in report["dangling"]:
            lines.append(f"  {d['file']}:{d['line']}  ->  /{d['skill']}")
            lines.append(f"      {d['text']}")
        lines.append("")
    if report.get("alias_out_of_scope"):
        lines.append("  An alias is used outside the Skill that declared it:")
        lines.append("")
        for d in report["alias_out_of_scope"]:
            owner = ", ".join(d.get("declared_by") or []) or "(unknown)"
            lines.append(f"  {d['file']}:{d['line']}  ->  {d['skill']}"
                         f"   [{d['shape']}, declared by {owner}]")
            lines.append(f"      {d['text']}")
        lines.append("")
        lines.append("  An alias answers for its OWN Skill's files only. A flat,")
        lines.append("  global alias namespace meant one line added to any")
        lines.append("  unrelated Skill silently excused a dangling reference")
        lines.append("  anywhere in the repo — so either name the Skill itself")
        lines.append("  here, or declare this alias on the Skill that owns it.")
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
