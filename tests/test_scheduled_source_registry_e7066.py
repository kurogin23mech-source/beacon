"""定期発火に相乗りしている source が「回っているか」を 1 つずつ観測できることを測る
(ms-185 e-7066)。

**なぜこのファイルが要るか**: 定期ティックに相乗りしているのは 1 つではなく 2 つある
— Operation の発火 と 締切リマインド。以前は tick の応答に「合計何件出たか」しか
載っておらず、**締切リマインドを落としても tick は緑のまま**だった。つまり
「ティックは動いている」を見ても「両方の自律が生きている」の証拠にならない。
ms-145 / e-4104 で一度踏んだ「駆動役が黙って消えて事故で気付く」型の source 単位版。

固定するのは 4 点:
  1. source の登録簿 (``SCHEDULED_SOURCES``) が、実際に回す対象であること
     — 応答に載る ``scheduled_sources`` は登録簿から導かれるので、「回していない
     のに回していると報告する」状態を作れない。
  2. 1 回の tick で **両方の source が実際に発火し**、出所の刻印と source 別の
     内訳が応答に載ること (= Operation だけ見て緑にできない)。
  3. ``/api/system/tick-health`` が ``scheduled_sources`` を出すこと
     (= box の外の watchdog が source の欠落を読める)。
  4. watchdog 側 (``lib/tick_health.py``) が欠落を alert し、source を報告しない
     旧デプロイでは黙ること。

ここは**振る舞い**を測る (ソース文字列の存在検査ではない)。配線を逆にしても
トークンは全部残るので、文字列検査は偽の緑になる — ms-185 で実際に踏んだ型。
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"
os.environ["BEACON_SCHEDULER_INTERNAL_KEY"] = "test-scheduler-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import tick_health as th  # noqa: E402

TICK_PATH = "/api/system/tick"
HEALTH_PATH = "/api/system/tick-health"
HEADERS_OK = {"X-Beacon-Scheduler-Key": "test-scheduler-key"}
APP_PY = pathlib.Path(__file__).resolve().parents[1] / "server" / "app.py"

PID = "p-e7066"
CLAIMER = "sv-claimer-e7066"


def _project() -> dict:
    """1 プロジェクトに「発火すべきもの」を source ごとに 1 件ずつ置く。

    - Operation: open + server_tick + 未発火 → due (envelope は下で stub)
    - milestone: target_date が過去 + claim 者あり → 締切リマインドが飛ぶ
    """
    return {
        "project_id": PID,
        "name": "e7066",
        "operations": [{
            "id": "op-1",
            "title": "watch",
            "status": "open",
            "log_source": "op-1",
            "meta": {"server_tick": True, "cadence_minutes": 60,
                     "claimer_session_id": CLAIMER},
        }],
        "milestones": [{
            "id": "ms-1",
            "title": "期日を過ぎたターゲット",
            "status": "in_progress",
            "target_date": "2000-01-01",
            "occupation": {"session_id": CLAIMER},
            "entries": [],
        }],
    }


@pytest.fixture
def harness():
    """保存層を差し替えて、tick を 1 回ハーメチックに走らせる足場。

    ``appended`` に積まれた bus event が「実際に発火したか」の真値源
    (= 応答の数字が報告だけで中身が無い、を防ぐ)。
    """
    db = app_module.db
    state = {"project": _project(), "appended": [], "saved": 0}

    def _append(pid, data):
        state["appended"].append((pid, data))
        return "ev-stub"

    def _save(pid, project):
        state["saved"] += 1

    stubs = {
        "list_treks": lambda actor_id=None, **kw: [],
        "list_tick_candidate_project_ids": lambda *a, **kw: [PID],
        "list_all_projects": lambda *a, **kw: [{"project_id": PID}],
        "get_project": lambda pid, *a, **kw: state["project"],
        "save_project": _save,
        "list_sessions": lambda *a, **kw: [],
        "append_bus_event": _append,
        # Operation は事前承認済み envelope が無いと発火しない (再利用する設計)。
        "list_operation_envelopes": lambda *a, **kw: [
            {"envelope": {"tier": "T1-system", "stub": True}}],
    }
    missing = object()
    saved = {name: getattr(db, name, missing) for name in stubs}
    for name, fn in stubs.items():
        setattr(db, name, fn)
    try:
        yield state
    finally:
        for name, val in saved.items():
            if val is missing:
                delattr(db, name)
            else:
                setattr(db, name, val)


# --- 1. 登録簿が実体であること -------------------------------------------

def test_registry_names_are_derived_from_the_registry_itself():
    """応答に載る source 名は、実際に回す tuple から導かれる。"""
    assert app_module.SCHEDULED_SOURCE_NAMES == tuple(
        name for name, _ in app_module.SCHEDULED_SOURCES)


def test_watchdog_required_sources_are_all_actually_driven():
    """watchdog が「在るはず」と宣言した source を、server が全部回している。

    2 つの一覧をあえて独立に宣言している (watchdog は箱の外の観測者なので、
    観測対象から期待値を導くと対象が source を失ったことに気付けない)。その
    代わりに**ここで突き合わせる**ので、server 側から source を消すと CI が
    赤くなる = 消すなら両方を明示的に直すことになる。
    """
    missing = [n for n in th.REQUIRED_SCHEDULED_SOURCES
               if n not in app_module.SCHEDULED_SOURCE_NAMES]
    assert not missing, (
        f"watchdog (lib/tick_health.REQUIRED_SCHEDULED_SOURCES) requires "
        f"{missing} but server/app.py SCHEDULED_SOURCES drives only "
        f"{list(app_module.SCHEDULED_SOURCE_NAMES)}. 定期発火から source を "
        f"落とすなら、watchdog の期待値も同時に直すこと (片方だけだと本番で "
        f"無音で止まる / 鳴り続ける)。")


def test_scheduled_sources_are_driven_only_through_the_registry():
    """``_fire_due_scheduled`` は登録簿を回す以外の方法で source を呼ばない。

    直呼びが 1 本でも残ると、その source は登録簿から消しても動き続け
    (= 報告と実体がズレる)、逆に登録簿にだけ在って動かない形も作れる。
    """
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_fire_due_scheduled")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    forbidden = {"_fire_due_deadlines", "_fire_source_operations", "_fire_operation"}
    assert not (called & forbidden), (
        f"{sorted(called & forbidden)} is called directly inside "
        f"_fire_due_scheduled; sources must go through SCHEDULED_SOURCES so the "
        f"reported scheduled_sources cannot drift from what actually runs.")
    assert any(isinstance(n, ast.Name) and n.id == "SCHEDULED_SOURCES"
               for n in ast.walk(fn)), \
        "_fire_due_scheduled must iterate SCHEDULED_SOURCES"


# --- 2. 1 tick で両方が実際に発火する ------------------------------------

def test_one_tick_fires_both_sources_and_reports_them_separately(harness):
    client = TestClient(app_module.app)
    resp = client.post(TICK_PATH, json={}, headers=HEADERS_OK)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # (a) 実体: 2 つの channel に bus event が出ている。
    channels = [data.get("channel") for _pid, data in harness["appended"]]
    assert "operation-trigger" in channels, (
        f"Operation が発火していない (appended channels={channels})")
    kinds = [(data.get("payload") or {}).get("kind")
             for _pid, data in harness["appended"]]
    assert "deadline-reminder" in kinds, (
        f"締切リマインドが発火していない (payload kinds={kinds})")

    # (b) 報告: source ごとに分けて載っている。
    assert body["scheduled_sources"] == ["operation", "deadline"]
    by_source = body["scheduled_by_source"]
    assert by_source["operation"]["fired"] == 1, by_source
    assert by_source["deadline"]["fired"] == 1, by_source

    # (c) 刻印: 各 report entry が出所を持つ (= 合計だけに潰れていない)。
    sources = {e.get("source") for e in body["scheduled_fires"]}
    assert sources == {"operation", "deadline"}, body["scheduled_fires"]


def test_dropping_one_source_is_visible_in_the_report(harness, monkeypatch):
    """締切リマインドを登録簿から外すと、応答がそれを隠さない。

    これがこのファイルの核心 — 「Operation だけ見て緑」を作れないこと。
    """
    monkeypatch.setattr(app_module, "SCHEDULED_SOURCES",
                        (("operation", app_module._fire_source_operations),))
    monkeypatch.setattr(app_module, "SCHEDULED_SOURCE_NAMES", ("operation",))
    client = TestClient(app_module.app)
    body = client.post(TICK_PATH, json={}, headers=HEADERS_OK).json()

    kinds = [(data.get("payload") or {}).get("kind")
             for _pid, data in harness["appended"]]
    assert "deadline-reminder" not in kinds  # 実際に止まっている

    # 報告する面は 3 つ (tick の応答 / 直近 tick の記録 / tick-health endpoint)。
    # **3 面とも**実体に追従することを見る。1 面でも手書き literal になっていると
    # 「回していないのに回していると報告する」状態が作れる — 観測する側は
    # どれか 1 面しか読まないので、無防備な面が 1 つでもあれば穴になる。
    assert body["scheduled_sources"] == ["operation"], "tick の応答が追従していない"
    health = client.get(HEALTH_PATH).json()
    assert health["scheduled_sources"] == ["operation"], \
        "tick-health endpoint が追従していない"
    assert health["last_tick_report"]["scheduled_sources"] == ["operation"], \
        "直近 tick の記録が追従していない"

    verdict = th.evaluate_scheduled_sources(body["scheduled_sources"])
    assert verdict["status"] == "missing"
    assert verdict["missing"] == ["deadline"]


# --- 3. 箱の外から読める --------------------------------------------------

def test_tick_health_exposes_the_driven_sources(harness):
    client = TestClient(app_module.app)
    client.post(TICK_PATH, json={}, headers=HEADERS_OK)
    health = client.get(HEALTH_PATH).json()
    assert health["scheduled_sources"] == ["operation", "deadline"]
    # 直近 tick の内訳も残る (= 「回ったが 0 件」と「消えた」を区別できる)。
    assert health["last_tick_report"]["scheduled_by_source"]["deadline"]["fired"] == 1


# --- 4. watchdog の判定 ---------------------------------------------------

def test_watchdog_alerts_on_missing_source_and_names_it():
    sources = th.evaluate_scheduled_sources(["operation"])
    health = {"status": "ok", "seconds_since": 5.0, "last_tick_at": "x"}
    assert th.should_alert(health, True, sources) is True
    msg = th.format_alert_message(True, health, "https://example.test", sources)
    assert "deadline" in msg
    assert "ティックは動いて" in msg  # 「止まった」と誤報しない


def test_watchdog_is_silent_against_a_server_that_predates_the_report():
    """``scheduled_sources`` を出さない旧デプロイで鳴らない。

    rollout 中に鳴きっぱなしの watchdog は mute され、結果として何も守らない。
    """
    sources = th.evaluate_scheduled_sources(None)
    health = {"status": "ok", "seconds_since": 5.0, "last_tick_at": "x"}
    assert sources["status"] == "unknown"
    assert th.should_alert(health, True, sources) is False


def test_missing_source_dedup_key_distinguishes_which_source_went():
    """2 つ目の source が落ちたとき、1 つ目の dedup に飲まれない。"""
    one = th.alert_dedup_key(True, {"status": "ok"},
                             th.evaluate_scheduled_sources(["operation"]))
    both = th.alert_dedup_key(True, {"status": "ok"},
                              th.evaluate_scheduled_sources([]))
    assert one != both


# --- 5. 「配線は在るが毎回失敗している」も鳴る ---------------------------
#
# e-7066 follow-up (独立 AX レビューの high)。上の 1〜4 が測っていたのは
# 「source の行が登録簿から消えた」形だけで、**登録簿に残ったまま毎回例外を
# 投げている** 形は status=ok のまま鳴らなかった。報告される source 一覧は
# 登録簿のキーであって成否とは独立なので、100% 失敗していても緑に見える。
# ms-185 の起点 (e-5366: 44 日 / 約 6 万回の無音失敗、e-7078: 本番で一度も
# 成立していなかった Operation の自律実行) と同じ失敗クラスの一段深い変種。


def test_a_source_that_errors_every_attempt_is_not_reported_ok(harness, monkeypatch):
    """登録簿に在り毎 tick 呼ばれるが全失敗する source を、watchdog が緑にしない。

    判定器の単体ではなく **本物の tick を回して** 測る: 発火関数を「呼ばれるが
    毎回例外」に挿げ替え、tick-health が返す payload をそのまま watchdog に
    食わせて、鳴るところまでを 1 本で繋ぐ。単体だけだと「判定器は直ったが
    monitor が内訳を渡していない」配線漏れを見逃す (= まさに今回の欠陥の形)。
    """
    def _always_raises(*_a, **_kw):
        raise RuntimeError("deadline source is broken")

    monkeypatch.setattr(
        app_module, "SCHEDULED_SOURCES",
        (("operation", app_module._fire_source_operations),
         ("deadline", _always_raises)))

    client = TestClient(app_module.app)
    client.post(TICK_PATH, json={}, headers=HEADERS_OK)
    health = client.get(HEALTH_PATH).json()

    # 登録簿の軸では欠落していない (= 名前は在る)。ここが緑に見える正体。
    assert "deadline" in health["scheduled_sources"]
    by_source = health["last_tick_report"]["scheduled_by_source"]
    assert by_source["deadline"]["error"] > 0, by_source
    assert by_source["deadline"]["fired"] == 0, by_source

    # 名前だけ渡す旧判定は ok を返す = これが塞いだ穴そのもの。
    assert th.evaluate_scheduled_sources(
        health["scheduled_sources"])["status"] == "ok"

    # 内訳まで渡すと failing になり、alert が鳴る。
    verdict = th.evaluate_scheduled_sources(
        health["scheduled_sources"], fire_summary=by_source)
    assert verdict["status"] == "failing"
    assert verdict["failing"] == ["deadline"]
    tick_ok = {"status": "ok", "seconds_since": 5.0, "last_tick_at": "x"}
    assert th.should_alert(tick_ok, True, verdict) is True
    msg = th.format_alert_message(True, tick_ok, "https://example.test", verdict)
    assert "deadline" in msg
    assert "ティックは動いて" in msg          # 「tick が止まった」と誤報しない
    assert "全試行失敗" in msg                # 欠落ではなく全失敗だと名指しする


def test_monitor_passes_the_breakdown_through_to_the_verdict(harness, monkeypatch):
    """monitor が内訳を判定器に渡している (= 配線が在る)。

    判定器が failing を返せるようになっても、monitor が
    ``last_tick_report.scheduled_by_source`` を渡さなければ本番では何も変わら
    ない。「足した」≠「全域に効く」を分けて測る。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_tick_monitor_e7066",
        pathlib.Path(__file__).resolve().parents[1] / "scripts"
        / "tick-health-monitor.py")
    mon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mon)

    import datetime
    sent = []
    payload = {
        "last_tick_at": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "uptime_seconds": 10_000,
        "scheduled_sources": ["operation", "deadline"],
        "last_tick_report": {
            "scheduled_by_source": {
                "operation": {"fired": 2, "skipped": 0, "error": 0, "total": 2},
                "deadline": {"fired": 0, "skipped": 0, "error": 3, "total": 3},
            },
        },
    }
    verdict = mon.decide_and_alert(
        reachable=True, payload=payload,
        now=datetime.datetime.now(datetime.timezone.utc),
        prod_url="https://example.test", live_sessions=[],
        owner_user_id="u-owner", last_alerted_key="",
        send=lambda _r, t: sent.append(t) or True)

    assert verdict["sources"]["status"] == "failing", verdict["sources"]
    assert verdict["alerted"] is True, verdict
    assert sent and "deadline" in sent[0]


def test_partial_failure_does_not_cry_wolf(harness):
    """一部成功・一部失敗では鳴らない。

    鳴きっぱなしの watchdog は mute され、結果として何も守らなくなる。
    「全試行失敗」(error>0 かつ fired==0) に絞るのは意図。
    """
    by_source = {
        "operation": {"fired": 3, "skipped": 0, "error": 1, "total": 4},
        "deadline": {"fired": 1, "skipped": 0, "error": 0, "total": 1},
    }
    verdict = th.evaluate_scheduled_sources(
        ["operation", "deadline"], fire_summary=by_source)
    assert verdict["status"] == "ok", verdict
    assert th.should_alert({"status": "ok", "seconds_since": 5.0,
                            "last_tick_at": "x"}, True, verdict) is False


def test_nothing_due_is_not_a_failure(harness):
    """「回ったが 0 件」は失敗ではない (error==0 かつ fired==0)。"""
    by_source = {
        "operation": {"fired": 0, "skipped": 0, "error": 0, "total": 0},
        "deadline": {"fired": 0, "skipped": 2, "error": 0, "total": 2},
    }
    assert th.evaluate_scheduled_sources(
        ["operation", "deadline"], fire_summary=by_source)["status"] == "ok"


def test_a_server_that_omits_the_breakdown_stays_silent(harness):
    """内訳を publish しない旧デプロイでは failing を判定しない (沈黙する)。"""
    for absent in (None, {}, "not-a-dict", {"deadline": "not-a-dict"}):
        verdict = th.evaluate_scheduled_sources(
            ["operation", "deadline"], fire_summary=absent)
        assert verdict["status"] == "ok", (absent, verdict)
        assert verdict["failing"] == []


def test_missing_outranks_failing(harness):
    """欠落と全失敗が同時なら、欠落を先に名指しする (直し方が違う)。"""
    by_source = {"operation": {"fired": 0, "skipped": 0, "error": 5, "total": 5}}
    verdict = th.evaluate_scheduled_sources(["operation"], fire_summary=by_source)
    assert verdict["status"] == "missing"
    assert verdict["missing"] == ["deadline"]
    msg = th.format_alert_message(
        True, {"status": "ok", "seconds_since": 5.0, "last_tick_at": "x"},
        "https://example.test", verdict)
    assert "欠けています" in msg


def test_failing_dedup_key_is_distinct_from_missing_and_per_source(harness):
    """dedup が欠落↔全失敗を混同しない / 2 つ目の全失敗を飲み込まない。"""
    ok = {"status": "ok"}
    miss = th.alert_dedup_key(True, ok, th.evaluate_scheduled_sources(["operation"]))
    one = th.alert_dedup_key(True, ok, th.evaluate_scheduled_sources(
        ["operation", "deadline"],
        fire_summary={"deadline": {"fired": 0, "error": 1, "total": 1}}))
    two = th.alert_dedup_key(True, ok, th.evaluate_scheduled_sources(
        ["operation", "deadline"],
        fire_summary={"deadline": {"fired": 0, "error": 1, "total": 1},
                      "operation": {"fired": 0, "error": 1, "total": 1}}))
    assert len({miss, one, two}) == 3, (miss, one, two)
