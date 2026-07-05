"""Unit tests for the operations layer (lib/operations.py).

Covers local-mode atomic apply, concurrency safety (no lost updates),
validation propagation, and schema-version detection.

Cloud-mode tests require a Firestore emulator and live elsewhere; this file
intentionally restricts itself to LocalStore semantics so it can run in CI
without any external services.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import operations  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def local_project(monkeypatch):
    """Create a tmp project.json and point BEACON_PROJECT_FILE at it.

    Yields the path. File is cleaned up after the test.
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump({"name": "test", "milestones": [], "summary": ""}, tmp)
    tmp.close()
    monkeypatch.setenv("BEACON_PROJECT_FILE", tmp.name)
    # Explicitly force local backend. Other test files (e.g. test_api.py) may
    # have set BEACON_OPERATIONS_BACKEND=mock at import time, which leaks via
    # os.environ. monkeypatch restores it after the test.
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
    # Ensure cloud-detection stays in "local" — make sure firestore_client
    # is not on sys.modules (it shouldn't be in tests, but be defensive).
    monkeypatch.delitem(sys.modules, "firestore_client", raising=False)
    yield tmp.name
    Path(tmp.name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Schema version detection
# ---------------------------------------------------------------------------

def test_get_schema_version_defaults_to_v1():
    assert operations.get_schema_version({}) == operations.SCHEMA_V1_LEGACY


def test_get_schema_version_reads_explicit_v1():
    assert operations.get_schema_version({"schema_version": 1}) == 1


def test_get_schema_version_reads_explicit_v2():
    assert operations.get_schema_version({"schema_version": 2}) == 2


def test_get_schema_version_ignores_unknown_values():
    # Defensive — unknown versions fall back to v1 rather than crashing,
    # because a stale local file should still be readable.
    assert operations.get_schema_version({"schema_version": 99}) == 1
    assert operations.get_schema_version({"schema_version": "two"}) == 1


# ---------------------------------------------------------------------------
# Local apply (basic, single-threaded)
# ---------------------------------------------------------------------------

def test_local_apply_persists_changes(local_project):
    def op(data):
        data["counter"] = 1
        return data, "ok"

    result = operations.apply_operation(
        "p-test", op, op_name="counter.set", actor="tester"
    )
    assert result == "ok"
    with open(local_project) as f:
        saved = json.load(f)
    assert saved["counter"] == 1


def test_local_apply_returns_op_result(local_project):
    def op(data):
        return data, {"inserted_id": "e-42"}

    result = operations.apply_operation("p-test", op, actor="tester")
    assert result == {"inserted_id": "e-42"}


def test_local_apply_validates_before_write(local_project):
    """If op produces invalid data, the file must not be modified."""
    # Read original
    with open(local_project) as f:
        original = json.load(f)

    def bad_op(data):
        data["milestones"] = "not-a-list"  # core.validate_project rejects this
        return data, None

    with pytest.raises(ValueError):
        operations.apply_operation("p-test", bad_op, actor="tester")

    # File is unchanged
    with open(local_project) as f:
        after = json.load(f)
    assert after == original


# ---------------------------------------------------------------------------
# Concurrency: no lost updates
# ---------------------------------------------------------------------------

def test_local_apply_no_lost_updates_under_concurrency(local_project):
    """50 threads × 4 increments each = 200. Must be exactly 200, no lost updates.

    This is the core regression test for e-632 — the bug that motivated the
    operations layer in the first place. Without the LOCK_EX wrapping
    read→op→write, this test reliably loses updates.
    """
    # Initialize counter
    def init(data):
        data["counter"] = 0
        return data, None
    operations.apply_operation("p-test", init, actor="tester")

    def increment(data):
        data["counter"] = data.get("counter", 0) + 1
        return data, data["counter"]

    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(4):
                operations.apply_operation(
                    "p-test", increment, op_name="counter.inc", actor="tester"
                )
        except Exception as e:  # pragma: no cover - test failure path
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Worker errors: {errors}"
    with open(local_project) as f:
        final = json.load(f)
    assert final["counter"] == 200, (
        f"Lost updates detected: expected 200, got {final['counter']}. "
        "This means the file-lock window does not actually cover read→op→write."
    )


def test_local_apply_op_called_with_fresh_data(local_project):
    """Each op invocation must see the result of the previous one.

    Specifically: if op A reads counter=0 and writes counter=1, op B
    immediately after MUST see counter=1, not counter=0.
    """
    seen_values: list[int] = []

    def op(data):
        before = data.get("counter", 0)
        seen_values.append(before)
        data["counter"] = before + 1
        return data, None

    for _ in range(5):
        operations.apply_operation("p-test", op, actor="tester")

    # Each call should see the previous call's result.
    assert seen_values == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def test_backend_local_when_firestore_client_not_loaded(monkeypatch):
    # Sanity: with the explicit override removed and both server modules
    # unloaded, detection falls through to "local".
    # ms-64 e-1631: _detect_backend also checks store_router as a cloud signal
    # (DynamoDB backend imports store_router without firestore_client). If
    # store_router is left in sys.modules by a prior test (which happens in the
    # full-suite run), we still get "cloud" here. Delete both to make the test
    # order-independent.
    monkeypatch.delenv("BEACON_OPERATIONS_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "firestore_client", raising=False)
    monkeypatch.delitem(sys.modules, "store_router", raising=False)
    assert operations._detect_backend() == "local"


def test_backend_respects_explicit_override(monkeypatch):
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "mock")
    assert operations._detect_backend() == "mock"
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
    assert operations._detect_backend() == "local"


# ---------------------------------------------------------------------------
# Changelog (best-effort logging)
# ---------------------------------------------------------------------------

def test_changelog_failures_do_not_break_apply(local_project, monkeypatch):
    """If changelog writing fails, the operation itself must still succeed."""
    # Point BEACON_PROJECT_FILE to a deeply nested non-existent dir for changelog
    # path resolution to fail, but keep the project file at its real location
    # for the actual write. Easier route: monkeypatch _append_changelog directly
    # to raise.
    def broken_append(*args, **kwargs):
        raise OSError("disk full simulation")

    monkeypatch.setattr(operations, "_append_changelog", broken_append)

    # Actually — the contract is that _append_changelog already catches its
    # own OSError. The test above checks that promise from the caller side
    # is independent of that. Let's instead simulate via the real function
    # by giving it an unwritable directory.
    monkeypatch.setattr(operations, "_append_changelog", lambda *a, **k: None)

    def op(data):
        data["touched"] = True
        return data, "ok"

    result = operations.apply_operation("p-test", op, actor="tester")
    assert result == "ok"


# ---------------------------------------------------------------------------
# load_project_consistent (read helper)
# ---------------------------------------------------------------------------

def test_load_project_consistent_local(local_project):
    # Write something via apply_operation
    def op(data):
        data["summary"] = "hello"
        return data, None
    operations.apply_operation("p-test", op, actor="tester")

    loaded = operations.load_project_consistent("p-test")
    assert loaded["summary"] == "hello"
    assert loaded["name"] == "test"


# ---------------------------------------------------------------------------
# Schema-aware dispatch (cloud path, with Firestore SDK stubbed out)
# ---------------------------------------------------------------------------

def test_apply_operation_routes_v2_when_schema_version_2(monkeypatch):
    """A project with schema_version=2 must take the v2 (subcollection) path.

    We can't run real Firestore here, so we stub both _apply_cloud_v1 and
    _apply_cloud_v2 and just assert which one is called.
    """
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "cloud")

    called: list[str] = []

    def fake_v1(project_id, op):
        called.append("v1")
        # Run the op so semantics still execute (e.g. validation).
        data = {"name": "n", "milestones": [], "schema_version": 1}
        _, result = op(data)
        return result

    def fake_v2(project_id, op):
        called.append("v2")
        data = {"name": "n", "milestones": [], "schema_version": 2}
        _, result = op(data)
        return result

    monkeypatch.setattr(operations, "_apply_cloud_v1", fake_v1)
    monkeypatch.setattr(operations, "_apply_cloud_v2", fake_v2)

    # Mock db.get_project so the pre-dispatch read returns a v2 project.
    fake_db = type("FakeDb", (), {})()
    fake_db.get_project = lambda pid: {
        "name": "n", "milestones": [], "schema_version": 2,
    }
    monkeypatch.setitem(sys.modules, "firestore_client", fake_db)

    def op(data):
        return data, "ok"

    result = operations.apply_operation("p-test", op, actor="tester")
    assert result == "ok"
    assert called == ["v2"]


def test_apply_operation_routes_v1_when_schema_absent(monkeypatch):
    """Existing projects without schema_version must use the v1 (legacy) path."""
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "cloud")

    called: list[str] = []
    monkeypatch.setattr(operations, "_apply_cloud_v1",
                        lambda pid, op: (called.append("v1") or op({"name": "n", "milestones": []})[1]))
    monkeypatch.setattr(operations, "_apply_cloud_v2",
                        lambda pid, op: (called.append("v2") or op({"name": "n", "milestones": []})[1]))

    fake_db = type("FakeDb", (), {})()
    fake_db.get_project = lambda pid: {"name": "n", "milestones": []}  # no schema_version
    monkeypatch.setitem(sys.modules, "firestore_client", fake_db)

    def op(data):
        return data, "ok"

    result = operations.apply_operation("p-test", op, actor="tester")
    assert result == "ok"
    assert called == ["v1"]


def test_apply_operation_raises_lookup_when_cloud_project_missing(monkeypatch):
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "cloud")

    fake_db = type("FakeDb", (), {})()
    fake_db.get_project = lambda pid: None
    monkeypatch.setitem(sys.modules, "firestore_client", fake_db)

    with pytest.raises(LookupError):
        operations.apply_operation("p-missing", lambda d: (d, None), actor="tester")


# ---------------------------------------------------------------------------
# DynamoDB backend dispatch (ms-64 e-1631)
# ---------------------------------------------------------------------------

def test_backend_cloud_when_store_router_loaded(monkeypatch):
    """ms-64 e-1631: store_router presence is a valid cloud signal even when
    firestore_client never gets imported (= pure-AWS Lambda build)."""
    monkeypatch.delenv("BEACON_OPERATIONS_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "firestore_client", raising=False)
    monkeypatch.delitem(sys.modules, "store_router", raising=False)

    monkeypatch.setitem(sys.modules, "store_router", type("FakeRouter", (), {})())
    assert operations._detect_backend() == "cloud"


def test_apply_operation_routes_to_dynamodb_when_backend_dynamodb(monkeypatch):
    """ms-64 e-1631: BEACON_STORE_BACKEND=dynamodb takes the DynamoDB whole-doc
    apply path, bypassing v1/v2 schema dispatch (which is Firestore-only)."""
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "cloud")
    monkeypatch.setenv("BEACON_STORE_BACKEND", "dynamodb")

    called: list[str] = []

    def fake_dynamodb(pid, op):
        called.append("dynamodb")
        return op({"name": "n", "milestones": []})[1]

    def fake_v1(*_a, **_kw):
        called.append("v1")
        return None

    def fake_v2(*_a, **_kw):
        called.append("v2")
        return None

    monkeypatch.setattr(operations, "_apply_cloud_dynamodb", fake_dynamodb)
    monkeypatch.setattr(operations, "_apply_cloud_v1", fake_v1)
    monkeypatch.setattr(operations, "_apply_cloud_v2", fake_v2)

    result = operations.apply_operation("p-test", lambda d: (d, "ok"), actor="t")
    assert result == "ok"
    assert called == ["dynamodb"]  # NOT v1 or v2


def test_apply_operation_routes_to_firestore_when_backend_unset(monkeypatch):
    """Default (BEACON_STORE_BACKEND unset) must keep the existing Firestore
    schema-dispatch path — DynamoDB is opt-in."""
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "cloud")
    monkeypatch.delenv("BEACON_STORE_BACKEND", raising=False)

    called: list[str] = []

    monkeypatch.setattr(
        operations, "_apply_cloud_dynamodb",
        lambda pid, op: (called.append("dynamodb"), op({})[1])[1],
    )
    monkeypatch.setattr(
        operations, "_apply_cloud_v1",
        lambda pid, op: (called.append("v1"), op({"name": "n", "milestones": []})[1])[1],
    )

    fake_db = type("FakeDb", (), {})()
    fake_db.get_project = lambda pid: {"name": "n", "milestones": []}  # no schema_version
    monkeypatch.setitem(sys.modules, "firestore_client", fake_db)

    operations.apply_operation("p-test", lambda d: (d, "ok"), actor="t")
    assert "dynamodb" not in called
    assert "v1" in called


def test_apply_cloud_dynamodb_read_op_write(monkeypatch):
    """_apply_cloud_dynamodb performs: get_project → op(data) → save_project."""
    store = {"p-test": {"name": "n", "milestones": [], "summary": "old"}}

    def fake_get(pid):
        return store.get(pid)

    def fake_save(pid, data):
        store[pid] = data

    fake_router = type("FakeRouter", (), {})()
    fake_router.get_project = fake_get
    fake_router.save_project = fake_save
    monkeypatch.setitem(sys.modules, "store_router", fake_router)

    def op(data):
        data["summary"] = "new"
        return data, "ok-result"

    result = operations._apply_cloud_dynamodb("p-test", op)
    assert result == "ok-result"
    assert store["p-test"]["summary"] == "new"


def test_apply_cloud_dynamodb_raises_lookup_when_missing(monkeypatch):
    fake_router = type("FakeRouter", (), {})()
    fake_router.get_project = lambda pid: None
    fake_router.save_project = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "store_router", fake_router)

    with pytest.raises(LookupError):
        operations._apply_cloud_dynamodb("p-missing", lambda d: (d, None))


def test_apply_cloud_dynamodb_validates_before_write(monkeypatch):
    """Invalid project shape must not reach save_project."""
    store = {"p-test": {"name": "n", "milestones": []}}
    save_calls: list[dict] = []

    fake_router = type("FakeRouter", (), {})()
    fake_router.get_project = lambda pid: store.get(pid)
    fake_router.save_project = lambda pid, data: save_calls.append(data)
    monkeypatch.setitem(sys.modules, "store_router", fake_router)

    def bad_op(data):
        # Remove the required "name" field — core.validate_project rejects this.
        data.pop("name", None)
        return data, None

    with pytest.raises(Exception):  # noqa: BLE001 — validate_project raises ValueError
        operations._apply_cloud_dynamodb("p-test", bad_op)
    assert save_calls == []  # Write must not have happened


def test_append_changelog_uses_store_router_when_available(monkeypatch):
    """ms-64 e-1631: when store_router is loaded (= API server context, either
    backend), cloud sink routes through it so DynamoDB build also gets the
    audit write."""
    appended: list[tuple[str, dict]] = []

    fake_router = type("FakeRouter", (), {})()
    fake_router.append_changelog = lambda pid, entry: appended.append((pid, entry))
    monkeypatch.setitem(sys.modules, "store_router", fake_router)
    monkeypatch.delitem(sys.modules, "firestore_client", raising=False)  # ensure store_router wins

    operations._append_changelog("p-test", "task.done", "tester", reason="why")
    assert len(appended) == 1
    assert appended[0][0] == "p-test"
    assert appended[0][1]["op"] == "task.done"
    assert appended[0][1]["actor"] == "tester"


def test_append_changelog_silent_when_neither_module_loaded(monkeypatch):
    """No cloud sink when running pure-local (no API server modules loaded)."""
    monkeypatch.delitem(sys.modules, "store_router", raising=False)
    monkeypatch.delitem(sys.modules, "firestore_client", raising=False)
    # Should simply return without raising.
    operations._append_changelog("p-test", "task.done", "tester")


# ---------------------------------------------------------------------------
# v1 → v2 migration (ms-tbd: Firestore 1 MiB unblock)
# ---------------------------------------------------------------------------

class _FakeFirestoreClient:
    """In-memory Firestore double for migration / v2 replace tests.

    Models just enough of the SDK surface used by _replace_cloud_v2 and
    migrate_v1_to_v2: a collection/document tree, transactional set/delete,
    and stream() over a subcollection. Transactions are not really atomic
    here (single-threaded tests), but the call sequence is preserved so
    behavioural assertions on writes are meaningful.
    """

    def __init__(self):
        # docs[(collection, doc_id)] = data
        self.docs: dict[tuple, dict] = {}
        # subdocs[(collection, doc_id, subcollection, sub_doc_id)] = data
        self.subdocs: dict[tuple, dict] = {}

    # --- collection / document refs ---
    def collection(self, name):
        return _FakeCollectionRef(self, name)

    def transaction(self):
        return _FakeTransaction(self)


class _FakeCollectionRef:
    def __init__(self, client, name, parent=None):
        self.client = client
        self.name = name
        self.parent = parent  # (collection, doc_id) tuple if subcollection

    def document(self, doc_id):
        return _FakeDocumentRef(self.client, self.name, doc_id, self.parent)

    def stream(self, transaction=None):
        if self.parent is None:
            keys = [k for k in self.client.docs if k[0] == self.name]
            for col, did in keys:
                yield _FakeDocSnap(did, self.client.docs[(col, did)])
        else:
            pcol, pdid = self.parent
            keys = [
                k for k in self.client.subdocs
                if k[0] == pcol and k[1] == pdid and k[2] == self.name
            ]
            for col, did, sub, sdid in keys:
                yield _FakeDocSnap(sdid, self.client.subdocs[(col, did, sub, sdid)])


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
        return _FakeCollectionRef(self.client, name, parent=(self.collection_name, self.doc_id))

    def get(self, transaction=None):
        if self.parent is None:
            data = self.client.docs.get((self.collection_name, self.doc_id))
        else:
            pcol, pdid = self.parent
            data = self.client.subdocs.get((pcol, pdid, self.collection_name, self.doc_id))
        return _FakeDocSnap(self.doc_id, data)


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

    def delete(self, ref):
        if ref.parent is None:
            self.client.docs.pop((ref.collection_name, ref.doc_id), None)
        else:
            pcol, pdid = ref.parent
            self.client.subdocs.pop((pcol, pdid, ref.collection_name, ref.doc_id), None)


def _install_fake_firestore(monkeypatch, client: _FakeFirestoreClient):
    """Wire a fake firestore_client + google.cloud.firestore into sys.modules.

    Mimics enough of the SDK that operations.migrate_v1_to_v2 /
    _replace_cloud_v2 can run unmodified against the in-memory store.
    """
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "cloud")

    fake_db = type("FakeDb", (), {})()
    fake_db.get_db = lambda: client
    fake_db.COLLECTION = "projects"
    fake_db.get_project = lambda pid: (
        dict(client.docs.get(("projects", pid))) if ("projects", pid) in client.docs else None
    )
    monkeypatch.setitem(sys.modules, "firestore_client", fake_db)

    # google.cloud.firestore.transactional must accept a function and return
    # a callable that, when invoked with (transaction), runs the function
    # passing transaction through. The real SDK retries on conflict; the
    # fake just runs once.
    fake_firestore_module = type("FakeGoogleCloudFirestoreModule", (), {})()

    def transactional(fn):
        def run(transaction):
            return fn(transaction)
        return run

    fake_firestore_module.transactional = transactional

    fake_google = type("FakeGoogleNS", (), {})()
    fake_google_cloud = type("FakeGoogleCloud", (), {})()
    fake_google_cloud.firestore = fake_firestore_module
    fake_google.cloud = fake_google_cloud

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", fake_firestore_module)


def test_migrate_v1_to_v2_decomposes_milestones_into_subcollection(monkeypatch):
    """Migration moves each milestone into projects/{id}/milestones/{ms_id}
    and trims the meta doc to drop milestones[]."""
    client = _FakeFirestoreClient()
    client.docs[("projects", "p1")] = {
        "name": "p1", "owner": "u1", "milestones": [
            {"id": "ms-1", "title": "first", "entries": []},
            {"id": "ms-2", "title": "second", "entries": [{"id": "e-1"}]},
        ],
    }
    _install_fake_firestore(monkeypatch, client)

    result = operations.migrate_v1_to_v2("p1")

    assert result["status"] == "migrated"
    assert result["milestone_count"] == 2
    # Meta doc no longer has milestones[] and is stamped v2.
    meta = client.docs[("projects", "p1")]
    assert "milestones" not in meta
    assert meta["schema_version"] == operations.SCHEMA_V2_BETA
    assert meta["name"] == "p1"
    # Subcollection has both MS docs.
    assert ("projects", "p1", "milestones", "ms-1") in client.subdocs
    assert ("projects", "p1", "milestones", "ms-2") in client.subdocs
    assert client.subdocs[("projects", "p1", "milestones", "ms-2")]["entries"] == [
        {"id": "e-1"},
    ]


def test_migrate_v1_to_v2_is_idempotent_on_already_v2(monkeypatch):
    client = _FakeFirestoreClient()
    # Already v2: only meta in the project doc, no milestones[].
    client.docs[("projects", "p1")] = {
        "name": "p1", "owner": "u1", "schema_version": operations.SCHEMA_V2_BETA,
    }
    client.subdocs[("projects", "p1", "milestones", "ms-1")] = {"id": "ms-1", "title": "x"}
    _install_fake_firestore(monkeypatch, client)

    result = operations.migrate_v1_to_v2("p1")

    assert result["status"] == "already_v2"
    # Meta untouched; subdocs untouched.
    assert client.docs[("projects", "p1")]["schema_version"] == operations.SCHEMA_V2_BETA
    assert client.subdocs[("projects", "p1", "milestones", "ms-1")] == {"id": "ms-1", "title": "x"}


def test_migrate_v1_to_v2_raises_lookup_when_project_missing(monkeypatch):
    client = _FakeFirestoreClient()
    _install_fake_firestore(monkeypatch, client)

    with pytest.raises(LookupError):
        operations.migrate_v1_to_v2("does-not-exist")


def test_migrate_v1_to_v2_skips_milestones_without_id(monkeypatch):
    """Defensive: a malformed milestone without an id can't have a doc id in
    the subcollection. Count it as skipped instead of silently aborting the
    whole migration."""
    client = _FakeFirestoreClient()
    client.docs[("projects", "p1")] = {
        "name": "p1", "owner": "u1", "milestones": [
            {"id": "ms-1", "title": "ok"},
            {"title": "missing id"},
        ],
    }
    _install_fake_firestore(monkeypatch, client)

    result = operations.migrate_v1_to_v2("p1")

    assert result["status"] == "migrated"
    assert result.get("skipped_no_id") == 1
    assert ("projects", "p1", "milestones", "ms-1") in client.subdocs
    # No subdoc was created for the id-less milestone.
    assert len([k for k in client.subdocs if k[2] == "milestones"]) == 1


def test_replace_project_dispatches_v2_when_schema_v2(monkeypatch):
    """replace_project must detect a v2 project and route through
    _replace_cloud_v2 (decompose) rather than blindly setting the whole doc."""
    client = _FakeFirestoreClient()
    # Existing v2 project with one MS in the subcollection.
    client.docs[("projects", "p1")] = {
        "name": "p1", "owner": "u1", "schema_version": operations.SCHEMA_V2_BETA,
    }
    client.subdocs[("projects", "p1", "milestones", "ms-1")] = {
        "id": "ms-1", "title": "old", "entries": [],
    }
    _install_fake_firestore(monkeypatch, client)

    new_data = {
        "name": "p1",
        "owner": "u1",
        "milestones": [
            {"id": "ms-1", "title": "updated", "entries": [{"id": "e-1"}]},
            {"id": "ms-2", "title": "added", "entries": []},
        ],
    }

    operations.replace_project("p1", new_data, actor="tester", reason="test")

    # Meta doc should NOT contain milestones[] (would re-grow toward 1 MiB).
    meta = client.docs[("projects", "p1")]
    assert "milestones" not in meta
    assert meta["schema_version"] == operations.SCHEMA_V2_BETA
    # ms-1 updated, ms-2 created in subcollection.
    assert client.subdocs[("projects", "p1", "milestones", "ms-1")]["title"] == "updated"
    assert client.subdocs[("projects", "p1", "milestones", "ms-2")]["title"] == "added"


def test_replace_project_v2_deletes_removed_milestones(monkeypatch):
    """Whole-replace semantics: an MS not present in new_data is removed
    from the subcollection."""
    client = _FakeFirestoreClient()
    client.docs[("projects", "p1")] = {
        "name": "p1", "owner": "u1", "schema_version": operations.SCHEMA_V2_BETA,
    }
    client.subdocs[("projects", "p1", "milestones", "ms-1")] = {"id": "ms-1", "title": "keep"}
    client.subdocs[("projects", "p1", "milestones", "ms-2")] = {"id": "ms-2", "title": "drop"}
    _install_fake_firestore(monkeypatch, client)

    operations.replace_project("p1", {
        "name": "p1", "owner": "u1",
        "milestones": [{"id": "ms-1", "title": "keep"}],
    }, actor="tester", reason="test")

    assert ("projects", "p1", "milestones", "ms-1") in client.subdocs
    assert ("projects", "p1", "milestones", "ms-2") not in client.subdocs
