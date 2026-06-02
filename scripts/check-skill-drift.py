#!/usr/bin/env python3
"""Skill drift detector for Beacon (ms-10 e-722).

Compares each ``skills/*.md`` in the repo against the installed copy at
``~/.claude/skills/<name>/skill.md`` and reports drift.

Why this exists
---------------
Skills are the runtime behavior of Claude Code Skills. Editing a Skill in
the repo without re-installing leaves ``~/.claude/skills/`` stale — the
next session on this machine will execute the old Skill while the repo
shows the new one. This is the same drift pattern as e-712 (Tauri dist)
and e-744 (frontend Web/Tauri), just on a different surface.

This script does NOT auto-install; it only reports. Auto-install on
commit is handled by the pre-commit hook when
``BEACON_AUTO_SKILL_INSTALL=1`` is set, and the user-facing repair path
is ``beacon skill install``.

Companion convention
--------------------
Files in ``skills/`` whose name starts with ``_`` are companion docs that
``cmd_skill_install`` places inside the matching skill's directory under
a derived filename (see ``cmd_skill_install`` for the rules). This script
mirrors that placement so the comparison stays meaningful.

Run modes
---------
``--warn`` (default): print mismatches, exit 0 (for pre-commit).
``--strict``        : print mismatches, exit 1 (for CI gate).
``--json``          : machine-readable output (used by ``beacon doctor``).

Exit codes
----------
0 = no drift (or warn-mode with drift)
1 = drift in strict-mode
2 = environment problem (no skills/ directory, etc.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_SRC = ROOT / "skills"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_installed_skill(skill_dir: Path) -> Path:
    """Return the installed skill file inside ``<dir>``.

    Claude Code expects ``SKILL.md`` (uppercase), but historic versions of
    ``cmd_skill_install`` wrote ``skill.md`` (lowercase). On case-insensitive
    file systems (default macOS APFS) both resolve to the same file; on
    Linux they don't. Look for whichever exists, preferring uppercase.
    """
    upper = skill_dir / "SKILL.md"
    if upper.exists():
        return upper
    lower = skill_dir / "skill.md"
    if lower.exists():
        return lower
    # Return upper as the canonical "expected" path even when missing.
    return upper


def _classify(skills_dir: Path) -> tuple[list[Path], list[Path]]:
    """Split skills/*.md into (skill files, companion files)."""
    skills: list[Path] = []
    companions: list[Path] = []
    for p in sorted(skills_dir.glob("*.md")):
        (companions if p.name.startswith("_") else skills).append(p)
    return skills, companions


def _resolve_companion_dest(
    companion: Path, skill_names: list[str], claude_skills: Path,
) -> Path | None:
    """Mirror cmd_skill_install's mapping rule for companions.

    ``_<skill>-<suffix>.md`` -> ``<claude_skills>/<skill>/<suffix>.md``.
    Longest matching skill-name wins. Returns None if no skill matches.
    """
    base = companion.name[1:-3]  # strip leading "_" and trailing ".md"
    match = ""
    for s in skill_names:
        if (base == s or base.startswith(s + "-")) and len(s) > len(match):
            match = s
    if not match:
        return None
    suffix = base[len(match) + 1:] if len(base) > len(match) else "companion"
    if not suffix:
        suffix = "companion"
    return claude_skills / match / f"{suffix}.md"


def collect_drift(
    skills_src: Path = SKILLS_SRC,
    claude_skills: Path | None = None,
) -> dict:
    """Return a structured drift report.

    Shape::

        {
          "ok": bool,
          "skills_dir_missing": bool,
          "no_install_dir": bool,
          "drift": [
            {"name": "beacon-log", "kind": "skill"|"companion",
             "reason": "content_diff"|"not_installed"|"orphan_in_install",
             "repo_path": "...", "installed_path": "..."},
            ...
          ],
        }

    ``orphan_in_install`` flags skills that exist in ``~/.claude/skills``
    but no longer in the repo (likely a renamed skill). It is a softer
    drift; the caller decides whether to warn.
    """
    if claude_skills is None:
        claude_skills = Path(os.path.expanduser("~")) / ".claude" / "skills"
    report = {
        "ok": True,
        "skills_dir_missing": False,
        "no_install_dir": False,
        "drift": [],
    }
    if not skills_src.is_dir():
        report["skills_dir_missing"] = True
        report["ok"] = False
        return report
    if not claude_skills.is_dir():
        # No install dir at all: every repo skill is effectively "not installed".
        # We still flag this so the user knows to run `beacon skill install`.
        report["no_install_dir"] = True
        report["ok"] = False
        skills, _companions = _classify(skills_src)
        for p in skills:
            report["drift"].append({
                "name": p.stem,
                "kind": "skill",
                "reason": "not_installed",
                "repo_path": str(p),
                "installed_path": str(_resolve_installed_skill(claude_skills / p.stem)),
            })
        return report

    skills, companions = _classify(skills_src)
    skill_names = [p.stem for p in skills]
    expected_installed: set[Path] = set()

    for src in skills:
        dest = _resolve_installed_skill(claude_skills / src.stem)
        expected_installed.add(dest)
        if not dest.exists():
            report["drift"].append({
                "name": src.stem,
                "kind": "skill",
                "reason": "not_installed",
                "repo_path": str(src),
                "installed_path": str(dest),
            })
            continue
        if _hash(src) != _hash(dest):
            report["drift"].append({
                "name": src.stem,
                "kind": "skill",
                "reason": "content_diff",
                "repo_path": str(src),
                "installed_path": str(dest),
            })

    for comp in companions:
        dest = _resolve_companion_dest(comp, skill_names, claude_skills)
        if dest is None:
            # Orphan companion in repo (no matching skill). Skill-install
            # itself prints a warning at install time, so re-surface here.
            report["drift"].append({
                "name": comp.name,
                "kind": "companion",
                "reason": "orphan_in_repo",
                "repo_path": str(comp),
                "installed_path": "",
            })
            continue
        expected_installed.add(dest)
        if not dest.exists():
            report["drift"].append({
                "name": f"{dest.parent.name}/{dest.name}",
                "kind": "companion",
                "reason": "not_installed",
                "repo_path": str(comp),
                "installed_path": str(dest),
            })
            continue
        if _hash(comp) != _hash(dest):
            report["drift"].append({
                "name": f"{dest.parent.name}/{dest.name}",
                "kind": "companion",
                "reason": "content_diff",
                "repo_path": str(comp),
                "installed_path": str(dest),
            })

    if report["drift"]:
        report["ok"] = False
    return report


def _format_text(report: dict) -> str:
    if report["skills_dir_missing"]:
        return (
            "[skill-drift] ERROR: skills/ directory not found in repo.\n"
            "             Expected at " + str(SKILLS_SRC) + "\n"
        )
    if report["no_install_dir"]:
        msg = [
            "[skill-drift] ~/.claude/skills/ does not exist.",
            "             Run: beacon skill install",
            "",
        ]
        for d in report["drift"]:
            msg.append(f"  - {d['name']} (not installed)")
        msg.append("")
        return "\n".join(msg)
    if report["ok"]:
        return "[skill-drift] OK: repo skills/ and ~/.claude/skills/ are in sync.\n"

    lines = [
        "[skill-drift] Drift detected between repo skills/ and ~/.claude/skills/:",
        "",
    ]
    for d in report["drift"]:
        reason_label = {
            "content_diff": "content differs",
            "not_installed": "not installed",
            "orphan_in_repo": "no matching skill in repo (rename?)",
            "orphan_in_install": "installed but absent from repo (rename?)",
        }.get(d["reason"], d["reason"])
        lines.append(f"  - [{d['kind']}] {d['name']}: {reason_label}")
        if d["installed_path"]:
            lines.append(f"      repo: {d['repo_path']}")
            lines.append(f"      live: {d['installed_path']}")
    lines.append("")
    lines.append("Repair: beacon skill install")
    lines.append("CORE: this guard is part of ms-10 e-722 (doc & skill auto-sync).")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 on drift (CI mode). Default: exit 0 with warnings.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output (used by `beacon doctor`).")
    parser.add_argument("--skills-src", default=str(SKILLS_SRC),
                        help="Override repo skills/ path (for tests).")
    parser.add_argument("--claude-skills", default=None,
                        help="Override ~/.claude/skills/ path (for tests).")
    args = parser.parse_args(argv)

    skills_src = Path(args.skills_src)
    claude_skills = Path(args.claude_skills) if args.claude_skills else None
    report = collect_drift(skills_src=skills_src, claude_skills=claude_skills)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(_format_text(report))

    if report["skills_dir_missing"]:
        return 2
    if report["ok"]:
        return 0
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
