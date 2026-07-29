"""ms-128 受入条件12 (AC12) — クロスインスタンス相互ブロックの e2e (評価軸そのもの)。

2 プロジェクト × 2 セッションにまたがる、互いにブロックする 2 ターゲットが、
tick 駆動と **注入可能な時計** で、人間の逐次介入なしに user_review 手前まで
自律的に進むことを、real endpoint (TestClient) で束ねて固定する。

シナリオ (SPEC 方針4 + 受入条件5/6/12):
  * project PA の実行セッション sv-exec-a が Target T_A を、project PB の
    sv-exec-b が Target T_B を持つ (= 2 プロジェクト × 2 セッション)。
  * リーダー sv-leader が依存台帳を所有 (方針7): T_A は T_B に依存する
    (blocker edge T_A → T_B)。T_A は block、T_B は todo で始まる。
  * 逆向き edge (T_B → T_A) を張ろうとすると循環になるので server が拒否する
    (二段循環検知の (i)、方針4)。
  * tick が実行セッションを起こし、sv-exec-b が T_B を working→leader_review へ
    進めた瞬間、reconcile が「blocker が取り込み済み (leader_review)」を検知して
    T_A を block→todo に自動再開する (AND auto-unblock)。
  * 時計を cadence 分進めて次の tick を打つと T_A が due になり、sv-exec-a が
    T_A を進める。
  * リーダー (= 実行者の外) が各 Target を全 met の attainment verdict で
    user_review に倒す (e-4386 完遂ゲートを通過)。**リーダーは自分が実行した
    Target を持たないので self_judgment ゲートに阻まれない。**
  * 最終的に両 Target が user_review に到達する。ここまで human の逐次承認は
    一度も挟まっていない (= 評価軸「人間の逐次介入なし」)。

時計の注入は server.app._INJECTED_SCHEDULER_NOW (in-process only の seam)。
"""
from __future__ import annotations

import copy
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")
os.environ.setdefault("BEACON_SCHEDULER_INTERNAL_KEY", "test-scheduler-key")

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402
import trek as trek_mod  # noqa: E402

sys.modules["firestore_client"] = app_module.db

PA = "proj-alpha"
PB = "proj-beta"
SCHED_HEADERS = {"X-Beacon-Scheduler-Key": "test-scheduler-key"}

_treks: dict[str, dict] = {}
_bus_events_by_project: dict[str, list[dict]] = {}
_sessions_by_project: dict[str, list[dict]] = {}
_projects: dict[str, dict] = {}
_project_pool: dict[str, dict] = {}


def _mock_get_trek(trek_id):
    d = _treks.get(trek_id)
    return copy.deepcopy(d) if d else None


def _mock_save_trek(trek_id, data):
    payload = {k: v for k, v in data.items() if k != "trek_id"}
    _treks[trek_id] = {**copy.deepcopy(payload), "trek_id": trek_id}


def _mock_list_treks(actor_id=None, *, status=None, include_archived=False):
    out = []
    for t in _treks.values():
        if not include_archived and t.get("status") == "archived":
            continue
        if status and t.get("status") != status:
            continue
        out.append(copy.deepcopy(t))
    return out


def _mock_append_bus_event(project_id, data):
    bus = _bus_events_by_project.setdefault(project_id, [])
    event_id = f"ev-{project_id}-{len(bus)}"
    bus.append({"event_id": event_id, **copy.deepcopy(data)})
    return event_id


def _mock_list_sessions(project_id):
    return copy.deepcopy(_sessions_by_project.get(project_id, []))


def _mock_get_project(project_id):
    return copy.deepcopy(_project_pool.get(project_id))


def _mock_list_projects(user_id="", include_archived=False):
    return [{"project_id": pid, "name": pid} for pid in _projects]


@pytest.fixture(autouse=True)
def _harness():
    db = app_module.db
    binds = [
        ("get_trek", _mock_get_trek), ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks), ("append_bus_event", _mock_append_bus_event),
        ("list_sessions", _mock_list_sessions), ("get_project", _mock_get_project),
        ("list_projects", _mock_list_projects),
    ]
    prior = {n: getattr(db, n, None) for n, _ in binds}
    for n, m in binds:
        setattr(db, n, m)
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    for store in (_treks, _bus_events_by_project, _sessions_by_project,
                  _projects, _project_pool):
        store.clear()
    yield
    app_module._auth_enabled = prior_auth
    app_module._INJECTED_SCHEDULER_NOW = None
    app_module.app.dependency_overrides.clear()
    for n, v in prior.items():
        if v is None:
            if hasattr(db, n):
                delattr(db, n)
        else:
            setattr(db, n, v)


client = TestClient(app_module.app)


def _as(uid):
    """Impersonate a user for the next request(s)."""
    app_module.app.dependency_overrides[app_module.require_auth] = (
        lambda: {"sub": uid, "email": f"{uid}@x"}
    )


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _set_clock(dt):
    """Inject the scheduler clock and refresh session liveness to it."""
    app_module._INJECTED_SCHEDULER_NOW = dt
    now_iso = _iso(dt)
    for sessions in _sessions_by_project.values():
        for s in sessions:
            s["last_active"] = now_iso


def _tick(trek_id):
    return client.post("/api/system/trek-scheduler/tick",
                       json={"trek_ids": [trek_id]}, headers=SCHED_HEADERS)


def _fired_ids(resp):
    """trek_ids that fired a progress-check on this tick (report['fired'])."""
    return {f.get("trek_id") for f in resp.json().get("fired", [])}


def _state(trek_id, target_id):
    return trek_mod.get_task_state(_treks[trek_id], target_id)


def _patch_state(trek_id, target_id, state, sid, uid, *, verdict="",
                 attainment=None):
    _as(uid)
    body = {"task_id": target_id, "state": state}
    if verdict:
        body["verdict"] = verdict
    if attainment is not None:
        body["attainment_verdict"] = attainment
    return client.patch(
        f"/api/treks/{trek_id}/task-state", json=body,
        headers={"X-Beacon-Session": sid},
    )


def _seed_trek(trek_id, t0):
    now_iso = _iso(t0)
    _projects[PA] = {"name": PA}
    _projects[PB] = {"name": PB}
    # Pool tasks — the slot-done precondition (e-2650) requires the pool entry
    # to be done before a slot may reach the terminal (user_review).
    _project_pool[PA] = {"milestones": [
        {"id": "ms-x", "entries": [{"id": "e-ta", "type": "task", "status": "done"}]}
    ], "operations": []}
    _project_pool[PB] = {"milestones": [
        {"id": "ms-x", "entries": [{"id": "e-tb", "type": "task", "status": "done"}]}
    ], "operations": []}
    _treks[trek_id] = {
        "trek_id": trek_id, "title": "AC12 cross-instance", "description": "",
        "type": "persistent", "status": "active",
        "creator_actor": {"user_id": "uid-leader", "email": ""},
        "leader_session_id": "sv-leader",
        "members": [
            {"user_id": "uid-leader", "session_id": "sv-leader",
             "role": "leader", "joined_at": now_iso},
            {"user_id": "uid-exec-a", "session_id": "sv-exec-a",
             "role": "member", "joined_at": now_iso},
            {"user_id": "uid-exec-b", "session_id": "sv-exec-b",
             "role": "member", "joined_at": now_iso},
        ],
        "scope": [
            {"project": PA, "milestone": "ms-x"},
            {"project": PB, "milestone": "ms-x"},
        ],
        "halt": None, "goal_state": "cross-instance block demo",
        "meta": {"cadence_minutes": 10, "migration_phase": "A"},
        "task_states": {
            "e-ta": {"state": "todo", "updated_by_session_id": "sv-exec-a",
                     "updated_at": now_iso, "last_activity_at": now_iso},
            "e-tb": {"state": "todo", "updated_by_session_id": "sv-exec-b",
                     "updated_at": now_iso, "last_activity_at": now_iso},
        },
        "created_at": now_iso, "updated_at": now_iso, "archived_at": None,
    }
    _sessions_by_project[PA] = [
        {"session_id": "sv-leader", "user_id": "uid-leader", "last_active": now_iso},
        {"session_id": "sv-exec-a", "user_id": "uid-exec-a", "last_active": now_iso},
    ]
    _sessions_by_project[PB] = [
        {"session_id": "sv-exec-b", "user_id": "uid-exec-b", "last_active": now_iso},
    ]


_ALL_MET = [{"criterion": "AC の全項目", "verdict": "met"}]


def test_cross_instance_mutual_block_reaches_user_review_without_human():
    tk = "tk-ac12"
    t0 = datetime.datetime(2026, 7, 29, 0, 0, 0, tzinfo=datetime.timezone.utc)
    _seed_trek(tk, t0)

    # --- リーダーが依存台帳を張る: T_A は T_B に依存 (方針7) ---
    _as("uid-leader")
    r = client.post(f"/api/treks/{tk}/blocker",
                    json={"target_id": "e-ta", "blocker_target_ids": ["e-tb"]},
                    headers={"X-Beacon-Session": "sv-leader"})
    assert r.status_code == 200, r.text
    assert _state(tk, "e-ta") == "block"   # 依存元は block
    assert _state(tk, "e-tb") == "todo"    # blocker は着手可能

    # --- 循環検知 (方針4 二段の (i)): 逆向き edge は拒否され、張れない ---
    _as("uid-leader")
    r = client.post(f"/api/treks/{tk}/blocker",
                    json={"target_id": "e-tb", "blocker_target_ids": ["e-ta"]},
                    headers={"X-Beacon-Session": "sv-leader"})
    assert r.status_code >= 400, "cycle edge must be rejected"
    assert _state(tk, "e-tb") == "todo"    # 拒否されたので T_B は block に落ちない

    # --- tick 1 (T0): 実行セッションを起こす。blocked な T_A は着手対象にならない ---
    _set_clock(t0)
    r = _tick(tk)
    assert r.status_code == 200, r.text
    assert tk in _fired_ids(r), "due trek should fire a progress-check"

    # --- sv-exec-b が T_B を進める (tick への型付き応答をシミュレート) ---
    assert _patch_state(tk, "e-tb", "working", "sv-exec-b", "uid-exec-b").status_code == 200
    r = _patch_state(tk, "e-tb", "leader_review", "sv-exec-b", "uid-exec-b")
    assert r.status_code == 200, r.text

    # --- AND auto-unblock は tick 駆動: blocker が leader_review (取り込み済み) に
    #     達した後の tick で、server の reconcile が依存元 T_A を block→todo に
    #     自動再開する (candidate ループで毎 tick 実行、人間不介在) ---
    assert _state(tk, "e-ta") == "block"   # tick 前はまだ block
    r = _tick(tk)
    assert r.status_code == 200, r.text
    assert _state(tk, "e-ta") == "todo", "T_A must auto-unblock once T_B merged"

    # --- リーダー (実行者の外) が T_B を全 met で user_review に倒す (e-4386 通過) ---
    r = _patch_state(tk, "e-tb", "user_review", "sv-leader", "uid-leader",
                     verdict="approve", attainment=_ALL_MET)
    assert r.status_code == 200, r.text
    assert _state(tk, "e-tb") == "user_review"

    # --- 時計を進めて tick 2: T_A が due になり実行セッションが起こされる ---
    t1 = t0 + datetime.timedelta(minutes=15)
    _set_clock(t1)
    r = _tick(tk)
    assert r.status_code == 200, r.text
    assert tk in _fired_ids(r)

    # --- sv-exec-a が T_A を進める ---
    assert _patch_state(tk, "e-ta", "working", "sv-exec-a", "uid-exec-a").status_code == 200
    assert _patch_state(tk, "e-ta", "leader_review", "sv-exec-a", "uid-exec-a").status_code == 200
    # --- リーダーが T_A を user_review に倒す ---
    r = _patch_state(tk, "e-ta", "user_review", "sv-leader", "uid-leader",
                     verdict="approve", attainment=_ALL_MET)
    assert r.status_code == 200, r.text

    # --- 評価軸: 両 Target が human の逐次承認なしに user_review 手前まで到達 ---
    assert _state(tk, "e-ta") == "user_review"
    assert _state(tk, "e-tb") == "user_review"


def test_self_judgment_leader_cannot_selfapprove_own_target():
    """AC12 の裏返し: リーダーが自分で実行した Target は self_judgment ゲートで
    user_review に倒せない (e-4386)。だから AC12 は実行者と judge を分ける。"""
    tk = "tk-ac12-self"
    t0 = datetime.datetime(2026, 7, 29, 0, 0, 0, tzinfo=datetime.timezone.utc)
    _seed_trek(tk, t0)
    # リーダー自身が T_B を working→leader_review に stamp (= 実行者 == leader)。
    assert _patch_state(tk, "e-tb", "working", "sv-leader", "uid-leader").status_code == 200
    assert _patch_state(tk, "e-tb", "leader_review", "sv-leader", "uid-leader").status_code == 200
    # 同じ session が approve で user_review へ → self_judgment 留置 (409)。
    r = _patch_state(tk, "e-tb", "user_review", "sv-leader", "uid-leader",
                     verdict="approve", attainment=_ALL_MET)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "attainment_self_judgment_banned"
    assert _state(tk, "e-tb") == "leader_review"


def test_injected_clock_controls_tick_due():
    """時計の注入が tick の due 判定を制御することを固定する (ハーネスの土台)。"""
    tk = "tk-ac12-clock"
    t0 = datetime.datetime(2026, 7, 29, 0, 0, 0, tzinfo=datetime.timezone.utc)
    _seed_trek(tk, t0)
    # 初回 tick は due (一度も fire していない)。
    _set_clock(t0)
    assert tk in _fired_ids(_tick(tk))
    # cadence 未満しか進めない 2 度目の tick は due でない (fire しない)。
    _set_clock(t0 + datetime.timedelta(minutes=3))
    assert tk not in _fired_ids(_tick(tk))
    # cadence を超えて進めた 3 度目は再び due。
    _set_clock(t0 + datetime.timedelta(minutes=20))
    assert tk in _fired_ids(_tick(tk))
