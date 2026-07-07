"""Tests for stable-recipient-identity resolve (ms-93 / e-2520).

Pins the core promise: a sender addressing "the codex in /path on this
machine" reaches the CURRENT live sid even after the daemon re-mints its sid,
and never leaks to a different agent_kind or cwd.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "beacon_cli"))

from skills_helpers.identity_resolve import (  # noqa: E402
    agent_kind_of,
    current_sid,
    describe_candidate,
    resolve_stable_identity,
    stable_identity_key,
)


def _sess(sid, *, machine="mac1", cwd="/work", agent_kind=None,
          last_poll_at="", created_at="", parent_pid=None, project_id="p"):
    row = {
        "session_id": sid,
        "machine_id": machine,
        "cwd": cwd,
        "project_id": project_id,
        "last_poll_at": last_poll_at,
    }
    if agent_kind is not None:
        row["agent"] = {"kind": agent_kind}
    if created_at:
        row["created_at"] = created_at
    if parent_pid is not None:
        row["parent_pid"] = parent_pid
    return row


# -- agent_kind_of ----------------------------------------------------------

def test_agent_kind_prefers_agent_block():
    assert agent_kind_of(_sess("sv-1", agent_kind="codex")) == "codex"


def test_agent_kind_falls_back_to_sid_prefix():
    # No agent block — classify from the sid prefix.
    assert agent_kind_of({"session_id": "codex-123"}) == "codex"
    assert agent_kind_of({"session_id": "sv-123"}) == "claude-code"


def test_agent_kind_ignores_actor_agent_machine_label():
    # actor.agent is a machine label, not the structural kind — must not leak.
    row = {"session_id": "codex-9", "actor": {"agent": "CFGW5D79LL"}}
    assert agent_kind_of(row) == "codex"


# -- resolve_stable_identity ------------------------------------------------

def test_resolve_picks_latest_live_across_sid_remint():
    """The daemon re-minted its sid; the coarse key still finds it and the
    freshest poll wins."""
    rows = [
        _sess("codex-OLD", agent_kind="codex", cwd="/work",
              last_poll_at="2026-07-07T00:00:01Z"),
        _sess("codex-NEW-remint", agent_kind="codex", cwd="/work",
              last_poll_at="2026-07-07T00:00:09Z"),
    ]
    ranked = resolve_stable_identity(rows, machine="mac1", cwd="/work",
                                     agent_kind="codex")
    assert [r["session_id"] for r in ranked] == ["codex-NEW-remint", "codex-OLD"]
    assert current_sid(rows, machine="mac1", cwd="/work",
                       agent_kind="codex") == "codex-NEW-remint"


def test_resolve_does_not_leak_across_agent_kind():
    rows = [
        _sess("codex-1", agent_kind="codex", cwd="/work",
              last_poll_at="2026-07-07T00:00:01Z"),
        _sess("claude-1", agent_kind="claude-code", cwd="/work",
              last_poll_at="2026-07-07T00:00:09Z"),
    ]
    ranked = resolve_stable_identity(rows, cwd="/work", agent_kind="codex")
    assert [r["session_id"] for r in ranked] == ["codex-1"]


def test_resolve_does_not_leak_across_cwd():
    rows = [
        _sess("codex-here", agent_kind="codex", cwd="/work",
              last_poll_at="2026-07-07T00:00:01Z"),
        _sess("codex-there", agent_kind="codex", cwd="/other",
              last_poll_at="2026-07-07T00:00:09Z"),
    ]
    ranked = resolve_stable_identity(rows, cwd="/work", agent_kind="codex")
    assert [r["session_id"] for r in ranked] == ["codex-here"]


def test_resolve_empty_filters_return_all_ranked():
    rows = [
        _sess("a", last_poll_at="2026-07-07T00:00:01Z"),
        _sess("b", last_poll_at="2026-07-07T00:00:09Z"),
    ]
    assert [r["session_id"] for r in resolve_stable_identity(rows)] == ["b", "a"]


def test_current_sid_empty_when_no_match():
    rows = [_sess("codex-1", agent_kind="codex", cwd="/work")]
    assert current_sid(rows, cwd="/nonexistent", agent_kind="codex") == ""


# -- stable_identity_key ----------------------------------------------------

def test_stable_identity_key_is_sid_independent():
    old = _sess("codex-OLD", agent_kind="codex", cwd="/work", parent_pid=1)
    new = _sess("codex-NEW", agent_kind="codex", cwd="/work", parent_pid=2)
    # Same logical identity (machine+cwd+kind+project) despite different sid.
    assert stable_identity_key(old) == stable_identity_key(new)


# -- describe_candidate -----------------------------------------------------

def test_describe_candidate_surfaces_disambiguators():
    row = _sess("codex-1", machine="mc-abc", agent_kind="codex",
                cwd="/Users/x/beacon", created_at="2026-07-07T05:44:42.0Z",
                parent_pid=23353)
    label = describe_candidate(row)
    assert "codex" in label
    assert "/Users/x/beacon" in label
    assert "2026-07-07T05:44" in label
    assert "23353" in label
