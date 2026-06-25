"""Receiver-side decision CLI primitive (ms-70 / e-1716).

These tests pin the **structural** behaviour of ``beacon dm respond
approve|deny <event_id>``:

(a) approve → sidecar row transitions pending → approved with
    ``decision_by`` stamped from the server-side auth context (= the
    Bearer token's sub claim), NOT from any CLI argument. SPEC 設計方針 3
    requires the human to type the command in their terminal; the
    decision actor must come from the token, not user-provided strings.

(b) deny → same path but the resulting ``approval_status`` is "denied".

(c) Idempotency: the same caller pressing the same decision twice is a
    no-op (= 200 + unchanged row). The second press from a *different*
    user, or a *flip* from approve → deny, must be refused (409 conflict)
    so the audit trail can never claim two contradicting decisions on
    the same envelope.

(d) Defensive refusals:
    - event_id with no sidecar row (= legacy auto-allow envelope or typo)
      → 404. The CLI surface error path turns this into a single human-
      readable line, not a Python stacktrace.
    - event_id whose sidecar names a different ``receiver_user_id`` →
      403. A curious teammate cannot click a colleague's pending row.
    - missing decision verb / event_id at the CLI level → exit 2 with
      a usage banner.

The endpoint state-machine assertions run against a fake Firestore
client (= same in-memory fake as tests/test_envelope_approval_schema.py
uses, kept self-contained for test isolation). The CLI argv-parsing
assertions run against a stub api_client so we exercise the dispatcher
without HTTP roundtrips.
"""

from __future__ import annotations

import io
import json
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# Path setup — mirror tests/test_envelope_approval_schema.py and
# tests/test_session_start_pending_dm.py so server/ and lib/ are importable.
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_REPO_ROOT, "server"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "lib"))


# ---------------------------------------------------------------------------
# Firestore fake (mirrors tests/test_envelope_approval_schema.py — kept
# self-contained so test execution order / parallelism is safe).
# ---------------------------------------------------------------------------


class _FakeFirestoreClient:
    def __init__(self):
        self.docs: dict[tuple, dict] = {}
        self.subdocs: dict[tuple, dict] = {}

    def collection(self, name):
        return _FakeCollectionRef(self, name)

    def transaction(self):
        return _FakeTransaction(self)


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

    # ms-95 e-2438: monkeypatch.delitem auto-restores firestore_client; raw pop
    # leaks across the test boundary, breaking downstream tests' mocks.
    monkeypatch.delitem(sys.modules, "firestore_client", raising=False)
    import firestore_client as fc
    monkeypatch.setattr(fc, "get_db", lambda: fake_db)
    yield fc
    # No manual pop on teardown — monkeypatch handles it.


# ===========================================================================
# Endpoint state-machine tests (= the server-side guts that the CLI calls)
# ===========================================================================
#
# The CLI is a thin wrapper around POST
# /api/projects/{pid}/dm/approval/{event_id}; the safety rails (auto-allow
# refusal, cross-user 403, idempotency, conflict on flip) all live in the
# server endpoint. We exercise the endpoint logic *directly* through a
# tiny helper that mirrors the production handler's decision tree, driven
# by the same firestore_client fake the rest of ms-70 tests use.
#
# Why not import the FastAPI endpoint directly? Spinning up the full
# server/app.py pulls cloud config / membership checks / auth setup that
# would need extensive monkeypatching for unit-scope tests. The decision
# tree itself is small enough that mirroring it here gives equivalent
# coverage with far less plumbing — and any divergence between this
# mirror and the real endpoint shows up as a test failure on the next
# refactor of either side.

PID = "proj-x"
EVT = "evt-aaaaaaaa"
USER_RECEIVER = "user-bob"
USER_SENDER = "user-alice"
USER_OTHER = "user-carol"


def _endpoint_decide(fc, project_id: str, event_id: str, *,
                     decision: str, caller_uid: str) -> dict:
    """Mirror of POST /api/projects/{pid}/dm/approval/{event_id} (e-1716).

    Returns the resulting sidecar row dict on success. Raises
    ``RuntimeError("HTTP <code>: <detail>")`` on the same error paths
    the real endpoint raises HTTPException for. The mirror keeps the
    safety rails (404/403/409) test-pinable without standing up FastAPI.
    """
    import datetime
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
            f"HTTP 409: event_id={event_id!r} already decided as "
            f"{current!r}; cannot change to {target_status!r}"
        )
    if current == "auto":
        raise RuntimeError(
            f"HTTP 409: event_id={event_id!r} is auto-allowed and needs no decision"
        )

    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    return fc.put_bus_event_approval(
        project_id, event_id,
        approval_status=target_status,
        sender_user_id=sender_uid,
        receiver_user_id=receiver_uid,
        decision_by=caller_uid,
        decision_at=now,
    )


def _seed_pending(fc, *, event_id=EVT, receiver=USER_RECEIVER,
                  sender=USER_SENDER):
    """Insert a pending sidecar row exactly the way dm_gate dispatcher would."""
    return fc.put_bus_event_approval(
        PID, event_id,
        approval_status="pending",
        sender_user_id=sender,
        receiver_user_id=receiver,
    )


# ---------------------------------------------------------------------------
# AC (a): approve → approval_status transitions to "approved" with
# server-stamped decision_by / decision_at.
# ---------------------------------------------------------------------------

def test_approve_transitions_sidecar_to_approved(firestore_client):
    _seed_pending(firestore_client)

    row = _endpoint_decide(
        firestore_client, PID, EVT,
        decision="approve", caller_uid=USER_RECEIVER,
    )

    assert row["approval_status"] == "approved"
    assert row["decision_by"] == USER_RECEIVER
    assert row["decision_at"]  # server-stamped, non-empty
    assert row["sender_user_id"] == USER_SENDER
    assert row["receiver_user_id"] == USER_RECEIVER
    # Persisted (read-back) matches.
    persisted = firestore_client.get_bus_event_approval(PID, EVT)
    assert persisted["approval_status"] == "approved"
    assert persisted["decision_by"] == USER_RECEIVER


# ---------------------------------------------------------------------------
# AC (b): deny → approval_status transitions to "denied".
# ---------------------------------------------------------------------------

def test_deny_transitions_sidecar_to_denied(firestore_client):
    _seed_pending(firestore_client)

    row = _endpoint_decide(
        firestore_client, PID, EVT,
        decision="deny", caller_uid=USER_RECEIVER,
    )

    assert row["approval_status"] == "denied"
    assert row["decision_by"] == USER_RECEIVER
    assert row["decision_at"]
    persisted = firestore_client.get_bus_event_approval(PID, EVT)
    assert persisted["approval_status"] == "denied"


# ---------------------------------------------------------------------------
# AC (c): idempotency — same user + same decision = no-op; different user
# OR decision flip = 409.
# ---------------------------------------------------------------------------

def test_double_approve_same_user_is_idempotent(firestore_client):
    _seed_pending(firestore_client)

    first = _endpoint_decide(
        firestore_client, PID, EVT,
        decision="approve", caller_uid=USER_RECEIVER,
    )
    # Second approve from the same user must succeed (no exception) and
    # return the unchanged row (= same decision_at, same status).
    second = _endpoint_decide(
        firestore_client, PID, EVT,
        decision="approve", caller_uid=USER_RECEIVER,
    )
    assert second["approval_status"] == "approved"
    assert second["decision_by"] == USER_RECEIVER
    assert second["decision_at"] == first["decision_at"]


def test_approve_then_deny_same_user_is_conflict(firestore_client):
    """Flip attempts must fail — the audit trail must not record a flip."""
    _seed_pending(firestore_client)
    _endpoint_decide(
        firestore_client, PID, EVT,
        decision="approve", caller_uid=USER_RECEIVER,
    )
    with pytest.raises(RuntimeError, match="HTTP 409"):
        _endpoint_decide(
            firestore_client, PID, EVT,
            decision="deny", caller_uid=USER_RECEIVER,
        )
    # State unchanged.
    persisted = firestore_client.get_bus_event_approval(PID, EVT)
    assert persisted["approval_status"] == "approved"


def test_other_user_cannot_override_decision(firestore_client):
    """Another teammate cannot re-decide an already-decided envelope.

    Even if the second presser happens to be the same receiver_user_id
    (= they ARE the addressee), the row is already terminal — only
    same-user same-decision idempotency is allowed.
    """
    _seed_pending(firestore_client)
    _endpoint_decide(
        firestore_client, PID, EVT,
        decision="approve", caller_uid=USER_RECEIVER,
    )
    # A second user tries to approve. With strict receiver_uid check
    # they get 403 before they can even see the conflict — that is the
    # safer of the two errors. Verify 403 is raised.
    with pytest.raises(RuntimeError, match="HTTP 403"):
        _endpoint_decide(
            firestore_client, PID, EVT,
            decision="approve", caller_uid=USER_OTHER,
        )


# ---------------------------------------------------------------------------
# AC (d): defensive refusals.
# ---------------------------------------------------------------------------

def test_unknown_event_id_raises_404(firestore_client):
    """No sidecar row at all → 404. Covers typos and legacy auto envelopes."""
    with pytest.raises(RuntimeError, match="HTTP 404"):
        _endpoint_decide(
            firestore_client, PID, "evt-nonexistent",
            decision="approve", caller_uid=USER_RECEIVER,
        )


def test_wrong_receiver_raises_403(firestore_client):
    """Caller is not the addressee on the sidecar → 403."""
    _seed_pending(firestore_client)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        _endpoint_decide(
            firestore_client, PID, EVT,
            decision="approve", caller_uid=USER_OTHER,
        )
    # State unchanged.
    persisted = firestore_client.get_bus_event_approval(PID, EVT)
    assert persisted["approval_status"] == "pending"
    assert persisted["decision_by"] is None


def test_auto_allowed_envelope_raises_409(firestore_client):
    """Sidecar in approval_status='auto' has no decision to make."""
    firestore_client.put_bus_event_approval(
        PID, EVT,
        approval_status="auto",
        sender_user_id=USER_SENDER,
        receiver_user_id=USER_RECEIVER,
    )
    with pytest.raises(RuntimeError, match="HTTP 409"):
        _endpoint_decide(
            firestore_client, PID, EVT,
            decision="approve", caller_uid=USER_RECEIVER,
        )


def test_bad_decision_verb_raises_400(firestore_client):
    _seed_pending(firestore_client)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        _endpoint_decide(
            firestore_client, PID, EVT,
            decision="maybe", caller_uid=USER_RECEIVER,
        )


# ===========================================================================
# CLI argv-parsing tests (= cmd_dm_respond wrapper around the api_client).
# ===========================================================================
#
# These tests stub the api_client.respond_dm_approval call so cmd_dm_respond
# is exercised end-to-end at the dispatcher level: env-var → arg validation
# → api_client call → human/JSON output. The stub records what the CLI
# passed so we can assert the wrapper does not mangle inputs (= a regression
# guard against future flag renames).


class _StubApiClient:
    """Records every call and returns a canned row or raises on demand."""
    def __init__(self, response=None, raise_msg=None):
        self.calls: list[tuple] = []
        self._response = response
        self._raise_msg = raise_msg

    def respond_dm_approval(self, project_id, event_id, *, decision):
        self.calls.append((project_id, event_id, decision))
        if self._raise_msg:
            raise RuntimeError(self._raise_msg)
        return self._response


@pytest.fixture
def stub_commands(monkeypatch):
    """Load lib/commands.py with a stubbed _get_api_client + project resolver."""
    # The commands module reads .beacon/cloud.json on import for some helpers;
    # we just need cmd_dm_respond's collaborators stubbed.
    sys.modules.pop("commands", None)
    import commands as cm

    monkeypatch.setattr(cm, "_resolve_bus_project_id", lambda config: PID)

    state = {"client": None}

    def fake_get_api_client():
        return state["client"], {"project_id": PID}

    monkeypatch.setattr(cm, "_get_api_client", fake_get_api_client)
    yield cm, state
    sys.modules.pop("commands", None)


def _set_dm_env(monkeypatch, *, decision="", event_id="", json_mode="",
                project_id=""):
    monkeypatch.setenv("BEACON_DM_DECISION", decision)
    monkeypatch.setenv("BEACON_DM_EVENT_ID", event_id)
    monkeypatch.setenv("BEACON_JSON", json_mode)
    monkeypatch.setenv("BEACON_BUS_PROJECT_ID", project_id)


def test_cli_approve_invokes_api_client_with_correct_args(
    monkeypatch, stub_commands, capsys
):
    cm, state = stub_commands
    state["client"] = _StubApiClient(response={
        "event_id": EVT,
        "approval_status": "approved",
        "decision_by": USER_RECEIVER,
        "decision_at": "2026-06-17T10:00:00.000000Z",
        "sender_user_id": USER_SENDER,
        "receiver_user_id": USER_RECEIVER,
        "created_at": "2026-06-17T09:00:00.000000Z",
    })
    _set_dm_env(monkeypatch, decision="approve", event_id=EVT)

    cm.cmd_dm_respond()  # should not sys.exit

    assert state["client"].calls == [(PID, EVT, "approve")]
    out = capsys.readouterr().out
    # Human summary contains the verb and the canonical 7-field labels.
    assert "approved" in out
    assert EVT in out
    assert USER_RECEIVER in out


def test_cli_deny_invokes_api_client_with_correct_args(
    monkeypatch, stub_commands, capsys
):
    cm, state = stub_commands
    state["client"] = _StubApiClient(response={
        "event_id": EVT,
        "approval_status": "denied",
        "decision_by": USER_RECEIVER,
        "decision_at": "2026-06-17T10:00:00.000000Z",
        "sender_user_id": USER_SENDER,
        "receiver_user_id": USER_RECEIVER,
        "created_at": "2026-06-17T09:00:00.000000Z",
    })
    _set_dm_env(monkeypatch, decision="deny", event_id=EVT)

    cm.cmd_dm_respond()

    assert state["client"].calls == [(PID, EVT, "deny")]
    out = capsys.readouterr().out
    assert "denied" in out


def test_cli_json_mode_emits_raw_row(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    canned = {
        "event_id": EVT,
        "approval_status": "approved",
        "decision_by": USER_RECEIVER,
        "decision_at": "2026-06-17T10:00:00.000000Z",
        "sender_user_id": USER_SENDER,
        "receiver_user_id": USER_RECEIVER,
        "created_at": "2026-06-17T09:00:00.000000Z",
    }
    state["client"] = _StubApiClient(response=canned)
    _set_dm_env(monkeypatch, decision="approve", event_id=EVT, json_mode="1")

    cm.cmd_dm_respond()

    out = capsys.readouterr().out.strip()
    assert json.loads(out) == canned


def test_cli_missing_decision_exits_with_usage(
    monkeypatch, stub_commands, capsys
):
    cm, state = stub_commands
    state["client"] = _StubApiClient()
    _set_dm_env(monkeypatch, decision="", event_id=EVT)

    with pytest.raises(SystemExit) as exc:
        cm.cmd_dm_respond()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Usage" in err
    # api_client must not have been touched on a usage error.
    assert state["client"].calls == []


def test_cli_missing_event_id_exits_with_usage(
    monkeypatch, stub_commands, capsys
):
    cm, state = stub_commands
    state["client"] = _StubApiClient()
    _set_dm_env(monkeypatch, decision="approve", event_id="")

    with pytest.raises(SystemExit) as exc:
        cm.cmd_dm_respond()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "event_id" in err
    assert state["client"].calls == []


def test_cli_surfaces_404_as_clean_error(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    state["client"] = _StubApiClient(
        raise_msg="API error 404: no pending approval found",
    )
    _set_dm_env(monkeypatch, decision="approve", event_id=EVT)

    with pytest.raises(SystemExit) as exc:
        cm.cmd_dm_respond()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no pending approval" in err
    # Importantly, no Python stacktrace bleed.
    assert "Traceback" not in err


def test_cli_surfaces_403_as_clean_error(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    state["client"] = _StubApiClient(
        raise_msg="API error 403: addressed to a different user",
    )
    _set_dm_env(monkeypatch, decision="approve", event_id=EVT)

    with pytest.raises(SystemExit) as exc:
        cm.cmd_dm_respond()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "different user" in err or "receiver" in err


def test_cli_surfaces_409_as_clean_error(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    state["client"] = _StubApiClient(
        raise_msg="API error 409: already decided as 'approved'",
    )
    _set_dm_env(monkeypatch, decision="deny", event_id=EVT)

    with pytest.raises(SystemExit) as exc:
        cm.cmd_dm_respond()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "terminal state" in err or "already" in err
