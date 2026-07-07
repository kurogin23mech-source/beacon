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


# ---------------------------------------------------------------------------
# ms-95 e-2288: project owner is surfaced as the first row
# ---------------------------------------------------------------------------
# Background: `project.json` stores the project owner in a top-level `owner`
# field (= user_id string) that is structurally separate from `members[]`.
# Previously `beacon member list` only iterated `members[]` and silently
# omitted the owner, causing AI / human readers to mis-identify the highest
# privileged actor (= 2026-06-23 profile-extractor DM mis-routing incident).
# The fix prepends a synthetic owner row with role="owner" and
# source="project.owner".

def test_member_list_owner_prepended_text_mode(tmp_path):
    """Text output prepends the project owner as the first row with
    role='owner', followed by existing members[] rows in order."""
    project = {
        "name": "t",
        "owner": "100953901118675629504",  # project owner user_id
        "milestones": [],
        "members": [
            {"id": "dolphin", "display_name": "dolphin",
             "email": "dolphin@example.com", "role": "editor"},
            {"id": "whale", "display_name": "whale",
             "email": "whale@example.com", "role": "viewer"},
        ],
    }
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps(project))
    rc, out, err = _run(["member", "list"], cwd=tmp_path)
    assert rc == 0, err

    # Owner row must appear with role 'owner'
    assert "owner" in out, f"owner role missing: {out!r}"
    # Owner user_id must be visible somewhere as fallback label (no email
    # / display_name was resolvable in pure-local mode)
    assert "100953901118675629504" in out, (
        f"owner user_id missing — silent omission regression: {out!r}"
    )

    # Header reports 3 (owner + 2 members)
    assert "Members (3):" in out, (
        f"expected combined count of 3 (owner + 2 members): {out!r}"
    )

    # Owner row must come BEFORE the existing members[] rows.
    # Find each line's index and assert ordering.
    owner_pos = out.find("100953901118675629504")
    dolphin_pos = out.find("dolphin")
    whale_pos = out.find("whale")
    assert owner_pos != -1
    assert dolphin_pos != -1
    assert whale_pos != -1
    assert owner_pos < dolphin_pos, (
        f"owner must appear before dolphin: owner@{owner_pos} dolphin@{dolphin_pos}"
    )
    assert dolphin_pos < whale_pos


def test_member_list_owner_prepended_json_mode(tmp_path):
    """JSON output includes the owner as a row with the same shape as
    members[] (user_id / email / display_name / role / source). External
    scripts can iterate the array and reach the owner like any other row."""
    project = {
        "name": "t",
        "owner": "100953901118675629504",
        "milestones": [],
        "members": [
            {"id": "dolphin", "display_name": "dolphin",
             "email": "dolphin@example.com", "role": "editor"},
        ],
    }
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps(project))
    rc, out, err = _run(["member", "list", "--json"], cwd=tmp_path)
    assert rc == 0, err
    rows = json.loads(out)
    assert isinstance(rows, list)
    assert len(rows) == 2, f"expected owner + 1 member, got {rows!r}"

    # Row 0 = owner
    owner = rows[0]
    assert owner.get("role") == "owner", f"row 0 must be owner: {owner!r}"
    assert owner.get("user_id") == "100953901118675629504"
    # Provenance marker so external tooling can tell synthetic owner row
    # apart from rows actually inside members[]
    assert owner.get("source") == "project.owner"

    # Row 1 = preserved existing member
    assert rows[1].get("id") == "dolphin"
    assert rows[1].get("role") == "editor"


def test_member_list_owner_already_in_members_no_dup(tmp_path):
    """If the project owner is ALSO listed inside members[] with
    role='owner' (same user_id), do not duplicate the row."""
    project = {
        "name": "t",
        "owner": "100953901118675629504",
        "milestones": [],
        "members": [
            {"user_id": "100953901118675629504", "id": "ownerguy",
             "display_name": "Owner Guy",
             "email": "owner@example.com", "role": "owner"},
            {"id": "dolphin", "email": "dolphin@example.com",
             "role": "editor"},
        ],
    }
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps(project))
    rc, out, err = _run(["member", "list", "--json"], cwd=tmp_path)
    assert rc == 0, err
    rows = json.loads(out)
    # 2 rows total (no synthetic duplicate). The owner row inside members[]
    # is preserved as-is.
    assert len(rows) == 2, f"unexpected duplication: {rows!r}"
    # The single owner row should be the one from members[] (carries the
    # full identity), not a synthetic stub.
    owner_rows = [r for r in rows if r.get("role") == "owner"]
    assert len(owner_rows) == 1
    assert owner_rows[0].get("email") == "owner@example.com"


def test_member_list_no_owner_no_members_keeps_empty_path(tmp_path):
    """AC #4 defensive guard: a project with neither owner nor members[]
    must still print the existing 'no members' guidance, not a crash."""
    project = {"name": "t", "milestones": [], "members": []}
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps(project))
    rc, out, err = _run(["member", "list"], cwd=tmp_path)
    assert rc == 0, err
    assert "no members" in out, (
        f"empty-project path regressed: {out!r}"
    )
