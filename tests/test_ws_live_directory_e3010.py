"""ms-101 / e-3010: directory の live 判定を接続台帳ベースへ切替えるテスト。

session 行に stamp される新フィールドを検証する:
  - ws_live : 接続台帳 (redis_client.ws_session_live) の raw signal (True/False/None)
  - live    : (ws_live is True) or (poll_health.healthy is True) の union

狙いは「WS では接続中なのに last_poll_at (= 最後の問い合わせ時刻) が遅れて
poll 的には not-healthy に見える session」を live として拾えること (= ms-96
= バックエンドの VPS 移行 直後に観測した「live 0 なのに DM は届く」ズレの解消)。
移行期の保険として、接続台帳に載らない旧 bridge でも polling が健全なら live に
残すこと、Redis 不通なら従来の poll 判定に一致することも locked-in する。

redis_client.ws_session_live は実 Redis を使わず session_id → 値のマップに
差し替える。auth / firestore は test_me_sessions.py と同型で mock する。
"""
from __future__ import annotations

import copy
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client

_sessions_store: dict[str, list[dict]] = {}
_projects_store: dict[str, dict] = {}


def _mock_list_sessions(project_id: str) -> list[dict]:
    return copy.deepcopy(list(_sessions_store.get(project_id, [])))


def _mock_list_projects(user_id=None, include_archived=False):
    out = []
    for pid, data in _projects_store.items():
        if not include_archived and data.get("archived"):
            continue
        if user_id:
            owner = data.get("owner")
            members = [m.get("user_id") for m in data.get("members", [])]
            if owner and owner != user_id and user_id not in members:
                continue
        out.append({"project_id": pid, "name": data.get("name", "")})
    return out


# NOTE: firestore_client / store_router の関数差し替えは module import 時ではなく
# reset_and_patch フィクスチャ内で monkeypatch する (= テストごとに自動復元し、
# 先に import した / 後から import する他テストモジュールを汚染しない)。store_router
# が import 時に firestore_client の list_* を束ねる件への対処もそこで行う。

from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

app_module._auth_enabled = False
app_module._start_watcher = lambda project_id: None
app_module._stop_watcher = lambda project_id: None

client = TestClient(app_module.app)

DEV_UID = "dev"

# session_id → ws_live 値 (True/False/None)。テストが書き換える。
_ws_live_map: dict[str, object] = {}


def _mock_get_project(pid):
    return _projects_store.get(pid) or {
        "name": "test", "milestones": [], "owner": "dev", "members": []
    }


@pytest.fixture(autouse=True)
def reset_and_patch(monkeypatch):
    import store_router

    _sessions_store.clear()
    _projects_store.clear()
    _ws_live_map.clear()
    # store_router (= app が使う ``db``) は import 時に firestore_client の
    # list_sessions/list_projects/get_project を自分の名前空間へ束ねる。よって
    # firestore_client 側だけ差し替えても db.* には反映されず、先に import した
    # 別テストモジュールの mock を掴んだままになる。ここでは db (store_router) を
    # 直接 monkeypatch して確実に自分の in-memory store へ向ける (monkeypatch は
    # テストごとに自動復元されるので他モジュールを汚染しない)。
    for mod in (firestore_client, store_router):
        monkeypatch.setattr(mod, "list_sessions", _mock_list_sessions, raising=False)
        monkeypatch.setattr(mod, "list_projects", _mock_list_projects, raising=False)
        monkeypatch.setattr(mod, "get_project", _mock_get_project, raising=False)
    # 接続台帳の signal をマップ差し替え。未登録 session_id は None (= Redis に
    # 情報が無い / 不通 とみなす)。
    monkeypatch.setattr(
        app_module.redis_client,
        "ws_session_live",
        lambda project_id, sid: _ws_live_map.get(sid, None),
    )
    yield
    _sessions_store.clear()
    _projects_store.clear()
    _ws_live_map.clear()


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _seed_project(pid: str, name: str = "P", owner: str = DEV_UID) -> None:
    _projects_store[pid] = {"name": name, "owner": owner, "members": [], "archived": False}


def _seed_session(pid: str, sid: str, *, poll_fresh: bool) -> None:
    """poll_fresh=True なら last_poll_at を今 (= poll_health healthy)、False なら
    十分古い時刻 (= not-healthy) にする。"""
    now = _now()
    last_poll = _iso(now) if poll_fresh else _iso(now - datetime.timedelta(minutes=10))
    _sessions_store.setdefault(pid, []).append({
        "session_id": sid,
        "actor": {"email": "", "machine": "", "agent": ""},
        "last_active": _iso(now),
        "poll_interval_ms": 2000,
        "shutdown": False,
        "last_poll_at": last_poll,
    })


def _row(sid: str) -> dict:
    rows = client.get("/api/me/sessions").json()
    return next(r for r in rows if r["session_id"] == sid)


# --- ws_live が接続を捉える (poll が遅れていても live) ----------------------

def test_ws_connected_but_poll_stale_is_live():
    _seed_project("p1")
    _seed_session("p1", "s-ws", poll_fresh=False)  # poll 的には not-healthy
    _ws_live_map["s-ws"] = True                     # だが WS 接続中
    row = _row("s-ws")
    assert row["ws_live"] is True
    assert row["poll_health"]["healthy"] is False
    assert row["live"] is True  # 接続ベースで live と判定 (ズレ解消)


# --- 旧 bridge (接続台帳に載らない) でも poll 健全なら live のまま ----------

def test_poll_healthy_but_ws_absent_stays_live():
    _seed_project("p1")
    _seed_session("p1", "s-old", poll_fresh=True)
    _ws_live_map["s-old"] = False  # 台帳に無い (= session_id を送らない旧版)
    row = _row("s-old")
    assert row["ws_live"] is False
    assert row["live"] is True  # polling が健全なので live (移行期の保険)


# --- 両方 dead なら not-live ------------------------------------------------

def test_both_signals_dead_is_not_live():
    _seed_project("p1")
    _seed_session("p1", "s-dead", poll_fresh=False)
    _ws_live_map["s-dead"] = False
    row = _row("s-dead")
    assert row["live"] is False


# --- Redis 不通 (ws_live=None) は従来の poll 判定に一致 ---------------------

def test_redis_down_falls_back_to_poll():
    _seed_project("p1")
    _seed_session("p1", "s-fresh", poll_fresh=True)
    _seed_session("p1", "s-stale", poll_fresh=False)
    # _ws_live_map に入れない → ws_session_live は None (= Redis 不通相当)
    assert _row("s-fresh")["ws_live"] is None
    assert _row("s-fresh")["live"] is True   # poll healthy
    assert _row("s-stale")["ws_live"] is None
    assert _row("s-stale")["live"] is False  # poll stale


# --- healthy_only フィルタが union を使う (接続中を取りこぼさない) ----------

def test_healthy_only_includes_ws_connected_poll_stale():
    _seed_project("p1")
    _seed_session("p1", "s-ws", poll_fresh=False)
    _ws_live_map["s-ws"] = True
    rows = client.get("/api/me/sessions?healthy_only=true").json()
    sids = [r["session_id"] for r in rows]
    assert "s-ws" in sids  # 従来 (poll_health.healthy only) なら落ちていた


def test_healthy_only_excludes_fully_dead():
    _seed_project("p1")
    _seed_session("p1", "s-dead", poll_fresh=False)
    _ws_live_map["s-dead"] = False
    rows = client.get("/api/me/sessions?healthy_only=true").json()
    assert "s-dead" not in [r["session_id"] for r in rows]


# --- per-project endpoint も同じ union 判定 ---------------------------------

def test_project_endpoint_healthy_only_union():
    _seed_project("p1")
    _seed_session("p1", "s-ws", poll_fresh=False)
    _ws_live_map["s-ws"] = True
    rows = client.get("/api/projects/p1/sessions?healthy_only=true").json()
    assert "s-ws" in [r["session_id"] for r in rows]
    assert next(r for r in rows if r["session_id"] == "s-ws")["ws_live"] is True


# --- e-3214: shutdown session must not linger in --live -----------------------
# A gracefully-stopped daemon stamps shutdown=true WITH a fresh last_poll_at/
# last_active (so healthy_only kills it immediately, e-2305). --live must drop
# it too — a recency check alone would keep advertising a just-stopped daemon
# as live for ~since_minutes (Codex e-3209 clean-env dogfood observation).

def _seed_shutdown_session(pid: str, sid: str) -> None:
    now = _now()
    _sessions_store.setdefault(pid, []).append({
        "session_id": sid,
        "actor": {"email": "", "machine": "", "agent": ""},
        "last_active": _iso(now),          # fresh
        "poll_interval_ms": 2000,
        "shutdown": True,                  # but gracefully shut down
        "last_poll_at": _iso(now),         # fresh shutdown stamp
    })


def test_live_only_drops_shutdown_session_even_when_fresh():
    _seed_project("p1")
    _seed_shutdown_session("p1", "s-stopped")
    _seed_session("p1", "s-alive", poll_fresh=True)  # control
    rows = client.get("/api/projects/p1/sessions?live_only=true").json()
    sids = [r["session_id"] for r in rows]
    assert "s-stopped" not in sids, (
        "a shut-down daemon must not appear in --live (e-3214)"
    )
    assert "s-alive" in sids  # a genuinely live session is unaffected


def test_healthy_only_and_live_only_agree_for_shutdown():
    """--live and --healthy must both exclude a stopped daemon (consistency)."""
    _seed_project("p1")
    _seed_shutdown_session("p1", "s-stopped")
    live = [r["session_id"] for r in client.get(
        "/api/projects/p1/sessions?live_only=true").json()]
    healthy = [r["session_id"] for r in client.get(
        "/api/projects/p1/sessions?healthy_only=true").json()]
    assert "s-stopped" not in live
    assert "s-stopped" not in healthy


# --- e-3220: the DEFAULT `bus directory --live` path is /api/me/sessions ------
# e-3214 fixed only /api/projects/{pid}/sessions. But `bus directory --live`
# defaults to the cross-project /api/me/sessions endpoint, whose live filter
# ALSO checked recency only — so the shutdown row still leaked. Both now share
# _session_is_live; pin the /me/sessions path too so it can't regress.

def test_me_sessions_live_only_drops_shutdown_session():
    _seed_project("p1")
    _seed_shutdown_session("p1", "s-stopped")
    _seed_session("p1", "s-alive", poll_fresh=True)
    rows = client.get("/api/me/sessions?live_only=true").json()
    sids = [r["session_id"] for r in rows]
    assert "s-stopped" not in sids, (
        "shut-down daemon must not appear in /api/me/sessions --live — this is "
        "the DEFAULT bus directory path (e-3220)"
    )
    assert "s-alive" in sids


def test_both_directory_endpoints_agree_on_shutdown():
    """Per-project and cross-project --live must drop the same stopped daemon."""
    _seed_project("p1")
    _seed_shutdown_session("p1", "s-stopped")
    me = [r["session_id"] for r in client.get("/api/me/sessions?live_only=true").json()]
    proj = [r["session_id"] for r in client.get("/api/projects/p1/sessions?live_only=true").json()]
    assert "s-stopped" not in me
    assert "s-stopped" not in proj
