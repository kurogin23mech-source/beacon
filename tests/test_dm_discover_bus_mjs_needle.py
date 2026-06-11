"""Unit tests for dm_discover process-needle compatibility (ms-43 follow-up).

Pins the contract that ``list_bus_bridge_processes`` finds bridges using
**both** the new ``beacon-bus:`` process-title needle (introduced by
ms-60 / e-1466) and the legacy ``channel/bus.mjs`` raw command-line needle.

Why test this:
    e-1330 (ms-54) added cross-project DM discovery via ``pgrep -f``
    against ``channel/bus.mjs``. When e-1466 (ms-60) set
    ``process.title = beacon-bus:<cwd>`` on bus.mjs startup, the title
    *replaced* the raw command line that ``pgrep -f`` sees on most
    macOS/Linux configurations, so the legacy needle silently returned 0
    pids from that day forward. The Skill kept working when the cwd had a
    bridge in the same project (cwd-only fallback), but cross-project
    discovery returned empty — without anyone noticing for ~1 month.

    The regression here is "single-string needle assumption". The fix is
    to walk a tuple of needles. The tests below pin the tuple and its
    failure-tolerance shape so a future refactor cannot silently revert
    the dual-needle behaviour.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "beacon_cli", "skills_helpers")
)

import dm_discover  # noqa: E402


def _proc(stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr="")


def test_needles_tuple_includes_both_old_and_new(monkeypatch):
    """The module must keep both needles. Order is: new first, legacy fallback."""
    assert dm_discover._BUS_MJS_NEEDLES == ("beacon-bus:", "channel/bus.mjs")


def test_new_needle_finds_modern_bus_mjs(monkeypatch):
    """When pgrep finds pids via ``beacon-bus:``, lsof yields a cwd row."""
    def fake_run(args, **kwargs):
        if args[:3] == ["pgrep", "-f", "beacon-bus:"]:
            return _proc(stdout="12345\n")
        if args[:3] == ["pgrep", "-f", "channel/bus.mjs"]:
            return _proc(stdout="")
        if args[:3] == ["lsof", "-p", "12345"]:
            # Realistic lsof line: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
            return _proc(stdout=(
                "node 12345 user cwd DIR 1,2 0 1 /Users/me/tools/beacon\n"
            ))
        return _proc(returncode=1)
    monkeypatch.setattr(dm_discover.subprocess, "run", fake_run)

    result = dm_discover.list_bus_bridge_processes()

    assert result == [("12345", "/Users/me/tools/beacon")]


def test_legacy_needle_still_finds_old_bus_mjs(monkeypatch):
    """A pre-e-1466 bridge (raw ``node .../channel/bus.mjs`` command line) is
    still discoverable via the legacy needle. This is the fallback path."""
    def fake_run(args, **kwargs):
        if args[:3] == ["pgrep", "-f", "beacon-bus:"]:
            return _proc(stdout="")  # new title not set yet on this bridge
        if args[:3] == ["pgrep", "-f", "channel/bus.mjs"]:
            return _proc(stdout="99999\n")
        if args[:3] == ["lsof", "-p", "99999"]:
            return _proc(stdout=(
                "node 99999 user cwd DIR 1,2 0 1 /Users/me/legacy-proj\n"
            ))
        return _proc(returncode=1)
    monkeypatch.setattr(dm_discover.subprocess, "run", fake_run)

    result = dm_discover.list_bus_bridge_processes()

    assert result == [("99999", "/Users/me/legacy-proj")]


def test_pid_matched_by_both_needles_is_deduped(monkeypatch):
    """A pid that matches BOTH needles must appear once, not twice."""
    def fake_run(args, **kwargs):
        if args[:3] == ["pgrep", "-f", "beacon-bus:"]:
            return _proc(stdout="42\n")
        if args[:3] == ["pgrep", "-f", "channel/bus.mjs"]:
            return _proc(stdout="42\n")  # same pid
        if args[:3] == ["lsof", "-p", "42"]:
            return _proc(stdout=(
                "node 42 user cwd DIR 1,2 0 1 /Users/me/dup-proj\n"
            ))
        return _proc(returncode=1)
    monkeypatch.setattr(dm_discover.subprocess, "run", fake_run)

    result = dm_discover.list_bus_bridge_processes()

    # Without dedup we'd get the row twice. The contract is once.
    assert result == [("42", "/Users/me/dup-proj")]


def test_both_needles_find_distinct_pids(monkeypatch):
    """If the new and legacy needles each surface a different pid (e.g. a
    mix of refreshed and stale bridges on the same machine), both appear."""
    def fake_run(args, **kwargs):
        if args[:3] == ["pgrep", "-f", "beacon-bus:"]:
            return _proc(stdout="111\n")
        if args[:3] == ["pgrep", "-f", "channel/bus.mjs"]:
            return _proc(stdout="222\n")
        if args[:3] == ["lsof", "-p", "111"]:
            return _proc(stdout=(
                "node 111 user cwd DIR 1,2 0 1 /Users/me/new-proj\n"
            ))
        if args[:3] == ["lsof", "-p", "222"]:
            return _proc(stdout=(
                "node 222 user cwd DIR 1,2 0 1 /Users/me/old-proj\n"
            ))
        return _proc(returncode=1)
    monkeypatch.setattr(dm_discover.subprocess, "run", fake_run)

    result = dm_discover.list_bus_bridge_processes()

    # The order is: new-needle hits first, then legacy. Both must be present.
    assert ("111", "/Users/me/new-proj") in result
    assert ("222", "/Users/me/old-proj") in result
    assert len(result) == 2


def test_one_needle_pgrep_failing_does_not_block_other(monkeypatch):
    """A timeout / FileNotFoundError on one needle's pgrep must not prevent
    the other needle from running. Belt-and-suspenders failure tolerance."""
    import subprocess as _sub

    def fake_run(args, **kwargs):
        if args[:3] == ["pgrep", "-f", "beacon-bus:"]:
            raise _sub.TimeoutExpired(cmd="pgrep", timeout=5)
        if args[:3] == ["pgrep", "-f", "channel/bus.mjs"]:
            return _proc(stdout="500\n")
        if args[:3] == ["lsof", "-p", "500"]:
            return _proc(stdout=(
                "node 500 user cwd DIR 1,2 0 1 /Users/me/survived\n"
            ))
        return _proc(returncode=1)
    monkeypatch.setattr(dm_discover.subprocess, "run", fake_run)

    result = dm_discover.list_bus_bridge_processes()

    # The new-needle timeout was tolerated; the legacy needle still produced.
    assert result == [("500", "/Users/me/survived")]


def test_no_bridges_anywhere_returns_empty(monkeypatch):
    """Both needles miss → empty list, no exception (Skill falls back to cwd-only)."""
    def fake_run(args, **kwargs):
        if args[:1] == ["pgrep"]:
            return _proc(stdout="")
        return _proc(returncode=1)
    monkeypatch.setattr(dm_discover.subprocess, "run", fake_run)

    assert dm_discover.list_bus_bridge_processes() == []


def test_pgrep_binary_absent_returns_empty(monkeypatch):
    """FileNotFoundError on every pgrep invocation → empty list, no crash."""
    def fake_run(args, **kwargs):
        raise FileNotFoundError("no pgrep")
    monkeypatch.setattr(dm_discover.subprocess, "run", fake_run)

    assert dm_discover.list_bus_bridge_processes() == []
