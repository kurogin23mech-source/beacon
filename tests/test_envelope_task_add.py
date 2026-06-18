"""Tests for AI-autonomous task.add envelope decision (ms-83 / e-2000).

The CORE doc (= QvyVwRU8otQEn5iMfP36) declares that ``task.add`` against
an MS in the envelope's scope is auto-permitted for T2 and T1-system,
and degrades to propose-to-ai when the MS is out of scope. This file
pins both the pure helpers and the integration endpoint.

Coverage:

  Pure:
    (1) check_task_add_scope_match — T1 / T2 in-scope / T2 out-of-scope /
        T1-system (defers to caller) / T3
    (2) trek_scope_includes_ms — match / no-match / no-milestone entry

  Integration:
    (3) endpoint with T2 envelope, MS in scope → auto
    (4) endpoint with T2 envelope, MS not in scope → propose
    (5) endpoint with T1-system envelope, trek scope includes MS → auto
    (6) endpoint with T1-system envelope, trek scope does NOT include
        MS → propose
    (7) endpoint with invalid envelope (= broken signature) → reject
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"
os.environ["BEACON_SCHEDULER_INTERNAL_KEY"] = "test-scheduler-key"

import envelope as env_mod  # noqa: E402

PROJECT_ID = "proj-x"
TREK_ID = "tk-12345678"


# ---------------------------------------------------------------------------
# (1) check_task_add_scope_match — pure
# ---------------------------------------------------------------------------

def test_check_task_add_t1_always_permits():
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T1, issuer="u@a", project_id=PROJECT_ID,
        actions_authorized=["task.add"], scope=None,
    )
    assert env_mod.check_task_add_scope_match(env, "ms-83") is True


def test_check_task_add_t2_exact_scope_match():
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T2, issuer="op", project_id=PROJECT_ID,
        actions_authorized=["task.add"], scope="ms:ms-83",
    )
    assert env_mod.check_task_add_scope_match(env, "ms-83") is True
    assert env_mod.check_task_add_scope_match(env, "ms-84") is False


def test_check_task_add_t2_multi_ms_scope_token():
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T2, issuer="op", project_id=PROJECT_ID,
        actions_authorized=["task.add"],
        scope="op:op-7|ms:ms-83|ms:ms-84",
    )
    assert env_mod.check_task_add_scope_match(env, "ms-83") is True
    assert env_mod.check_task_add_scope_match(env, "ms-84") is True
    # ms-8 must NOT match because the token boundary check rejects substrings.
    assert env_mod.check_task_add_scope_match(env, "ms-8") is False


def test_check_task_add_t1_system_defers_true_pure():
    """The pure helper returns True for T1-system (= 'tier permits, defer
    to scope walk'). The actual trek scope walk happens in the caller."""
    env = env_mod.issue_t1_system_envelope(
        project_id=PROJECT_ID, trek_id=TREK_ID,
        actions_authorized=["task.add"],
    )
    assert env_mod.check_task_add_scope_match(env, "ms-83") is True


def test_check_task_add_t3_rejects():
    """T3 reply envelopes never auto-permit task.add."""
    # T3 has scope=None; manually construct via issue_envelope:
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T3, issuer="u@a", project_id=PROJECT_ID,
        actions_authorized=["task.add"], scope=None,
    )
    assert env_mod.check_task_add_scope_match(env, "ms-83") is False


# ---------------------------------------------------------------------------
# (2) trek_scope_includes_ms
# ---------------------------------------------------------------------------

def test_trek_scope_includes_ms_match():
    trek = {
        "trek_id": TREK_ID,
        "scope": [
            {"project": "beacon-1", "milestone": "ms-83"},
            {"project": "beacon-1", "task": "e-1994"},
        ],
    }
    assert env_mod.trek_scope_includes_ms(trek, "ms-83") is True


def test_trek_scope_includes_ms_no_match():
    trek = {
        "trek_id": TREK_ID,
        "scope": [
            {"project": "beacon-1", "milestone": "ms-99"},
        ],
    }
    assert env_mod.trek_scope_includes_ms(trek, "ms-83") is False


def test_trek_scope_includes_ms_empty_scope():
    assert env_mod.trek_scope_includes_ms({"scope": []}, "ms-83") is False
    assert env_mod.trek_scope_includes_ms({}, "ms-83") is False


def test_trek_scope_includes_ms_target_required():
    trek = {"scope": [{"project": "p", "milestone": "ms-83"}]}
    assert env_mod.trek_scope_includes_ms(trek, "") is False


# ---------------------------------------------------------------------------
# (3)-(7) integration — HTTP endpoint
# ---------------------------------------------------------------------------

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db

_treks: dict[str, dict] = {}
_projects: dict[str, dict] = {}


def _mock_get_trek(trek_id: str):
    data = _treks.get(trek_id)
    return copy.deepcopy(data) if data else None


def _mock_get_project(project_id: str):
    data = _projects.get(project_id)
    return copy.deepcopy(data) if data else None


@pytest.fixture(autouse=True)
def _rebind():
    db_module = app_module.db
    prior = {}
    for name, mock in [
        ("get_trek", _mock_get_trek),
        ("get_project", _mock_get_project),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    _treks.clear()
    _projects.clear()
    # Reset the module-level nonce singleton so each test starts clean.
    if hasattr(app_module, "_envelope_nonce_store_singleton"):
        delattr(app_module, "_envelope_nonce_store_singleton")
    yield
    for name, val in prior.items():
        if val is None and hasattr(db_module, name):
            delattr(db_module, name)
        elif val is not None:
            setattr(db_module, name, val)


app_module._auth_enabled = False
client = TestClient(app_module.app)


def _seed_project_with_member():
    _projects[PROJECT_ID] = {
        "project_id": PROJECT_ID,
        "name": "test",
        "members": [{"user_id": "u-test", "role": "owner"}],
    }


def _seed_trek_with_scope(*, ms_list: list[str]):
    scope = [{"project": PROJECT_ID, "milestone": m} for m in ms_list]
    _treks[TREK_ID] = {
        "trek_id": TREK_ID,
        "status": "active",
        "scope": scope,
        "members": [],
        "creator_actor": {"user_id": "u-1", "email": "a@b"},
    }


# (3) T2 in-scope → auto
def test_endpoint_t2_in_scope_returns_auto():
    _seed_project_with_member()
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T2, issuer="op", project_id=PROJECT_ID,
        actions_authorized=["task.add"], scope="ms:ms-83",
    )
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/bus/envelope/check-task-add",
        json={"envelope": env, "target_ms": "ms-83", "payload": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["permit"] == "auto"
    assert body["reason"] == "t2_scope_match"


# (4) T2 out-of-scope → propose
def test_endpoint_t2_out_of_scope_returns_propose():
    _seed_project_with_member()
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T2, issuer="op", project_id=PROJECT_ID,
        actions_authorized=["task.add"], scope="ms:ms-83",
    )
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/bus/envelope/check-task-add",
        json={"envelope": env, "target_ms": "ms-84", "payload": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["permit"] == "propose"


# (5) T1-system + trek scope includes MS → auto
def test_endpoint_t1_system_trek_scope_match_returns_auto():
    _seed_project_with_member()
    _seed_trek_with_scope(ms_list=["ms-83", "ms-84"])
    env = env_mod.issue_t1_system_envelope(
        project_id=PROJECT_ID, trek_id=TREK_ID,
        actions_authorized=["task.add"],
    )
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/bus/envelope/check-task-add",
        json={"envelope": env, "target_ms": "ms-83", "payload": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["permit"] == "auto"
    assert body["reason"] == "t1_system_trek_scope_match"


# (6) T1-system + trek scope mismatch → propose
def test_endpoint_t1_system_trek_scope_mismatch_returns_propose():
    _seed_project_with_member()
    _seed_trek_with_scope(ms_list=["ms-99"])
    env = env_mod.issue_t1_system_envelope(
        project_id=PROJECT_ID, trek_id=TREK_ID,
        actions_authorized=["task.add"],
    )
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/bus/envelope/check-task-add",
        json={"envelope": env, "target_ms": "ms-83", "payload": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["permit"] == "propose"
    assert "scope_mismatch" in body["reason"]


# (7) Broken envelope (= tampered signature) → reject
def test_endpoint_invalid_envelope_returns_reject():
    _seed_project_with_member()
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T2, issuer="op", project_id=PROJECT_ID,
        actions_authorized=["task.add"], scope="ms:ms-83",
    )
    env["actions_authorized"] = ["deploy"]  # post-sign tamper
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/bus/envelope/check-task-add",
        json={"envelope": env, "target_ms": "ms-83", "payload": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["permit"] == "reject"
