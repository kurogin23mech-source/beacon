"""書いた判断が既定の読みに載るかを **種別ごとに** 往復で測る (ms-166 e-6997)。

なぜ要るか
----------
この MS の受入条件 7 は「**配線はあるが** produce/query できない」を検知する
instrument を求めている。ところが出荷された checker
(``scripts/check-capability-scope.py::find_decision_capture_gaps``) が測っているのは
「producer の呼び出しが在るか」= **配線の有無** だった。独立思想レビューの指摘:

    e-5970 以前は producer は全て配線済みで、欠陥は server の読み取り窓だった。
    つまり SPEC が主犯と呼ぶ状態で、あの checker は緑だった。

さらに SPEC の §検知で再発を止める が名指ししていた
「write は成功を返すが read に載らない型を検知する **round-trip 監査**」は、CI にも
doctor にも tick-health にも存在しなかった (grep で確認)。約束された 2 つの instrument の
うち、弱い方だけが実装されていた。

この module がその欠けていた半分を埋める。**書いて、既定の読みで読み返す。**

母集団は機械で導出する
----------------------
種別を手で並べると、次に種別を足した人が 1 行書き忘れた瞬間にその種別だけ測られなく
なる (この MS が何度も直している型)。なので母集団は
``capability_ledger.DECISION_CAPTURE_PRODUCERS`` と
``decision_event.KNOWN_DECISION_KINDS`` の **和** から導く。新しい種別は自動で covered。

意図して既定の読みから外す種別 (``decision_event.NON_DECISION_KINDS`` = dm-send) は
「外れていること」を測る —— 外す判断そのものも回帰から守る。

測れる範囲の明示
----------------
ここで動かすのは実バックエンドの ``append_decision_event`` →
``list_decision_events`` で、ストアは偽 (dynamodb は in-process fallback、mysql は
生成 SQL を仕様表で評価する偽カーソル)。したがって「窓とフィルタの契約」は本物を
測るが、**本物の MySQL / Firestore が同じ SQL を同じように評価するか**は測っていない
(それはデプロイ後の実機突合)。この限界を docstring に書くのは、名乗りを広げないため。
"""
import json
import os
import sys

import pytest

THIS = os.path.dirname(__file__)
SERVER = os.path.normpath(os.path.join(THIS, "..", "server"))
LIB = os.path.normpath(os.path.join(THIS, "..", "lib"))
for _p in (SERVER, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import capability_ledger as cl   # noqa: E402
import decision_event            # noqa: E402
import dynamodb_client as ddb    # noqa: E402
import mysql_client as mc        # noqa: E402

# 既定の読みの件数 (= list_decision_events の limit 既定)。背景の積み上げをこれより
# 多くしないと、窓が古い側を取る実装でも新しい行が偶然見えてしまう。
DEFAULT_LIMIT = 100
BACKLOG = DEFAULT_LIMIT * 3


def _kinds_under_audit() -> list:
    """往復を測る種別の母集団 (機械導出)。"""
    kinds = set(cl.DECISION_CAPTURE_PRODUCERS) | set(decision_event.KNOWN_DECISION_KINDS)
    return sorted(kinds)


def _visible_kinds() -> list:
    return [k for k in _kinds_under_audit()
            if k not in decision_event.NON_DECISION_KINDS]


def _pad_kind_for(kind_under_test: str) -> str:
    """背景に積む種別。**既定の読みに載る種別**から、試験対象とは別のものを選ぶ。

    最初の版はここを ``NON_DECISION_KINDS`` (= dm-send) で埋めていた。だが除外は
    件数制限の **前** に効くので、背景が丸ごと消えて窓が一度も働かず、**旧実装
    (古い側の窓) に戻しても緑のまま**だった —— 自分で偽の緑を作っていた
    (test-the-test が捕まえた)。背景は「読みに載る種別」でなければ窓を溢れさせない。
    """
    for k in _visible_kinds():
        if k != kind_under_test:
            return k
    raise AssertionError("読みに載る種別が 1 つしか無い (母集団の導出が壊れている)")


def _backlog_rows(pad_kind: str) -> list:
    """既定の読みを溢れさせるための背景 (= 本番の『積み上がった stream』の形)。"""
    return [{"decision_id": f"dec-pad-{i:05d}", "kind": pad_kind,
             "decision": "pad",
             "created_at": f"2026-07-{(i % 27) + 1:02d}T00:00:{i % 60:02d}.000000Z"}
            for i in range(BACKLOG)]


@pytest.fixture
def ddb_store(request):
    pad = _pad_kind_for(getattr(request, "param", ""))
    ddb._DECISION_EVENTS_FALLBACK.clear()
    ddb._DECISION_EVENTS_FALLBACK["p1"] = list(_backlog_rows(pad))
    yield ddb._DECISION_EVENTS_FALLBACK["p1"]
    ddb._DECISION_EVENTS_FALLBACK.clear()


@pytest.fixture
def mysql_store(request, monkeypatch):
    """mysql backend: 書き込みは偽ストアに積み、読みは生成 SQL を仕様表で評価する。"""
    rows = list(_backlog_rows(_pad_kind_for(getattr(request, "param", ""))))

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, sql, params=()):
            if sql.strip().upper().startswith("INSERT"):
                # append 側: params から payload を取り出して積む。
                payload = None
                for v in params:
                    if isinstance(v, str) and v.lstrip().startswith("{"):
                        try:
                            payload = json.loads(v)
                        except ValueError:
                            payload = None
                if payload is not None:
                    rows.append(payload)
                self._rows = []
                return
            evaluated = decision_event.mysql_window_eval(rows, sql, list(params[1:]))
            self._rows = [{"sk": r.get("decision_id", ""),
                           "data": json.dumps(r, ensure_ascii=False)}
                          for r in evaluated]

        def fetchall(self):
            return list(self._rows)

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            return None

    monkeypatch.setattr(mc, "_conn", lambda: _Conn())
    yield rows


# --------------------------------------------------------------------------
# 母集団そのものを守る (手で並べていないことを固定する)
# --------------------------------------------------------------------------

def test_the_audited_population_is_derived_not_hand_listed():
    """母集団が台帳と語彙の和から導かれており、空でないこと。

    空振り防止: 導出が壊れて 0 件になると、以下の往復テストは「全部通った」と
    見えるが実は 1 件も測っていない。
    """
    kinds = _kinds_under_audit()
    assert len(kinds) >= 10, f"母集団が小さすぎる (導出が壊れている?): {kinds}"
    # 台帳側 / 語彙側のどちらにしか無い種別も拾っていること (和集合である証拠)。
    assert set(cl.DECISION_CAPTURE_PRODUCERS) <= set(kinds)
    assert set(decision_event.KNOWN_DECISION_KINDS) <= set(kinds)


# --------------------------------------------------------------------------
# 往復 — 書いたものが既定の読みに載る
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind,ddb_store", [(k, k) for k in _visible_kinds()],
                         indirect=["ddb_store"])
def test_ddb_a_written_decision_shows_up_in_the_default_read(kind, ddb_store):
    """dynamodb backend: 書いた直後に既定パラメータで読んで、その行が見えること。"""
    new_id = ddb.append_decision_event("p1", {
        "kind": kind, "decision": f"{kind} を記録した",
        "created_at": "2026-09-30T23:59:59.000000Z"})

    rows = ddb.list_decision_events("p1", limit=DEFAULT_LIMIT,
                                    exclude_kinds=decision_event.NON_DECISION_KINDS)

    ids = [r.get("decision_id") for r in rows]
    assert new_id in ids, (
        f"kind={kind} を書いたのに既定の読みに載っていない "
        f"(背景 {BACKLOG} 件 / 窓 {DEFAULT_LIMIT} 件)。これが e-5970 の形。")


@pytest.mark.parametrize("kind,ddb_store", [(k, k) for k in _visible_kinds()],
                         indirect=["ddb_store"])
def test_ddb_a_written_decision_is_findable_by_its_kind(kind, ddb_store):
    """種別で絞れば、**流れが先に進んだ後でも** 引けること。

    ここは「絞り込みが件数制限の *前* に効いているか」を測る。測り方が要点:
    自分の行を書いた **後に、窓を超える量の新しい行を積む**。絞り込みが後なら
    「最新 N 件 = 全部その後の行」になってから絞るので 0 件になる (= 監査が踏んだ
    『list 0 件』の形)。

    最初に書いた版は「書いた直後に読む」だけだったので、自分の行が最新側に居て
    **絞り込みを後に掛けても通った** (反転ハーネスが捕まえた)。
    """
    new_id = ddb.append_decision_event("p1", {
        "kind": kind, "decision": f"{kind} を記録した",
        "created_at": "2026-09-01T00:00:00.000000Z"})
    # その後に流れが進む (本番の append-only stream の形)。
    pad = _pad_kind_for(kind)
    for i in range(BACKLOG):
        ddb.append_decision_event("p1", {
            "kind": pad, "decision": "later",
            "created_at": f"2026-10-{(i % 28) + 1:02d}T12:00:{i % 60:02d}.000000Z"})

    rows = ddb.list_decision_events("p1", kind=kind, limit=DEFAULT_LIMIT)

    assert new_id in [r.get("decision_id") for r in rows], (
        f"--kind {kind} で引けない。絞り込みが件数制限の **後** に掛かっている形 "
        f"(= 監査が踏んだ『list 0 件』)。")


@pytest.mark.parametrize("kind,mysql_store", [(k, k) for k in _visible_kinds()],
                         indirect=["mysql_store"])
def test_mysql_a_written_decision_shows_up_in_the_default_read(kind, mysql_store):
    """mysql backend (本番): 同じ往復を生成 SQL の評価越しに測る。"""
    new_id = mc.append_decision_event("p1", {
        "kind": kind, "decision": f"{kind} を記録した",
        "created_at": "2026-09-30T23:59:59.000000Z"})

    rows = mc.list_decision_events("p1", limit=DEFAULT_LIMIT,
                                   exclude_kinds=decision_event.NON_DECISION_KINDS)

    assert new_id in [r.get("decision_id") for r in rows], (
        f"kind={kind} を書いたのに既定の読みに載っていない (mysql)。")


# --------------------------------------------------------------------------
# 意図して外している種別は「外れている」ことも測る
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind,ddb_store",
                         [(k, k) for k in sorted(decision_event.NON_DECISION_KINDS)],
                         indirect=["ddb_store"])
def test_an_excluded_kind_stays_out_of_the_default_read(kind, ddb_store):
    """既定から外す判断そのものを回帰から守る (外し忘れ / 外し過ぎの両方)。"""
    new_id = ddb.append_decision_event("p1", {
        "kind": kind, "decision": "sent",
        "created_at": "2026-09-30T23:59:59.000000Z"})

    default_rows = ddb.list_decision_events(
        "p1", limit=DEFAULT_LIMIT, exclude_kinds=decision_event.NON_DECISION_KINDS)
    assert new_id not in [r.get("decision_id") for r in default_rows], (
        f"{kind} は既定の読みから外す判断なのに載っている")

    # ただし **名指しすれば読める** こと (隠蔽ではなく既定の絞り込みであること)。
    named = ddb.list_decision_events("p1", kind=kind, limit=DEFAULT_LIMIT)
    assert new_id in [r.get("decision_id") for r in named], (
        f"{kind} が名指しでも読めない = 隠蔽になっている")
