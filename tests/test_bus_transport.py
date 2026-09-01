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
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

# ms-95 e-2407: Module-load defense (mirrors tests/test_trek_api.py / test_api.py).
# app.py does `import store_router as db`. If pytest collects another test file
# (e.g. test_api.py) before this one, app — and thus store_router — get imported
# first. store_router captures `from firestore_client import append_bus_event` etc.
# at that moment, so any later `firestore_client.append_bus_event = mock` in this
# file rebinds the ORIGINAL firestore_client module but leaves store_router's
# cached reference untouched. Production code calls `db.X` (= store_router.X) and
# hits the real Firestore client → DefaultCredentialsError in CI (no ADC).
#
# By aliasing `sys.modules["firestore_client"] = app_module.db` (= store_router)
# BEFORE this file's `import firestore_client`, the subsequent rebinds land ON
# store_router, which is what app.py actually reads.
import app as app_module  # noqa: E402
sys.modules["firestore_client"] = app_module.db

import firestore_client  # noqa: E402  (= store_router after the alias above)

# In-memory store mirroring the Firestore subcollection (per project).
# Mapping: project_id -> list of event dicts (each dict contains event_id +
# server-side fields). Order of insertion is preserved.
_bus_store: dict[str, list[dict]] = {}
_event_seq = [0]
# Per-(project, recipient) cursor store mirroring the bus_cursors subcollection.
_cursor_store: dict[tuple[str, str], dict] = {}


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


def _mock_get_bus_cursor(project_id: str, recipient_id: str) -> dict:
    return copy.deepcopy(_cursor_store.get((project_id, recipient_id), {}))


def _mock_advance_bus_cursor(project_id: str, recipient_id: str,
                             last_seen_at: str) -> dict:
    """Forward-only mirror of firestore_client.advance_bus_cursor."""
    import datetime
    key = (project_id, recipient_id)
    existing = _cursor_store.get(key)
    if existing and last_seen_at <= existing.get("last_seen_at", ""):
        return copy.deepcopy(existing)
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    data = {"last_seen_at": last_seen_at, "updated_at": now}
    _cursor_store[key] = data
    return copy.deepcopy(data)


firestore_client.append_bus_event = _mock_append_bus_event
firestore_client.list_bus_events = _mock_list_bus_events
firestore_client.get_bus_cursor = _mock_get_bus_cursor
firestore_client.advance_bus_cursor = _mock_advance_bus_cursor


# ms-54 / e-1155 Phase 1: envelope verify + audit hit the same firestore_client
# module. The bus transport contract pre-dates envelopes, so events posted by
# this test use the legacy (no envelope) path → T5-equivalent (ping or no-action).
# Provide in-memory stubs for the new functions so the contract tests run
# without standing up a Firestore emulator.
_nonce_store: dict[tuple[str, str], bool] = {}
_audit_store: dict[str, list[dict]] = {}


def _mock_check_and_record_bus_nonce(project_id: str, nonce: str,
                                     expires_at: str) -> bool:
    key = (project_id, nonce)
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


def _mock_set_bus_event_receipt(project_id: str, event_id: str, stage: str,
                                recipient_session_id: str) -> dict | None:
    """Mirror firestore_client.set_bus_event_receipt: first-write-wins per stage."""
    import datetime
    if stage not in ("delivered", "opened"):
        raise ValueError(f"invalid stage: {stage!r}")
    ts_field = f"{stage}_at"
    by_field = f"{stage}_by"
    for ev in _bus_store.get(project_id, []):
        if ev.get("event_id") == event_id:
            existing_ts = ev.get(ts_field)
            if existing_ts:
                return {
                    "event_id": event_id,
                    "stage": stage,
                    "timestamp": existing_ts,
                    "by": ev.get(by_field, ""),
                    "already_set": True,
                }
            now = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            ev[ts_field] = now
            ev[by_field] = recipient_session_id
            return {
                "event_id": event_id,
                "stage": stage,
                "timestamp": now,
                "by": recipient_session_id,
                "already_set": False,
            }
    return None


firestore_client.check_and_record_bus_nonce = _mock_check_and_record_bus_nonce
firestore_client.append_bus_audit = _mock_append_bus_audit
firestore_client.list_bus_audit = _mock_list_bus_audit
firestore_client.find_bus_event = _mock_find_bus_event
firestore_client.set_bus_event_receipt = _mock_set_bus_event_receipt


# Stubs for the existing _load auth/loader hooks so the bus endpoints don't
# crash trying to fetch a real project.
firestore_client.get_project = lambda pid: {"name": "test", "milestones": []}
firestore_client.save_project = lambda pid, data: None
firestore_client.list_projects = lambda: []


from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

app_module._auth_enabled = False

# The WS endpoint starts a Firestore on_snapshot watcher on accept and stops
# it on disconnect. That path needs a real Firestore client; for these tests
# we stub it out so WS push can be exercised in isolation without standing up
# Firestore (and without surfacing pre-existing watcher cleanup quirks).
app_module._start_watcher = lambda project_id: None
app_module._stop_watcher = lambda project_id: None

client = TestClient(app_module.app)

PROJECT_ID = "bus-test"


@pytest.fixture(autouse=True)
def reset_store(monkeypatch):
    # ms-110 / e-3443: these tests post cross-user DMs directly to exercise
    # transport/routing/receipts. The sender-consent backstop would 403 those
    # unconfirmed cross-user posts; bypass it here (function-scoped so it does
    # not leak to other files). The backstop is covered in
    # test_sender_consent_backstop.py.
    monkeypatch.setattr(
        app_module.dm_consent_mod, "classify_send_consent",
        lambda **k: (False, "transport-test-bypass"),
    )
    # Re-apply firestore_client stubs every test in case another test module
    # (notably tests/test_api.py) has restored its own mocks at module import
    # time. Without this re-application the bus tests would see surprise side
    # effects from whichever module's get_project mock was last bound.
    firestore_client.append_bus_event = _mock_append_bus_event
    firestore_client.list_bus_events = _mock_list_bus_events
    firestore_client.get_bus_cursor = _mock_get_bus_cursor
    firestore_client.advance_bus_cursor = _mock_advance_bus_cursor
    firestore_client.check_and_record_bus_nonce = _mock_check_and_record_bus_nonce
    firestore_client.append_bus_audit = _mock_append_bus_audit
    firestore_client.list_bus_audit = _mock_list_bus_audit
    firestore_client.find_bus_event = _mock_find_bus_event
    firestore_client.set_bus_event_receipt = _mock_set_bus_event_receipt
    firestore_client.get_project = lambda pid: {"name": "test", "milestones": []}
    firestore_client.save_project = lambda pid, data: None
    firestore_client.list_projects = lambda: []
    _bus_store.clear()
    _cursor_store.clear()
    _nonce_store.clear()
    _audit_store.clear()
    _event_seq[0] = 0
    yield
    _bus_store.clear()
    _cursor_store.clear()
    _nonce_store.clear()
    _audit_store.clear()


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
    # e-1155 Phase 1: legacy (no envelope) + free-text payload gets soft-
    # degraded to T5 disclosure, which caps delivery at notify-user-only.
    # Pre-envelope tests asserted propose-to-ai; the rule changed when
    # the envelope verify pipeline landed (CORE doc 1UGomhHqCQo0iYSRtCdB,
    # § backward compat: "missing envelope → restricted permissions").
    assert body["delivery"] == "notify-user-only"


# ---------------------------------------------------------------------------
# Delivery policy (e-1135)
# ---------------------------------------------------------------------------

def test_delivery_explicit_modes_round_trip():
    """propose-to-ai and notify-user-only round-trip for legacy (no
    envelope) posts. auto-execute does NOT round-trip on the legacy path
    after e-1155 — that's the whole point: callers must mint an envelope
    to elevate to auto-execute. Test in test_envelope_integration.py
    covers the envelope-bearing path that does round-trip auto-execute."""
    for mode in ("propose-to-ai", "notify-user-only"):
        resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
            "channel": "policy",
            "payload": {},
            "delivery": mode,
        })
        assert resp.status_code == 200
        assert resp.json()["delivery"] == mode

    # Legacy auto-execute → downgraded to propose-to-ai (ping-shape empty
    # payload is safe to expose under T5 propose; only auto-execute is
    # denied).
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "policy", "payload": {}, "delivery": "auto-execute",
    })
    assert resp.status_code == 200
    assert resp.json()["delivery"] == "propose-to-ai"


def test_unknown_delivery_mode_coerces_to_default():
    """A typo (or schema drift between an old sender and a new server) must
    NEVER silently upgrade to auto-execute. Coerce unknowns to the safe
    default rather than 422'ing (which would just retry-loop)."""
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "policy",
        "payload": {},
        "delivery": "auto-exec",  # near-miss of auto-execute
    })
    assert resp.status_code == 200
    assert resp.json()["delivery"] == "propose-to-ai"


def test_ws_bus_event_is_signal_only_no_payload_leak():
    """ms-97 P1 (review finding H1): the WS bus_event frame is signal-only.

    Previously the server broadcast the full event (payload, sender, delivery,
    envelope) to every project subscriber — a DM-body leak to bystanders and
    bridges. The frame now carries only a wake hint (event_id); receivers
    re-fetch via the REST inbox, which applies the per-recipient addressed-to
    filter + payload redaction. So the DM body MUST NOT appear on the wire."""
    with client.websocket_connect(f"/ws/projects/{PROJECT_ID}") as ws:
        ws.receive_json()  # drain initial project frame
        client.post(f"/api/projects/{PROJECT_ID}/bus", json={
            "channel": "policy",
            "payload": {"op": "run", "secret": "top-secret-dm-body"},
            "delivery": "notify-user-only",
        })
        pushed = ws.receive_json()
        assert pushed["type"] == "bus_event"
        assert pushed["event_id"]  # wake hint present so clients can dedupe
        # The leak-prone fields must be absent from the frame entirely.
        assert "data" not in pushed
        assert "payload" not in pushed
        assert "sender_session_id" not in pushed
        assert "envelope" not in pushed
        # Belt-and-suspenders: the DM body text must not appear anywhere in the
        # serialized frame (guards against a future field re-introducing it).
        assert "top-secret-dm-body" not in json.dumps(pushed)


def test_post_publishes_cross_process_wake_hint(monkeypatch):
    """ms-101 / e-3011: DM を post すると、受信者がどのプロセスにつながっていても
    届くよう Redis pub/sub へ wake hint を publish する。

    旧 e-997 は「同一プロセスに WS 接続がある場合のみ push」だったため、別プロセス
    (uvicorn 別ワーカー) につながる受信者には届かなかった。ここでは publish_ws_push
    がローカル接続の有無に関わらず、post した project_id + 採番された event_id で
    必ず呼ばれることを確認する (= 別プロセス接続にも届く経路が張られた証跡)。"""
    calls = []
    monkeypatch.setattr(
        app_module.redis_client, "publish_ws_push",
        lambda pid, eid, **kw: calls.append((pid, eid)) or True,
    )
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "session-dm",
        "payload": {"text": "hi"},
    })
    assert resp.status_code == 200, resp.text
    eid = resp.json()["event_id"]
    # WS 接続が 1 本も無くても publish される (= ローカル接続ガードを撤去した)。
    assert calls == [(PROJECT_ID, eid)]


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


def _seed_raw(events: list[dict]) -> None:
    """Seed the store directly with full control of created_at / channel /
    payload, bypassing the POST path (which enforces envelope + unicast rules).
    Used by the e-5964 forward-walk tests, which need a precise created_at
    ordering and a high-volume other-recipient backlog."""
    bucket = _bus_store.setdefault(PROJECT_ID, [])
    for e in events:
        _event_seq[0] += 1
        bucket.append({"event_id": f"raw-{_event_seq[0]:06d}", **copy.deepcopy(e)})


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

# ---------------------------------------------------------------------------
# WS push (e-997)
# ---------------------------------------------------------------------------

def test_ws_push_delivers_bus_event_to_subscribers():
    """A POST to /bus fans out a signal-only {type: "bus_event", event_id}
    frame to all open WS connections of the project, in addition to being
    persisted. The frame is a wake hint — subscribers re-fetch via GET /bus
    (which applies the addressed-to filter + payload redaction) rather than
    trusting the pushed payload. This keeps the real-time advantage of push
    (UC1/UC2) without leaking DM bodies to bystanders (ms-97 P1)."""
    with client.websocket_connect(f"/ws/projects/{PROJECT_ID}") as ws:
        # The initial frame is a ws_ready signal (e-2326: signal-only protocol).
        # Drain it so the next receive sees the bus event we trigger below.
        first = ws.receive_json()
        assert first["type"] == "ws_ready"

        resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
            "channel": "session-dm",
            "sender_session_id": "sess-A",
            "payload": {"text": "hello via ws"},
        })
        assert resp.status_code == 200

        pushed = ws.receive_json()
        assert pushed["type"] == "bus_event"
        # The event_id round-trips so a subscriber can dedupe / drive its
        # re-fetch. No payload / sender / channel body rides the frame.
        assert pushed["event_id"] == resp.json()["event_id"]
        assert "data" not in pushed
        assert "hello via ws" not in json.dumps(pushed)


def test_post_without_subscribers_is_not_an_error():
    """Posting to a project with no live WS subscribers must still succeed —
    the bus persists the event for later catch-up via GET. WS push is a
    convenience, not a precondition."""
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
        "channel": "ch", "sender_session_id": "A", "payload": {},
    })
    assert resp.status_code == 200
    assert resp.json()["event_id"]


# ---------------------------------------------------------------------------
# Per-recipient cursor (e-998)
# ---------------------------------------------------------------------------

def test_unread_returns_all_events_when_cursor_unset():
    _seed_events([
        {"channel": "c", "payload": {"n": 1}},
        {"channel": "c", "payload": {"n": 2}},
    ])
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=R1"
    ).json()
    assert [e["payload"]["n"] for e in resp] == [1, 2]


def _noise_then_addressed(n_noise: int, addressed: bool):
    """Seed ``n_noise`` dm events addressed to a high-volume OTHER recipient,
    optionally followed by one addressed to R1 (newest). Returns R1's event_id
    (or None). created_at is monotonic across the whole set."""
    raw = [
        {
            "channel": "dm",
            "sender_session_id": "sender",
            "created_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}.000000Z",
            "payload": {"recipient_session_id": "R_OTHER", "text": f"noise {i}"},
        }
        for i in range(n_noise)
    ]
    if addressed:
        raw.append({
            "channel": "dm",
            "sender_session_id": "sender",
            "created_at": "2026-01-02T00:00:00.000000Z",
            "payload": {"recipient_session_id": "R1", "text": "for R1"},
        })
    _seed_raw(raw)
    return f"raw-{_event_seq[0]:06d}" if addressed else None


def test_unread_forward_walk_reaches_addressed_event_past_window():
    """e-5964: the recipient's addressed event sits FAR beyond a single
    over-fetch window (starved by 20 events for a high-volume OTHER recipient,
    raw_limit=8 at limit=2). The old single-window fetch returned only
    other-recipient events → empty → the client watermark never advanced → the
    recipient could never receive. The endpoint now walks forward and delivers
    it."""
    r1_id = _noise_then_addressed(20, addressed=True)
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=R1&limit=2"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # exactly the R1 event is delivered despite the 20-event other backlog.
    assert [e["event_id"] for e in body] == [r1_id]
    # frontier advanced to (at least) the delivered event so the client's
    # watermark moves past the barren backlog.
    assert resp.headers.get("X-Bus-Unread-Frontier") == "2026-01-02T00:00:00.000000Z"


def test_unread_empty_scan_reports_frontier_to_break_deadlock():
    """e-5964: NOTHING is addressed to R1 in the whole scanned backlog. The body
    is empty (correct — nothing for R1) but the frontier header reports how far
    the server scanned, so the client advances its watermark past the barren
    region instead of re-fetching the same wedged window every poll forever."""
    _noise_then_addressed(20, addressed=False)
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=R1&limit=2"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    # scanned to the newest event even though none were for R1.
    assert resp.headers.get("X-Bus-Unread-Frontier") == "2026-01-01T00:00:19.000000Z"


def test_advance_then_unread_only_returns_newer_events():
    """After advancing the cursor past event 2, /unread must return only event 3.
    Without this, the cursor wouldn't satisfy 'each recipient sees each event
    exactly once' (重複配信が起きない)."""
    _seed_events([
        {"channel": "c", "payload": {"n": 1}},
        {"channel": "c", "payload": {"n": 2}},
    ])
    first = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=R1").json()
    cursor_ts = first[-1]["created_at"]
    ack = client.post(
        f"/api/projects/{PROJECT_ID}/bus/cursors/R1",
        json={"last_seen_at": cursor_ts},
    )
    assert ack.status_code == 200
    assert ack.json()["last_seen_at"] == cursor_ts

    _seed_events([{"channel": "c", "payload": {"n": 3}}])
    catch_up = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=R1"
    ).json()
    assert [e["payload"]["n"] for e in catch_up] == [3]


def test_advance_is_forward_only_against_stale_clients():
    """A stale client that POSTs an older cursor must NOT rewind another live
    client's progress. This is the structural defense against the lost-update
    class of bugs that ms-39 cured for project.json."""
    _seed_events([
        {"channel": "c", "payload": {"n": 1}},
        {"channel": "c", "payload": {"n": 2}},
        {"channel": "c", "payload": {"n": 3}},
    ])
    listed = client.get(f"/api/projects/{PROJECT_ID}/bus").json()
    fresh = listed[-1]["created_at"]
    stale = listed[0]["created_at"]

    client.post(
        f"/api/projects/{PROJECT_ID}/bus/cursors/R1",
        json={"last_seen_at": fresh},
    )
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/bus/cursors/R1",
        json={"last_seen_at": stale},
    )
    assert resp.status_code == 200
    # Cursor must still be at `fresh` — the stale commit is a silent no-op.
    assert resp.json()["last_seen_at"] == fresh

    catch_up = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=R1"
    ).json()
    assert catch_up == []


def test_recipients_have_independent_cursors():
    """A's commit must not advance B's cursor — otherwise the bus collapses
    into a single shared inbox instead of per-recipient delivery."""
    _seed_events([
        {"channel": "c", "payload": {"n": 1}},
        {"channel": "c", "payload": {"n": 2}},
    ])
    full = client.get(f"/api/projects/{PROJECT_ID}/bus").json()
    client.post(
        f"/api/projects/{PROJECT_ID}/bus/cursors/A",
        json={"last_seen_at": full[-1]["created_at"]},
    )
    a_next = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=A").json()
    b_view = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=B").json()
    assert a_next == []
    assert [e["payload"]["n"] for e in b_view] == [1, 2]


def test_get_cursor_returns_current_state():
    """Round-trip the cursor so a restarting consumer can recover its position
    without replaying every event first."""
    _seed_events([{"channel": "c", "payload": {"n": 1}}])
    full = client.get(f"/api/projects/{PROJECT_ID}/bus").json()
    ts = full[-1]["created_at"]
    client.post(
        f"/api/projects/{PROJECT_ID}/bus/cursors/R1",
        json={"last_seen_at": ts},
    )
    got = client.get(f"/api/projects/{PROJECT_ID}/bus/cursors/R1").json()
    assert got["last_seen_at"] == ts


def test_unread_channel_filter_combines_with_cursor():
    """Channel filter must still apply on the /unread path — otherwise a
    consumer subscribed to one channel would lose ordering when noise on
    other channels advanced its cursor."""
    _seed_events([
        {"channel": "ch1", "payload": {"n": 1}},
        {"channel": "ch2", "payload": {"n": 2}},
        {"channel": "ch1", "payload": {"n": 3}},
    ])
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=R1&channel=ch1"
    ).json()
    assert [e["payload"]["n"] for e in resp] == [1, 3]


def test_no_duplicate_delivery_across_acks():
    """Read → ack → read again must produce zero duplicates over many rounds.
    This is the literal phrasing of the AC: 重複配信が起きない."""
    seen: set[tuple[int, ...]] = set()
    for i in range(5):
        _seed_events([{"channel": "c", "payload": {"n": i}}])
        batch = client.get(
            f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=R1"
        ).json()
        ids = tuple(e["event_id"] for e in batch)
        assert all((eid,) not in seen for eid in ids), \
            f"duplicate delivery at round {i}: {ids}"
        for eid in ids:
            seen.add((eid,))
        if batch:
            client.post(
                f"/api/projects/{PROJECT_ID}/bus/cursors/R1",
                json={"last_seen_at": batch[-1]["created_at"]},
            )
    # All 5 events should have been delivered exactly once.
    assert len(seen) == 5


def test_api_client_cursor_round_trip():
    import api_client as ac
    fake = ac.ApiClient("http://testserver", token="")
    fake._request = lambda method, path, body=None: (
        client.post(path, json=body).json() if method == "POST"
        else client.get(path).json()
    )
    _seed_events([{"channel": "c", "payload": {"n": 1}}])
    unread = fake.list_unread_bus_events(PROJECT_ID, "R1")
    assert len(unread) == 1
    ts = unread[0]["created_at"]
    fake.advance_bus_cursor(PROJECT_ID, "R1", ts)
    assert fake.get_bus_cursor(PROJECT_ID, "R1")["last_seen_at"] == ts
    assert fake.list_unread_bus_events(PROJECT_ID, "R1") == []


# ---------------------------------------------------------------------------
# e-1209: DM server-side recipient filter
#
# Before e-1209, /bus/unread returned every event in the project to whichever
# session asked for them, with routing happening only at the receiver
# (channel/bus.mjs Filter 2). That meant a `dm` event meant for session A
# fanned out to every dm-subscribed session in the project, because the
# sender CLI never stamped payload.recipient_session_id and the receiver
# treated missing-recipient as broadcast.
#
# The fix moves authoritative routing into the server: /bus/unread now drops
# events whose payload.recipient_session_id doesn't match the caller, and
# treats `dm`-channel events without any recipient stamp as malformed-drop
# rather than broadcast. Non-DM channels keep broadcast semantics for events
# without a recipient stamp so notify/ops/etc still work unchanged.
# ---------------------------------------------------------------------------

def test_dm_with_recipient_stamp_routes_only_to_target():
    """A dm event addressed to A must reach A and only A — the original
    e-1209 symptom (multi-delivery to every dm-subscribed session)."""
    _seed_events([
        {"channel": "dm", "sender_session_id": "sender",
         "payload": {"recipient_session_id": "A", "text": "hi A"}},
    ])
    a_view = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=A"
    ).json()
    b_view = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=B"
    ).json()
    assert [e["payload"]["text"] for e in a_view] == ["hi A"]
    assert b_view == []


def test_dm_without_recipient_stamp_is_dropped_not_broadcast():
    """Legacy senders that don't stamp recipient_session_id on dm channel
    used to broadcast to everyone (the e-1209 bug). After the fix, those
    events are dropped server-side so the bug class becomes structurally
    impossible — there's no way to express 'dm broadcast' anymore."""
    _seed_events([
        {"channel": "dm", "sender_session_id": "sender",
         "payload": {"text": "unaddressed dm"}},
    ])
    a = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=A").json()
    b = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=B").json()
    assert a == []
    assert b == []


def test_non_dm_without_recipient_stamp_still_broadcasts():
    """Non-DM channels (notify, ops, ping) must retain broadcast semantics
    when no recipient is stamped. Otherwise every existing operation alert
    or trigger ping breaks on this deploy."""
    _seed_events([
        {"channel": "notify", "sender_session_id": "sender",
         "payload": {"text": "ops alert"}},
    ])
    a = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=A").json()
    b = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=B").json()
    assert [e["payload"]["text"] for e in a] == ["ops alert"]
    assert [e["payload"]["text"] for e in b] == ["ops alert"]


def test_non_dm_with_explicit_recipient_still_filters():
    """If a non-DM channel does stamp recipient_session_id, we honor it.
    This lets a caller scope a notify to one session if they want, without
    needing a new channel."""
    _seed_events([
        {"channel": "notify", "sender_session_id": "sender",
         "payload": {"recipient_session_id": "A", "text": "only A"}},
    ])
    a = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=A").json()
    b = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=B").json()
    assert [e["payload"]["text"] for e in a] == ["only A"]
    assert b == []


def test_dm_self_sent_event_not_delivered_to_sender():
    """Even when the recipient stamp matches the sender (a misconfigured
    or echoing client), the server must drop it. Otherwise the budget gate
    on an armed receiver can be tripped by its own outbound message echoing
    back through the bus."""
    _seed_events([
        {"channel": "dm", "sender_session_id": "A",
         "payload": {"recipient_session_id": "A", "text": "self-echo"}},
    ])
    a = client.get(f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=A").json()
    assert a == []


def test_dm_multi_session_simulate_unicast_end_to_end():
    """End-to-end simulation of e-1209 AC#3: same project, multiple
    subscribed sessions, a DM addressed to one of them must reach only
    that one. This mirrors the real-world multi-laptop scenario without
    requiring two physical machines."""
    # Three sessions all interested in the dm channel.
    sessions = ["mac-host", "win-host", "linux-host"]
    _seed_events([
        {"channel": "dm", "sender_session_id": "mac-host",
         "payload": {"recipient_session_id": "win-host", "text": "ping win"}},
        {"channel": "dm", "sender_session_id": "win-host",
         "payload": {"recipient_session_id": "mac-host", "text": "pong mac"}},
    ])
    views = {
        sid: client.get(
            f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id={sid}"
        ).json()
        for sid in sessions
    }
    assert [e["payload"]["text"] for e in views["mac-host"]] == ["pong mac"]
    assert [e["payload"]["text"] for e in views["win-host"]] == ["ping win"]
    # Crucially: the third session must see neither event despite being
    # subscribed to the same channel in the same project. This is the
    # property that was BROKEN before e-1209.
    assert views["linux-host"] == []


def test_dm_channel_filter_combines_with_recipient_filter():
    """A consumer requesting channel=dm only must still get its addressed
    events and only those. Combines the existing channel-filter behavior
    (test_unread_channel_filter_combines_with_cursor) with the new
    recipient filter."""
    _seed_events([
        {"channel": "dm", "sender_session_id": "S",
         "payload": {"recipient_session_id": "A", "text": "dm-A"}},
        {"channel": "dm", "sender_session_id": "S",
         "payload": {"recipient_session_id": "B", "text": "dm-B"}},
        {"channel": "notify", "sender_session_id": "S",
         "payload": {"text": "noise"}},
    ])
    a = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=A&channel=dm"
    ).json()
    assert [e["payload"]["text"] for e in a] == ["dm-A"]


def test_dm_routing_does_not_blank_window_when_others_dominate():
    """If channel=dm traffic is dominated by messages to other sessions,
    a request for A's events must still surface A's events rather than
    return empty just because the un-filtered first page was all addressed
    to B. This is the 'over-fetch then filter' contract."""
    # Seed 30 dm events to B, then 1 to A, then 30 more to B.
    events = []
    for i in range(30):
        events.append({"channel": "dm", "sender_session_id": "S",
                       "payload": {"recipient_session_id": "B", "text": f"B-{i}"}})
    events.append({"channel": "dm", "sender_session_id": "S",
                   "payload": {"recipient_session_id": "A", "text": "for-A"}})
    for i in range(30):
        events.append({"channel": "dm", "sender_session_id": "S",
                       "payload": {"recipient_session_id": "B",
                                   "text": f"B-{i + 30}"}})
    _seed_events(events)
    a = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=A&limit=10"
    ).json()
    # A's lone event must survive the filter even though it's surrounded
    # by other-addressed events in the raw page.
    assert any(e["payload"]["text"] == "for-A" for e in a)
    assert all(
        e["payload"].get("recipient_session_id") == "A" for e in a
    ), "no event addressed to anyone else should leak into A's view"


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


# ---------------------------------------------------------------------------
# Per-event read receipts (ms-54 / e-1348)
#
# Three properties the new endpoints must enforce:
#   1. Sender can ask "did this event reach anyone?" via GET /bus/{event_id}
#      and see the 3-stage state (sent / delivered / opened).
#   2. ack is first-write-wins per stage — repeat calls round-trip the
#      original timestamp with already_set=True. Otherwise two recipients
#      racing on a broadcast would produce a flip-flop and the sender
#      couldn't trust the "opened_at" they saw earlier.
#   3. The stages are independent: stamping `delivered` does not implicitly
#      stamp `opened`. The receiver must explicitly ack each transition.
# ---------------------------------------------------------------------------


def _post_one(project_id: str = PROJECT_ID, payload: dict | None = None) -> str:
    """Helper: create one event and return its event_id."""
    payload = payload or {"text": "hello"}
    resp = client.post(f"/api/projects/{project_id}/bus", json={
        "channel": "dm",
        "sender_session_id": "sender-A",
        "payload": {"recipient_session_id": "recv-B", **payload},
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["event_id"]


def test_receipt_get_single_event_returns_full_dict():
    """The GET endpoint must return the event with channel/sender/payload
    intact so the CLI doesn't need a second round-trip to render it."""
    eid = _post_one()
    resp = client.get(f"/api/projects/{PROJECT_ID}/bus/{eid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["event_id"] == eid
    assert body["channel"] == "dm"
    assert body["sender_session_id"] == "sender-A"
    assert body["created_at"]
    # No receipts stamped yet — those fields are simply absent from the doc.
    assert "delivered_at" not in body
    assert "opened_at" not in body


def test_receipt_get_unknown_event_returns_404():
    """An unknown event_id must 404 cleanly so the CLI can render
    a useful 'not found' line instead of mystery-empty output."""
    resp = client.get(f"/api/projects/{PROJECT_ID}/bus/does-not-exist")
    assert resp.status_code == 404


def test_receipt_ack_delivered_then_opened_records_both_stages():
    """After two acks the event should carry both delivered_at and opened_at
    plus the corresponding _by attribution. This is the happy-path that
    bus.mjs will trigger for every DM it surfaces."""
    eid = _post_one()
    delivered = client.post(
        f"/api/projects/{PROJECT_ID}/bus/{eid}/ack",
        json={"stage": "delivered", "recipient_session_id": "recv-B"},
    )
    assert delivered.status_code == 200
    d = delivered.json()
    assert d["stage"] == "delivered"
    assert d["already_set"] is False
    assert d["by"] == "recv-B"
    assert d["timestamp"].endswith("Z")

    opened = client.post(
        f"/api/projects/{PROJECT_ID}/bus/{eid}/ack",
        json={"stage": "opened", "recipient_session_id": "recv-B"},
    )
    assert opened.status_code == 200
    o = opened.json()
    assert o["stage"] == "opened"
    assert o["already_set"] is False

    # GET reflects the two new fields.
    fetched = client.get(f"/api/projects/{PROJECT_ID}/bus/{eid}").json()
    assert fetched["delivered_at"] == d["timestamp"]
    assert fetched["delivered_by"] == "recv-B"
    assert fetched["opened_at"] == o["timestamp"]
    assert fetched["opened_by"] == "recv-B"


def test_receipt_ack_is_idempotent_first_write_wins():
    """A second ack of the same stage must return the ORIGINAL timestamp
    (already_set=True) rather than overwrite it. Without this, two
    bus.mjs replicas racing on a broadcast would flip-flop opened_at and
    the sender couldn't trust the receipt they observed earlier."""
    eid = _post_one()
    first = client.post(
        f"/api/projects/{PROJECT_ID}/bus/{eid}/ack",
        json={"stage": "delivered", "recipient_session_id": "recv-B"},
    ).json()
    second = client.post(
        f"/api/projects/{PROJECT_ID}/bus/{eid}/ack",
        json={"stage": "delivered", "recipient_session_id": "other-replica"},
    ).json()
    assert second["already_set"] is True
    assert second["timestamp"] == first["timestamp"]
    # The 'by' attribution must NOT silently switch to other-replica either.
    assert second["by"] == "recv-B"
    assert first["by"] == "recv-B"


def test_receipt_ack_stages_are_independent():
    """Stamping `delivered` must not implicitly stamp `opened`, and vice
    versa. The CLI relies on this to render a partial 3-stage view (e.g.
    'sent ✓ / delivered ✓ / opened ✗ (not yet)') without forcing the
    receiver to ack stages it hasn't reached."""
    eid = _post_one()
    client.post(
        f"/api/projects/{PROJECT_ID}/bus/{eid}/ack",
        json={"stage": "delivered", "recipient_session_id": "recv-B"},
    )
    fetched = client.get(f"/api/projects/{PROJECT_ID}/bus/{eid}").json()
    assert fetched.get("delivered_at")
    assert "opened_at" not in fetched
    assert "opened_by" not in fetched


def test_receipt_ack_invalid_stage_is_rejected_at_boundary():
    """Unknown stage values must 400 rather than silently land as a new
    field on the event document. Otherwise a typo on the receiver path
    would smuggle arbitrary fields into the durable event record."""
    eid = _post_one()
    bad = client.post(
        f"/api/projects/{PROJECT_ID}/bus/{eid}/ack",
        json={"stage": "delivered-typo", "recipient_session_id": "recv-B"},
    )
    assert bad.status_code == 400
    fetched = client.get(f"/api/projects/{PROJECT_ID}/bus/{eid}").json()
    # No accidental field write occurred.
    assert "delivered-typo_at" not in fetched
    assert "delivered_at" not in fetched


def test_receipt_ack_unknown_event_returns_404():
    """ack on a non-existent event must 404 cleanly. The receiver bridge
    shouldn't crash if it observed an event whose Firestore doc was GCed
    or never existed (e.g. truncated channel)."""
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/bus/does-not-exist/ack",
        json={"stage": "delivered", "recipient_session_id": "recv-B"},
    )
    assert resp.status_code == 404


def test_receipt_independent_stage_writers_both_succeed_first_time():
    """delivered-then-opened by the same receiver and delivered-by-A then
    opened-by-B both work first-time — neither stage is gated on the
    other. (Same receiver is the common case; cross-receiver per-stage is
    rare but exercised here for completeness.)"""
    eid = _post_one()
    d = client.post(
        f"/api/projects/{PROJECT_ID}/bus/{eid}/ack",
        json={"stage": "delivered", "recipient_session_id": "bridge-X"},
    ).json()
    o = client.post(
        f"/api/projects/{PROJECT_ID}/bus/{eid}/ack",
        json={"stage": "opened", "recipient_session_id": "harness-Y"},
    ).json()
    assert d["already_set"] is False
    assert o["already_set"] is False
    fetched = client.get(f"/api/projects/{PROJECT_ID}/bus/{eid}").json()
    assert fetched["delivered_by"] == "bridge-X"
    assert fetched["opened_by"] == "harness-Y"
