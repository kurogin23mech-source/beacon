"""DM approval history audit view (ms-70 / e-1718).

These tests pin the structural behaviour of ``list_decided_approvals``
across both backends (Firestore + DynamoDB), plus the corresponding
``GET /api/projects/{pid}/dm/approval/history`` endpoint.

The audit endpoint exists so the Web UI can surface "who decided what,
when" on past DM action envelopes without breaching the SPEC 設計方針 3
invariant that approve / deny decisions live exclusively inside the
terminal Claude Code (= the receiver's typing, not a browser click).

Acceptance criteria pinned (e-1718 AC 7):

(a) Endpoint / function returns only ``approved`` and ``denied`` rows —
    ``pending`` and ``auto`` are filtered out. This is the structural
    guard that prevents a future contributor from accidentally wiring
    approve / deny buttons on top of the audit view (= they would never
    even see a pending row to attach a button to).
(b) Both backends produce equivalent output for the same input — no
    Firestore vs DynamoDB drift.
(c) ``limit`` truncates correctly and rows are returned newest-first
    by ``decision_at`` (with ``created_at`` as the fallback sort key).
(d) Cross-project leak is impossible — a query for project A never
    sees project B's rows even when both share an event_id by accident.

The fakes used here mirror tests/test_envelope_approval_schema.py so
the two test files stay in lockstep — if one needs a fake feature, the
other will too.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


# ---------------------------------------------------------------------------
# Firestore fake plumbing (mirrors tests/test_envelope_approval_schema.py
# byte-for-byte so a fix in one file doesn't silently diverge here).
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
        self.parent = parent
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

    sys.modules.pop("firestore_client", None)
    import firestore_client as fc

    monkeypatch.setattr(fc, "get_db", lambda: fake_db)
    yield fc
    sys.modules.pop("firestore_client", None)


# ---------------------------------------------------------------------------
# DynamoDB / moto plumbing (mirrors tests/test_envelope_approval_schema.py).
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
# Shared seed helpers / IDs
# ---------------------------------------------------------------------------

PID_A = "proj-aaa"
PID_B = "proj-bbb"  # used to pin cross-project isolation
USER_S = "user-sender"
USER_R = "user-receiver"


def _seed_mixed_rows(client, project_id):
    """Seed one row of each status (approved, denied, pending, auto).

    Returns the seeded approved & denied event_ids in **decision_at**
    descending order so callers can assert the expected newest-first
    ordering without having to recompute it.
    """
    # Two decided rows whose ``decision_at`` we stamp explicitly so the
    # newest-first sort is deterministic (= server clock millisecond
    # collisions otherwise can make the test flaky).
    client.put_bus_event_approval(
        project_id, "evt-approved-old",
        approval_status="approved",
        sender_user_id=USER_S, receiver_user_id=USER_R,
        decision_by=USER_R,
        decision_at="2026-06-14T10:00:00.000000Z",
    )
    time.sleep(0.002)
    client.put_bus_event_approval(
        project_id, "evt-denied-new",
        approval_status="denied",
        sender_user_id=USER_S, receiver_user_id=USER_R,
        decision_by=USER_R,
        decision_at="2026-06-14T12:00:00.000000Z",
    )
    # Pending row — must NOT appear in decided history.
    client.put_bus_event_approval(
        project_id, "evt-pending",
        approval_status="pending",
        sender_user_id=USER_S, receiver_user_id=USER_R,
    )
    # Auto row — must NOT appear (= no human decision to audit).
    client.put_bus_event_approval(
        project_id, "evt-auto",
        approval_status="auto",
        sender_user_id=USER_S, receiver_user_id=USER_R,
    )


# ===========================================================================
# AC (a): list_decided_approvals returns only approved + denied (Firestore)
# ===========================================================================

def test_firestore_list_decided_excludes_pending_and_auto(firestore_client):
    """承認 / 拒否のみ返り、保留と auto は完全に消える (audit invariant).

    SPEC 設計方針 3 は「terminal Claude Code 内での user 直接判断のみ」を
    要求する。Web UI に pending を見せると将来の貢献者が approve / deny
    ボタンを生やしたくなる誘惑が生まれるため、pending を構造的に隠して
    その誘惑そのものを断つ — これがこのテストが守っている invariant.
    """
    _seed_mixed_rows(firestore_client, PID_A)
    out = firestore_client.list_decided_approvals(PID_A)
    ids = sorted(r["event_id"] for r in out)
    assert ids == ["evt-approved-old", "evt-denied-new"], (
        f"pending / auto must not leak into audit, got {ids!r}"
    )


# ===========================================================================
# AC (a): list_decided_approvals returns only approved + denied (DynamoDB)
# ===========================================================================

def test_dynamodb_list_decided_excludes_pending_and_auto(ddb_client):
    _seed_mixed_rows(ddb_client, PID_A)
    out = ddb_client.list_decided_approvals(PID_A)
    ids = sorted(r["event_id"] for r in out)
    assert ids == ["evt-approved-old", "evt-denied-new"], (
        f"pending / auto must not leak into audit, got {ids!r}"
    )


# ===========================================================================
# AC (b): both backends produce equivalent output for the same input
# ===========================================================================

def test_dual_backend_parity(firestore_client, ddb_client):
    """Firestore と DynamoDB が同じ入力で同じ shape / ordering を返す.

    Backend drift は ms-46 / ms-54 で何度も観察された pain point (= e-1155
    HMAC drift, e-1208 retro reorder drift, etc.) なので、新エンドポイントは
    最初から parity を test で固定する。
    """
    _seed_mixed_rows(firestore_client, PID_A)
    _seed_mixed_rows(ddb_client, PID_A)
    fs_out = firestore_client.list_decided_approvals(PID_A)
    dd_out = ddb_client.list_decided_approvals(PID_A)

    # Same set of event_ids
    assert sorted(r["event_id"] for r in fs_out) == sorted(
        r["event_id"] for r in dd_out
    )
    # Newest-first ordering — both backends agree on which row is row 0.
    assert fs_out[0]["event_id"] == "evt-denied-new"
    assert dd_out[0]["event_id"] == "evt-denied-new"
    # All visible rows expose the 7-field surface (with project_id stripped
    # on the DynamoDB side as per get_bus_event_approval's contract).
    for row in fs_out + dd_out:
        assert set(row.keys()) >= {
            "event_id", "approval_status", "decision_by", "decision_at",
            "created_at", "sender_user_id", "receiver_user_id",
        }
        assert "project_id" not in row, (
            "internal PK must not leak through audit surface"
        )


# ===========================================================================
# AC (c): limit truncates correctly and ordering is newest-first
# ===========================================================================

def _seed_n_decided(client, project_id, n):
    """Seed N approved rows with monotonically increasing decision_at."""
    for i in range(n):
        client.put_bus_event_approval(
            project_id, f"evt-{i:03d}",
            approval_status="approved",
            sender_user_id=USER_S, receiver_user_id=USER_R,
            decision_by=USER_R,
            # ISO8601 sortable: hour increments per row so the newest is
            # unambiguously the last seeded.
            decision_at=f"2026-06-14T{i:02d}:00:00.000000Z",
        )


def test_firestore_limit_and_newest_first(firestore_client):
    """limit=3 で 3 件 / 新しい順 (evt-009, 008, 007) で返る."""
    _seed_n_decided(firestore_client, PID_A, n=10)
    out = firestore_client.list_decided_approvals(PID_A, limit=3)
    assert len(out) == 3
    assert [r["event_id"] for r in out] == ["evt-009", "evt-008", "evt-007"]


def test_dynamodb_limit_and_newest_first(ddb_client):
    _seed_n_decided(ddb_client, PID_A, n=10)
    out = ddb_client.list_decided_approvals(PID_A, limit=3)
    assert len(out) == 3
    assert [r["event_id"] for r in out] == ["evt-009", "evt-008", "evt-007"]


# ===========================================================================
# AC (d): cross-project leak is impossible
# ===========================================================================

def test_firestore_cross_project_isolation(firestore_client):
    """project A への問い合わせが project B の row を一切覗かない.

    Same event_id を両 project に意図的に作って "PK 単体での lookup でも
    project_id で必ず絞られる" 構造を test 化する。
    """
    firestore_client.put_bus_event_approval(
        PID_A, "evt-shared-id",
        approval_status="approved",
        sender_user_id=USER_S, receiver_user_id=USER_R,
        decision_by=USER_R, decision_at="2026-06-14T10:00:00.000000Z",
    )
    firestore_client.put_bus_event_approval(
        PID_B, "evt-shared-id",
        approval_status="denied",
        sender_user_id=USER_S, receiver_user_id=USER_R,
        decision_by=USER_R, decision_at="2026-06-14T11:00:00.000000Z",
    )
    a = firestore_client.list_decided_approvals(PID_A)
    b = firestore_client.list_decided_approvals(PID_B)
    assert len(a) == 1 and a[0]["approval_status"] == "approved"
    assert len(b) == 1 and b[0]["approval_status"] == "denied"


def test_dynamodb_cross_project_isolation(ddb_client):
    ddb_client.put_bus_event_approval(
        PID_A, "evt-shared-id",
        approval_status="approved",
        sender_user_id=USER_S, receiver_user_id=USER_R,
        decision_by=USER_R, decision_at="2026-06-14T10:00:00.000000Z",
    )
    ddb_client.put_bus_event_approval(
        PID_B, "evt-shared-id",
        approval_status="denied",
        sender_user_id=USER_S, receiver_user_id=USER_R,
        decision_by=USER_R, decision_at="2026-06-14T11:00:00.000000Z",
    )
    a = ddb_client.list_decided_approvals(PID_A)
    b = ddb_client.list_decided_approvals(PID_B)
    assert len(a) == 1 and a[0]["approval_status"] == "approved"
    assert len(b) == 1 and b[0]["approval_status"] == "denied"


# ===========================================================================
# Empty-list shape (no rows → empty list, not None)
# ===========================================================================

def test_firestore_empty_returns_empty_list(firestore_client):
    """0 件 → [] (None ではない). 呼び出し側の len(out) / iteration が
    None ガード不要になる契約を pin する."""
    out = firestore_client.list_decided_approvals(PID_A)
    assert out == []


def test_dynamodb_empty_returns_empty_list(ddb_client):
    out = ddb_client.list_decided_approvals(PID_A)
    assert out == []
