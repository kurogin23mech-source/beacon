"""Regression: WS connect must not attach a Firestore on_snapshot listener
(ms-84 / e-2325).

User-observed symptom (2026-06-23 smoke test after e-2303 / e-2322 deploy):
the Web UI WS Messages tab shows a new ``project_changed`` row every 2-3
seconds with no user write in flight. Direct read of the WS path showed two
broadcast emitters for every project doc write:

  1. ``_broadcast_project_after_write`` — explicit, fires from the writer
     thread immediately after the HTTP write returns.
  2. ``_on_snapshot`` (attached by ``_start_watcher`` at WS connect) —
     Firestore listener, fires when the SDK observes the same write.

After e-2326 made WS payloads signal-only (``{"type":"project_changed"}``),
both emitters produce byte-identical messages. The original justification
for keeping the listener (``client-side JSON dedup absorbs the duplicate``,
``_broadcast_project_after_write`` docstring pre-e-2325) became structurally
false — identical payloads have no dedup signal, so every write reaches
clients as 2 messages.

Cloud Run is pinned single-instance
(``--min-instances=1 --max-instances=1`` in
``.github/workflows/deploy-cloud-run.yml``), so the cross-instance fanout
fallback that the listener was supposed to provide is structurally unneeded
— every write hits the same instance that owns the WS connections.

This test locks in the e-2325 decision: ``_start_watcher`` is a no-op stub.
If somebody re-implements the body (re-attaches the listener), this test
fails and points back to this file for the rationale.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Mock backend so apply_operation runs against an in-memory dict, not real
# Firestore. Matches test_ws_broadcast_after_write.py's strategy.
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


_store: dict[str, dict] = {}


def _mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def _mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


@pytest.fixture(autouse=True)
def _patch_firestore_mocks():
    import store_router  # noqa: F401

    prev_fc_get = firestore_client.get_project
    prev_fc_save = firestore_client.save_project
    prev_sr_get = store_router.get_project
    prev_sr_save = store_router.save_project
    firestore_client.get_project = _mock_get_project
    firestore_client.save_project = _mock_save_project
    store_router.get_project = _mock_get_project
    store_router.save_project = _mock_save_project
    _store.clear()
    yield
    firestore_client.get_project = prev_fc_get
    firestore_client.save_project = prev_fc_save
    store_router.get_project = prev_sr_get
    store_router.save_project = prev_sr_save
    _store.clear()


@pytest.fixture(autouse=True)
def _disable_auth():
    prev = app_module._auth_enabled
    app_module._auth_enabled = False
    yield
    app_module._auth_enabled = prev


client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _lifespan_context():
    with client:
        yield


def _seed_project(pid: str) -> None:
    _store[pid] = {
        "name": "test project",
        "summary": "",
        "owner": "",
        "members": [],
        "milestones": [],
        "operations": [],
        "schema_version": 1,
    }


def _ws_url(pid: str) -> str:
    return f"/ws/projects/{pid}"


def test_start_watcher_is_a_no_op_stub():
    """``_start_watcher`` returns without populating ``_watchers``.

    The pre-e-2325 body called ``doc_ref.on_snapshot(...)`` and stored the
    unsubscribe handle in ``_watchers[project_id]``. If a future change
    re-attaches the listener, ``_watchers`` will gain an entry and this
    assertion catches it before the over-broadcast pathology returns.
    """
    pid = "p-no-watcher-1"
    _watchers_before = dict(app_module._watchers)
    app_module._start_watcher(pid)
    assert app_module._watchers == _watchers_before, (
        f"_start_watcher must be a no-op (e-2325), but _watchers changed: "
        f"before={_watchers_before!r} after={app_module._watchers!r}. "
        f"Re-attaching the Firestore on_snapshot listener brings back the "
        f"double-broadcast pathology — see _start_watcher docstring."
    )


def test_ws_connect_does_not_attach_firestore_listener():
    """WS subscribe path must not register a project_id in ``_watchers``.

    End-to-end version of the assertion above: a real WS connect against the
    FastAPI app exercises the WS endpoint which calls ``_start_watcher``.
    With the e-2325 no-op stub, the dict stays empty.
    """
    pid = "p-no-listener-e2e"
    _seed_project(pid)
    assert pid not in app_module._watchers
    with client.websocket_connect(_ws_url(pid)) as ws:
        ready = ws.receive_json()
        assert ready.get("type") == "ws_ready"
        assert pid not in app_module._watchers, (
            f"WS connect must not attach a Firestore on_snapshot listener "
            f"(e-2325). The explicit broadcast from "
            f"_broadcast_project_after_write covers all real-time updates "
            f"in single-instance Cloud Run posture."
        )


def test_single_write_produces_single_broadcast():
    """One HTTP write must produce exactly one ``_broadcast`` call (e-2325).

    Counter-fact: pre-e-2325 the same write produced two — explicit +
    listener — for byte-identical signal-only payloads. We wrap ``_broadcast``
    with a counter, perform a write, then assert ``calls == 1``.

    Note: ``_broadcast_project_after_write`` schedules the async broadcast
    via ``asyncio.run_coroutine_threadsafe`` from the writer thread. The
    TestClient runs the event loop in-thread for the request, so by the
    time ``.post(...)`` returns the broadcast coroutine has been scheduled.
    The WS receive on a subscribed client drains pending frames; we count
    via a wrapping coroutine instead so the test does not depend on WS
    timing.
    """
    pid = "p-single-broadcast"
    _seed_project(pid)
    calls: list[str] = []

    original_broadcast = app_module._broadcast

    async def counting_broadcast(project_id: str, data=None):
        calls.append(project_id)
        await original_broadcast(project_id, data)

    app_module._broadcast = counting_broadcast
    try:
        with client.websocket_connect(_ws_url(pid)) as ws:
            ws.receive_json()  # consume ws_ready
            resp = client.post(
                f"/api/projects/{pid}/milestones",
                json={"title": "Single Broadcast MS", "target_date": "", "priority": "medium"},
            )
            assert resp.status_code == 200, resp.text
            # Drain any pending WS frames so the broadcast coroutine actually
            # runs to completion before we tally.
            try:
                ws.receive_json(timeout=1)
            except Exception:
                pass
        assert calls == [pid], (
            f"expected exactly 1 _broadcast call per write (e-2325), "
            f"got {calls!r}. Two calls would mean the on_snapshot listener "
            f"is duplicating the explicit broadcast — see _start_watcher "
            f"docstring for why that's the bug."
        )
    finally:
        app_module._broadcast = original_broadcast
