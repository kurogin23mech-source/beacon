"""Integration tests for the bus transport (ms-54 / e-996).

Exercise the POST/GET endpoints with an in-memory stub that mirrors
the Firestore queries `order_by(created_at)` + optional `where(created_at>since)`
+ optional `where(channel==X)` so the cursor and routing semantics are
locked in at the API contract level.

Subsequent slices (WS push e-997, per-recipient cursor e-998, delivery
policy e-1135, subscribe filter §9) layer on top of this contract — if
this test starts to fail, the bus transport's wire format has drifted.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import firestore_client  # noqa: E402

# In-memory store mirroring the Firestore subcollection (per project).
# Mapping: project_id -> list of event dicts (each dict contains event_id +
# server-side fields). Order of insertion is preserved.
_bus_store: dict[str, list[dict]] = {}
_event_seq = [0]


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    _event_seq[0] += 1
    eid = f"e2e-{_event_seq[0]:06d}"
    bucket = _bus_store.setdefault(project_id, [])
    bucket.append({"event_id": eid, **copy.deepcopy(data)})
    return eid


def _mock_list_bus_events(project_id: str, since: str = "", channel: str = "",
                          limit: int = 100) -> list[dict]:
    """Mirror the Firestore query: order_by(created_at), since>, channel==, limit."""
    items = list(_bus_store.get(project_id, []))
    items.sort(key=lambda e: e.get("created_at", ""))
    if since:
        items = [e for e in items if e.get("created_at", "") > since]
    if channel:
        items = [e for e in items if e.get("channel") == channel]
    if limit:
        items = items[:limit]
    return copy.deepcopy(items)


firestore_client.append_bus_event = _mock_append_bus_event
firestore_client.list_bus_events = _mock_list_bus_events


# Stubs for the existing _load auth/loader hooks so the bus endpoints don't
# crash trying to fetch a real project.
firestore_client.get_project = lambda pid: {"name": "test", "milestones": []}
firestore_client.save_project = lambda pid, data: None
firestore_client.list_projects = lambda: []


from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

app_module._auth_enabled = False
client = TestClient(app_module.app)

PROJECT_ID = "bus-test"


@pytest.fixture(autouse=True)
def reset_store():
    _bus_store.clear()
    _event_seq[0] = 0
    yield
    _bus_store.clear()


# ---------------------------------------------------------------------------
# POST contract
# ---------------------------------------------------------------------------

def test_post_returns_event_id_and_stamped_created_at():
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "session-dm",
        "sender_session_id": "sess-A",
        "payload": {"text": "hello"},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["event_id"]
    assert body["channel"] == "session-dm"
    assert body["sender_session_id"] == "sess-A"
    assert body["payload"] == {"text": "hello"}
    # server-stamped created_at, not client-supplied
    assert body["created_at"]
    assert body["created_at"].endswith("Z")


def test_post_uses_server_clock_not_client_supplied():
    """Clients can't backdate or future-date events — the cursor only works
    if all clients agree on event ordering, which requires server time."""
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "x",
        # If the model accepted created_at it could be smuggled here; the
        # Pydantic body should reject extras silently and stamp fresh.
        "sender_session_id": "s",
        "payload": {"created_at": "1970-01-01T00:00:00Z"},  # Trojan
    })
    assert resp.status_code == 200
    stored = _bus_store[PROJECT_ID][0]
    assert stored["created_at"] != "1970-01-01T00:00:00Z"


def test_post_empty_payload_is_valid():
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={"channel": "ping"})
    assert resp.status_code == 200
    assert resp.json()["payload"] == {}


def test_post_requires_channel():
    """channel is the routing tag; events without it would be unfilterable."""
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={"payload": {}})
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# GET contract
# ---------------------------------------------------------------------------

def _seed_events(events: list[dict]) -> None:
    for e in events:
        resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json=e)
        assert resp.status_code == 200


def test_get_returns_events_in_chronological_order():
    _seed_events([
        {"channel": "ch1", "sender_session_id": "A", "payload": {"n": 1}},
        {"channel": "ch1", "sender_session_id": "A", "payload": {"n": 2}},
        {"channel": "ch1", "sender_session_id": "A", "payload": {"n": 3}},
    ])
    resp = client.get(f"/api/projects/{PROJECT_ID}/bus")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["payload"]["n"] for e in body] == [1, 2, 3]


def test_get_since_cursor_returns_only_newer_events():
    _seed_events([
        {"channel": "ch1", "payload": {"n": 1}},
        {"channel": "ch1", "payload": {"n": 2}},
    ])
    first_batch = client.get(f"/api/projects/{PROJECT_ID}/bus").json()
    cursor = first_batch[-1]["created_at"]

    _seed_events([{"channel": "ch1", "payload": {"n": 3}}])
    catch_up = client.get(f"/api/projects/{PROJECT_ID}/bus?since={cursor}").json()
    assert [e["payload"]["n"] for e in catch_up] == [3]


def test_get_channel_filter_returns_only_matching():
    _seed_events([
        {"channel": "ch1", "payload": {"n": 1}},
        {"channel": "ch2", "payload": {"n": 2}},
        {"channel": "ch1", "payload": {"n": 3}},
    ])
    resp = client.get(f"/api/projects/{PROJECT_ID}/bus?channel=ch1").json()
    assert [e["payload"]["n"] for e in resp] == [1, 3]


def test_get_limit_caps_results():
    _seed_events([{"channel": "x", "payload": {"n": i}} for i in range(20)])
    resp = client.get(f"/api/projects/{PROJECT_ID}/bus?limit=5").json()
    assert len(resp) == 5
    # Must be the OLDEST 5 (since cursor advance pattern expects ascending).
    assert [e["payload"]["n"] for e in resp] == [0, 1, 2, 3, 4]


def test_get_empty_returns_empty_array():
    resp = client.get(f"/api/projects/{PROJECT_ID}/bus").json()
    assert resp == []


# ---------------------------------------------------------------------------
# api_client contract (lib/api_client.py shape)
# ---------------------------------------------------------------------------

def test_api_client_post_bus_event_round_trip():
    """The api_client method shape must match the server's accept body."""
    import api_client as ac
    fake = ac.ApiClient("http://testserver", token="")
    # Reuse the TestClient: monkey-patch the api_client's HTTP layer to
    # route through TestClient instead of opening a real socket.
    def _post(path, body):
        return client.post(path, json=body).json()
    def _get(path):
        return client.get(path).json()
    fake._request = lambda method, path, body=None: (
        _post(path, body) if method == "POST" else _get(path)
    )
    out = fake.post_bus_event(
        PROJECT_ID, "ch", sender_session_id="A", payload={"hi": True},
    )
    assert out["channel"] == "ch"
    listed = fake.list_bus_events(PROJECT_ID, channel="ch")
    assert len(listed) == 1
    assert listed[0]["payload"] == {"hi": True}
