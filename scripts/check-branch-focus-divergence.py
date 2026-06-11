#!/usr/bin/env python3
"""Branch / workspace divergence warning (ms-65 e-1481).

Warns when the current session is in the **main project root**, sitting on
a **non-default branch**, while **other live bridges share the cwd** — the
unsafe combination that caused the 2026-06-10 incident (= one session's
`git checkout ms-64-...` silently moved another session's HEAD to a
mismatched branch).

The structural fix in e-1477 (cwd-aware ``beacon milestone start``) makes
the dangerous setup hard to enter accidentally, but cannot prevent users
who run plain `git checkout` outside of a worktree. This script is the
detection-side forcing function: surface the warning at session-start
and pre-commit so the user can react before silent breakage happens.

Always exits 0 — warn-only, never blocks the calling flow.

Call sites:
  * scripts/hooks/pre-commit (Check 8)
  * /beacon-session-start Skill (Step 1k)
  * Anyone running it manually from a cwd they're unsure about
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def _run(args: list[str], timeout: int = 5) -> str:
    """Run a process, return stripped stdout on success, "" on any failure."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ""


def is_in_main_project_root() -> bool:
    """True if cwd is the main project root (= git-dir == git-common-dir)."""
    gd = _run(["git", "rev-parse", "--git-dir"])
    cd = _run(["git", "rev-parse", "--git-common-dir"])
    if not gd or not cd:
        return False  # No git repo → no warning makes sense
    return os.path.abspath(gd) == os.path.abspath(cd)


def detect_default_branch() -> str:
    """Try to detect the repo's default branch; fall back to "main"."""
    out = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    if out:
        # Format: refs/remotes/origin/<branch>
        parts = out.rsplit("/", 1)
        if len(parts) == 2 and parts[1]:
            return parts[1]
    return "main"


def count_live_bridges_same_cwd(cwd: str) -> int:
    """Count live bridges whose ``cwd`` field equals ``cwd`` (incl. self)."""
    raw = _run(["beacon", "bus", "directory", "--live", "--json"])
    if not raw:
        return 0
    try:
        sessions = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(sessions, list):
        return 0
    return sum(1 for s in sessions if isinstance(s, dict) and s.get("cwd") == cwd)


def emit_warning(cwd: str, branch: str, default: str, bridge_count: int) -> None:
    """Print the warning to stderr (so pre-commit / Skill output stays clean on stdout)."""
    others = bridge_count - 1
    lines = [
        "⚠ branch / workspace 乖離の警告 (ms-65 e-1481):",
        f"   cwd       : {cwd} (main project root)",
        f"   branch    : {branch} (default は {default})",
        f"   live 並走  : {bridge_count} セッション (自分含む)",
        "",
        f"   このセッションは main project root で『{branch}』ブランチに居ますが、",
        f"   同 cwd に他に {others} 個のセッションが live です。",
        "   別のセッションが git checkout でブランチを切り替えると、ここで commit/edit",
        "   している作業も巻き込まれる可能性があります (2026-06-10 のインシデントと同型)。",
        "",
        "   推奨: `beacon milestone start <ms-id>` で .worktrees/<ms-slug>/ に作業領域",
        "   を切り出し、cd した上で新しい bclaude を起動して作業を続けてください。",
        "   この警告は block しません (= commit / session-start はそのまま続行)。",
    ]
    for line in lines:
        print(line, file=sys.stderr)


def check_divergence() -> bool:
    """Return True if the unsafe combination is detected (warning was emitted)."""
    if not is_in_main_project_root():
        return False
    branch = _run(["git", "branch", "--show-current"])
    if not branch:
        return False
    default = detect_default_branch()
    if branch == default:
        return False
    cwd = os.getcwd()
    bridge_count = count_live_bridges_same_cwd(cwd)
    if bridge_count < 2:
        return False
    emit_warning(cwd, branch, default, bridge_count)
    return True


def main() -> int:
    check_divergence()
    return 0  # Always 0: warn-only, never block


if __name__ == "__main__":
    sys.exit(main())
