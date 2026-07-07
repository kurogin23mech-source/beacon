"""Bridge-claim resolution across git worktrees (ms-93 receive-orphan root fix).

The receive-bridge orphan bug: a bclaude launched in worktree A writes its
bus.mjs bridge claim to ``A/.beacon/bridges/<sid>.json``. When the CLI later
runs in a different worktree B (hand ``cd`` / ``beacon milestone start``),
``B/.beacon/bridges/`` is empty, so ``find_my_bridge_claim`` used to miss A's
claim and the session silently fell through to B's own sid — orphaning the
receive path (sends work, incoming DMs never live-wake). ``bridge_status.py``
made that state *visible*; this is the actual fix that keeps the running
bridge's sid authoritative regardless of which worktree the CLI sits in.

The fix keys off the pid-tree: a claim is only adopted when its ``parent_pid``
is one of the CLI's ancestor pids (= it belongs to *my* bclaude) and its bridge
process is alive. These tests pin that the cross-worktree fallback finds the
right claim without loosening either guard.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session  # noqa: E402

MY_BCLAUDE_PID = 5000          # in my ancestor chain
ALIVE_BRIDGE_A = 9001
ALIVE_BRIDGE_B = 9002
DEAD_BRIDGE = 9999


def _write_claim(bridges_dir: Path, sid: str, *, pid: int, parent_pid: int):
    bridges_dir.mkdir(parents=True, exist_ok=True)
    (bridges_dir / f"{sid}.json").write_text(json.dumps({
        "session_id": sid, "pid": pid, "parent_pid": parent_pid,
        "cwd": str(bridges_dir.parent.parent), "started_at": "2026-07-08T00:00:00Z",
    }), encoding="utf-8")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Isolate the resolver from real ps / os.kill / git and point its dirs at
    tmp worktrees. Returns (cwd_bridges, sibling_bridges)."""
    cwd_bridges = tmp_path / "A" / ".beacon" / "bridges"
    sibling_bridges = tmp_path / "B" / ".beacon" / "bridges"

    monkeypatch.setattr(session, "_get_ancestor_pids", lambda: {MY_BCLAUDE_PID})
    monkeypatch.setattr(
        session, "_pid_alive",
        lambda pid: pid in {ALIVE_BRIDGE_A, ALIVE_BRIDGE_B},
    )
    monkeypatch.setattr(session, "_bridges_dir", lambda: cwd_bridges)
    monkeypatch.setattr(session, "_bridge_claim_path",
                        lambda: tmp_path / "A" / ".beacon" / "bridge.json")
    monkeypatch.setattr(session, "_sibling_worktree_bridges_dirs",
                        lambda: [sibling_bridges])
    # We're simulating "cd'd into a linked worktree" — the gate that lets the
    # cross-worktree scan run at all.
    monkeypatch.setattr(session, "_in_linked_worktree", lambda: True)
    return cwd_bridges, sibling_bridges


def test_fast_path_local_claim_still_wins(wired):
    """A claim in the current cwd is resolved without consulting siblings
    (behavior-preserving fast path)."""
    cwd_bridges, sibling_bridges = wired
    _write_claim(cwd_bridges, "sv-CWD", pid=ALIVE_BRIDGE_A,
                 parent_pid=MY_BCLAUDE_PID)
    assert session.find_my_bridge_claim().get("session_id") == "sv-CWD"


def test_cross_worktree_fallback_finds_sibling_bridge(wired):
    """The root fix: cwd has no local claim, but my bclaude's live bridge is in
    a sibling worktree — the CLI adopts that sid instead of orphaning itself."""
    cwd_bridges, sibling_bridges = wired
    # cwd bridges dir intentionally empty.
    _write_claim(sibling_bridges, "sv-SIB", pid=ALIVE_BRIDGE_B,
                 parent_pid=MY_BCLAUDE_PID)
    assert session.find_my_bridge_claim().get("session_id") == "sv-SIB"


def test_sibling_claim_not_adopted_when_pidtree_mismatches(wired):
    """A sibling bridge that belongs to a DIFFERENT bclaude (parent_pid not in
    my ancestors) must not be adopted — that would cross-wire two sessions."""
    cwd_bridges, sibling_bridges = wired
    _write_claim(sibling_bridges, "sv-OTHER", pid=ALIVE_BRIDGE_B,
                 parent_pid=7777)  # not in {MY_BCLAUDE_PID}
    assert session.find_my_bridge_claim() == {}


def test_sibling_claim_not_adopted_when_bridge_dead(wired):
    """Even a pid-tree match must have a live bridge process; a dead bus.mjs
    cannot own the session."""
    cwd_bridges, sibling_bridges = wired
    _write_claim(sibling_bridges, "sv-DEAD", pid=DEAD_BRIDGE,
                 parent_pid=MY_BCLAUDE_PID)
    assert session.find_my_bridge_claim() == {}


def test_local_claim_preferred_over_sibling(wired):
    """When both exist, the local cwd claim wins (fast path returns first)."""
    cwd_bridges, sibling_bridges = wired
    _write_claim(cwd_bridges, "sv-CWD", pid=ALIVE_BRIDGE_A,
                 parent_pid=MY_BCLAUDE_PID)
    _write_claim(sibling_bridges, "sv-SIB", pid=ALIVE_BRIDGE_B,
                 parent_pid=MY_BCLAUDE_PID)
    assert session.find_my_bridge_claim().get("session_id") == "sv-CWD"


# -- _sibling_worktree_bridges_dirs (git enumeration) -----------------------

def test_sibling_dirs_parses_worktree_list_and_excludes_cwd(monkeypatch, tmp_path):
    import subprocess

    class _R:
        returncode = 0
        stdout = (
            "worktree /repo/main\n"
            "HEAD abc\n\n"
            "worktree /repo/.worktrees/B\n"
            "HEAD def\n\n"
        )

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(session.Path, "cwd", classmethod(lambda cls: Path("/repo/main")))
    dirs = session._sibling_worktree_bridges_dirs()
    # /repo/main is cwd → excluded; only B remains.
    assert dirs == [Path("/repo/.worktrees/B") / session._BRIDGES_DIR_RELATIVE]


def test_sibling_dirs_empty_outside_git(monkeypatch):
    import subprocess

    class _R:
        returncode = 128  # git: not a repository
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert session._sibling_worktree_bridges_dirs() == []


def test_sibling_dirs_empty_when_git_raises(monkeypatch):
    import subprocess

    def _boom(*a, **k):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert session._sibling_worktree_bridges_dirs() == []


# -- gate: the scan only runs inside a linked worktree ----------------------

def test_scan_gated_out_when_not_linked_worktree(tmp_path, monkeypatch):
    """In a non-worktree cwd the cross-worktree scan must not run — even if a
    sibling claim exists — so bridge-less sessions in a normal repo don't pay
    the git subprocess. Here the local claim is absent and the gate is False,
    so the resolver returns {} without ever consulting siblings."""
    cwd_bridges = tmp_path / "A" / ".beacon" / "bridges"
    sibling_bridges = tmp_path / "B" / ".beacon" / "bridges"
    _write_claim(sibling_bridges, "sv-SIB", pid=ALIVE_BRIDGE_B,
                 parent_pid=MY_BCLAUDE_PID)
    monkeypatch.setattr(session, "_get_ancestor_pids", lambda: {MY_BCLAUDE_PID})
    monkeypatch.setattr(session, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(session, "_bridges_dir", lambda: cwd_bridges)
    monkeypatch.setattr(session, "_bridge_claim_path",
                        lambda: tmp_path / "A" / ".beacon" / "bridge.json")
    monkeypatch.setattr(session, "_in_linked_worktree", lambda: False)

    called = {"n": 0}

    def _tracked():
        called["n"] += 1
        return [sibling_bridges]
    monkeypatch.setattr(session, "_sibling_worktree_bridges_dirs", _tracked)

    assert session.find_my_bridge_claim() == {}
    assert called["n"] == 0  # gate prevented the scan entirely


def test_in_linked_worktree_detects_git_file_vs_dir(tmp_path, monkeypatch):
    # Linked worktree: .git is a file pointer.
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /repo/.git/worktrees/linked\n")
    monkeypatch.setattr(session.Path, "cwd", classmethod(lambda cls: linked))
    assert session._in_linked_worktree() is True

    # Main worktree: .git is a directory.
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    monkeypatch.setattr(session.Path, "cwd", classmethod(lambda cls: main))
    assert session._in_linked_worktree() is False
