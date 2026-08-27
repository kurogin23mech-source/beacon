#!/usr/bin/env python3
"""gh_port.py — the GitHub forge port (ms-142 e-5527, spine §5 hexagonal split).

Part of the C10 thin-action decomposition. A thin-action verb (issue import,
pr create, …) used to braid three concerns in one function: record (L2 ledger
append), the business decision (L3), and the outward tool call (adapter = the
`gh` subprocess). This module is the *port* for the `gh` forge — the thin
interface Beacon *declares*. Beacon owns the declaration; the concrete adapter
(here a subprocess shell-out to the `gh` CLI) is swappable via ``set_adapter``
for tests (a fake) or a future MCP adapter.

§5 (line 142): "Beacon's responsibility = declare / verify / record, not *do*
the outward effect." So handlers call ``gh_port.issue_view(...)`` and drop the
result into ``core.issue_import`` (the record); the subprocess lives behind the
port, never inside the L3 handler.

The port grows one thin-action vine at a time (e-5527 strangler order:
issue → … → pr). Issue read verbs land first; pr verbs join when cmd_pr is
split.
"""
from __future__ import annotations

import json
import subprocess
from typing import Protocol


class GhAdapter(Protocol):
    """The gh forge interface Beacon declares. Implementations shell out to
    `gh`, call an MCP server, or return canned data in tests."""

    def issue_view(self, number: int) -> dict: ...

    def issue_list(self, state: str = "open") -> list: ...

    def pr_view(self, url: str) -> dict: ...

    def pr_list_all(self) -> list: ...

    def run(self, argv: list) -> "subprocess.CompletedProcess": ...


class SubprocessGhAdapter:
    """Default adapter: shells out to the `gh` CLI. Moved verbatim from
    cmd_issue._gh_issue_fetch / _gh_issues_list (behavior unchanged)."""

    def issue_view(self, number: int) -> dict:
        result = subprocess.run(
            ["gh", "issue", "view", str(number),
             "--json", "number,title,body,url,labels,state"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"gh issue view {number} failed")
        return json.loads(result.stdout)

    def issue_list(self, state: str = "open") -> list:
        result = subprocess.run(
            ["gh", "issue", "list", "--state", state, "--limit", "200",
             "--json", "number,title,body,url,labels,state"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "gh issue list failed")
        return json.loads(result.stdout)

    def pr_view(self, url: str) -> dict:
        result = subprocess.run(
            ["gh", "pr", "view", url, "--json", "title,body,commits"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"gh pr view {url} failed")
        return json.loads(result.stdout)

    def pr_list_all(self) -> list:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--limit", "100",
             "--json", "number,state,url,mergedAt,title"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "gh pr list failed")
        return json.loads(result.stdout or "[]")

    def run(self, argv: list) -> subprocess.CompletedProcess:
        """Execute a prepared gh argv (e.g. a fully-built `gh pr create …`).
        The caller owns the argv (incl. user-forwarded flags); the port owns
        only the outward execution. Captures stdout/stderr as text."""
        return subprocess.run(argv, capture_output=True, text=True)


_adapter: GhAdapter = SubprocessGhAdapter()


def set_adapter(adapter: GhAdapter) -> None:
    """L4 wiring / tests: swap the concrete adapter behind the port."""
    global _adapter
    _adapter = adapter


def get_adapter() -> GhAdapter:
    return _adapter


# --- port surface: the thin IF Beacon declares ----------------------------

def issue_view(number: int) -> dict:
    """Fetch a single GitHub issue. Returns gh's JSON dict, raises RuntimeError."""
    return _adapter.issue_view(number)


def issue_list(state: str = "open") -> list:
    """List GitHub issues in ``state``. Returns a list of dicts, raises RuntimeError."""
    return _adapter.issue_list(state)


def pr_view(url: str) -> dict:
    """Fetch a PR's title/body/commits. Returns gh's JSON dict, raises on failure."""
    return _adapter.pr_view(url)


def pr_list_all() -> list:
    """List all PRs (number/state/url/mergedAt/title). Returns list, raises on failure."""
    return _adapter.pr_list_all()


def run(argv: list) -> subprocess.CompletedProcess:
    """Execute a prepared gh argv, CAPTURING stdout/stderr as text (so the caller
    can parse e.g. the created PR URL). Returns CompletedProcess. Note: this
    captures, whereas cloud_run_port.execute() streams to the terminal — the two
    are intentionally different, hence the different names (PR #690 review)."""
    return _adapter.run(argv)
