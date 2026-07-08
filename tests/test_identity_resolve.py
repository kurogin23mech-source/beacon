"""Tests for stable-recipient-identity resolve (ms-93 / e-2520).

Pins the core promise: a sender addressing "the codex in /path on this
machine" reaches the CURRENT live sid even after the daemon re-mints its sid,
and never leaks to a different agent_kind or cwd.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "beacon_cli"))

import io  # noqa: E402
import json  # noqa: E402

from skills_helpers.identity_resolve import (  # noqa: E402
    agent_kind_of,
    current_sid,
    describe_candidate,
    label_rows,
    main,
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


# -- label_rows (picker annotation) -----------------------------------------

def test_label_rows_keeps_sid_and_adds_label_and_key():
    rows = [_sess("sv-1", machine="mc-a", agent_kind="claude-code",
                  cwd="/w", last_poll_at="2026-07-07T10:00:00Z")]
    out = label_rows(rows)
    assert len(out) == 1
    r = out[0]
    assert r["session_id"] == "sv-1"          # route token preserved
    assert "claude-code" in r["label"]        # human-first label
    assert r["machine"] == "mc-a"
    assert r["cwd"] == "/w"
    assert r["agent_kind"] == "claude-code"
    assert r["last_poll_at"] == "2026-07-07T10:00:00Z"


# -- main() CLI (Skill entrypoint) ------------------------------------------

def _run_main(argv, rows, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps(rows)))
    rc = main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_main_label_mode_emits_annotated_rows(monkeypatch, capsys):
    rows = [_sess("sv-1", machine="mc-a", agent_kind="claude-code", cwd="/w")]
    rc, data = _run_main(["label"], rows, monkeypatch, capsys)
    assert rc == 0
    assert data[0]["session_id"] == "sv-1"
    assert "claude-code" in data[0]["label"]


def test_main_resolve_mode_picks_freshest_sid(monkeypatch, capsys):
    """The send-time re-resolve: a churned identity routes to the current
    live sid, not the stale one the picker first listed."""
    rows = [
        _sess("sv-OLD", machine="mc-a", agent_kind="claude-code", cwd="/w",
              last_poll_at="2026-07-07T10:00:00Z", project_id="p1"),
        _sess("sv-NEW", machine="mc-a", agent_kind="claude-code", cwd="/w",
              last_poll_at="2026-07-07T22:00:00Z", project_id="p1"),
    ]
    rc, data = _run_main(
        ["resolve", "--machine", "mc-a", "--cwd", "/w",
         "--agent-kind", "claude-code", "--project", "p1"],
        rows, monkeypatch, capsys)
    assert rc == 0
    assert data["current_sid"] == "sv-NEW"
    assert len(data["candidates"]) == 2


def test_main_resolve_mode_empty_when_no_match(monkeypatch, capsys):
    rows = [_sess("sv-1", machine="mc-a", agent_kind="codex", cwd="/w")]
    rc, data = _run_main(
        ["resolve", "--machine", "mc-a", "--cwd", "/w",
         "--agent-kind", "claude-code"],  # kind mismatch
        rows, monkeypatch, capsys)
    assert rc == 0
    assert data["current_sid"] == ""
    assert data["candidates"] == []


def test_main_label_mode_handles_empty_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = main(["label"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []
