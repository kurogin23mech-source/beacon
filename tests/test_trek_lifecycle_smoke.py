"""End-to-end smoke test for the trek CLI lifecycle (ms-69 / e-1658).

SPEC AC #14: "4 project (Beacon / PE / LPS / TrailNode) のうち 2 つ以上を
scope に含む trek を 1 つ作成し、各 project の session が join → claim
→ stop → resume → archive のフルサイクルを実機で確認"

Approach: we simulate 4 distinct sessions (= different env vars) running
against the same trek storage. The trek's scope spans 4 projects; each
session represents a participant from a different project. We exercise:

  1. Alice (creator) creates the trek
  2. Alice plans 4 cross-project scope entries
  3. Alice invites Bob, Carol, Dave from 3 other projects
  4. Each invitee joins
  5. Alice starts the trek (planning → active)
  6. Bob pulls the Andon cord (stop with reason)
  7. Carol resumes (clear halt)
  8. Alice transfers leader to Bob (leader_session_id swap)
  9. Carol leaves
 10. Dave leaves
 11. Alice (now non-leader) tries to leave — should fail (no leader)
     so Alice transfers back to herself, then archives
 12. Verify final archived state has all the audit fields intact

Claim post (= SPEC `beacon claim post`) is ms-55 scope — not exercised
here; the CLI does not yet implement it. This smoke focuses on the
trek lifecycle proper.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BEACON = REPO_ROOT / "bin" / "beacon"


def _run(env, *args):
    """Run beacon CLI in an isolated cwd so ``bin/beacon`` can't walk up to
    a parent repo's cloud config and flip ``_is_cloud_mode()`` (e-1681
    regression: cmd_trek_* now dispatches to HTTP when cloud is detected)."""
    full_env = os.environ.copy()
    full_env.update(env)
    cwd = full_env.pop("BEACON_CWD", None)
    return subprocess.run(
        [str(BEACON), "trek", *args],
        env=full_env, capture_output=True, text=True, cwd=cwd,
    )


def _ok(result, *args):
    """Assert returncode 0 with helpful context on failure."""
    assert result.returncode == 0, (
        f"command failed: {' '.join(str(a) for a in args)}\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    return result


@pytest.fixture
def smoke_env(tmp_path):
    # Minimal isolated project so bin/beacon's walk-up settles inside tmp_path
    # rather than the host repo whose .beacon/config.json may set cloud mode.
    project_file = tmp_path / ".beacon" / "project.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text('{"name":"test","milestones":[],"operations":[]}\n')
    base = {
        "BEACON_TREKS_DIR": str(tmp_path / "treks"),
        "BEACON_CWD": str(tmp_path),
    }
    alice = {
        **base,
        "BEACON_USER_ID": "u-alice",
        "BEACON_USER_EMAIL": "alice@beacon.dev",
        "BEACON_SESSION_ID": "sv-alice-beacon-1",
    }
    bob = {
        **base,
        "BEACON_USER_ID": "u-bob",
        "BEACON_USER_EMAIL": "bob@pe.dev",
        "BEACON_SESSION_ID": "sv-bob-pe-1",
    }
    carol = {
        **base,
        "BEACON_USER_ID": "u-carol",
        "BEACON_USER_EMAIL": "carol@lps.dev",
        "BEACON_SESSION_ID": "sv-carol-lps-1",
    }
    dave = {
        **base,
        "BEACON_USER_ID": "u-dave",
        "BEACON_USER_EMAIL": "dave@trailnode.dev",
        "BEACON_SESSION_ID": "sv-dave-trailnode-1",
    }
    return {"alice": alice, "bob": bob, "carol": carol, "dave": dave}


def test_cross_project_trek_full_lifecycle(smoke_env):
    """Full trek lifecycle with 4 sessions spanning 4 projects."""
    alice = smoke_env["alice"]
    bob = smoke_env["bob"]
    carol = smoke_env["carol"]
    dave = smoke_env["dave"]

    # --- 1. Alice creates the trek
    r = _ok(_run(alice, "create",
                 "Cross-Project Release Sprint",
                 "--type", "temporary",
                 "--description", "Coordinate Beacon + PE + LPS + TrailNode "
                                  "for the v1.0 release",
                 "--json"))
    trek_id = json.loads(r.stdout)["trek_id"]
    assert trek_id.startswith("tk-")

    # --- 2. Alice plans cross-project scope (4 projects)
    _ok(_run(alice, "plan", trek_id, "--add-scope", "beacon-b95643:ms-69"),
        "plan beacon")
    _ok(_run(alice, "plan", trek_id, "--add-scope", "pe-xxx:op-12"), "plan pe")
    _ok(_run(alice, "plan", trek_id, "--add-scope", "lps-xxx:e-1234"), "plan lps")
    _ok(_run(alice, "plan", trek_id, "--add-scope", "trailnode-xxx"), "plan tn")

    r = _ok(_run(alice, "show", trek_id, "--json"))
    doc = json.loads(r.stdout)
    assert len(doc["scope"]) == 4
    projects_in_scope = {s["project"] for s in doc["scope"]}
    assert projects_in_scope == {
        "beacon-b95643", "pe-xxx", "lps-xxx", "trailnode-xxx",
    }

    # --- 3. Alice invites Bob, Carol, Dave (different projects' sessions)
    _ok(_run(alice, "invite", trek_id, "--actor", "bob@pe.dev"),
        "invite bob")
    _ok(_run(alice, "invite", trek_id, "--actor", "carol@lps.dev"),
        "invite carol")
    _ok(_run(alice, "invite", trek_id, "--actor", "dave@trailnode.dev"),
        "invite dave")

    r = _ok(_run(alice, "show", trek_id, "--json"))
    doc = json.loads(r.stdout)
    assert len(doc["members"]) == 4
    # Alice already joined as leader; others joined_at empty until they join
    not_yet_joined = [m for m in doc["members"] if not m.get("joined_at")]
    assert len(not_yet_joined) == 3

    # --- 4. Each invitee joins from their own session
    _ok(_run(bob, "join", trek_id), "bob join")
    _ok(_run(carol, "join", trek_id), "carol join")
    _ok(_run(dave, "join", trek_id), "dave join")

    r = _ok(_run(alice, "show", trek_id, "--json"))
    doc = json.loads(r.stdout)
    assert all(m["joined_at"] for m in doc["members"]), \
        "every member should now be joined"

    # --- 5. Alice transitions planning → active
    _ok(_run(alice, "start", trek_id), "start")
    r = _ok(_run(alice, "show", trek_id, "--json"))
    assert json.loads(r.stdout)["status"] == "active"

    # --- 6. Bob pulls the Andon cord (anyone can stop)
    _ok(_run(bob, "stop", trek_id, "--reason", "PE pipeline regression"),
        "bob stop")
    r = _ok(_run(alice, "show", trek_id, "--json"))
    doc = json.loads(r.stdout)
    assert doc["halt"] is not None
    assert doc["halt"]["issued_by_session_id"] == "sv-bob-pe-1"
    assert doc["halt"]["reason"] == "PE pipeline regression"
    assert doc["status"] == "active"  # halt does NOT change status

    # --- 7. Carol resumes (clears halt)
    _ok(_run(carol, "resume", trek_id), "carol resume")
    r = _ok(_run(alice, "show", trek_id, "--json"))
    assert json.loads(r.stdout)["halt"] is None

    # --- 8. Alice transfers leader to Bob
    assert json.loads(_ok(_run(alice, "show", trek_id, "--json")).stdout)[
        "leader_session_id"
    ] == "sv-alice-beacon-1"
    _ok(_run(alice, "transfer-leader", trek_id, "--to", "sv-bob-pe-1"),
        "transfer to bob")
    r = _ok(_run(alice, "show", trek_id, "--json"))
    assert json.loads(r.stdout)["leader_session_id"] == "sv-bob-pe-1"

    # --- 9. Carol leaves (she's a member, not leader)
    _ok(_run(carol, "leave", trek_id), "carol leave")
    r = _ok(_run(alice, "show", trek_id, "--json"))
    emails = {m["email"] for m in json.loads(r.stdout)["members"]}
    assert "carol@lps.dev" not in emails

    # --- 10. Dave leaves
    _ok(_run(dave, "leave", trek_id), "dave leave")
    r = _ok(_run(alice, "show", trek_id, "--json"))
    emails = {m["email"] for m in json.loads(r.stdout)["members"]}
    assert emails == {"alice@beacon.dev", "bob@pe.dev"}

    # --- 11. Alice (now non-leader after step 8) tries to leave
    #         The "leader" *member* (= role=leader, separate from leader_session_id)
    #         is still Alice from creation. The remove_member guard checks role,
    #         not leader_session_id, so this should fail.
    r = _run(alice, "leave", trek_id)
    assert r.returncode != 0, "Alice still has role=leader, must transfer first"
    assert "leader" in r.stderr.lower()

    # --- 12. Bob (the new leader session) archives the trek
    #         (the role-level leader is still Alice; we archive directly without
    #         transferring role membership — archive is allowed by any role per
    #         the current local-mode CLI; cloud auth refines this in e-1656)
    _ok(_run(bob, "archive", trek_id), "archive")
    r = _ok(_run(alice, "show", trek_id, "--json"))
    final = json.loads(r.stdout)
    assert final["status"] == "archived"
    assert final["archived_at"]
    # audit fields intact for posterity
    assert final["title"] == "Cross-Project Release Sprint"
    assert final["type"] == "temporary"
    assert final["creator_actor"]["email"] == "alice@beacon.dev"
    assert len(final["scope"]) == 4
    # archived treks default-hide from list
    r = _ok(_run(alice, "list", "--all-actors", "--json"))
    assert json.loads(r.stdout) == []
    r = _ok(_run(alice, "list", "--all-actors", "--all", "--json"))
    assert len(json.loads(r.stdout)) == 1


def test_archived_is_terminal_no_revive(smoke_env):
    """Confirm SPEC 方針 2: archived → active path is blocked."""
    alice = smoke_env["alice"]
    r = _ok(_run(alice, "create", "Brief Coordinate", "--json"))
    tid = json.loads(r.stdout)["trek_id"]
    _ok(_run(alice, "archive", tid))
    r_bad = _run(alice, "start", tid)
    assert r_bad.returncode != 0
    assert "archived" in r_bad.stderr.lower() or "invalid" in r_bad.stderr.lower()


def test_halt_persists_across_sessions(smoke_env):
    """Halt set by one session is visible to all other sessions reading the trek."""
    alice = smoke_env["alice"]
    bob = smoke_env["bob"]
    r = _ok(_run(alice, "create", "Coord", "--json"))
    tid = json.loads(r.stdout)["trek_id"]
    _ok(_run(alice, "invite", tid, "--actor", "bob@pe.dev"))
    _ok(_run(bob, "join", tid))
    _ok(_run(alice, "start", tid))
    _ok(_run(alice, "stop", tid, "--reason", "discussing"))
    # Bob can see the halt
    r = _ok(_run(bob, "show", tid, "--json"))
    doc = json.loads(r.stdout)
    assert doc["halt"]["issued_by_session_id"] == "sv-alice-beacon-1"
    assert doc["halt"]["reason"] == "discussing"
