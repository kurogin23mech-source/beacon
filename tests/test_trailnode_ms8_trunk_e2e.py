"""ms-8 e-85: end-to-end trunk push via FastAPI TestClient.

Network-free integration: an in-memory Firestore + GCS stub stands in for
Cloud Run dependencies so the trunk-push pipeline can be exercised in CI.
Branch routing is out of scope (no second user identity to test with) —
covered by `_compute_route` + helper unit tests in
`test_trailnode_ms8_routing.py`.

Verified end-to-end:
- bare-id push to `<org>/<name>` → server assigns `YYYY-MM-DD.1`
- repeat push from the same owner → `YYYY-MM-DD.2` (same-day increment)
- legacy versioned id `<org>/<name>@9.9.9` → version ignored, server still
  auto-assigns
- push URL containing `:branch` → 409 (branch-of-branch / cross-user
  attempt rejected, §設計方針3)
"""

from __future__ import annotations

import io
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import firestore_client as db
import trailnode  # noqa: E402
import trailnode_orgs as orgs  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# Minimal in-memory Firestore stub
# ──────────────────────────────────────────────────────────────────────────


class _Snapshot:
    def __init__(self, doc_id: str, data: Optional[dict]):
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> Optional[dict]:
        if self._data is None:
            return None
        # Defensive copy so callers can mutate without polluting the store.
        return dict(self._data)


class _Document:
    def __init__(self, collection: "_Collection", doc_id: str):
        self._collection = collection
        self.id = doc_id

    def get(self, transaction: Optional[Any] = None) -> _Snapshot:
        # `transaction` kwarg is accepted (real SDK signature) but ignored
        # by the in-memory stub — single-threaded tests don't need MVCC.
        return _Snapshot(self.id, self._collection._docs.get(self.id))

    def set(self, data: dict) -> None:
        materialized = _materialize_sentinels(data)
        self._collection._docs[self.id] = materialized


class _Query:
    def __init__(self, collection: "_Collection", filters: Optional[list] = None,
                 order: Optional[tuple] = None, limit: Optional[int] = None):
        self._collection = collection
        self._filters = list(filters or [])
        self._order = order
        self._limit = limit

    def where(self, field: str, op: str, value: Any) -> "_Query":
        return _Query(
            self._collection,
            self._filters + [(field, op, value)],
            self._order,
            self._limit,
        )

    def order_by(self, field: str, direction: Any = None) -> "_Query":
        # `direction` is one of fs.Query.ASCENDING / DESCENDING; we treat
        # anything truthy that isn't ASCENDING as DESCENDING for simplicity.
        from google.cloud import firestore as fs

        is_desc = direction == fs.Query.DESCENDING
        return _Query(self._collection, self._filters, (field, is_desc), self._limit)

    def limit(self, n: int) -> "_Query":
        return _Query(self._collection, self._filters, self._order, n)

    def stream(self):
        rows: list[_Snapshot] = []
        for doc_id, data in self._collection._docs.items():
            if all(_match(data, f, op, v) for f, op, v in self._filters):
                rows.append(_Snapshot(doc_id, data))
        if self._order:
            field, is_desc = self._order
            rows.sort(
                key=lambda s: (s.to_dict() or {}).get(field) or "",
                reverse=is_desc,
            )
        if self._limit is not None:
            rows = rows[: self._limit]
        return iter(rows)


def _match(data: Optional[dict], field: str, op: str, value: Any) -> bool:
    if data is None:
        return False
    actual = data.get(field)
    if op == "==":
        return actual == value
    if op == ">=":
        if actual is None or value is None:
            return False
        try:
            return actual >= value
        except TypeError:
            return False
    raise NotImplementedError(f"_match: unsupported op {op!r}")


class _Collection:
    def __init__(self):
        self._docs: dict[str, dict] = {}

    def document(self, doc_id: str) -> _Document:
        return _Document(self, doc_id)

    def where(self, field: str, op: str, value: Any) -> _Query:
        return _Query(self).where(field, op, value)


class _FakeTxn:
    """Minimal stand-in for a Firestore transaction. The real SDK provides
    atomic read/write; the stub just forwards `set` to the underlying doc.
    Used by ms-53 e-1163 concurrency hardening tests.
    """

    def set(self, ref: "_Document", data: dict) -> None:
        ref.set(data)


class _FakeDB:
    def __init__(self):
        self._collections: dict[str, _Collection] = {}

    def collection(self, name: str) -> _Collection:
        return self._collections.setdefault(name, _Collection())

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()


def _materialize_sentinels(data: dict) -> dict:
    """Convert `fs.SERVER_TIMESTAMP` placeholders to a concrete UTC datetime
    so reads after writes are stable inside the stub.
    """
    from google.cloud import firestore as fs

    now = datetime.now(timezone.utc)
    out: dict = {}
    for k, v in data.items():
        out[k] = now if v is fs.SERVER_TIMESTAMP else v
    return out


# ──────────────────────────────────────────────────────────────────────────
# Minimal in-memory GCS stub
# ──────────────────────────────────────────────────────────────────────────


class _FakeBlob:
    def __init__(self, name: str, store: dict):
        self._name = name
        self._store = store

    def upload_from_string(self, data: bytes, content_type: str = "") -> None:
        self._store[self._name] = bytes(data)

    def exists(self) -> bool:
        return self._name in self._store

    def open(self, mode: str = "rb"):
        return io.BytesIO(self._store.get(self._name, b""))


class _FakeBucket:
    def __init__(self, store: dict):
        self._store = store

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(name, self._store)


class _FakeGCSClient:
    def __init__(self):
        self._store: dict[str, bytes] = {}

    def bucket(self, _name: str) -> _FakeBucket:
        return _FakeBucket(self._store)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


OWNER_SUB = "owner-sub-aaaaa"
OWNER_EMAIL = "owner@example.test"
ORG_SLUG = "test-org"
ORG_MEMBERSHIPS = {OWNER_SUB: {ORG_SLUG}}


@pytest.fixture
def fake_app(monkeypatch):
    fake_db = _FakeDB()
    fake_gcs = _FakeGCSClient()

    # Swap Firestore + GCS for the duration of this test.
    #
    # ms-95 e-2441: patch `trailnode.db` (= the firestore_client module
    # reference that the trailnode.py production code actually uses for
    # `db.get_db()` calls). Why not patch our top-level `db` import?
    # Because adjacent test files (e.g. test_bus_transport.py) do
    # `sys.modules["firestore_client"] = app_module.db` at MODULE LOAD time.
    # When those files are collected first, `import firestore_client as db`
    # at the top of THIS file resolves to store_router (= the alias target),
    # NOT the real firestore_client module. trailnode.py was already loaded
    # by app.py BEFORE the alias swap, so `trailnode.db` still points at the
    # real firestore_client module. Patching the wrong reference leaves
    # production calls hitting the real Firestore client → CI no-creds.
    real_fc_in_trailnode = trailnode.db
    monkeypatch.setattr(real_fc_in_trailnode, "_db", fake_db)
    monkeypatch.setattr(real_fc_in_trailnode, "get_db", lambda: fake_db)
    # Also patch the test-local alias so any direct `db.X` references in
    # this file (= rare, but defense-in-depth) see the same fake.
    monkeypatch.setattr(db, "_db", fake_db)
    monkeypatch.setattr(db, "get_db", lambda: fake_db)
    monkeypatch.setattr(trailnode, "_gcs_client", fake_gcs)
    monkeypatch.setattr(trailnode, "_get_gcs_client", lambda: fake_gcs)

    # ms-53 e-1163: replace `firestore.transactional` with a no-op decorator
    # so the SDK doesn't reject our `_FakeTxn` for not being a real
    # `Transaction` instance. The fake passes itself through; the wrapped
    # function still receives a transaction-shaped object via the stub.
    from google.cloud import firestore as fs
    monkeypatch.setattr(fs, "transactional", lambda fn: fn)

    # Org membership: only OWNER_SUB belongs to ORG_SLUG. Calls to
    # require_org_member from any other identity (e.g. spoofed branch
    # tests) would raise 403 — but trunk tests never go down that path.
    def fake_require_member(org_slug: str, user_sub: str) -> None:
        if user_sub not in ORG_MEMBERSHIPS or org_slug not in ORG_MEMBERSHIPS[user_sub]:
            from fastapi import HTTPException

            raise HTTPException(403, detail=f"not a member of {org_slug}")

    monkeypatch.setattr(orgs, "require_org_member", fake_require_member)
    monkeypatch.setattr(
        orgs, "list_org_slugs_for_user", lambda sub: ORG_MEMBERSHIPS.get(sub, set())
    )

    async def fake_require_auth():
        return {"sub": OWNER_SUB, "email": OWNER_EMAIL}

    app = FastAPI()
    app.include_router(trailnode.make_router(fake_require_auth))
    return app, fake_db, fake_gcs


def _push(client: TestClient, ref: str, manifest: dict, *, files_name: str = "x"):
    """Wrap the multipart push call so test bodies stay tidy."""
    return client.put(
        f"/api/trailnode/capabilities/{ref}",
        data={"manifest": json.dumps(manifest)},
        files={"artifact": (f"{files_name}.tar.gz", b"tar-bytes-here", "application/gzip")},
    )


def _bare_manifest(org: str = ORG_SLUG, name: str = "foo") -> dict:
    return {
        "schema_version": "0.1",
        "id": f"{org}/{name}",
        "name": name,
        "type": "skill",
        "description": "test capability",
        "author": "alice",
    }


# ──────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────


def test_bare_id_first_push_assigns_trunk_version(fake_app, monkeypatch):
    """First push of a brand-new `<org>/<name>` establishes the owner and
    is routed to trunk. The server returns the assigned `YYYY-MM-DD.1`.
    """
    app, fake_db, _ = fake_app
    monkeypatch.setattr(trailnode, "_today_utc_str", lambda: "2026-06-07")
    client = TestClient(app)

    resp = _push(client, f"{ORG_SLUG}/foo", _bare_manifest())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == f"{ORG_SLUG}/foo@2026-06-07.1"
    assert body["version"] == "2026-06-07.1"
    assert body["branch"] is None
    assert body["base_trunk"] is None

    # The namespace doc records the caller as owner.
    ns = (
        fake_db.collection(trailnode._NAMESPACES_COLLECTION)
        .document(f"{ORG_SLUG}__foo")
        .get()
        .to_dict()
    )
    assert ns is not None
    assert ns["owner_sub"] == OWNER_SUB
    assert ns["owner_email"] == OWNER_EMAIL


def test_owner_second_push_increments_trunk_version(fake_app, monkeypatch):
    """A second push from the same owner on the same day increments N."""
    app, _, _ = fake_app
    monkeypatch.setattr(trailnode, "_today_utc_str", lambda: "2026-06-07")
    client = TestClient(app)

    r1 = _push(client, f"{ORG_SLUG}/foo", _bare_manifest())
    assert r1.status_code == 200
    assert r1.json()["version"] == "2026-06-07.1"

    r2 = _push(client, f"{ORG_SLUG}/foo", _bare_manifest())
    assert r2.status_code == 200
    assert r2.json()["version"] == "2026-06-07.2"
    assert r2.json()["branch"] is None


def test_owner_third_push_continues_sequence(fake_app, monkeypatch):
    """N keeps climbing — `.3` after `.1` and `.2`."""
    app, _, _ = fake_app
    monkeypatch.setattr(trailnode, "_today_utc_str", lambda: "2026-06-07")
    client = TestClient(app)

    for expected_n in (1, 2, 3):
        resp = _push(client, f"{ORG_SLUG}/foo", _bare_manifest())
        assert resp.status_code == 200
        assert resp.json()["version"] == f"2026-06-07.{expected_n}"


def test_push_with_legacy_versioned_id_still_auto_assigns(fake_app, monkeypatch):
    """Legacy `<org>/<name>@9.9.9` push: the version in URL + manifest is
    silently overridden with the server-assigned `YYYY-MM-DD.N`. Old
    clients still get a working response.
    """
    app, _, _ = fake_app
    monkeypatch.setattr(trailnode, "_today_utc_str", lambda: "2026-06-07")
    client = TestClient(app)

    legacy_manifest = {
        "schema_version": "0.1",
        "id": f"{ORG_SLUG}/foo@9.9.9",
        "name": "foo",
        "type": "skill",
        "version": "9.9.9",
        "description": "legacy",
        "author": "alice",
    }
    resp = _push(client, f"{ORG_SLUG}/foo@9.9.9", legacy_manifest)
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "2026-06-07.1"
    assert resp.json()["id"] == f"{ORG_SLUG}/foo@2026-06-07.1"


def test_push_with_branch_in_url_rejected_409(fake_app):
    """The pusher cannot pick a branch — even when no second user exists
    yet, attempting `:bob` in the URL must surface a 409 with the
    "re-push without :branch" hint.
    """
    app, _, _ = fake_app
    client = TestClient(app)

    resp = _push(client, f"{ORG_SLUG}/foo:bob", _bare_manifest())
    assert resp.status_code == 409, resp.text
    assert "branch" in resp.json()["detail"].lower()


def test_pull_pinned_version_returns_stored_manifest(fake_app, monkeypatch):
    """After a trunk push, GET with the assigned version pin reads back
    the manifest. This is the read-path users will exercise after pushing.
    """
    app, _, _ = fake_app
    monkeypatch.setattr(trailnode, "_today_utc_str", lambda: "2026-06-07")
    client = TestClient(app)

    pushed = _push(client, f"{ORG_SLUG}/foo", _bare_manifest()).json()
    cap_id = pushed["id"]  # e.g. test-org/foo@2026-06-07.1

    resp = client.get(f"/api/trailnode/capabilities/{cap_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == cap_id
    assert body["version"] == "2026-06-07.1"
    assert body["branch"] is None
    assert body["uploaded_by"] == OWNER_SUB
    assert body["org_slug"] == ORG_SLUG
    # `artifact_url` was server-assigned; its presence is the contract,
    # not the exact gs:// value.
    assert isinstance(body["artifact_url"], list) and body["artifact_url"]


def test_pull_without_version_returns_400_pointing_at_e86(fake_app, monkeypatch):
    """ms-8 e-86 (latest resolver) is not in this MS — the bare GET path
    must surface a clear 400 with a forward reference, not a 404.
    """
    app, _, _ = fake_app
    monkeypatch.setattr(trailnode, "_today_utc_str", lambda: "2026-06-07")
    client = TestClient(app)
    _push(client, f"{ORG_SLUG}/foo", _bare_manifest())

    resp = client.get(f"/api/trailnode/capabilities/{ORG_SLUG}/foo")
    assert resp.status_code == 400
    assert "version is required" in resp.json()["detail"]
    assert "e-86" in resp.json()["detail"]


# ──────────────────────────────────────────────────────────────────────────
# ms-53 e-1163: concurrency hardening — version race produces 409
# ──────────────────────────────────────────────────────────────────────────


def test_push_returns_409_when_version_slot_already_taken(fake_app, monkeypatch):
    """If a concurrent push wrote the target doc_slug between version compute
    and our doc write, the transactional check refuses to overwrite and
    surfaces 409 so the client can retry. This guards against the
    `_query_existing_versions → _compute_next_version_from_existing → set`
    race window where two pushes silently overwrote each other.
    """
    app, fake_db, _ = fake_app
    monkeypatch.setattr(trailnode, "_today_utc_str", lambda: "2026-06-07")
    client = TestClient(app)

    # Simulate the concurrent push having already written the doc_slug
    # that our upcoming push will compute. `_slug_for(trunk)` is
    # `<org>__<name>__<version>`.
    existing_slug = f"{ORG_SLUG}__foo__2026-06-07.1"
    fake_db.collection(trailnode._CAPABILITIES_COLLECTION).document(
        existing_slug
    ).set(
        {
            "id": f"{ORG_SLUG}/foo@2026-06-07.1",
            "name": "foo",
            "org_slug": ORG_SLUG,
            "version": "2026-06-07.1",
            "branch": None,
            "type": "skill",
            "deleted_at": None,
        }
    )
    # But the namespace doc is NOT created yet (the owner hasn't been
    # established). That way our push thinks it's the first to push, computes
    # 2026-06-07.1, hits the same doc_slug, and the transaction guard fires.
    # (In real concurrency the namespace would be set by whichever push commits
    # first; here we just simulate the doc-slug clash that the guard catches.)

    # The pre-populated doc DOES exist for the _query_existing_versions
    # query — which means the real flow would compute 2026-06-07.2 and not
    # collide. To exercise the guard we need a slot collision where the
    # post-upload write hits an already-occupied doc. Force _today_utc_str
    # and the existing version list so this happens.
    monkeypatch.setattr(
        trailnode, "_query_existing_versions",
        lambda org, name, branch: [],  # pretend no existing → compute .1
    )

    resp = _push(client, f"{ORG_SLUG}/foo", _bare_manifest())
    assert resp.status_code == 409, resp.text
    assert "concurrent push" in resp.json()["detail"].lower() or "retry" in resp.json()["detail"].lower()
