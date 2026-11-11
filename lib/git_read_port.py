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

import os
import subprocess
from typing import Protocol


def _git_cwd():
    """どのディレクトリで git を読むか。``BEACON_INVOCATION_CWD`` を優先する。

    ``bin/beacon`` は commands.py を呼ぶ前にプロジェクトのルート (= ``.beacon/`` が
    在る親リポジトリ) へ移動する。そのため **worktree から叩いても、この port は親の
    ブランチ・HEAD を読んでいた**。実測 (2026-11-10): worktree で
    ``fix/log-gate-ignores-beacon-churn`` の f163f2cc を push した直後に
    ``beacon push record`` を叩いたら、別セッションが作業中の
    ``ms-185-residue-cleanup 0a3ba117..0a3ba117 (0 commits)`` が記録された。

    ``bin/beacon`` は呼び出し元の cwd を ``BEACON_INVOCATION_CWD`` に export して
    いて、``bin/lib/cmd_log.sh`` は ``git -C "$_git_cwd"`` でそれを使っている
    (e-1227 / ms-17)。python 側の読み取りだけがその規約から外れていた。

    ここで 1 回決めるのは、この port の 6 メソッド全部と、それを呼ぶ 12 箇所
    (push / pr / deploy / rollback / 台帳) が同じ答えを見る必要があるため。
    呼び出し側ごとに ``-C`` を足す形にすると、足し忘れた口だけが親を読み続ける。

    未設定なら None を返す = プロセスの cwd を継承する (bin/beacon を経由しない
    直接 import や試験では、現在の挙動を変えない)。
    """
    cwd = (os.environ.get("BEACON_INVOCATION_CWD") or "").strip()
    if cwd and os.path.isdir(cwd):
        return cwd
    return None


class GitReadAdapter(Protocol):
    """Read-only git introspection interface Beacon declares."""

    def current_branch(self) -> str: ...

    def branch_show_current(self, timeout: int = 5) -> str: ...

    def rev_parse_short(self, ref: str = "HEAD") -> str: ...

    def log_commits(self, from_hash: str, to_hash: str, limit: int) -> list: ...

    def log_subjects(self, limit: int, timeout: int = 5) -> list: ...

    def config_user_name(self) -> str: ...


class SubprocessGitReadAdapter:
    """Default adapter: shells out to read-only `git` invocations. Raises
    (CalledProcessError / OSError) on failure; callers own the fallback."""

    def current_branch(self) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, cwd=_git_cwd(),
        ).strip()

    def branch_show_current(self, timeout: int = 5) -> str:
        """Current branch via `git branch --show-current` (empty on detached
        HEAD — distinct from current_branch(), which returns "HEAD" there).
        Returncode-agnostic like the caller: returns stdout as-is."""
        return subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=timeout, cwd=_git_cwd(),
        ).stdout.strip()

    def rev_parse_short(self, ref: str = "HEAD") -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", ref],
            stderr=subprocess.DEVNULL, text=True, cwd=_git_cwd(),
        ).strip()

    def log_commits(self, from_hash: str, to_hash: str, limit: int) -> list:
        """Commits in ``from_hash..to_hash`` (or the last ``limit`` reaching
        ``to_hash`` when ``from_hash`` is empty). Returns [{"hash": <7>, "message"}]."""
        if from_hash:
            args = ["git", "log", f"{from_hash}..{to_hash}", "--format=%H %s"]
        else:
            args = ["git", "log", to_hash, "--format=%H %s", f"-{limit}"]
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True,
                                      cwd=_git_cwd()).strip()
        commits = []
        for line in out.splitlines():
            if line.strip():
                parts = line.split(" ", 1)
                commits.append({"hash": parts[0][:7],
                                "message": parts[1] if len(parts) > 1 else ""})
        return commits

    def log_subjects(self, limit: int, timeout: int = 5) -> list:
        """The last ``limit`` commit subjects (git log --pretty=%s). Returncode-
        agnostic: returns whatever lines stdout carried (empty list if none)."""
        out = subprocess.run(
            ["git", "log", "--pretty=%s", f"-{limit}"],
            capture_output=True, text=True, timeout=timeout, cwd=_git_cwd(),
        ).stdout
        return [ln for ln in out.splitlines()]

    def config_user_name(self) -> str:
        return subprocess.check_output(
            ["git", "config", "user.name"],
            stderr=subprocess.DEVNULL, text=True, cwd=_git_cwd(),
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
    """Current branch (rev-parse --abbrev-ref HEAD). RAISES on git failure, and
    returns the literal "HEAD" on a detached HEAD. Prefer this when you want a
    git failure to surface as an exception; use branch_show_current() when you
    want the soft-fail contract instead."""
    return _adapter.current_branch()


def branch_show_current(timeout: int = 5) -> str:
    """Current branch via `git branch --show-current`. Returns "" on a detached
    HEAD **and on any git failure** (no repo / no git / git error) — this method
    is returncode-agnostic and NEVER raises for a git-level error, so a caller
    cannot distinguish "detached HEAD" from "git broken"; wrap in try/except only
    if you need to observe process-launch errors (FileNotFoundError etc.). This
    soft-fail contract is deliberate (it preserves the PR-create MS-inference
    call site's original behavior); current_branch() is the raise-on-failure
    sibling. (PR #690 independent review: contract promoted to the port surface.)"""
    return _adapter.branch_show_current(timeout)


def log_subjects(limit: int, timeout: int = 5) -> list:
    """Last ``limit`` commit subject lines (git log --pretty=%s). Returncode-
    agnostic like branch_show_current(): returns [] on any git failure without
    raising a git-level error (callers treat empty as "no signal"). Contrast
    log_commits(), which returns list[dict] and raises on failure."""
    return _adapter.log_subjects(limit, timeout)


def rev_parse_short(ref: str = "HEAD") -> str:
    """Short hash of ``ref`` (rev-parse --short). Raises on failure."""
    return _adapter.rev_parse_short(ref)


def log_commits(from_hash: str = "", to_hash: str = "HEAD", limit: int = 50) -> list:
    """Parsed commit list for a range. Raises on failure."""
    return _adapter.log_commits(from_hash, to_hash, limit)


def config_user_name() -> str:
    """git config user.name. Raises on failure."""
    return _adapter.config_user_name()
