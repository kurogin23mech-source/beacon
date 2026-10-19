"""ms-184 e-7017 — GET /api/me/recap, the cross-project period query.

SPEC 方針 3 asks for the sweep on the server: the truth for every project the
caller owns already lives in that one store, so nothing needs to publish a
summary that could drift from the records. These tests drive the REAL app
in-process (full FastAPI routing, no network) and pin the three properties the
endpoint is worth having for:

1. it runs the SAME derivation the single-project path runs (no second
   definition of "progressed"),
2. it omits projects that did not move (方針 2),
3. it REPORTS projects it could not read instead of dropping them, so
   "nothing happened" stays distinguishable from "we could not tell"
   (受入条件 8) — the property a one-call sweep is most likely to lose, since
   one bad project could silently truncate the whole answer.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import pytest                                   # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

import app as app_module                        # noqa: E402
import routers_me                               # noqa: E402

client = TestClient(app_module.app)

PATH = "/api/me/recap"


def _project(pid: str, *, commit_at: str) -> dict:
    return {
        "project_id": pid, "name": pid.upper(),
        "milestones": [{
            "id": "ms-1", "title": "work", "status": "in_progress",
            "created_at": "2026-01-01T00:00:00Z",
            "entries": [{"id": "e-1", "type": "commit", "description": "did a thing",
                         "created_at": commit_at}],
        }],
    }


class _FakeDB:
    """Minimal store: two readable projects (one moved) + one that raises."""

    def __init__(self):
        self.data = {
            "p-moved": _project("p-moved", commit_at="2026-10-14T09:00:00Z"),
            "p-still": _project("p-still", commit_at="2026-02-01T09:00:00Z"),
        }

    def list_projects(self, user_id=None, include_archived=False):
        return [{"project_id": p, "name": p.upper()}
                for p in ("p-moved", "p-still", "p-broken")]

    def list_documents(self, pid):
        return []

    def list_decision_events(self, pid, limit=100):
        return []


def _loader(db: _FakeDB):
    def _load(pid, user):
        if pid not in db.data:
            raise LookupError(f"Project '{pid}' not found")
        return db.data[pid]
    return _load


def _standalone(fake: _FakeDB, *, with_loader: bool = True) -> TestClient:
    """A throwaway app carrying ONLY this router, with auth bypassed.

    The route body is driven here rather than by swapping the production app's
    endpoint: FastAPI compiles each route's handler when the app is built, so
    reassigning ``route.endpoint`` afterwards silently does nothing and the
    test would have exercised the production loader while looking like it
    tested the stub (observed while writing this file — the first version
    passed through to the real ``_load`` and reported every project as
    unreadable). "Is it mounted in the real app" is pinned separately by
    :func:`test_route_is_mounted`, which reads the real app's OpenAPI paths.
    """
    from fastapi import FastAPI

    kwargs = {}
    if with_loader:
        kwargs["load_project"] = _loader(fake)
    router = routers_me.make_router(
        lambda: {"sub": "u1", "email": "me@example.com"},
        stamp_session_liveness=lambda *a, **k: None,
        session_is_live=lambda *a, **k: True,
        is_member=lambda *a, **k: True,
        **kwargs,
    )
    standalone = FastAPI()
    standalone.include_router(router)
    return TestClient(standalone, raise_server_exceptions=False)


@pytest.fixture
def wired(monkeypatch):
    """Stub store + stub loader, mounted on a standalone app."""
    fake = _FakeDB()
    monkeypatch.setattr(routers_me, "db", fake)
    yield _standalone(fake)


def test_route_is_mounted():
    assert PATH in app_module.app.openapi()["paths"], \
        "GET /api/me/recap is not served by the mounted router"


def test_requires_auth(monkeypatch):
    """No token → 401 at the injected ``require_auth``, before any store code.

    ``_auth_enabled`` is forced True locally: another module in the same
    process sets the shared ``app._auth_enabled = False`` at import, and
    without this the request falls through to the store layer (in CI, with no
    Firestore credentials, that crashes instead of returning 401). monkeypatch
    restores it, so later tests are unaffected. Same reasoning as the sibling
    /api/me auth test.
    """
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    app_module.app.dependency_overrides.pop(app_module.require_auth, None)
    r = client.get(PATH)
    assert r.status_code == 401, (r.status_code, r.text)


def test_returns_only_the_projects_that_moved(wired):
    r = wired.get(PATH, params={"from": "2026-10-14", "to": "2026-10-14"})
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert [p["project_id"] for p in body["projects"]] == ["p-moved"], \
        "a project with no in-window evidence must not be returned"
    assert body["project_count"] == 1
    assert body["source"] == "server"
    assert body["projects_scanned"] == 3


def test_reports_the_project_it_could_not_read(wired):
    r = wired.get(PATH, params={"from": "2026-10-14", "to": "2026-10-14"})
    body = r.json()
    assert [u["project_id"] for u in body["unavailable"]] == ["p-broken"], (
        "an unreadable project must be REPORTED, not dropped — otherwise the "
        "sweep understates silently")
    assert "LookupError" in body["unavailable"][0]["error"]


def test_an_unrelated_period_returns_zero_not_everything(wired):
    r = wired.get(PATH, params={"from": "2020-01-01", "to": "2020-01-02"})
    body = r.json()
    assert body["projects"] == []
    assert body["project_count"] == 0
    # ... and the unreadable project is STILL reported, so a zero answer never
    # hides a failed read.
    assert len(body["unavailable"]) == 1


def test_agrees_with_the_single_project_derivation(wired):
    """The endpoint must not carry its own idea of "progressed"."""
    import progress_window as pw

    r = wired.get(PATH, params={"from": "2026-10-14", "to": "2026-10-14"})
    served = r.json()["projects"][0]
    direct = pw.derive(_project("p-moved", commit_at="2026-10-14T09:00:00Z"),
                       from_date="2026-10-14", to_date="2026-10-14",
                       documents=[], decisions=[], evidence_limit=0)
    assert served["target_count"] == direct["target_count"]
    assert served["evidence_count"] == direct["evidence_count"]
    assert served["last_evidence_at"] == direct["last_evidence_at"]


def test_says_so_when_the_loader_is_not_injected(monkeypatch):
    """A server that mounts the router without a loader must NOT answer with an
    empty sweep — that reads exactly like "nothing moved"."""
    fake = _FakeDB()
    monkeypatch.setattr(routers_me, "db", fake)
    r = _standalone(fake, with_loader=False).get(PATH)
    assert r.status_code == 501, (r.status_code, r.text)
