#!/usr/bin/env python3
"""check-mandated-tools — a mandated tool must actually ship (ms-180 e-7005).

The hole this closes
--------------------
``CLAUDE.md`` declares hard dependencies on tools: 「PR のレビューは必ず ``/review``
を使う」, 「``/beacon-session-end`` を実行する」, 「``beacon pr approve`` を直接呼ばない」.
Those are instructions an AI on ANY machine is required to follow.

But until ms-180 the ``/review`` Skill existed only in the authoring machine's
``~/.claude/skills/`` and not in the repository at all. A clean install on another
machine was therefore ordered to use a tool that was not there — and nothing
detected it. A doc that references a MILESTONE is validated by the capability
ledger's dependency invariant; a doc that references a TOOL was validated by
nothing (SPEC ms-180 § 背景 症状3).

This is the missing check. It reads the repository's own agent instructions, pulls
out the tools they MANDATE, and fails when one of them is not shipped by the repo.

Why it looks for mandates rather than every mention
---------------------------------------------------
Agent instructions also mention tools in passing — examples, alternatives, "or
use X". Failing on those would make the guard noisy and it would be switched off.
A mandate is the thing whose absence actually breaks an instruction, so a line
qualifies only when it carries one of a CLOSED set of mandate markers.

Why the matching is structural, not substring
---------------------------------------------
A loose match here would be worse than no check: the guard would go green because
some unrelated text happened to contain the name, and a green guard is trusted.
So existence is decided by resolving each reference to a concrete artifact —
a Skill file at a known path, or a verb key in the live CLI dispatch surface
(``lib/cli_surface``, the same single source of truth the map-drift lint and the
verb ledger use) — never by searching text for the name.

Exit 0 when every mandated tool is shipped; exit 1 with the list otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "lib"))

import cli_surface  # noqa: E402

#: The repo's own agent-instruction files. The USER's global ~/.claude/CLAUDE.md is
#: deliberately out of scope: this checks what the REPOSITORY orders a clean
#: install to do, which is the thing a clean install can actually be missing.
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")

#: A line states a requirement only if it carries one of these. Closed set, so
#: widening it is a deliberate edit rather than an accident of phrasing.
MANDATE_MARKERS = (
    "必ず", "禁止", "しない", "ならない", "must", "MUST", "Must",
    "never", "Never", "NEVER", "always", "Always", "ALWAYS", "required",
)
# NOTE on 「ならない」: the first version used the full 「してはならない」 and missed
# 「使ってはならない」 — the form these instructions actually use. The verb varies, the
# tail does not, so the tail is the marker. Found by this guard's own test suite,
# which is the point of having one: a prohibition that the detector cannot see is
# a mandate the guard silently stops covering.

#: ``/skill-name`` — a Skill invocation. Anchored so a path fragment such as
#: ``lib/review`` or ``a/b`` cannot masquerade as one: the slash must start the
#: token (line start, whitespace, or an opening quote/backtick/bracket).
_SKILL_REF = re.compile(r"(?:(?<=^)|(?<=[\s`\"'(（「]))/([a-z][a-z0-9-]{2,})")

#: ``beacon <verb>`` — a CLI verb, with or without backticks.
_CLI_REF = re.compile(r"beacon\s+([a-z][a-z0-9-]{1,})")

#: Words that follow a slash in prose but are not Skills. Keeping this explicit
#: (rather than loosening the regex) means an unexpected token is reported, not
#: silently ignored — the guard errs toward speaking up.
_NOT_A_SKILL = frozenset({
    "or", "and", "the", "a", "an", "etc", "json", "md", "py", "sh", "yaml",
    "pr", "ms", "ai", "ux", "ci", "cli", "api", "dm", "mcp",
})


def skill_exists(name: str, root: str = "") -> bool:
    """True when the repository at ``root`` ships this Skill.

    Canonical layout (``shared/skills/<name>/SKILL.md``) or the legacy flat file
    (``skills/<name>.md``). Resolved as a path, never by searching text — and note
    the spelling difference between the two layouts, which is exactly why a
    name-based grep misses one of them.

    ``root`` must be threaded from the caller rather than read off the module:
    the first version of this function ignored it and resolved against the real
    repository, so pointing the checker at ANY other tree reported "nothing
    missing" — the guard was green for the wrong reason, which is the exact
    failure its own inversion test exists to catch (and did).
    """
    base = root or _REPO
    canonical = os.path.join(base, "shared", "skills", name, "SKILL.md")
    legacy = os.path.join(base, "skills", f"{name}.md")
    return os.path.isfile(canonical) or os.path.isfile(legacy)


def cli_verb_exists(noun: str, verbs: set) -> bool:
    """True when ``beacon <noun>`` resolves on the live dispatch surface.

    Dispatch keys are ``<noun>_<subaction>`` (``pr_approve``) or a bare noun
    (``status``), so a mandated NOUN is satisfied by the noun itself or by any
    key under it. Compared as a set membership / prefix over the enumerated
    surface — not a substring search of source text.
    """
    token = noun.replace("-", "_")
    return token in verbs or any(v.startswith(token + "_") for v in verbs)


def extract_mandates(text: str) -> dict:
    """Return ``{"skills": [...], "cli": [...]}`` — the tools MANDATED by ``text``.

    Scans per line and keeps a reference only when the line carries a mandate
    marker, so passing mentions and examples are not treated as requirements.
    """
    skills, cli = [], []
    for line in text.splitlines():
        if not any(marker in line for marker in MANDATE_MARKERS):
            continue
        for name in _SKILL_REF.findall(line):
            if name not in _NOT_A_SKILL and name not in skills:
                skills.append(name)
        for noun in _CLI_REF.findall(line):
            if noun not in cli:
                cli.append(noun)
    return {"skills": skills, "cli": cli}


def audit(repo: str = "") -> dict:
    root = repo or _REPO
    verbs = cli_surface.enumerate_cli_verbs(
        os.path.join(root, "lib", "commands.py"))
    missing, checked, scanned = [], 0, []
    for filename in INSTRUCTION_FILES:
        path = os.path.join(root, filename)
        if not os.path.isfile(path):
            continue
        scanned.append(filename)
        with open(path, encoding="utf-8") as fh:
            mandates = extract_mandates(fh.read())
        for name in mandates["skills"]:
            checked += 1
            if not skill_exists(name, root):
                missing.append({"file": filename, "kind": "skill",
                                "ref": f"/{name}"})
        for noun in mandates["cli"]:
            checked += 1
            if not cli_verb_exists(noun, verbs):
                missing.append({"file": filename, "kind": "cli",
                                "ref": f"beacon {noun}"})
    return {"scanned": scanned, "checked": checked, "missing": missing}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo", default="", help="repository root (default: this one)")
    args = ap.parse_args()

    result = audit(args.repo)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["missing"] else 0

    if result["missing"]:
        print("[mandated-tools] エージェント指示が必須と命じている道具が "
              "リポジトリに存在しません:")
        for row in result["missing"]:
            print(f"  - {row['file']}: {row['ref']} ({row['kind']}) が見つかりません")
        print("")
        print("  これは clean install した別マシンで「必須と命じられた道具が無い」"
              "状態になります (ms-180 e-7005 の実害)。")
        print("  Skill なら shared/skills/<name>/SKILL.md (+ skills/<name>.md) を"
              "追加し、CLI verb なら両フロントに配線してください。")
        print("  指示の側が間違っているなら、その行の必須表現を外してください。")
        return 1

    print(f"[mandated-tools] OK: {', '.join(result['scanned']) or '(none)'} が "
          f"必須と命じる道具 {result['checked']} 件はすべてリポジトリに存在します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
