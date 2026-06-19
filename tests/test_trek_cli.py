"""CLI smoke tests for `beacon trek` (ms-69 / e-1653).

Spawns bin/beacon as a subprocess, points trek storage at a tmp dir via
BEACON_TREKS_DIR, exercises create → list → show → start → archive →
listing semantics + transition rejection.

These tests confirm:
- bash wiring → commands.py dispatcher → trek_store round trip
- creator identity gating (email + session_id required)
- list visibility filter (actor-scoped vs --all-actors)
- archived hides by default, --all surfaces it
- terminal archived state rejects start
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BEACON = REPO_ROOT / "bin" / "beacon"


def _run(env_extra: dict, *args: str) -> subprocess.CompletedProcess:
    """Run beacon CLI in an isolated cwd (= the tmp project dir from the
    fixture) so ``bin/beacon`` doesn't walk up to a parent repo's cloud
    config and flip ``_is_cloud_mode()`` to True (e-1681 regression).

    The fixture stamps ``BEACON_CWD`` into env_extra; we pop it out and
    hand it to ``subprocess.run(cwd=)``.
    """
    env = os.environ.copy()
    env.update(env_extra)
    cwd = env.pop("BEACON_CWD", None)
    return subprocess.run(
        [str(BEACON), "trek", *args],
        env=env, capture_output=True, text=True, cwd=cwd,
    )


@pytest.fixture
def trek_env(tmp_path):
    """Base env with BEACON_TREKS_DIR pointing at a per-test tmp dir.

    The fixture also creates a minimal .beacon/project.json INSIDE
    ``tmp_path`` and asks ``_run`` to launch the subprocess with cwd at
    that tmp_path. This way ``bin/beacon``'s ``find_beacon_root`` walk
    settles on the tmp project (which has no cloud config) instead of
    the parent repo's worktree, which can have cloud mode set globally.

    BEACON_PROJECT_FILE is intentionally NOT passed in env_extra because
    ``bin/beacon`` line 7 unconditionally re-exports it to the relative
    ``.beacon/project.json``; the cwd-based isolation is the only path
    that actually shields the test from parent cloud config.
    """
    treks_dir = tmp_path / "treks"
    project_file = tmp_path / ".beacon" / "project.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text('{"name":"test","milestones":[],"operations":[]}\n')
    return {
        "BEACON_TREKS_DIR": str(treks_dir),
        "BEACON_USER_ID": "u-test",
        "BEACON_USER_EMAIL": "test@example.com",
        "BEACON_SESSION_ID": "sv-test-1",
        "BEACON_CWD": str(tmp_path),
    }


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_trek_create_basic(trek_env):
    r = _run(trek_env, "create", "My Trek", "--type", "temporary", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["title"] == "My Trek"
    assert doc["type"] == "temporary"
    assert doc["status"] == "planning"
    assert doc["leader_session_id"] == "sv-test-1"
    assert doc["creator_actor"] == {"user_id": "u-test", "email": "test@example.com"}
    assert doc["halt"] is None


def test_trek_create_requires_email(trek_env):
    env = dict(trek_env)
    env.pop("BEACON_USER_EMAIL")
    r = _run(env, "create", "x", "--json")
    assert r.returncode != 0
    assert "EMAIL" in r.stderr


def test_trek_create_requires_session_id(trek_env):
    env = dict(trek_env)
    env.pop("BEACON_SESSION_ID")
    r = _run(env, "create", "x", "--json")
    assert r.returncode != 0
    assert "SESSION_ID" in r.stderr or "session" in r.stderr.lower()


def test_trek_create_default_type_is_persistent(trek_env):
    r = _run(trek_env, "create", "x", "--json")
    assert r.returncode == 0
    assert json.loads(r.stdout)["type"] == "persistent"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_trek_list_empty(trek_env):
    r = _run(trek_env, "list", "--json")
    assert r.returncode == 0
    assert json.loads(r.stdout) == []


def test_trek_list_returns_created(trek_env):
    _run(trek_env, "create", "T1", "--json")
    _run(trek_env, "create", "T2", "--json")
    r = _run(trek_env, "list", "--json")
    assert r.returncode == 0
    titles = {t["title"] for t in json.loads(r.stdout)}
    assert titles == {"T1", "T2"}


def test_trek_list_actor_scoped_default(trek_env):
    """Default list filters by current actor."""
    _run(trek_env, "create", "Mine", "--json")
    env_other = dict(trek_env)
    env_other.update({
        "BEACON_USER_ID": "u-other",
        "BEACON_USER_EMAIL": "other@example.com",
    })
    _run(env_other, "create", "Theirs", "--json")
    # current user only sees their own
    r = _run(trek_env, "list", "--json")
    visible = {t["title"] for t in json.loads(r.stdout)}
    assert visible == {"Mine"}
    # --all-actors disables filter
    r_all = _run(trek_env, "list", "--all-actors", "--json")
    visible_all = {t["title"] for t in json.loads(r_all.stdout)}
    assert visible_all == {"Mine", "Theirs"}


def test_trek_list_hides_archived_by_default(trek_env):
    r = _run(trek_env, "create", "soon-archived", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    _run(trek_env, "start", tid)
    _run(trek_env, "archive", tid)
    r_default = _run(trek_env, "list", "--json")
    assert json.loads(r_default.stdout) == []
    r_all = _run(trek_env, "list", "--all", "--json")
    assert len(json.loads(r_all.stdout)) == 1


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def test_trek_show_existing(trek_env):
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    r2 = _run(trek_env, "show", tid, "--json")
    assert r2.returncode == 0
    assert json.loads(r2.stdout)["trek_id"] == tid


def test_trek_show_missing(trek_env):
    r = _run(trek_env, "show", "tk-nope", "--json")
    assert r.returncode != 0
    assert "not found" in r.stderr


# ---------------------------------------------------------------------------
# state transitions: start / archive
# ---------------------------------------------------------------------------

def test_trek_start_transitions_planning_to_active(trek_env):
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    r_start = _run(trek_env, "start", tid, "--json")
    assert r_start.returncode == 0
    assert json.loads(r_start.stdout)["status"] == "active"


def test_trek_archive_from_active(trek_env):
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    _run(trek_env, "start", tid)
    r_a = _run(trek_env, "archive", tid, "--json")
    assert r_a.returncode == 0
    doc = json.loads(r_a.stdout)
    assert doc["status"] == "archived"
    assert doc["archived_at"]


def test_trek_archive_from_planning(trek_env):
    """planning → archived should work (= cancel before start)."""
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    r_a = _run(trek_env, "archive", tid, "--json")
    assert r_a.returncode == 0
    assert json.loads(r_a.stdout)["status"] == "archived"


def test_trek_archived_is_terminal(trek_env):
    """archived → start must reject (= terminal state, SPEC 方針 2)."""
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    _run(trek_env, "start", tid)
    _run(trek_env, "archive", tid)
    r_bad = _run(trek_env, "start", tid)
    assert r_bad.returncode != 0
    assert "invalid trek transition" in r_bad.stderr.lower() or "archived" in r_bad.stderr


def test_trek_start_from_archived_planning_rejected(trek_env):
    """planning → archived → start should also reject."""
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    _run(trek_env, "archive", tid)
    r_bad = _run(trek_env, "start", tid)
    assert r_bad.returncode != 0


def test_trek_start_missing_trek(trek_env):
    r = _run(trek_env, "start", "tk-missing")
    assert r.returncode != 0
    assert "not found" in r.stderr


# ---------------------------------------------------------------------------
# invite / join / leave (e-1654)
# ---------------------------------------------------------------------------

def _make_trek_and_return_id(trek_env, title="T") -> str:
    r = _run(trek_env, "create", title, "--json")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["trek_id"]


def test_trek_invite_basic(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "invite", tid, "--actor", "b@x.com", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    emails = {m["email"] for m in doc["members"]}
    assert "b@x.com" in emails
    invitee = next(m for m in doc["members"] if m["email"] == "b@x.com")
    assert invitee["role"] == "member"
    assert invitee["joined_at"] == ""  # invited but not yet joined


def test_trek_invite_requires_actor(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "invite", tid)
    assert r.returncode != 0
    assert "actor" in r.stderr.lower() or "email" in r.stderr.lower()


def test_trek_invite_rejects_duplicate(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    r = _run(trek_env, "invite", tid, "--actor", "b@x.com")
    assert r.returncode != 0
    assert "already" in r.stderr.lower()


def test_trek_invite_notify_acknowledged_but_noop(trek_env):
    """--notify should not fail and should mention deferred implementation."""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "invite", tid, "--actor", "b@x.com", "--notify")
    assert r.returncode == 0
    assert "notify" in r.stdout.lower()  # acknowledged in human output


def test_trek_join_after_invite(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    # B joins from their own session
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r = _run(env_b, "join", tid, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    b_member = next(m for m in doc["members"] if m["email"] == "b@x.com")
    assert b_member["joined_at"]  # now joined


# ---------------------------------------------------------------------------
# Auto-arm (ms-75 / e-2047): default arms session, --no-arm opts out
# ---------------------------------------------------------------------------

def test_trek_join_auto_arms_session_by_default(trek_env):
    """beacon trek join (= without --no-arm) should add 3 trek channels to
    bus_auto_execute_channels, write .beacon/bus-budget.json with 20 turns,
    and report the arm summary in JSON output."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r = _run(env_b, "join", tid, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    arm = doc.get("_arm")
    assert arm is not None
    assert arm["trek_id"] == tid
    assert arm["budget_turns"] == 20
    # All 3 trek channels must be in the post-arm allowlist.
    expected = {"trek-progress-check", "trek-trigger", "trek-task-review"}
    assert expected.issubset(set(arm["channels"]))
    # Three brand-new channels for the first-ever join.
    assert set(arm["channels_added"]) == expected

    # project.json reflects the channel allowlist write.
    cwd = trek_env["BEACON_CWD"]
    with open(Path(cwd) / ".beacon" / "project.json") as f:
        proj = json.load(f)
    assert expected.issubset(set(proj.get("bus_auto_execute_channels") or []))

    # bus-budget.json holds the 20-turn grant + trek_id audit marker.
    with open(Path(cwd) / ".beacon" / "bus-budget.json") as f:
        budget = json.load(f)
    assert budget["total"] == 20
    assert budget["used"] == 0
    assert budget["trek_id"] == tid


def test_trek_join_no_arm_flag_skips_auto_arm(trek_env):
    """--no-arm: trek joined, but no bus_auto_execute_channels mutation and
    no bus-budget.json write. JSON output carries _arm.skipped marker."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r = _run(env_b, "join", tid, "--no-arm", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["_arm"] == {"skipped": True, "reason": "--no-arm"}

    cwd = trek_env["BEACON_CWD"]
    with open(Path(cwd) / ".beacon" / "project.json") as f:
        proj = json.load(f)
    # No trek channels added with opt-out.
    auto_chans = set(proj.get("bus_auto_execute_channels") or [])
    assert "trek-progress-check" not in auto_chans
    assert "trek-trigger" not in auto_chans
    # No budget file (= bus-budget.json not created).
    assert not (Path(cwd) / ".beacon" / "bus-budget.json").exists()


def test_trek_join_auto_arm_is_idempotent_on_channels(trek_env):
    """Re-joining a trek (= after leave + rejoin, or a second member joining
    the same project's trek) must not duplicate channel entries. Budget
    is unconditionally refreshed though, since rejoining signals intent
    to be armed again."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r1 = _run(env_b, "join", tid, "--json")
    assert r1.returncode == 0
    # leave + re-invite + re-join to exercise the idempotent path.
    _run(env_b, "leave", tid)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    r2 = _run(env_b, "join", tid, "--json")
    assert r2.returncode == 0
    doc = json.loads(r2.stdout)
    arm = doc["_arm"]
    # Second join: channels already in allowlist → channels_added is empty.
    assert arm["channels_added"] == []
    # But the channels list still reflects the trek channels.
    expected = {"trek-progress-check", "trek-trigger", "trek-task-review"}
    assert expected.issubset(set(arm["channels"]))


def test_trek_join_auto_arm_human_output_mentions_skill_hint(trek_env):
    """Non-JSON output should tell the user to start /beacon-bus-armed
    Skill — the 3rd auto-arm action per AC 1."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r = _run(env_b, "join", tid)
    assert r.returncode == 0, r.stderr
    assert "/beacon-bus-armed" in r.stdout
    assert "budget granted" in r.stdout
    assert "--no-arm" in r.stdout  # opt-out reminder


def test_trek_join_rejects_uninvited(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    env_stranger = dict(trek_env)
    env_stranger.update({
        "BEACON_USER_ID": "u-stranger",
        "BEACON_USER_EMAIL": "stranger@x",
    })
    r = _run(env_stranger, "join", tid)
    assert r.returncode != 0
    assert "no invitation" in r.stderr.lower() or "not invited" in r.stderr.lower()


def test_trek_leave_removes_member(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({"BEACON_USER_ID": "u-b", "BEACON_USER_EMAIL": "b@x.com"})
    _run(env_b, "join", tid)
    r = _run(env_b, "leave", tid, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    emails = {m["email"] for m in doc["members"]}
    assert "b@x.com" not in emails


def test_trek_leave_blocks_leader(trek_env):
    """Leader cannot leave (must transfer-leader first)."""
    tid = _make_trek_and_return_id(trek_env)
    # creator is the leader; trying to leave should fail
    r = _run(trek_env, "leave", tid)
    assert r.returncode != 0
    assert "leader" in r.stderr.lower()


def test_trek_leave_non_member(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    env_stranger = dict(trek_env)
    env_stranger.update({
        "BEACON_USER_ID": "u-stranger",
        "BEACON_USER_EMAIL": "stranger@x",
    })
    r = _run(env_stranger, "leave", tid)
    assert r.returncode != 0
    assert "not a member" in r.stderr.lower()


# ---------------------------------------------------------------------------
# plan (= scope editing) (e-1655)
# ---------------------------------------------------------------------------

def test_trek_plan_add_scope_milestone(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "beacon-1:ms-64", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert {"project": "beacon-1", "milestone": "ms-64"} in doc["scope"]


def test_trek_plan_add_scope_operation(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "pe-1:op-12", "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert {"project": "pe-1", "operation": "op-12"} in doc["scope"]


def test_trek_plan_add_scope_task(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "lps-1:e-1234", "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert {"project": "lps-1", "task": "e-1234"} in doc["scope"]


def test_trek_plan_add_scope_project_wide(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "lps-1", "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert {"project": "lps-1"} in doc["scope"]


def test_trek_plan_remove_scope(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "plan", tid, "--add-scope", "beacon-1:ms-64")
    _run(trek_env, "plan", tid, "--add-scope", "pe-1")
    r = _run(trek_env, "plan", tid, "--remove-scope", "beacon-1:ms-64", "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert doc["scope"] == [{"project": "pe-1"}]


def test_trek_plan_requires_add_or_remove(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid)
    assert r.returncode != 0
    assert "add-scope" in r.stderr or "remove-scope" in r.stderr


def test_trek_plan_rejects_both_add_and_remove(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid,
             "--add-scope", "a:ms-1",
             "--remove-scope", "a:ms-1")
    assert r.returncode != 0
    assert "not both" in r.stderr.lower() or "one" in r.stderr.lower()


def test_trek_plan_add_scope_rejects_duplicate(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "plan", tid, "--add-scope", "beacon-1:ms-64")
    r = _run(trek_env, "plan", tid, "--add-scope", "beacon-1:ms-64")
    assert r.returncode != 0
    assert "already" in r.stderr.lower()


def test_trek_plan_remove_scope_missing(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--remove-scope", "beacon-1:ms-64")
    assert r.returncode != 0
    assert "not found" in r.stderr.lower()


def test_trek_plan_unknown_ref_prefix(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "beacon-1:foo-99")
    assert r.returncode != 0
    assert "unknown" in r.stderr.lower() or "ref" in r.stderr.lower()


# ---------------------------------------------------------------------------
# stop / resume / transfer-leader (e-1662)
# ---------------------------------------------------------------------------

def test_trek_stop_sets_halt(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "start", tid)
    r = _run(trek_env, "stop", tid, "--reason", "deploy in progress", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["status"] == "active"  # status unchanged
    assert doc["halt"]
    assert doc["halt"]["issued_by_session_id"] == "sv-test-1"
    assert doc["halt"]["reason"] == "deploy in progress"


def test_trek_stop_rejects_planning(trek_env):
    """Cannot stop a trek that hasn't started."""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "stop", tid)
    assert r.returncode != 0
    assert "active" in r.stderr.lower()


def test_trek_stop_requires_session_id(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "start", tid)
    env = dict(trek_env)
    env.pop("BEACON_SESSION_ID")
    r = _run(env, "stop", tid)
    assert r.returncode != 0
    assert "session" in r.stderr.lower()


def test_trek_resume_clears_halt(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "start", tid)
    _run(trek_env, "stop", tid)
    r = _run(trek_env, "resume", tid, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["halt"] is None
    assert doc["status"] == "active"


def test_trek_resume_idempotent(trek_env):
    """Resuming an unhalted trek is a no-op (= no error)."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "start", tid)
    r = _run(trek_env, "resume", tid)
    assert r.returncode == 0
    assert "not halted" in r.stdout.lower() or "no-op" in r.stdout.lower()


def test_trek_transfer_leader_basic(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "transfer-leader", tid, "--to", "sv-new-leader", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["leader_session_id"] == "sv-new-leader"


def test_trek_transfer_leader_requires_to(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "transfer-leader", tid)
    assert r.returncode != 0
    assert "to" in r.stderr.lower() or "session" in r.stderr.lower()
