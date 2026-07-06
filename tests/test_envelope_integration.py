"""Integration tests for the envelope-bearing bus path (ms-54 / e-1155 Phase 1).

Where ``test_envelope.py`` is unit-level on the verify pipeline and
``test_bus_transport.py`` locks the pre-envelope transport contract,
this module exercises the full server flow:

  issuance (POST /bus/envelope/issue) →
    post with envelope (POST /bus) →
      audit record (GET /bus/audit) →
        defense-in-depth rejection paths

If any of these break, the Phase 1 acceptance criteria from
CORE doc ``1UGomhHqCQo0iYSRtCdB`` have regressed.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")
# Pin the secret so signatures are deterministic across test re-imports.
os.environ.setdefault("BEACON_ENVELOPE_SECRET", "integration-test-secret")

# ms-95 e-2407: Same alias trick as test_bus_transport — make `firestore_client.X
# = mock` rebinds below land on store_router (= what app.py actually reads via
# `import store_router as db`). Without this, `db.X` hits the real Firestore
# client in CI → DefaultCredentialsError on all 10 envelope-integration tests.
# See tests/test_bus_transport.py for the longer rationale.
import app as app_module  # noqa: E402
sys.modules["firestore_client"] = app_module.db

import firestore_client  # noqa: E402  (= store_router after the alias above)

# Reuse the in-memory store pattern from test_bus_transport.
_bus_store: dict[str, list[dict]] = {}
_audit_store: dict[str, list[dict]] = {}
_nonce_store: dict[str, bool] = {}
_event_seq = [0]


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    _event_seq[0] += 1
    eid = f"evt-{_event_seq[0]:06d}"
    bucket = _bus_store.setdefault(project_id, [])
    bucket.append({"event_id": eid, **copy.deepcopy(data)})
    return eid


def _mock_list_bus_events(project_id: str, since: str = "",
                          channel: str = "", limit: int = 100) -> list[dict]:
    items = list(_bus_store.get(project_id, []))
    items.sort(key=lambda e: e.get("created_at", ""))
    if since:
        items = [e for e in items if e.get("created_at", "") > since]
    if channel:
        items = [e for e in items if e.get("channel") == channel]
    if limit:
        items = items[:limit]
    return copy.deepcopy(items)


def _mock_check_and_record_bus_nonce(project_id: str, nonce: str,
                                     expires_at: str) -> bool:
    key = f"{project_id}::{nonce}"
    if key in _nonce_store:
        return False
    _nonce_store[key] = True
    return True


def _mock_append_bus_audit(project_id: str, record: dict) -> str:
    bucket = _audit_store.setdefault(project_id, [])
    audit_id = f"audit-{len(bucket) + 1:06d}"
    bucket.append({"audit_id": audit_id, **copy.deepcopy(record)})
    return audit_id


def _mock_list_bus_audit(project_id: str, *, since: str = "",
                         limit: int = 100) -> list[dict]:
    items = list(_audit_store.get(project_id, []))
    items.sort(key=lambda r: r.get("received_at", ""))
    if since:
        items = [r for r in items if r.get("received_at", "") > since]
    if limit:
        items = items[:limit]
    return copy.deepcopy(items)


def _mock_find_bus_event(project_id: str, event_id: str) -> dict | None:
    for ev in _bus_store.get(project_id, []):
        if ev.get("event_id") == event_id:
            return copy.deepcopy(ev)
    return None


from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402
import envelope as envelope_mod  # noqa: E402

# ms-95 e-2441: mutations DELAYED to setup_module (runs at THIS module's test
# start, not at pytest collection time) so they don't leak into adjacent test
# files. teardown_module restores. Previously these mutations ran at module
# import time and leaked for the entire pytest session, polluting
# test_trek_scope_aggregate (15), test_api (8), test_ws_auth_close (2), etc.
#
# PR #263's earlier attempt (= teardown_module restore but keep mutations at
# module level) had cross-collection ordering bugs where the restore
# overwrote adjacent files' OWN module-level mocks. Delaying the mutations
# entirely to setup_module avoids the ordering trap because env_integration
# only touches global state during its own tests' execution window.

client = TestClient(app_module.app)
PROJECT_ID = "envelope-itest"

_FC_ATTRS = (
    "append_bus_event", "list_bus_events", "get_bus_cursor",
    "advance_bus_cursor", "check_and_record_bus_nonce",
    "append_bus_audit", "list_bus_audit", "find_bus_event",
    "get_project", "save_project", "list_projects",
)
# ms-95 e-2441: do NOT restore `_auth_enabled` — test_api.py and other
# adjacent files set it at module load and rely on the value persisting
# through their tests. Restoring forces it back to the app.py default (True)
# and breaks adjacent test_api requests with 401.
_APP_ATTRS = (
    "_start_watcher", "_stop_watcher", "_require_project_role",
    "_resolve_bus_event_user_ids",
)
_fc_originals: dict = {}
_app_originals: dict = {}


def setup_module(_module):
    """ms-95 e-2441: apply env_integration's firestore_client + app_module
    overrides only when THIS module's tests are about to run. Adjacent files'
    tests have already executed by then (or run after teardown_module restores
    everything)."""
    global _fc_originals, _app_originals
    _fc_originals = {name: getattr(firestore_client, name, None) for name in _FC_ATTRS}
    _app_originals = {name: getattr(app_module, name, None) for name in _APP_ATTRS}

    firestore_client.append_bus_event = _mock_append_bus_event
    firestore_client.list_bus_events = _mock_list_bus_events
    firestore_client.get_bus_cursor = lambda pid, rid: {}
    firestore_client.advance_bus_cursor = lambda pid, rid, ts: {}
    firestore_client.check_and_record_bus_nonce = _mock_check_and_record_bus_nonce
    firestore_client.append_bus_audit = _mock_append_bus_audit
    firestore_client.list_bus_audit = _mock_list_bus_audit
    firestore_client.find_bus_event = _mock_find_bus_event
    firestore_client.get_project = lambda pid: {"name": "test", "milestones": []}
    firestore_client.save_project = lambda pid, data: None
    firestore_client.list_projects = lambda: []

    # Auth off (dev mode) so we can call endpoints without minting tokens.
    app_module._auth_enabled = False
    app_module._start_watcher = lambda project_id: None
    app_module._stop_watcher = lambda project_id: None
    # The _require_project_role helper depends on
    # operations.load_project_consistent, which isn't stubbed at module
    # import. Bypass by patching the helper to a no-op pair (membership is
    # auth-enabled-only anyway).
    app_module._require_project_role = lambda project_id, user, **kw: (
        {"name": "test", "milestones": []}, "owner"
    )
    # ms-70 e-1713: the cross-user DM action gate was added after this test
    # was written. Its resolver would leave sender_uid / receiver_uid empty
    # for these unregistered sessions, which the gate treats as a
    # cross-user-with-actions case and downgrades auto-execute →
    # propose-to-ai (= what test_t1_in_actions_enables_auto_execute regressed
    # on). Stub the resolver to return a same-user pair so the gate takes
    # its intended "same-user skip" path and the pre-ms-70 auto-execute
    # contract still holds. If we ever want cross-user coverage, add a
    # dedicated test that seeds a real session pair.
    app_module._resolve_bus_event_user_ids = lambda project_id, sender_session_id, payload: (
        "uid-test", "uid-test"
    )


def teardown_module(_module):
    """Restore the snapshots so downstream tests see the original module."""
    for name, value in _fc_originals.items():
        if value is not None:
            setattr(firestore_client, name, value)
    for name, value in _app_originals.items():
        if value is not None:
            setattr(app_module, name, value)


@pytest.fixture(autouse=True)
def reset():
    firestore_client.append_bus_event = _mock_append_bus_event
    firestore_client.list_bus_events = _mock_list_bus_events
    firestore_client.check_and_record_bus_nonce = _mock_check_and_record_bus_nonce
    firestore_client.append_bus_audit = _mock_append_bus_audit
    firestore_client.list_bus_audit = _mock_list_bus_audit
    firestore_client.find_bus_event = _mock_find_bus_event
    _bus_store.clear()
    _audit_store.clear()
    _nonce_store.clear()
    _event_seq[0] = 0
    yield


# ---------------------------------------------------------------------------
# Issuance endpoint
# ---------------------------------------------------------------------------

def test_issue_t1_envelope():
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T1",
        "actions_authorized": ["status_check"],
    })
    assert resp.status_code == 200, resp.text
    env = resp.json()
    assert env["tier"] == "T1"
    assert env["scope"] is None
    assert env["signature"]
    assert env["tokens"] is None
    assert env["refill_policy"] is None
    assert env["project_id"] == PROJECT_ID


def test_issue_t2_envelope_requires_scope():
    """T2 without scope must be refused at the issuance boundary — Phase 2
    can't accidentally mint a 'free-floating' Operation envelope."""
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T2",
        "actions_authorized": ["deploy"],
        # scope missing
    })
    assert resp.status_code == 400


def test_issue_rejects_wildcard_action():
    """CORE doc § "scope 自然言語の曖昧性": wildcards/regex banned."""
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T1",
        "actions_authorized": ["deploy:*"],
    })
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Post-with-envelope happy path
# ---------------------------------------------------------------------------

def test_post_with_t1_envelope_round_trip():
    env_resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T1",
        "actions_authorized": ["status_check"],
    })
    env = env_resp.json()
    post_resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "dm",
        "sender_session_id": "alice",
        "payload": {"text": "hello bob", "recipient_session_id": "bob"},
        "delivery": "propose-to-ai",
        "envelope": env,
    })
    assert post_resp.status_code == 200, post_resp.text
    body = post_resp.json()
    assert body["event_id"]
    # Envelope persisted with the event so receivers can re-verify.
    assert body.get("envelope") is not None
    assert body["envelope"]["tier"] == "T1"


def test_t1_in_actions_enables_auto_execute():
    env_resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T1",
        "actions_authorized": ["status_check"],
    })
    env = env_resp.json()
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops",
        "sender_session_id": "alice",
        "payload": {"status": "running"},
        "delivery": "auto-execute",
        "envelope": env,
        "requested_action": "status_check",
    })
    assert resp.status_code == 200
    assert resp.json()["delivery"] == "auto-execute"


# ---------------------------------------------------------------------------
# Defense paths
# ---------------------------------------------------------------------------

def test_t1_action_not_authorized_is_rejected():
    """An envelope minted for status_check can't be used to deploy.
    This is the CORE doc anti-injection guarantee: action requests must
    match actions_authorized, no exceptions."""
    env_resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T1",
        "actions_authorized": ["status_check"],
    })
    env = env_resp.json()
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops",
        "sender_session_id": "alice",
        "payload": {"text": "deploy now"},
        "delivery": "auto-execute",
        "envelope": env,
        "requested_action": "deploy",
    })
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "envelope_verify_rejected"


def test_tampered_envelope_rejected():
    """Signature verify failure → T5 degrade → if payload non-conforming
    or action requested, hard reject."""
    env_resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T1",
        "actions_authorized": ["deploy"],
    })
    env = env_resp.json()
    env["actions_authorized"] = ["deploy", "delete"]  # post-sign tamper
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops",
        "sender_session_id": "alice",
        "payload": {"text": "x"},
        "delivery": "auto-execute",
        "envelope": env,
        "requested_action": "delete",
    })
    assert resp.status_code == 403


def test_replay_blocked():
    """Same envelope used twice → second post fails on nonce."""
    env_resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T1",
        "actions_authorized": ["status_check"],
    })
    env = env_resp.json()
    first = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops", "sender_session_id": "a",
        "payload": {"ping": "ok"}, "envelope": env,
    })
    assert first.status_code == 200
    second = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops", "sender_session_id": "a",
        "payload": {"text": "different free prose"},  # force hard reject
        "envelope": env, "requested_action": "status_check",
    })
    # Replay → degrade → action requested → hard reject.
    assert second.status_code == 403


def test_high_risk_action_rejected_at_t2():
    """T2 envelopes cannot authorize high-risk actions; only T1 can.
    This is the server-side defense-in-depth gate from CORE doc
    § "高リスク action の server-side gate"."""
    env_resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T2",
        "scope": "scope-name",
        "actions_authorized": ["deploy"],
    })
    env = env_resp.json()
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops", "sender_session_id": "a",
        "payload": {"text": "x"},
        "envelope": env, "requested_action": "deploy",
    })
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "high-risk" in (detail.get("reason") or "")


def test_project_id_mismatch_rejected():
    env_resp = client.post(f"/api/projects/other-project/bus/envelope/issue", json={
        "tier": "T1",
        "actions_authorized": ["status_check"],
    })
    env = env_resp.json()
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops", "sender_session_id": "a",
        "payload": {"text": "cross-project attack"},
        "envelope": env, "requested_action": "status_check",
    })
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# T5 short-ping enforcement
# ---------------------------------------------------------------------------

def test_t5_envelope_freetext_rejected():
    """An AI-self-send T5 envelope cannot carry free text — only the
    short-ping schema. This structurally prevents an AI from exfiltrating
    info via 'autonomous send'."""
    env_resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T5",
        "actions_authorized": [],
    })
    env = env_resp.json()
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops", "sender_session_id": "a",
        "payload": {"text": "the password is hunter2"},
        "envelope": env,
    })
    assert resp.status_code == 403


def test_t5_envelope_pingshape_ok():
    env_resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T5",
        "actions_authorized": [],
    })
    env = env_resp.json()
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops", "sender_session_id": "a",
        "payload": {"ping": "ok", "status": "awake"},
        "envelope": env,
    })
    assert resp.status_code == 200
    # T5 caps at notify-user-only when caller asks for propose-to-ai
    # with ping-shape — per decide_delivery, ping is conforming so we
    # stay at propose-to-ai (default). Let's just check it's not
    # auto-execute (the structural T5 guarantee).
    assert resp.json()["delivery"] != "auto-execute"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_audit_record_written_for_every_receive():
    # 1 pass
    env_resp = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T1", "actions_authorized": ["status_check"],
    })
    env = env_resp.json()
    client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops", "sender_session_id": "a",
        "payload": {"ping": "ok"}, "envelope": env,
    })
    # 1 reject (action not authorized)
    env_resp2 = client.post(f"/api/projects/{PROJECT_ID}/bus/envelope/issue", json={
        "tier": "T1", "actions_authorized": ["status_check"],
    })
    env2 = env_resp2.json()
    client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ops", "sender_session_id": "a",
        "payload": {"text": "x"},
        "envelope": env2, "requested_action": "deploy",
    })
    # 1 legacy soft-degrade
    client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "dm", "sender_session_id": "a",
        "payload": {"text": "legacy"},
    })

    audit_resp = client.get(f"/api/projects/{PROJECT_ID}/bus/audit")
    assert audit_resp.status_code == 200
    records = audit_resp.json()
    assert len(records) == 3
    # Pass record
    assert records[0]["verify"]["passed"] is True
    assert records[0]["rejected"] is False
    # Reject record
    assert records[1]["verify"]["passed"] is False
    assert records[1]["rejected"] is True
    # Legacy record — passed=False because verify soft-degraded (free text);
    # rejected=False because we kept the bus flowing.
    assert records[2]["rejected"] is False
    assert records[2]["envelope"] is None  # legacy
