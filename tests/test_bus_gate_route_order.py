"""Route-ordering guard for the bus gate extraction (ms-127 e-4871 PR3a).

`GET /api/projects/{project_id}/bus/audit` lives in
`routers_projects.make_bus_gate_router` (an included router), while the delivery
route `GET /api/projects/{project_id}/bus/{event_id}` still lives in app.py.
Starlette matches routes in **registration order**, so if the gate router is
mounted *after* the delivery route, `/bus/audit` gets shadowed by
`/bus/{event_id}` (event_id="audit") and every audit read 404s.

app.py mounts the gate router *before* the delivery route on purpose to preserve
the pre-extraction order. This test pins that invariant so a future "tidy the
include_router calls into one block at the bottom" refactor cannot silently
re-introduce the 404 — the exact regression an independent review flagged.

It is a behavioural TestClient check (not route-list introspection) so it
exercises real dispatch and is robust to the FastAPI-version difference in
whether include_router flattens routes into app.routes or nests them (see
ms-127 e-4871 PR1 CI note): `list_bus_audit` returns a sentinel, so a 200 with
that sentinel proves the request reached the audit handler and was not shadowed
by `get_bus_event`.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "server"))

import app as app_module  # noqa: E402
# Alias trick (same as test_bus_transport / test_envelope_integration): make
# `db.X = mock` land on store_router, which is what app.py reads as `db`, so the
# handlers never hit real Firestore in CI.
sys.modules["firestore_client"] = app_module.db
import firestore_client  # noqa: E402  (= store_router after the alias above)

from fastapi.testclient import TestClient  # noqa: E402

app_module._auth_enabled = False
_client = TestClient(app_module.app)

_SENTINEL = [{"__audit_sentinel__": True}]


@pytest.fixture(autouse=True)
def _stub_guards_and_db():
    """Bypass membership/project load and stub the two db reads under test.

    The injected `_require_project_role` / `_load` are late-binding thunks
    (lambda that calls app._require_project_role at request time), so patching
    the app-module names takes effect inside the included router's handlers.
    """
    saved = {
        "_require_project_role": app_module._require_project_role,
        "_load": app_module._load,
        "list_bus_audit": getattr(firestore_client, "list_bus_audit", None),
        "find_bus_event": getattr(firestore_client, "find_bus_event", None),
    }
    app_module._require_project_role = lambda *a, **k: None
    app_module._load = lambda *a, **k: {}
    firestore_client.list_bus_audit = lambda *a, **k: _SENTINEL
    firestore_client.find_bus_event = lambda *a, **k: None  # → get_bus_event 404s
    yield
    app_module._require_project_role = saved["_require_project_role"]
    app_module._load = saved["_load"]
    for name in ("list_bus_audit", "find_bus_event"):
        if saved[name] is None:
            if hasattr(firestore_client, name):
                delattr(firestore_client, name)
        else:
            setattr(firestore_client, name, saved[name])


def test_bus_audit_not_shadowed_by_event_id_wildcard():
    resp = _client.get("/api/projects/p1/bus/audit")
    assert resp.status_code == 200 and resp.json() == _SENTINEL, (
        "GET /bus/audit did not reach list_bus_audit (got %s %r) — the gate "
        "router is likely mounted AFTER the delivery /bus/{event_id} route and "
        "got shadowed (event_id='audit'). Keep the make_bus_gate_router mount "
        "before the /bus/{event_id} definition in app.py."
        % (resp.status_code, resp.text[:120])
    )


def test_bus_unread_not_shadowed_by_event_id_wildcard():
    # ms-127 e-4871 PR3b: /bus/unread and /bus/{event_id} now live in the SAME
    # router (make_bus_delivery_router). /bus/{event_id} must be registered
    # LAST or it shadows the literal /bus/unread (event_id="unread"). A
    # 200+sentinel proves the request reached list_unread_bus_events.
    # list_unread_bus_events has a REQUIRED `recipient_id` query param, so
    # omitting it yields a 422 validation error — which uniquely proves the
    # request reached list_unread_bus_events. If /bus/unread were shadowed by
    # the /bus/{event_id} wildcard, it would instead hit get_bus_event (which
    # has no required query param) and return 404, never 422.
    resp = _client.get("/api/projects/p1/bus/unread")
    assert resp.status_code == 422 and "recipient_id" in resp.text, (
        "GET /bus/unread did not reach list_unread_bus_events (got %s %r) — the "
        "/bus/{event_id} wildcard is likely registered BEFORE /bus/unread in "
        "make_bus_delivery_router. Keep get_bus_event (/bus/{event_id}) as the "
        "LAST route defined in that factory."
        % (resp.status_code, resp.text[:120])
    )


def test_bus_event_id_still_resolves_for_real_event_ids():
    # A non-'audit' segment must still reach the delivery handler (which 404s
    # here because find_bus_event is stubbed to None) — the ordering fix must
    # not have broken /bus/{event_id} for ordinary event ids.
    resp = _client.get("/api/projects/p1/bus/evt-12345")
    assert resp.status_code == 404, (
        "GET /bus/evt-12345 expected 404 from get_bus_event, got %s %r"
        % (resp.status_code, resp.text[:120])
    )
