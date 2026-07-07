"""Sidecar bus_event_approvals schema pin (ms-70 / e-1712).

The envelope itself (in bus_events) is HMAC-signed and immutable. The
receiver-side decision_stamp lives in a parallel subcollection / table
``bus_event_approvals`` keyed by the parent ``event_id``. These tests pin:

1. 7-field schema is persisted round-trip for both backends:
   event_id / approval_status / decision_by / decision_at /
   created_at / sender_user_id / receiver_user_id
2. Migration semantics: a bus_event with no sidecar row reads as None,
   which callers interpret as ``approval_status="auto"`` (= legacy /
   shared-Trek blanket-allow path).
3. ``list_pending_approvals`` returns only ``pending`` rows in
   ``created_at`` order, with optional ``receiver_user_id`` filter.
4. ``put_bus_event_approval`` validates the approval_status enum.
5. Lifecycle update (= pending → approved) preserves the original
   ``created_at`` while updating ``approval_status`` /
   ``decision_by`` / ``decision_at``.

The Firestore path uses the same in-memory ``_FakeFirestoreClient`` fake
used by tests/test_operations.py (= no real google.cloud dependency).
The DynamoDB path uses moto's ``mock_aws`` to drive real boto3 code
against an in-memory DynamoDB (= same pattern as
tests/test_dynamodb_bus_sessions.py).
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


# ---------------------------------------------------------------------------
# Firestore fake plumbing (mirrors tests/test_operations.py's helpers, kept
# self-contained here so the tests do not import test code from other files).
# ---------------------------------------------------------------------------

class _FakeFirestoreClient:
    def __init__(self):
        self.docs: dict[tuple, dict] = {}
        self.subdocs: dict[tuple, dict] = {}

    def collection(self, name):
        return _FakeCollectionRef(self, name)

    def transaction(self):
        return _FakeTransaction(self)


class _FakeCollectionRef:
    def __init__(self, client, name, parent=None, filters=None, order=None,
                 limit_n=None):
        self.client = client
        self.name = name
        self.parent = parent  # (collection, doc_id) tuple if subcollection
        self.filters = list(filters or [])
        self.order = order
        self.limit_n = limit_n

    def document(self, doc_id):
        return _FakeDocumentRef(self.client, self.name, doc_id, self.parent)

    def where(self, field, op, value):
        new_filters = self.filters + [(field, op, value)]
        return _FakeCollectionRef(
            self.client, self.name, self.parent,
            new_filters, self.order, self.limit_n,
        )

    def order_by(self, field):
        return _FakeCollectionRef(
            self.client, self.name, self.parent,
            self.filters, field, self.limit_n,
        )

    def limit(self, n):
        return _FakeCollectionRef(
            self.client, self.name, self.parent,
            self.filters, self.order, n,
        )

    def _iter_docs(self):
        if self.parent is None:
            keys = [k for k in self.client.docs if k[0] == self.name]
            for col, did in keys:
                yield did, self.client.docs[(col, did)]
        else:
            pcol, pdid = self.parent
            keys = [
                k for k in self.client.subdocs
                if k[0] == pcol and k[1] == pdid and k[2] == self.name
            ]
            for _col, _did, _sub, sdid in keys:
                yield sdid, self.client.subdocs[(_col, _did, _sub, sdid)]

    def stream(self, transaction=None):
        rows = list(self._iter_docs())
        for field, op, value in self.filters:
            if op == "==":
                rows = [(d, data) for d, data in rows if data.get(field) == value]
            elif op == ">":
                rows = [(d, data) for d, data in rows if data.get(field, "") > value]
            else:
                raise NotImplementedError(f"_FakeCollectionRef.where op={op}")
        if self.order:
            rows.sort(key=lambda pair: pair[1].get(self.order, ""))
        if self.limit_n:
            rows = rows[: self.limit_n]
        for did, data in rows:
            yield _FakeDocSnap(did, data)


class _FakeDocumentRef:
    def __init__(self, client, collection, doc_id, parent=None):
        self.client = client
        self.collection_name = collection
        self.doc_id = doc_id
        self.parent = parent

    @property
    def id(self):
        return self.doc_id

    def collection(self, name):
        return _FakeCollectionRef(
            self.client, name,
            parent=(self.collection_name, self.doc_id),
        )

    def get(self, transaction=None):
        if self.parent is None:
            data = self.client.docs.get((self.collection_name, self.doc_id))
        else:
            pcol, pdid = self.parent
            data = self.client.subdocs.get(
                (pcol, pdid, self.collection_name, self.doc_id)
            )
        return _FakeDocSnap(self.doc_id, data)

    def set(self, data, merge=False):
        if self.parent is None:
            existing = self.client.docs.get((self.collection_name, self.doc_id), {})
            new_data = {**existing, **data} if merge else dict(data)
            self.client.docs[(self.collection_name, self.doc_id)] = new_data
        else:
            pcol, pdid = self.parent
            key = (pcol, pdid, self.collection_name, self.doc_id)
            existing = self.client.subdocs.get(key, {})
            new_data = {**existing, **data} if merge else dict(data)
            self.client.subdocs[key] = new_data


class _FakeDocSnap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _FakeTransaction:
    def __init__(self, client):
        self.client = client

    def set(self, ref, data):
        if ref.parent is None:
            self.client.docs[(ref.collection_name, ref.doc_id)] = dict(data)
        else:
            pcol, pdid = ref.parent
            self.client.subdocs[(pcol, pdid, ref.collection_name, ref.doc_id)] = dict(data)


@pytest.fixture
def firestore_client(monkeypatch):
    """Provide a firestore_client module wired against an in-memory fake."""
    monkeypatch.setenv("BEACON_ENV", "dev")
    fake_db = _FakeFirestoreClient()

    # Stub google.cloud.firestore before firestore_client is imported so the
    # ``from google.cloud import firestore`` line resolves to our fake.
    fake_firestore_module = type("FakeFirestoreModule", (), {})()

    def transactional(fn):
        def run(transaction):
            return fn(transaction)
        return run

    fake_firestore_module.transactional = transactional

    class _FakeFirestoreClientCls:
        def __init__(self, *_a, **_kw):
            pass

    fake_firestore_module.Client = _FakeFirestoreClientCls

    fake_google = type("FakeGoogleNS", (), {})()
    fake_google_cloud = type("FakeGoogleCloud", (), {})()
    fake_google_cloud.firestore = fake_firestore_module
    fake_google.cloud = fake_google_cloud
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", fake_firestore_module)

    # (Re)load the module so its ``firestore.Client()`` call hits our stub.
    # ms-95 e-2438: monkeypatch.delitem auto-restores the prior firestore_client
    # entry; a raw pop leaks across the test boundary, wiping downstream tests'
    # mocks (test_invitation_api etc.) → DefaultCredentialsError on CI.
    if "firestore_client" in sys.modules:
        monkeypatch.delitem(sys.modules, "firestore_client")
    import firestore_client as fc

    # Override get_db to return the in-memory fake (the real one would try to
    # talk to GCP credentials).
    monkeypatch.setattr(fc, "get_db", lambda: fake_db)
    yield fc
    # No manual pop on teardown — monkeypatch handles it.


# ---------------------------------------------------------------------------
# DynamoDB / moto plumbing (mirrors tests/test_dynamodb_bus_sessions.py).
# ---------------------------------------------------------------------------

_REQUIRED_TABLES = [
    # (name, PK, SK)
    ("beacon-dev-bus_event_approvals", "project_id", "event_id"),
]


@pytest.fixture
def ddb_client(monkeypatch):
    monkeypatch.setenv("BEACON_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("BEACON_ENV", "dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    moto = pytest.importorskip("moto")
    import boto3

    mock_ctx = moto.mock_aws()
    mock_ctx.start()
    try:
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, pk, sk in _REQUIRED_TABLES:
            ddb.create_table(
                TableName=name,
                KeySchema=[
                    {"AttributeName": pk, "KeyType": "HASH"},
                    {"AttributeName": sk, "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": pk, "AttributeType": "S"},
                    {"AttributeName": sk, "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        sys.modules.pop("dynamodb_client", None)
        import dynamodb_client as db
        yield db
    finally:
        mock_ctx.stop()
        sys.modules.pop("dynamodb_client", None)


# ---------------------------------------------------------------------------
# Shared assertions (parametrised: each test runs against both backends).
# ---------------------------------------------------------------------------

PID = "proj-x"
EV1 = "evt-aaaaaaaa"
EV2 = "evt-bbbbbbbb"
EV3 = "evt-cccccccc"
USER_A = "user-a-receiver"
USER_B = "user-b-receiver"
USER_S = "user-s-sender"


def _assert_seven_fields(row, *, event_id, approval_status,
                         decision_by, decision_at,
                         sender_user_id, receiver_user_id):
    """All 7 fields are present with the expected shape and types."""
    assert set(row.keys()) >= {
        "event_id", "approval_status", "decision_by", "decision_at",
        "created_at", "sender_user_id", "receiver_user_id",
    }, f"missing fields in {row!r}"
    assert row["event_id"] == event_id
    assert row["approval_status"] == approval_status
    assert row["decision_by"] == decision_by
    assert row["decision_at"] == decision_at
    assert row["sender_user_id"] == sender_user_id
    assert row["receiver_user_id"] == receiver_user_id
    assert isinstance(row["created_at"], str) and row["created_at"]


# ===========================================================================
# Firestore-backed tests
# ===========================================================================

def test_firestore_get_missing_returns_none_for_legacy_auto(firestore_client):
    """Legacy bus_event with no sidecar row reads as None == auto."""
    assert firestore_client.get_bus_event_approval(PID, "no-such-event") is None


def test_firestore_put_then_get_persists_seven_fields(firestore_client):
    written = firestore_client.put_bus_event_approval(
        PID, EV1,
        approval_status="pending",
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
    )
    _assert_seven_fields(
        written,
        event_id=EV1,
        approval_status="pending",
        decision_by=None,
        decision_at=None,
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
    )
    read_back = firestore_client.get_bus_event_approval(PID, EV1)
    assert read_back is not None
    _assert_seven_fields(
        read_back,
        event_id=EV1,
        approval_status="pending",
        decision_by=None,
        decision_at=None,
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
    )


def test_firestore_lifecycle_preserves_created_at(firestore_client):
    """pending → approved transition keeps the original created_at."""
    first = firestore_client.put_bus_event_approval(
        PID, EV1,
        approval_status="pending",
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
    )
    original_created_at = first["created_at"]
    time.sleep(0.005)  # ensure now() would differ if it were re-stamped

    updated = firestore_client.put_bus_event_approval(
        PID, EV1,
        approval_status="approved",
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
        decision_by=USER_A,
        decision_at="2026-06-14T12:00:00.000000Z",
    )
    assert updated["approval_status"] == "approved"
    assert updated["decision_by"] == USER_A
    assert updated["decision_at"] == "2026-06-14T12:00:00.000000Z"
    assert updated["created_at"] == original_created_at


def test_firestore_invalid_status_rejected(firestore_client):
    with pytest.raises(ValueError):
        firestore_client.put_bus_event_approval(
            PID, EV1,
            approval_status="bogus-status",
            sender_user_id=USER_S,
            receiver_user_id=USER_A,
        )


def test_firestore_list_pending_orders_and_filters(firestore_client):
    # Three pending rows for distinct events / receivers, plus one approved.
    firestore_client.put_bus_event_approval(
        PID, EV1,
        approval_status="pending",
        sender_user_id=USER_S, receiver_user_id=USER_A,
    )
    time.sleep(0.002)
    firestore_client.put_bus_event_approval(
        PID, EV2,
        approval_status="pending",
        sender_user_id=USER_S, receiver_user_id=USER_B,
    )
    time.sleep(0.002)
    firestore_client.put_bus_event_approval(
        PID, EV3,
        approval_status="pending",
        sender_user_id=USER_S, receiver_user_id=USER_A,
    )
    # Non-pending row must not show up.
    firestore_client.put_bus_event_approval(
        PID, "evt-approved",
        approval_status="approved",
        sender_user_id=USER_S, receiver_user_id=USER_A,
        decision_by=USER_A, decision_at="2026-06-14T12:00:00.000000Z",
    )

    all_pending = firestore_client.list_pending_approvals(PID)
    ids_in_order = [r["event_id"] for r in all_pending]
    assert ids_in_order == [EV1, EV2, EV3], (
        f"expected created_at order [EV1, EV2, EV3], got {ids_in_order!r}"
    )

    just_a = firestore_client.list_pending_approvals(
        PID, receiver_user_id=USER_A
    )
    assert [r["event_id"] for r in just_a] == [EV1, EV3]
    assert all(r["receiver_user_id"] == USER_A for r in just_a)

    just_b = firestore_client.list_pending_approvals(
        PID, receiver_user_id=USER_B
    )
    assert [r["event_id"] for r in just_b] == [EV2]


# ===========================================================================
# DynamoDB-backed tests (= symmetric coverage with moto)
# ===========================================================================

def test_dynamodb_get_missing_returns_none_for_legacy_auto(ddb_client):
    assert ddb_client.get_bus_event_approval(PID, "no-such-event") is None


def test_dynamodb_put_then_get_persists_seven_fields(ddb_client):
    written = ddb_client.put_bus_event_approval(
        PID, EV1,
        approval_status="pending",
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
    )
    _assert_seven_fields(
        written,
        event_id=EV1,
        approval_status="pending",
        decision_by=None,
        decision_at=None,
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
    )
    read_back = ddb_client.get_bus_event_approval(PID, EV1)
    assert read_back is not None
    _assert_seven_fields(
        read_back,
        event_id=EV1,
        approval_status="pending",
        decision_by=None,
        decision_at=None,
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
    )
    # Internal PK must not leak to the caller surface.
    assert "project_id" not in read_back


def test_dynamodb_lifecycle_preserves_created_at(ddb_client):
    first = ddb_client.put_bus_event_approval(
        PID, EV1,
        approval_status="pending",
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
    )
    original_created_at = first["created_at"]
    time.sleep(0.005)

    updated = ddb_client.put_bus_event_approval(
        PID, EV1,
        approval_status="denied",
        sender_user_id=USER_S,
        receiver_user_id=USER_A,
        decision_by=USER_A,
        decision_at="2026-06-14T13:00:00.000000Z",
    )
    assert updated["approval_status"] == "denied"
    assert updated["decision_by"] == USER_A
    assert updated["decision_at"] == "2026-06-14T13:00:00.000000Z"
    assert updated["created_at"] == original_created_at


def test_dynamodb_invalid_status_rejected(ddb_client):
    with pytest.raises(ValueError):
        ddb_client.put_bus_event_approval(
            PID, EV1,
            approval_status="bogus-status",
            sender_user_id=USER_S,
            receiver_user_id=USER_A,
        )


def test_dynamodb_list_pending_orders_and_filters(ddb_client):
    ddb_client.put_bus_event_approval(
        PID, EV1,
        approval_status="pending",
        sender_user_id=USER_S, receiver_user_id=USER_A,
    )
    time.sleep(0.002)
    ddb_client.put_bus_event_approval(
        PID, EV2,
        approval_status="pending",
        sender_user_id=USER_S, receiver_user_id=USER_B,
    )
    time.sleep(0.002)
    ddb_client.put_bus_event_approval(
        PID, EV3,
        approval_status="pending",
        sender_user_id=USER_S, receiver_user_id=USER_A,
    )
    ddb_client.put_bus_event_approval(
        PID, "evt-approved",
        approval_status="approved",
        sender_user_id=USER_S, receiver_user_id=USER_A,
        decision_by=USER_A, decision_at="2026-06-14T12:00:00.000000Z",
    )

    all_pending = ddb_client.list_pending_approvals(PID)
    assert [r["event_id"] for r in all_pending] == [EV1, EV2, EV3]
    assert all(r["approval_status"] == "pending" for r in all_pending)

    just_a = ddb_client.list_pending_approvals(
        PID, receiver_user_id=USER_A
    )
    assert [r["event_id"] for r in just_a] == [EV1, EV3]

    just_b = ddb_client.list_pending_approvals(
        PID, receiver_user_id=USER_B
    )
    assert [r["event_id"] for r in just_b] == [EV2]


def test_dynamodb_tables_dict_registers_new_entity(ddb_client):
    """The new sidecar table is wired into the TABLES dict so _table() resolves."""
    assert "bus_event_approvals" in ddb_client.TABLES
    assert ddb_client.TABLES["bus_event_approvals"] == "beacon-dev-bus_event_approvals"
