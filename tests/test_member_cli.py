"""CLI smoke tests for the new member subcommands (ms-78 e-1805 / e-1807).

Pure CLI-shape tests — they exercise `bin/beacon member [invite|join|whoami|...]`
through subprocess so the dispatcher arg-parse and the cmd_member_* Python
functions both stay wired. Cloud-bound flows (= those that call out to the
server) are exercised via stub paths that short-circuit before HTTP.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BEACON = REPO / "bin" / "beacon"


def _run(args, *, cwd=None, env=None, input_text=None):
    """Run bin/beacon with args, returning (rc, stdout, stderr)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    # Make sure tests pick up the repo-local lib/commands.py and not a
    # system-wide install (the doctor [PATH] warning we observed earlier).
    full_env["PATH"] = f"{REPO / 'bin'}:" + full_env.get("PATH", "")
    p = subprocess.run(
        [str(BEACON), *args],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return p.returncode, p.stdout, p.stderr


# ---------------------------------------------------------------------------
# bin/beacon dispatcher: prints help, validates required args
# ---------------------------------------------------------------------------

def test_member_help_lists_new_subcommands(tmp_path):
    # `member` with no subcmd inside a non-project cwd should still print help
    # for the new subcommands (= ensure they appear in the usage line, so
    # users discover them).
    rc, out, err = _run(["member"], cwd=tmp_path)
    combined = out + err
    for needle in ("invite", "invitation", "join", "whoami"):
        assert needle in combined, (
            f"`beacon member` usage missing '{needle}': {combined!r}"
        )


def test_member_invite_requires_email(tmp_path):
    # Run inside a fake .beacon project so ensure_project passes.
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []})
    )
    rc, out, err = _run(["member", "invite"], cwd=tmp_path)
    assert rc != 0
    assert "email is required" in (out + err) or "Usage" in (out + err)


def test_member_join_requires_token(tmp_path):
    # `join` is allowed to run without an existing project (= invitee
    # accepting before they bind). It must fail cleanly when --token is
    # missing rather than tripping ensure_project first.
    rc, out, err = _run(["member", "join"], cwd=tmp_path)
    assert rc != 0
    assert "--token is required" in (out + err) or "Usage" in (out + err)


def test_member_invitation_subhelp_when_no_arg(tmp_path):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []})
    )
    rc, out, err = _run(["member", "invitation"], cwd=tmp_path)
    combined = out + err
    assert "list" in combined and "cancel" in combined


def test_member_invitation_cancel_requires_id(tmp_path):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []})
    )
    rc, out, err = _run(["member", "invitation", "cancel"], cwd=tmp_path)
    assert rc != 0
    assert "invitation id required" in (out + err) or "Usage" in (out + err)


# ---------------------------------------------------------------------------
# `cmd_member_list` formatting: display_name preferred over email
# ---------------------------------------------------------------------------

def test_member_list_prefers_display_name(tmp_path):
    """`beacon member list` should surface display_name when present, with
    email parenthesised. Falls back to id/email when display_name is empty."""
    project = {
        "name": "t",
        "milestones": [],
        "members": [
            {"id": "alice", "name": "Alice Anderson",
             "display_name": "Alice", "email": "alice@example.com",
             "role": "owner"},
            {"id": "bob", "email": "bob@example.com", "role": "viewer"},
        ],
    }
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps(project))
    rc, out, err = _run(["member", "list"], cwd=tmp_path)
    assert rc == 0, err
    # Alice's display_name is the primary label, email in parens
    assert "Alice" in out
    assert "alice@example.com" in out
    # Bob has no display_name → fall back to id, email in parens
    assert "bob" in out and "bob@example.com" in out
