"""Forcing-function: high-frequency / auth-only endpoints must NOT full-reload.

ms-98 (e-3838) — 2026-07-21 の本番 API メモリ枯渇インシデントの再発防止。

真因: 認可チェックのためだけに巨大プロジェクト全体を毎回再構成する高頻度
エンドポイント (心拍 PUT /sessions/{sid}、 bus polling 等) が
``operations.load_project_consistent`` (= 全 milestones/entries を行から
再構成) を呼び、 大 dict を秒間何度も生成・破棄してメモリ churn を起こし、
スワップスラッシングでプロセスがハングした。 軽量版
``operations.load_project_meta_only`` (= meta doc 1 読み、 milestones=[]) が
既に存在したのに移行が未完だった。

このテストは、 移行した高頻度/認可のみ経路が認可時に
``load_project_consistent`` (= フル再構成) を **呼ばない** ことを固定する。
新しい高頻度経路がうっかり heavy ``_load`` に戻ると、 このテストが落ちる
(= forcing function)。 移行の学び (report doc xuY8PsI630Shs1SsXJLN): 「移行系
の最適化は『全高頻度経路に適用したか』を forcing function で担保すべき」。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# mock backend so no real Firestore/MySQL client is required at import time.
os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import operations  # noqa: E402
import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# Auth disabled: _require_project_role skips the role gate but STILL calls the
# loader (consistent vs meta_only) selected by the endpoint — which is exactly
# what we are asserting on. The dev-mode bypass does not change WHICH loader
# runs, only whether the returned role is enforced.
app_module._auth_enabled = False
app_module._start_watcher = lambda project_id: None
app_module._stop_watcher = lambda project_id: None

client = TestClient(app_module.app)

_PID = "proj-forcing"


class _LoaderSpy:
    """Counts calls to the two project loaders so a test can assert which one
    an endpoint used for its authorization check."""

    def __init__(self) -> None:
        self.consistent = 0
        self.meta_only = 0

    def _fake_consistent(self, project_id: str) -> dict:
        self.consistent += 1
        # Shape mirrors a hydrated project. If an endpoint under test ever
        # started reading data["milestones"], returning a non-empty list here
        # would let that dependency surface instead of being masked.
        return {"name": "forcing", "owner": "dev", "members": [],
                "milestones": [{"id": "ms-1", "entries": []}]}

    def _fake_meta_only(self, project_id: str) -> dict:
        self.meta_only += 1
        # meta_only contract: same shape but milestones guaranteed empty.
        return {"name": "forcing", "owner": "dev", "members": [],
                "milestones": []}


@pytest.fixture()
def spy(monkeypatch):
    s = _LoaderSpy()
    monkeypatch.setattr(operations, "load_project_consistent", s._fake_consistent)
    monkeypatch.setattr(operations, "load_project_meta_only", s._fake_meta_only)
    # app.py calls via the ``operations`` module object it imported, so the two
    # setattr above cover both _load and _load_meta_only paths. db.* writes/reads
    # are stubbed per-endpoint below so the handler body does not need a backend.
    yield s


# ---------------------------------------------------------------------------
# Migrated high-frequency / auth-only endpoints.
# Each asserts: the auth path used meta_only (>=1) and never the full reload.
# ---------------------------------------------------------------------------

def test_heartbeat_upsert_session_uses_meta_only(spy, monkeypatch):
    """PUT /sessions/{sid} is the CLI heartbeat (every few seconds)."""
    import store_router as db
    monkeypatch.setattr(db, "upsert_session", lambda *a, **k: None)
    monkeypatch.setattr(db, "stamp_session_actor_email", lambda *a, **k: None)

    resp = client.put(
        f"/api/projects/{_PID}/sessions/sess-hb",
        json={"last_active": "2026-07-21T00:00:00Z"},
    )
    assert resp.status_code == 200, resp.text
    assert spy.consistent == 0, "heartbeat must NOT full-reload the project"
    assert spy.meta_only >= 1, "heartbeat auth must use meta-only load"


def test_bus_poll_list_uses_meta_only(spy, monkeypatch):
    """GET /bus is a polling catch-up path (since= on a loop)."""
    import store_router as db
    monkeypatch.setattr(db, "list_bus_events", lambda *a, **k: [])

    resp = client.get(
        f"/api/projects/{_PID}/bus",
        params={"since": "2026-07-21T00:00:00Z", "limit": 10},
    )
    assert resp.status_code == 200, resp.text
    assert spy.consistent == 0, "bus poll must NOT full-reload the project"
    assert spy.meta_only >= 1, "bus poll auth must use meta-only load"


def test_dm_pending_uses_meta_only(spy, monkeypatch):
    """GET /dm/pending is polled by session-start / Web UI dashboards."""
    import store_router as db
    monkeypatch.setattr(db, "list_pending_approvals", lambda *a, **k: [])

    resp = client.get(
        f"/api/projects/{_PID}/dm/pending",
        params={"receiver_user_id": "u-1"},
    )
    assert resp.status_code == 200, resp.text
    assert spy.consistent == 0, "dm/pending must NOT full-reload the project"
    assert spy.meta_only >= 1, "dm/pending auth must use meta-only load"


def test_bus_unread_uses_meta_only(spy, monkeypatch):
    """GET /bus/unread — the dominant high-volume poll (bus.mjs every 2-3s)."""
    import store_router as db
    monkeypatch.setattr(db, "get_bus_cursor", lambda *a, **k: {})
    monkeypatch.setattr(db, "list_bus_events", lambda *a, **k: [])

    resp = client.get(
        f"/api/projects/{_PID}/bus/unread",
        params={"recipient_id": "sess-r", "limit": 10},
    )
    assert resp.status_code == 200, resp.text
    assert spy.consistent == 0, "bus/unread must NOT full-reload the project"
    assert spy.meta_only >= 1, "bus/unread auth must use meta-only load"


def test_bus_cursor_advance_uses_meta_only(spy, monkeypatch):
    """POST /bus/cursors/{rid} — the paired write after every unread poll."""
    import store_router as db
    monkeypatch.setattr(db, "advance_bus_cursor",
                        lambda *a, **k: {"last_seen_at": "2026-07-21T00:00:00Z"})

    resp = client.post(
        f"/api/projects/{_PID}/bus/cursors/sess-r",
        json={"last_seen_at": "2026-07-21T00:00:00Z"},
    )
    assert resp.status_code == 200, resp.text
    assert spy.consistent == 0, "cursor advance must NOT full-reload the project"
    assert spy.meta_only >= 1, "cursor advance auth must use meta-only load"
