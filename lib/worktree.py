"""Worktree creation helper (ms-65 e-1476).

Pure function that wraps ``git worktree add`` with the branch-may-already-exist
retry behaviour previously inlined in ``cmd_milestone_workspace`` (lib/commands.py).
Both ``beacon milestone workspace`` (legacy CLI) and the upcoming cwd-aware
``beacon milestone start`` (e-1477) will call this to keep worktree creation
behaviour identical across all entry points.

Why a helper:
    Today the worktree creation lives only in cmd_milestone_workspace, and the
    manual ``beacon milestone start`` path does ``git checkout`` on the main
    cwd instead. That asymmetry is the root of the 2026-06-10 incident where
    two bclaude sessions in the same cwd silently shared a git HEAD. Pulling
    the creation logic out so multiple callers can share it is the substrate
    for the cwd-aware milestone-start refactor in e-1477.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional


class WorktreeError(RuntimeError):
    """Worktree creation failed in a way the caller may want to surface."""


class GitNotInstalledError(WorktreeError):
    """``git`` binary not found in PATH."""


class WorktreeCreateError(WorktreeError):
    """``git worktree add`` itself failed (both create-branch and reuse-branch attempts)."""


def create_workspace(
    workspace_path: str,
    branch_name: str,
    *,
    runner: Optional[callable] = None,
) -> dict:
    """Create a git worktree at ``workspace_path`` on ``branch_name``.

    Idempotent: if ``workspace_path`` already exists on disk, return the
    metadata dict with ``created=False`` and do not touch git. This mirrors
    the prior cmd_milestone_workspace behaviour ("Worktree already exists at
    ...") so the helper is a drop-in for existing callers.

    Branch handling: try ``git worktree add <path> -b <branch>`` first (create
    branch). If that fails because the branch already exists, retry with
    ``git worktree add <path> <branch>`` (reuse). If both fail, raise
    ``WorktreeCreateError`` with the captured stderr from both attempts.

    Args:
        workspace_path: Where the worktree will live. May be relative
            (resolved against cwd) or absolute.
        branch_name: Branch to check out (or create) in the worktree.
        runner: Optional callable (used only for testing) with the
            ``subprocess.run`` signature. Defaults to ``subprocess.run``.

    Returns:
        Dict with keys:
            - ``workspace_path``: relative-or-absolute path the caller passed
            - ``abs_workspace_path``: resolved absolute path
            - ``branch_name``: the branch the worktree is on
            - ``created``: True if we ran ``git worktree add``, False if path
              already existed (idempotent return)
            - ``branch_created``: True if a brand-new branch was created, False
              if an existing branch was reused. Only meaningful when
              ``created`` is True.

    Raises:
        GitNotInstalledError: ``git`` not in PATH.
        WorktreeCreateError: ``git worktree add`` failed both with -b and
            without; stderr from both attempts is in the message.
    """
    abs_workspace_path = os.path.abspath(workspace_path)
    result = {
        "workspace_path": workspace_path,
        "abs_workspace_path": abs_workspace_path,
        "branch_name": branch_name,
        "created": False,
        "branch_created": False,
    }

    if os.path.exists(abs_workspace_path):
        # Idempotent: caller already has a worktree here. Don't disturb it.
        return result

    run = runner or subprocess.run
    try:
        first = run(
            ["git", "worktree", "add", abs_workspace_path, "-b", branch_name],
            capture_output=True, text=True,
        )
    except FileNotFoundError as exc:
        raise GitNotInstalledError("git not found in PATH") from exc

    if first.returncode == 0:
        result["created"] = True
        result["branch_created"] = True
        return result

    # Branch likely already exists; retry without -b to reuse it.
    second = run(
        ["git", "worktree", "add", abs_workspace_path, branch_name],
        capture_output=True, text=True,
    )
    if second.returncode == 0:
        result["created"] = True
        result["branch_created"] = False
        return result

    raise WorktreeCreateError(
        f"git worktree add failed.\n"
        f"  with -b: {first.stderr.strip()}\n"
        f"  without -b: {second.stderr.strip()}"
    )


__all__ = [
    "create_workspace",
    "WorktreeError",
    "GitNotInstalledError",
    "WorktreeCreateError",
]
