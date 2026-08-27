#!/usr/bin/env python3
"""git_write_port.py — the outward-effect git port (ms-142 e-5527, spine §5).

The *write* half of the git ports/adapters split — the git calls that MUTATE
(tag, force-tag, force-push a tag, working-tree reset/stash). Kept separate from
``git_read_port`` (rev-parse / log / config, read-only) precisely so §5's rule
is visible in the type: "Beacon declares / verifies / records, it does not *do*
the outward effect." The outward effect lives here, behind the port; the L3
handler keeps the policy (best-effort? require --execute? surface the error?).

The concrete adapter shells out to `git`. It is swappable via ``set_adapter``.
These methods RAISE on failure (CalledProcessError / OSError / TimeoutExpired);
callers that want best-effort semantics (e.g. the deployed-prod marker writer)
wrap the call and build their own observable result — that best-effort policy is
a business decision, not the port's.
"""
from __future__ import annotations

import subprocess
from typing import Protocol


class GitWriteAdapter(Protocol):
    """Outward-effect git interface Beacon declares."""

    def tag(self, name: str) -> None: ...

    def tag_force(self, name: str, rev: str, timeout: int = 10) -> None: ...

    def push_tag_force(self, remote: str, name: str, timeout: int = 30) -> None: ...


class SubprocessGitWriteAdapter:
    """Default adapter: shells out to mutating `git` invocations. Raises on
    failure; the caller owns any best-effort / retry policy."""

    def tag(self, name: str) -> None:
        subprocess.run(["git", "tag", name], check=True, capture_output=True)

    def tag_force(self, name: str, rev: str, timeout: int = 10) -> None:
        subprocess.run(["git", "tag", "-f", name, rev],
                       check=True, capture_output=True, text=True, timeout=timeout)

    def push_tag_force(self, remote: str, name: str, timeout: int = 30) -> None:
        subprocess.run(["git", "push", "-f", remote, name],
                       check=True, capture_output=True, text=True, timeout=timeout)


_adapter: GitWriteAdapter = SubprocessGitWriteAdapter()


def set_adapter(adapter: GitWriteAdapter) -> None:
    """L4 wiring / tests: swap the concrete adapter behind the port."""
    global _adapter
    _adapter = adapter


def get_adapter() -> GitWriteAdapter:
    return _adapter


# --- port surface: the thin IF Beacon declares ----------------------------

def tag(name: str) -> None:
    """Create git tag ``name``. Raises CalledProcessError if it exists / fails."""
    _adapter.tag(name)


def tag_force(name: str, rev: str, timeout: int = 10) -> None:
    """Force-move git tag ``name`` to ``rev`` (git tag -f). Raises on failure."""
    _adapter.tag_force(name, rev, timeout)


def push_tag_force(remote: str, name: str, timeout: int = 30) -> None:
    """Force-push tag ``name`` to ``remote`` (git push -f). Raises on failure."""
    _adapter.push_tag_force(remote, name, timeout)
