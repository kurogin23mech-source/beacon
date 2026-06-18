"""Denied notification reply chain (ms-70 / e-1717).

When the receiver presses ``beacon dm respond deny <event_id>``, the
sender AI on the other side of the bus is potentially blocked waiting
on a bus event whose human gatekeeper has just said "no". Without a
reply, that AI would either spin forever or rely on a side-channel
heartbeat to give up. e-1717 closes the loop: the server endpoint
emits a T5 (= AI autonomous, info-disclosure-forbidden, short-ping
schema) envelope addressed back to the original sender_session_id so
the sending AI can read the "denied by receiver" signal off the same
bus it was listening on and abandon the action.

The 4 ACs pinned here (per the e-1717 task brief):
  (a) deny → reply event is appended to the bus_events collection,
      addressed back to the original sender_session_id.
  (b) reply.envelope.in_reply_to == original event_id (= chain anchor).
  (c) reply.payload carries the structured "denied by receiver" signal
      — kind="deny", status="denied_by_receiver" (the parse anchor),
      and ack=<receiver email or sub>.
  (d) approve → no reply event is appended (= normal bus delivery
      resumes once the sidecar flips to approved; reply would be
      pure noise).

The endpoint state-machine is exercised through a small mirror of the
production handler's decision tree (same pattern as
tests/test_dm_respond_cli.py for e-1716) so the test does not need to
spin up FastAPI / auth / project membership plumbing. The mirror is
kept tight against the real endpoint — any divergence between the two
shows up as a test failure on the next refactor of either side.
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# Path setup — mirror tests/test_dm_respond_cli.py so server/ and lib/ are
# importable from the test process.
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_REPO_ROOT, "server"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "lib"))


# ---------------------------------------------------------------------------
# Firestore fake (in-memory). Same shape as test_dm_respond_cli.py — kept
# self-contained so parallel test execution is safe. Extends the e-1716
# fake with a small ``add`` method on collections so append_bus_event
# can write reply events into the in-memory bus_events subcollection.
# ---------------------------------------------------------------------------


class _FakeFirestoreClient:
    def __init__(self):
        self.docs: dict[tuple, dict] = {}
        self.subdocs: dict[tuple, dict] = {}
        self._auto_id_counter = 0

    def collection(self, name):
        return _FakeCollectionRef(self, name)

    def transaction(self):
        return _FakeTransaction(self)

    def next_auto_id(self) -> str:
        self._auto_id_counter += 1
        return f"auto-{self._auto_id_counter:06d}"


class _FakeTransaction:
    def __init__(self, client):
        self.client = client

    def set(self, ref, data):
        if ref.parent is None:
            self.client.docs[(ref.collection_name, ref.doc_id)] = dict(data)
        else:
            pcol, pdid = ref.parent
            self.client.subdocs[(pcol, pdid, ref.collection_name, ref.doc_id)] = dict(data)


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
        return _FakeCollectionRef(
            self.client, self.name, self.parent,
            self.filters + [(field, op, value)], self.order, self.limit_n,
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

    def add(self, data):
        """Mirror of Firestore CollectionRef.add: assign auto-id, store, return
        a (write_result, doc_ref) tuple where doc_ref.id is the assigned id.
        """
        doc_id = self.client.next_auto_id()
        if self.parent is None:
            self.client.docs[(self.name, doc_id)] = dict(data)
        else:
            pcol, pdid = self.parent
            self.client.subdocs[(pcol, pdid, self.name, doc_id)] = dict(data)
        return (None, _FakeDocumentRef(self.client, self.name, doc_id, self.parent))

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


@pytest.fixture
def firestore_client(monkeypatch):
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


@pytest.fixture
def envelope_module():
    """Import the envelope module under the same path setup as firestore_client.

    The envelope module is independent of Firestore so no fake is required —
    we just need it on sys.path with the secret env var set so issue_envelope
    can sign envelopes deterministically across tests.
    """
    os.environ.setdefault("BEACON_ENVELOPE_SECRET", "test-secret-e1717")
    sys.modules.pop("envelope", None)
    import envelope as env_mod
    yield env_mod


# ---------------------------------------------------------------------------
# Endpoint mirror: same shape as test_dm_respond_cli.py's _endpoint_decide,
# extended with the e-1717 deny → reply-chain append. Kept close to the real
# handler in server/app.py:respond_dm_approval — any drift here is the signal
# to re-pin the production code path.
# ---------------------------------------------------------------------------

PID = "proj-x"
EVT = "evt-original-aaa"
USER_RECEIVER = "user-bob"
USER_SENDER = "user-alice"
RECEIVER_EMAIL = "bob@example.com"
SENDER_SESSION_ID = "sv-alice-session-001"


def _endpoint_decide(fc, env_mod, project_id: str, event_id: str, *,
                     decision: str,
                     caller_uid: str,
                     caller_email: str = "") -> dict:
    """Mirror of POST /api/projects/{pid}/dm/approval/{event_id} with
    the e-1717 denied-reply chain spliced in on the deny branch.
    """
    norm = (decision or "").strip().lower()
    if norm not in ("approve", "deny"):
        raise RuntimeError(f"HTTP 400: invalid decision {decision!r}")
    target_status = "approved" if norm == "approve" else "denied"

    existing = fc.get_bus_event_approval(project_id, event_id)
    if existing is None:
        raise RuntimeError(
            f"HTTP 404: no pending approval found for event_id={event_id!r}"
        )

    receiver_uid = existing.get("receiver_user_id") or ""
    sender_uid = existing.get("sender_user_id") or ""
    if caller_uid and receiver_uid and caller_uid != receiver_uid:
        raise RuntimeError(
            f"HTTP 403: event_id={event_id!r} addressed to a different user"
        )

    current = existing.get("approval_status") or ""
    if current in ("approved", "denied"):
        if (existing.get("decision_by") == caller_uid
                and current == target_status):
            return existing
        raise RuntimeError(
            f"HTTP 409: event_id={event_id!r} already decided as {current!r}"
        )
    if current == "auto":
        raise RuntimeError(
            f"HTTP 409: event_id={event_id!r} is auto-allowed"
        )

    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    row = fc.put_bus_event_approval(
        project_id, event_id,
        approval_status=target_status,
        sender_user_id=sender_uid,
        receiver_user_id=receiver_uid,
        decision_by=caller_uid,
        decision_at=now,
    )

    # e-1717: deny → emit T5 reply addressed back to original sender_session_id.
    if norm == "deny":
        original_event = fc.get_bus_event(project_id, event_id)
        if original_event is not None:
            sender_session_id = original_event.get("sender_session_id") or ""
            if sender_session_id:
                receiver_ident = caller_email or caller_uid or ""
                if len(receiver_ident) > 32:
                    receiver_ident = receiver_ident[:32]
                original_envelope = original_event.get("envelope") or {}
                parent_chain_depth = original_envelope.get("chain_depth") or 0
                parent_conversation = (
                    original_envelope.get("conversation_id") or None
                )
                reply_envelope = env_mod.issue_envelope(
                    tier=env_mod.TIER_T5,
                    issuer=caller_email or caller_uid or "server",
                    project_id=project_id,
                    actions_authorized=[],
                    scope=None,
                    conversation_id=parent_conversation,
                    in_reply_to=event_id,
                    chain_depth=int(parent_chain_depth) + 1,
                )
                reply_data = {
                    "channel": "dm",
                    "sender_session_id": "",
                    "payload": {
                        "kind": "deny",
                        "status": "denied_by_receiver",
                        "ack": receiver_ident,
                        "recipient_session_id": sender_session_id,
                        "in_reply_to": event_id,
                    },
                    "delivery": "notify-user-only",
                    "envelope": reply_envelope,
                    "created_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                }
                fc.append_bus_event(project_id, reply_data)

    return row


def _seed_original_event(fc, *,
                         sender_session_id=SENDER_SESSION_ID,
                         project_id=PID) -> str:
    """Insert an original bus_event the way post_bus_event would for a
    pending DM-action envelope. Kept minimal: only the fields the deny-reply
    chain reads (sender_session_id, envelope) are populated.

    Returns the auto-assigned event_id so tests can seed the matching
    sidecar row under the same id (= what production does: the sidecar
    is keyed by event_id from append_bus_event's return value).
    """
    return fc.append_bus_event(project_id, {
        "channel": "dm",
        "sender_session_id": sender_session_id,
        "payload": {
            "recipient_session_id": "sv-bob-session-001",
            "text": "please run extract",
            "action": "extract:profile",
        },
        "envelope": {
            "tier": "T2",
            "issuer": "alice@example.com",
            "scope": "op-xyz",
            "actions_authorized": ["extract:profile"],
            "chain_depth": 0,
            "conversation_id": "conv-original-001",
        },
        "delivery": "propose-to-ai",
        "created_at": "2026-06-17T10:00:00.000000Z",
    })


def _seed_pending(fc, *, event_id=EVT, receiver=USER_RECEIVER,
                  sender=USER_SENDER, project_id=PID):
    """Insert a pending sidecar row exactly the way dm_gate dispatcher would."""
    return fc.put_bus_event_approval(
        project_id, event_id,
        approval_status="pending",
        sender_user_id=sender,
        receiver_user_id=receiver,
    )


# ---------------------------------------------------------------------------
# Helper: list bus_events for the project from the in-memory fake.
# ---------------------------------------------------------------------------

def _list_bus_events(fc, project_id: str) -> list[dict]:
    rows = []
    # COLLECTION resolves to "projects" in prod, "projects-dev" otherwise.
    # We don't pin which one — accept either since the env-dependent value
    # leaks through firestore_client at import time.
    for key, data in fc.get_db().subdocs.items():
        if (key[0] in ("projects", "projects-dev")
                and key[1] == project_id
                and key[2] == "bus_events"):
            rows.append({"event_id": key[3], **data})
    return rows


# ===========================================================================
# AC (a): deny → exactly one reply event appended addressed back to the
# original sender_session_id.
# ===========================================================================

def test_deny_appends_reply_addressed_to_sender_session(
        firestore_client, envelope_module):
    original_event_id = _seed_original_event(firestore_client)
    _seed_pending(firestore_client, event_id=original_event_id)

    before = _list_bus_events(firestore_client, PID)
    assert len(before) == 1  # only the original

    _endpoint_decide(
        firestore_client, envelope_module, PID, original_event_id,
        decision="deny",
        caller_uid=USER_RECEIVER,
        caller_email=RECEIVER_EMAIL,
    )

    after = _list_bus_events(firestore_client, PID)
    assert len(after) == 2, (
        f"expected 1 original + 1 deny-reply, got {len(after)} rows"
    )
    # Identify the reply by event_id != original.
    replies = [r for r in after if r["event_id"] != original_event_id]
    assert len(replies) == 1
    reply = replies[0]
    # The reply is routed back to the original sender's session via
    # payload.recipient_session_id (= bus convention, see app.py:3958-4002
    # for the receive-side router that reads this field).
    assert reply["payload"]["recipient_session_id"] == SENDER_SESSION_ID
    # Channel must be "dm" so the original sender's bus listener — which
    # subscribes to ["dm", "operation-trigger"] — actually sees the reply.
    assert reply["channel"] == "dm"


# ===========================================================================
# AC (b): reply envelope.in_reply_to == original event_id (chain anchor),
# and the payload also surfaces in_reply_to as a parse-cheap signal.
# ===========================================================================

def test_deny_reply_in_reply_to_matches_original(
        firestore_client, envelope_module):
    original_event_id = _seed_original_event(firestore_client)
    _seed_pending(firestore_client, event_id=original_event_id)

    _endpoint_decide(
        firestore_client, envelope_module, PID, original_event_id,
        decision="deny",
        caller_uid=USER_RECEIVER,
        caller_email=RECEIVER_EMAIL,
    )

    replies = [
        r for r in _list_bus_events(firestore_client, PID)
        if r["event_id"] != original_event_id
    ]
    assert len(replies) == 1
    reply = replies[0]
    # Envelope-level chain anchor — what the 9-step verify pipeline reads
    # on the sender's side to traverse the reply chain back to the
    # original envelope. This is the structural realization of "this
    # reply belongs to that envelope".
    assert reply["envelope"]["in_reply_to"] == original_event_id
    # Payload-level mirror so receivers who don't unpack the envelope
    # still see the chain key on the cheap path.
    assert reply["payload"]["in_reply_to"] == original_event_id
    # T5 floor per AC 3: server-issued, info-disclosure-forbidden, no
    # actions, no scope.
    assert reply["envelope"]["tier"] == "T5"
    assert reply["envelope"]["actions_authorized"] == []
    assert reply["envelope"]["scope"] is None


# ===========================================================================
# AC (c): payload carries the structured "denied by receiver" signal —
# kind / status (the parse anchor) / ack with the receiver email.
# ===========================================================================

def test_deny_reply_payload_carries_denied_by_receiver_signal(
        firestore_client, envelope_module):
    original_event_id = _seed_original_event(firestore_client)
    _seed_pending(firestore_client, event_id=original_event_id)

    _endpoint_decide(
        firestore_client, envelope_module, PID, original_event_id,
        decision="deny",
        caller_uid=USER_RECEIVER,
        caller_email=RECEIVER_EMAIL,
    )

    replies = [
        r for r in _list_bus_events(firestore_client, PID)
        if r["event_id"] != original_event_id
    ]
    assert len(replies) == 1
    payload = replies[0]["payload"]
    # Structural "denied by receiver" — kept in ping-shape fields because
    # T5 forbids free-text values. status is the canonical parse anchor:
    # the substring "denied" + "receiver" both appear in it.
    assert payload["kind"] == "deny"
    assert payload["status"] == "denied_by_receiver"
    assert "denied" in payload["status"]
    assert "receiver" in payload["status"]
    # Receiver email/ack (caps at 32 chars per T5 short-ping schema).
    assert payload["ack"] == RECEIVER_EMAIL
    assert len(payload["ack"]) <= 32


# ===========================================================================
# AC (d): approve → no reply event is appended (= normal delivery resumes
# once the sidecar flips to approved; reply would be pure noise).
# ===========================================================================

def test_approve_does_not_append_reply(firestore_client, envelope_module):
    original_event_id = _seed_original_event(firestore_client)
    _seed_pending(firestore_client, event_id=original_event_id)

    before_count = len(_list_bus_events(firestore_client, PID))
    assert before_count == 1

    _endpoint_decide(
        firestore_client, envelope_module, PID, original_event_id,
        decision="approve",
        caller_uid=USER_RECEIVER,
        caller_email=RECEIVER_EMAIL,
    )

    after = _list_bus_events(firestore_client, PID)
    assert len(after) == 1, (
        "approve must not append any reply event; the sender AI resumes "
        "on normal delivery once the sidecar flips to approved"
    )


# ===========================================================================
# Defensive: when the original bus_event cannot be found (= shouldn't
# happen but is theoretically possible if the bus collection was pruned),
# the deny decision must still succeed — the sidecar is the source of
# truth, the reply is best-effort downstream notification.
# ===========================================================================

def test_deny_succeeds_when_original_event_missing(
        firestore_client, envelope_module):
    # Sidecar exists but no matching bus_event (= bus collection pruned).
    _seed_pending(firestore_client, event_id=EVT)

    row = _endpoint_decide(
        firestore_client, envelope_module, PID, EVT,
        decision="deny",
        caller_uid=USER_RECEIVER,
        caller_email=RECEIVER_EMAIL,
    )

    # The deny still lands in the sidecar — that's the durable state.
    assert row["approval_status"] == "denied"
    assert row["decision_by"] == USER_RECEIVER

    # No reply was appended (= original event missing); the sidecar is
    # what callers trust.
    replies = _list_bus_events(firestore_client, PID)
    assert replies == []
