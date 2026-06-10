"""Server-side envelope enforcement on high-risk endpoints (ms-60 / e-1344).

Defense in depth: AI-side self-check (Skill ``/beacon-operation-execute`` Step
4) is layer 1. Even if the AI bypasses its own check (intentionally or via
prompt injection), the server demands a valid T2 envelope authorizing the
exact ``requested_action`` before the destructive mutation runs. This is the
"銃はガラスの向こう" principle (CORE doc ``1UGomhHqCQo0iYSRtCdB``).

Five endpoints are protected in Phase 1:

  * ``POST /api/projects/{id}/archive``           → ``project.archive``
  * ``POST /api/projects/{id}/migrate-to-v2``     → ``project.migrate.v2``
  * ``POST /api/projects/{id}/milestones/{ms}/purge``    → ``milestone.purge``
  * ``POST /api/projects/{id}/entries/{e}/purge``        → ``entry.purge``
  * ``POST /api/projects/{id}/operations/{op}/purge``    → ``operation.purge``

Test strategy: the archive endpoint covers all five envelope failure modes
(no header / malformed / wrong action / expired / valid pass). The other
four endpoints get smoke coverage (403 without header, 200 with valid
envelope) — the shared dependency means deeper coverage on archive is
sufficient.

Auth and storage are swapped for in-memory mocks (mirroring
``tests/test_purge_api.py``) so we can exercise the envelope gate without
real Google token verification or Firestore round-trips.
"""

from __future__ import annotations

import base64
import copy
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Route operations.apply_operation through the in-memory mock. Must be set
# BEFORE importing operations / app.
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

# Stable envelope-signing secret so tests don't accidentally mint with a
# value that differs from what the server sees. Set before import.
os.environ.setdefault("BEACON_ENVELOPE_SECRET", "test-envelope-secret-e1344")

import firestore_client  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import envelope as env_mod  # noqa: E402


PROJECT_ID = "test-project-e1344"
OWNER_UID = "uid-owner"

_store: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# In-memory firestore mock (same shape used by test_purge_api.py)
# ---------------------------------------------------------------------------

def _mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def _mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def _mock_list_projects():
    return [{"project_id": pid} for pid in _store]


def _seed_project() -> dict:
    return {
        "name": "Envelope enforcement fixture",
        "owner": OWNER_UID,
        "members": [
            {"user_id": OWNER_UID, "email": "owner@x", "role": "owner"},
        ],
        "milestones": [
            {
                "id": "ms-1", "title": "Seed MS", "status": "in_progress",
                "progress": 0, "target_date": "", "commits": [],
                "entries": [
                    {"id": "e-1", "type": "task", "description": "Seed task",
                     "status": "todo", "meta": {}},
                ],
            },
        ],
        "operations": [
            {"id": "op-1", "status": "open", "title": "Seed op", "entries": []},
        ],
    }


client = TestClient(app_module.app)


def _rebind_db():
    db_module = app_module.db
    prior = {
        "get_project": getattr(db_module, "get_project", None),
        "save_project": getattr(db_module, "save_project", None),
        "list_projects": getattr(db_module, "list_projects", None),
    }
    db_module.get_project = _mock_get_project
    db_module.save_project = _mock_save_project
    db_module.list_projects = _mock_list_projects
    prior_module = sys.modules.get("firestore_client")
    sys.modules["firestore_client"] = db_module
    return prior, prior_module


def _restore_db(prior, prior_module):
    db_module = app_module.db
    for k, v in prior.items():
        if v is None:
            if hasattr(db_module, k):
                delattr(db_module, k)
        else:
            setattr(db_module, k, v)
    if prior_module is not None:
        sys.modules["firestore_client"] = prior_module


@pytest.fixture(autouse=True)
def reset_store():
    prior_db, prior_module = _rebind_db()
    _store.clear()
    _store[PROJECT_ID] = _seed_project()
    # Each test starts with no auth override; _impersonate_owner() opts in.
    app_module.app.dependency_overrides.clear()
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    # Swap in an in-memory nonce store so the verify pipeline runs hermetic.
    # The production helper returns a Firestore-backed store; tests don't
    # touch Firestore.
    nonce_store = env_mod.InMemoryNonceStore()
    prior_nonce_helper = app_module._envelope_nonce_store
    prior_parent_helper = app_module._envelope_parent_lookup
    app_module._envelope_nonce_store = lambda: nonce_store
    app_module._envelope_parent_lookup = lambda: env_mod.FunctionParentLookup(
        lambda _pid, _eid: None
    )
    try:
        yield
    finally:
        app_module._auth_enabled = prior_auth
        app_module._envelope_nonce_store = prior_nonce_helper
        app_module._envelope_parent_lookup = prior_parent_helper
        _store.clear()
        app_module.app.dependency_overrides.clear()
        _restore_db(prior_db, prior_module)


def _impersonate_owner() -> None:
    def _fake_auth():
        return {"sub": OWNER_UID, "email": "owner@x"}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------

def _envelope(
    *,
    actions: list[str],
    tier: str = env_mod.TIER_T2,
    scope: str | None = "op-1",
    issuer: str = "op-1",
    project_id: str = PROJECT_ID,
    issued_at: str | None = None,
    expires_at: str | None = None,
) -> dict:
    """Mint a valid envelope for ``actions``.

    Defaults to T2 (Operation scope) since that's the bulk of the
    high-risk endpoints. ``project.archive`` is in ``HIGH_RISK_ACTIONS``
    (envelope.py) so it requires T1 — callers must pass
    ``tier=env_mod.TIER_T1, scope=None``.

    Times can be overridden to construct an expired envelope; if so the
    envelope is re-signed so the time-window step is what trips (not the
    signature step).
    """
    # T1 envelopes must have scope=None per envelope.py invariant.
    if tier == env_mod.TIER_T1:
        scope = None
    env = env_mod.issue_envelope(
        tier=tier,
        issuer=issuer,
        project_id=project_id,
        scope=scope,
        actions_authorized=actions,
    )
    if issued_at is not None:
        env["issued_at"] = issued_at
    if expires_at is not None:
        env["expires_at"] = expires_at
    if issued_at is not None or expires_at is not None:
        env["signature"] = env_mod.sign(env)
    return env


def _t1_envelope(**kwargs) -> dict:
    """Shortcut for T1 envelopes (used for HIGH_RISK_ACTIONS like
    ``project.archive``). T1 means scope=None, no Operation binding."""
    return _envelope(
        tier=env_mod.TIER_T1, issuer="user@example.com", scope=None, **kwargs
    )


def _t2_envelope(**kwargs) -> dict:
    """Shortcut for T2 (Operation) envelopes — purge / migrate actions."""
    return _envelope(tier=env_mod.TIER_T2, **kwargs)


def _hdr(envelope: dict) -> dict:
    """Build the X-Beacon-Envelope header (base64-JSON form)."""
    raw = base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")
    return {"X-Beacon-Envelope": raw}


# ---------------------------------------------------------------------------
# 1. Archive — full coverage of all envelope failure modes + happy path
# ---------------------------------------------------------------------------

class TestArchiveEnvelopeEnforcement:
    URL = f"/api/projects/{PROJECT_ID}/archive"

    def test_403_when_envelope_header_missing(self):
        _impersonate_owner()
        r = client.post(self.URL)
        assert r.status_code == 403
        body = r.json()
        assert body["detail"]["error"] == "envelope_required"
        assert body["detail"]["header"] == "X-Beacon-Envelope"
        # Side-effect check: project not archived.
        assert _store[PROJECT_ID].get("archived") is not True

    def test_403_when_envelope_malformed(self):
        _impersonate_owner()
        r = client.post(
            self.URL,
            headers={"X-Beacon-Envelope": "not-base64-not-json-!!!"},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "envelope_malformed"

    def test_200_with_valid_t1_envelope(self):
        # project.archive is in envelope.py HIGH_RISK_ACTIONS → T1-only.
        # This is the legitimate flow: user signs an envelope authorizing
        # the action (T1 = explicit user signature) and presents it to the
        # destructive endpoint.
        _impersonate_owner()
        env = _t1_envelope(actions=["project.archive"])
        r = client.post(self.URL, headers=_hdr(env))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "archived"
        # And the side-effect actually landed.
        assert _store[PROJECT_ID].get("archived") is True

    def test_403_when_envelope_authorizes_different_action(self):
        _impersonate_owner()
        # T1 envelope that DOES authorize a sibling action but not archive.
        env = _t1_envelope(actions=["project.unarchive"])
        r = client.post(self.URL, headers=_hdr(env))
        assert r.status_code == 403
        body = r.json()
        # Verify step 8 (action_permission) catches "T1 with requested
        # action not in actions_authorized" → ``envelope_verify_rejected``.
        # The post-verify fallback ``envelope_action_not_authorized`` is the
        # backstop for tiers verify doesn't enumerate. Accept both shapes
        # so the test stays robust against verify-vs-post-verify ordering.
        err = body["detail"]["error"]
        assert err in (
            "envelope_verify_rejected",
            "envelope_action_not_authorized",
        )
        assert _store[PROJECT_ID].get("archived") is not True

    def test_403_when_t2_envelope_used_for_high_risk_action(self):
        # Defense-in-depth check baked into envelope.py: high-risk actions
        # require T1 even if a T2 envelope enumerates them. This is the
        # important property — the AI cannot self-mint a T2 envelope (= via
        # Operation activation) that authorizes project.archive.
        _impersonate_owner()
        env = _t2_envelope(actions=["project.archive"])
        r = client.post(self.URL, headers=_hdr(env))
        assert r.status_code == 403
        body = r.json()
        assert body["detail"]["error"] == "envelope_verify_rejected"
        assert "high-risk" in body["detail"]["reason"].lower()
        assert _store[PROJECT_ID].get("archived") is not True

    def test_403_when_envelope_expired(self):
        _impersonate_owner()
        # Issue an envelope whose expires_at is in the past, then re-sign so
        # the signature step passes and the time-window step is what trips.
        now = datetime.datetime.now(datetime.timezone.utc)
        past = (now - datetime.timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        before_past = (now - datetime.timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        env = _t1_envelope(
            actions=["project.archive"],
            issued_at=before_past,
            expires_at=past,
        )
        r = client.post(self.URL, headers=_hdr(env))
        assert r.status_code == 403
        body = r.json()
        assert body["detail"]["error"] == "envelope_verify_rejected"
        # The verify pipeline reports the failing step in ``steps``.
        steps = body["detail"]["steps"]
        assert "time_window" in steps
        assert "expired" in steps["time_window"].lower()
        assert _store[PROJECT_ID].get("archived") is not True


# ---------------------------------------------------------------------------
# 2-5. Smoke coverage on the other 4 endpoints: no-header 403 + valid 200
# ---------------------------------------------------------------------------

class TestMigrateToV2EnvelopeEnforcement:
    URL = f"/api/projects/{PROJECT_ID}/migrate-to-v2"

    def test_403_without_envelope(self):
        _impersonate_owner()
        r = client.post(self.URL)
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "envelope_required"

    def test_envelope_gate_passes_through_to_endpoint(self):
        # Smoke test: a valid envelope clears the gate. The endpoint itself
        # may return success ("already_v2") or a downstream error depending
        # on the seed project's schema_version. We only care that the gate
        # didn't 403; any non-403 means the gate let it through.
        _impersonate_owner()
        env = _t2_envelope(actions=["project.migrate.v2"])
        r = client.post(self.URL, headers=_hdr(env))
        assert r.status_code != 403, r.text


class TestMilestonePurgeEnvelopeEnforcement:
    URL = f"/api/projects/{PROJECT_ID}/milestones/ms-1/purge"

    def test_403_without_envelope(self):
        _impersonate_owner()
        r = client.post(self.URL, json={"reason": "test"})
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "envelope_required"
        # And the milestone is still there.
        assert any(m["id"] == "ms-1"
                   for m in _store[PROJECT_ID]["milestones"])

    def test_200_with_valid_envelope(self):
        _impersonate_owner()
        env = _t2_envelope(actions=["milestone.purge"])
        r = client.post(
            self.URL, json={"reason": "test recovery"}, headers=_hdr(env),
        )
        assert r.status_code == 200, r.text
        assert r.json()["purged"] is True


class TestEntryPurgeEnvelopeEnforcement:
    URL = f"/api/projects/{PROJECT_ID}/entries/e-1/purge"

    def test_403_without_envelope(self):
        _impersonate_owner()
        r = client.post(self.URL, json={"reason": "test"})
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "envelope_required"

    def test_200_with_valid_envelope(self):
        _impersonate_owner()
        env = _t2_envelope(actions=["entry.purge"])
        r = client.post(
            self.URL, json={"reason": "test recovery"}, headers=_hdr(env),
        )
        assert r.status_code == 200, r.text
        assert r.json()["purged"] is True


class TestOperationPurgeEnvelopeEnforcement:
    URL = f"/api/projects/{PROJECT_ID}/operations/op-1/purge"

    def test_403_without_envelope(self):
        _impersonate_owner()
        r = client.post(self.URL, json={"reason": "test"})
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "envelope_required"

    def test_200_with_valid_envelope(self):
        _impersonate_owner()
        env = _t2_envelope(actions=["operation.purge"])
        r = client.post(
            self.URL, json={"reason": "test recovery"}, headers=_hdr(env),
        )
        assert r.status_code == 200, r.text
        assert r.json()["purged"] is True
