"""decision stream の read 窓の契約テスト (ms-166 e-5970)。

回帰の芯: append-only な decision_events は無制限に伸びる (本番 dm-send は 500+)。
以前の実装は **古い方から** limit 件を返し (``[:limit]``)、route が kind フィルタを
truncate の **後** に掛けていた。その結果:

  * backlog が limit を超えると、新しく書いた decision がすべて既定 read から
    消える (= 永続化は成功しているのに「載らない」)。
  * ``--kind X`` は「最古 limit 件の中に X があるか」を見るので、X が古い slice に
    無ければ 0 件に見える (= 監査が踏んだ "list 0件")。

本番の実バックエンドは mysql、fallback は dynamodb。両者の list_decision_events は
同一ロジック (firestore も同型) なので、ここで両方の pure-python 窓ロジックを固定する。
真因は write→read の *ストア* 不整合ではなく read 窓だったので、テストも書き込みの
永続性ではなく「何を返す窓か」を突く。
"""
import os
import sys

import json

import pytest

THIS = os.path.dirname(__file__)
SERVER = os.path.normpath(os.path.join(THIS, "..", "server"))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import dynamodb_client as ddb  # noqa: E402
import mysql_client as mc      # noqa: E402
import decision_event          # noqa: E402


def _events():
    """本番形を模した混在ストリーム: 古い dm-send が大量、直近に他 kind が少量。

    created_at は昇順に振る。dm-send は最古 300 件、その後に監査対象の client-write
    kind (review-adjudication / completion-verdict / log-backstop) を直近に置く。
    """
    rows = []
    for i in range(300):
        rows.append({
            "decision_id": f"dec-old-{i:04d}",
            "kind": "dm-send",
            "decision": "sent",
            "created_at": f"2026-07-{(i % 27) + 1:02d}T00:00:{i % 60:02d}.000000Z",
        })
    recent = [
        ("review-adjudication", "finding A を却下"),
        ("completion-verdict", "ms-X を attained と判定"),
        ("review-adjudication", "finding B を採用"),
        ("log-backstop", "commit の判断を記録"),
    ]
    for j, (kind, what) in enumerate(recent):
        rows.append({
            "decision_id": f"dec-new-{j:04d}",
            "kind": kind,
            "decision": what,
            "created_at": f"2026-09-01T10:00:{j:02d}.000000Z",
        })
    return rows


@pytest.fixture
def ddb_stream():
    ddb._DECISION_EVENTS_FALLBACK.clear()
    ddb._DECISION_EVENTS_FALLBACK["p1"] = list(_events())
    yield
    ddb._DECISION_EVENTS_FALLBACK.clear()


@pytest.fixture
def mysql_stream(monkeypatch):
    """mysql backend の読み取りを偽 DB に差し替える。

    ms-166 e-5986 で mysql は絞り込み・並び・件数制限を **SQL へ押し下げた** ので、
    旧実装のように ``_query`` を差し替えるだけでは窓の契約を測れない (押し下げた部分が
    評価されず、最古 limit 件が返ってしまう = e-5970 が直した穴の形)。偽カーソルは
    生成 SQL を **仕様表に基づいて** 評価する (``decision_event.mysql_window_eval``)。

    検証できるのは「生成 SQL が仕様表どおりに評価されたら結果はどうなるか」まで。
    本物の MySQL が仕様表どおりに評価するかはデプロイ後に実機で突合する。
    """
    events = list(_events())

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, sql, params=()):
            rows = decision_event.mysql_window_eval(events, sql, list(params[1:]))
            self._rows = [{"sk": r.get("decision_id", ""),
                           "data": json.dumps(r, ensure_ascii=False)}
                          for r in rows]

        def fetchall(self):
            return list(self._rows)

    class _Conn:
        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(mc, "_conn", lambda: _Conn())


# --- window_decision_events (単一真実源、3 backend 共通) --------------------
# ms-166 e-5970 maintainability review (M2/M3): 窓ロジックは 3 backend に逐語
# コピーせず decision_event.window_decision_events に集約した。ここでその pure
# 関数を直接固定する = firestore を含む 3 backend の窓挙動が同時に守られる。

def test_window_helper_最新側の窓():
    rows = decision_event.window_decision_events(_events(), limit=100)
    assert len(rows) == 100
    assert rows[-1]["decision_id"] == "dec-new-0003"  # 最新が末尾
    assert "review-adjudication" in {r["kind"] for r in rows}


def test_window_helper_kindはlimitの前に絞る():
    rows = decision_event.window_decision_events(
        _events(), kind="review-adjudication", limit=100)
    assert [r["decision_id"] for r in rows] == ["dec-new-0000", "dec-new-0002"]


def test_window_helper_sinceは下限():
    rows = decision_event.window_decision_events(
        _events(), since="2026-09-01T00:00:00.000000Z")
    assert rows and all(r["created_at"] > "2026-09-01T00:00:00.000000Z" for r in rows)


def test_window_helper_入力listを変更しない():
    src = _events()
    before = list(src)
    decision_event.window_decision_events(src, kind="dm-send", limit=5)
    assert src == before  # 純関数 — 副作用なし


# --- dynamodb fallback ------------------------------------------------------

def test_ddb_既定は最新側の窓を返す(ddb_stream):
    """limit=100 は最古 100 件ではなく最新 100 件 (末尾) を返す。"""
    rows = ddb.list_decision_events("p1", limit=100)
    assert len(rows) == 100
    # 昇順で返る (時系列表示) が、窓は最新側 → 直近の client-write kind が入る。
    kinds = {r["kind"] for r in rows}
    assert "review-adjudication" in kinds
    assert rows[-1]["decision_id"] == "dec-new-0003"  # 最新が末尾


def test_ddb_kindはlimitの前に絞る(ddb_stream):
    """--kind review-adjudication は limit の窓に関係なく該当2件を返す。

    旧実装 (最古 100=全部 dm-send を取ってから kind フィルタ) では 0 件だった。"""
    rows = ddb.list_decision_events("p1", kind="review-adjudication", limit=100)
    assert [r["decision_id"] for r in rows] == ["dec-new-0000", "dec-new-0002"]


def test_ddb_旗艦回帰_client_write_kindが見える(ddb_stream):
    """監査が踏んだ症状の直接回帰: 大量の古い dm-send があっても、
    直近の completion-verdict / log-backstop が既定 kind read で見える。"""
    for k in ("completion-verdict", "log-backstop"):
        rows = ddb.list_decision_events("p1", kind=k, limit=100)
        assert len(rows) == 1, f"{k} が read から欠落 (= e-5970 回帰)"
        assert rows[0]["kind"] == k


# --- mysql (本番バックエンド) ----------------------------------------------

def test_mysql_既定は最新側の窓を返す(mysql_stream):
    rows = mc.list_decision_events("p1", limit=100)
    assert len(rows) == 100
    assert rows[-1]["decision_id"] == "dec-new-0003"
    assert {r["kind"] for r in rows} & {"review-adjudication", "log-backstop"}


def test_mysql_kindはlimitの前に絞る(mysql_stream):
    rows = mc.list_decision_events("p1", kind="review-adjudication", limit=100)
    assert [r["decision_id"] for r in rows] == ["dec-new-0000", "dec-new-0002"]


def test_mysql_sinceは下限として効く(mysql_stream):
    rows = mc.list_decision_events("p1", since="2026-09-01T00:00:00.000000Z")
    assert rows and all(r["created_at"] > "2026-09-01T00:00:00.000000Z" for r in rows)
    assert all(r["kind"] != "dm-send" for r in rows)
