#!/usr/bin/env python3
"""git_read_port.py — the read-only git introspection port (ms-142 e-5527, spine §5).

The *read* half of the git ports/adapters split. Thin-action verbs (push, deploy,
pr, …) inspect the working tree to assemble their records: current branch, HEAD
hash, the commit range since the last push, the git user. These are read-only
introspection — no outward effect — so they live behind this port, separate from
``git_write_port`` (tag/push/reset/stash) which mutates.

§5: the port is the thin IF Beacon *declares*; the concrete adapter (here a
subprocess shell-out to `git`) is swappable via ``set_adapter`` for tests / a
future non-subprocess adapter. Policy stays in the L3 handler: this port raises
on a git failure (or absence of a repo); the *fallback default* ("main" / "HEAD"
/ empty range) is a business decision the caller owns, so handlers keep their
try/except around these calls.
"""
from __future__ import annotations

import subprocess
from typing import Protocol


class GitReadAdapter(Protocol):
    """Read-only git introspection interface Beacon declares."""

    def current_branch(self) -> str: ...

    def rev_parse_short(self, ref: str = "HEAD") -> str: ...

    def log_commits(self, from_hash: str, to_hash: str, limit: int) -> list: ...

    def config_user_name(self) -> str: ...


class SubprocessGitReadAdapter:
    """Default adapter: shells out to read-only `git` invocations. Raises
    (CalledProcessError / OSError) on failure; callers own the fallback."""

    def current_branch(self) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()

    def rev_parse_short(self, ref: str = "HEAD") -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", ref],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()

    def log_commits(self, from_hash: str, to_hash: str, limit: int) -> list:
        """Commits in ``from_hash..to_hash`` (or the last ``limit`` reaching
        ``to_hash`` when ``from_hash`` is empty). Returns [{"hash": <7>, "message"}]."""
        if from_hash:
            args = ["git", "log", f"{from_hash}..{to_hash}", "--format=%H %s"]
        else:
            args = ["git", "log", to_hash, "--format=%H %s", f"-{limit}"]
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True).strip()
        commits = []
        for line in out.splitlines():
            if line.strip():
                parts = line.split(" ", 1)
                commits.append({"hash": parts[0][:7],
                                "message": parts[1] if len(parts) > 1 else ""})
        return commits

    def config_user_name(self) -> str:
        return subprocess.check_output(
            ["git", "config", "user.name"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()


_adapter: GitReadAdapter = SubprocessGitReadAdapter()


def set_adapter(adapter: GitReadAdapter) -> None:
    """L4 wiring / tests: swap the concrete adapter behind the port."""
    global _adapter
    _adapter = adapter


def get_adapter() -> GitReadAdapter:
    return _adapter


# --- port surface: the thin IF Beacon declares ----------------------------

def current_branch() -> str:
    """Current branch (rev-parse --abbrev-ref HEAD). Raises on failure."""
    return _adapter.current_branch()


def rev_parse_short(ref: str = "HEAD") -> str:
    """Short hash of ``ref`` (rev-parse --short). Raises on failure."""
    return _adapter.rev_parse_short(ref)


def log_commits(from_hash: str = "", to_hash: str = "HEAD", limit: int = 50) -> list:
    """Parsed commit list for a range. Raises on failure."""
    return _adapter.log_commits(from_hash, to_hash, limit)


def config_user_name() -> str:
    """git config user.name. Raises on failure."""
    return _adapter.config_user_name()
