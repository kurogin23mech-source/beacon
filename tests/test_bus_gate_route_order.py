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

The test resolves the concrete path the way Starlette does at runtime (first
full match wins), recursing into included/mounted sub-routers so it is robust to
the FastAPI-version difference in whether include_router flattens routes into
app.routes or nests them (see ms-127 e-4871 PR1 CI note).
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "server"))
sys.path.insert(0, os.path.join(REPO, "lib"))

import app as app_module  # noqa: E402


def _resolve(method: str, path: str):
    """Return the endpoint name Starlette would dispatch `method path` to.

    Mirrors Starlette's first-full-match-wins semantics, recursing into
    included routers / mounts so it does not depend on whether the running
    FastAPI version flattens include_router routes.
    """
    scope = {"type": "http", "method": method, "path": path}

    def walk(routes):
        from starlette.routing import Match
        for route in routes:
            match, child = route.matches(scope)
            if match == Match.FULL:
                sub = getattr(route, "routes", None) or getattr(
                    getattr(route, "app", None), "routes", None)
                if sub:
                    inner = walk(sub)
                    if inner is not None:
                        return inner
                    continue
                return getattr(route, "endpoint", None) or getattr(
                    route, "name", None)
        return None

    return walk(app_module.app.router.routes)


def test_bus_audit_not_shadowed_by_event_id_wildcard():
    ep = _resolve("GET", "/api/projects/p1/bus/audit")
    name = getattr(ep, "__name__", ep)
    assert name == "list_bus_audit", (
        "GET /bus/audit resolved to %r, not list_bus_audit — the gate router "
        "is likely mounted AFTER the delivery /bus/{event_id} route and got "
        "shadowed (event_id='audit'). Keep the make_bus_gate_router mount "
        "before the /bus/{event_id} definition in app.py." % (name,)
    )


def test_bus_event_id_still_resolves_for_real_event_ids():
    # A non-'audit' segment must still reach the delivery handler — the ordering
    # fix must not have broken /bus/{event_id} for ordinary event ids.
    ep = _resolve("GET", "/api/projects/p1/bus/evt-12345")
    name = getattr(ep, "__name__", ep)
    assert name == "get_bus_event", (
        "GET /bus/evt-12345 resolved to %r, not get_bus_event" % (name,)
    )
