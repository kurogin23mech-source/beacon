"""Daemon-restart identity invariance (ms-93 Phase 4).

The Codex receive-loop daemon mints a fresh ``codex-<epoch>-<nonce>`` session_id
every cold start (see ``lib.codex_session.acquire_codex_session``): a graceful
close marks the record closed, and the next acquire replaces it with a new sid.
So a sender that remembers a raw sid is addressing a route token that dies on
the next daemon bounce — the exact churn that dropped two DMs on 2026-07-07.

Phase 1-3 fixed this by routing on the *sid-independent* coarse identity key
``(project_id, machine, cwd, agent_kind)`` and resolving it to the current live
sid at send time (``beacon_cli.skills_helpers.identity_resolve``).

This test is the Phase 4 pin: drive the REAL daemon mint across three restarts
and assert the two invariants that make the fix hold end to end:

  1. Each restart mints a *distinct* sid (the churn is real, not mocked away).
  2. The coarse identity key is *identical* across all three, and the resolver
     always routes to the CURRENT (freshest) sid — never a dead one.

If a future change reintroduces a sid component into the identity key, or makes
the daemon reuse a stale sid, invariant 1 or 2 breaks here.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "beacon_cli"))

import codex_session  # noqa: E402
from skills_helpers.identity_resolve import (  # noqa: E402
    current_sid,
    resolve_stable_identity,
    stable_identity_key,
)

PROJECT_ID = "proj-restart"
CWD = "/Users/dev/beacon"
MACHINE = "mc-dev-01"
PARENT_PID = 4242


def _restart_daemon(home: str) -> codex_session.CodexSession:
    """Simulate one daemon cold start: acquire (mint) then graceful close so
    the NEXT acquire is forced to mint a fresh sid rather than reuse."""
    sess = codex_session.acquire_codex_session(
        project_id=PROJECT_ID, cwd=CWD, parent_pid=PARENT_PID,
        pid=os.getpid(), home=home,
    )
    path = codex_session.codex_session_path(
        PROJECT_ID,
        codex_session.derive_stable_instance_key(CWD, PARENT_PID),
        home=home,
    )
    codex_session.close_codex_session(path, reason="restart-test")
    return sess


def _dir_row(sess: codex_session.CodexSession, *, last_poll_at: str) -> dict:
    """Shape a directory row the way ``beacon bus directory --json`` would,
    for a Codex session (cwd + agent.kind + machine + freshness)."""
    return {
        "session_id": sess.session_id,
        "project_id": sess.project_id,
        "cwd": sess.cwd,
        "agent": {"kind": sess.agent_kind},
        "machine_id": MACHINE,
        "last_poll_at": last_poll_at,
    }


def test_three_restarts_mint_distinct_sids_but_one_stable_identity(tmp_path):
    home = str(tmp_path)
    sessions = [_restart_daemon(home) for _ in range(3)]
    sids = [s.session_id for s in sessions]

    # Invariant 1: three restarts, three distinct route tokens.
    assert len(set(sids)) == 3, f"expected 3 distinct sids, got {sids}"
    assert all(s.startswith("codex-") for s in sids)

    # Invariant 2a: the coarse identity key is the SAME for all three.
    rows = [
        _dir_row(sessions[0], last_poll_at="2026-07-08T00:00:01Z"),
        _dir_row(sessions[1], last_poll_at="2026-07-08T00:00:02Z"),
        _dir_row(sessions[2], last_poll_at="2026-07-08T00:00:03Z"),  # freshest
    ]
    keys = {stable_identity_key(r) for r in rows}
    assert len(keys) == 1, f"identity key drifted across restarts: {keys}"
    assert next(iter(keys)) == (PROJECT_ID, MACHINE, CWD, "codex")


def test_resolver_routes_to_current_sid_after_restarts(tmp_path):
    """Even with stale rows lingering in the directory, a sender addressing the
    coarse identity reaches the newest daemon, not a dead sid."""
    home = str(tmp_path)
    sessions = [_restart_daemon(home) for _ in range(3)]
    rows = [
        _dir_row(sessions[0], last_poll_at="2026-07-08T00:00:01Z"),
        _dir_row(sessions[1], last_poll_at="2026-07-08T00:00:02Z"),
        _dir_row(sessions[2], last_poll_at="2026-07-08T00:00:03Z"),  # freshest
    ]

    resolved = current_sid(
        rows, machine=MACHINE, cwd=CWD, agent_kind="codex", project_id=PROJECT_ID,
    )
    assert resolved == sessions[2].session_id  # newest restart wins

    # The stale sids are NOT what a sender would route to.
    assert resolved != sessions[0].session_id
    assert resolved != sessions[1].session_id

    # Ranking is freshness-descending across all three matches.
    ranked = resolve_stable_identity(
        rows, machine=MACHINE, cwd=CWD, agent_kind="codex", project_id=PROJECT_ID,
    )
    assert [r["session_id"] for r in ranked] == [
        sessions[2].session_id, sessions[1].session_id, sessions[0].session_id,
    ]


def test_identity_does_not_leak_across_cwd_after_restart(tmp_path):
    """A daemon in a different cwd is a different identity even on the same
    machine/agent_kind — the resolver must not cross-route to it."""
    home = str(tmp_path)
    mine = _restart_daemon(home)
    other = codex_session.acquire_codex_session(
        project_id=PROJECT_ID, cwd="/Users/dev/other-repo",
        parent_pid=PARENT_PID + 1, pid=os.getpid(), home=home,
    )
    rows = [
        _dir_row(mine, last_poll_at="2026-07-08T00:00:01Z"),
        {
            "session_id": other.session_id, "project_id": PROJECT_ID,
            "cwd": "/Users/dev/other-repo", "agent": {"kind": "codex"},
            "machine_id": MACHINE, "last_poll_at": "2026-07-08T00:00:09Z",
        },
    ]
    resolved = current_sid(
        rows, machine=MACHINE, cwd=CWD, agent_kind="codex", project_id=PROJECT_ID,
    )
    assert resolved == mine.session_id  # freshest is `other`, but cwd gates it out
