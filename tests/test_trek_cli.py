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
    env = os.environ.copy()
    env.update(env_extra)
    return subprocess.run(
        [str(BEACON), "trek", *args],
        env=env, capture_output=True, text=True,
    )


@pytest.fixture
def trek_env(tmp_path):
    """Base env with BEACON_TREKS_DIR pointing at a per-test tmp dir."""
    treks_dir = tmp_path / "treks"
    return {
        "BEACON_TREKS_DIR": str(treks_dir),
        "BEACON_USER_ID": "u-test",
        "BEACON_USER_EMAIL": "test@example.com",
        "BEACON_SESSION_ID": "sv-test-1",
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
