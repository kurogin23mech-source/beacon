"""ms-166 e-5986 — 判断記録の read を規模で硬くする (SQL へ押し下げる)。

## なぜ

2026-08-20 に本番が最大 12 時間停止した原因の 1 つは、バス取得が project の全イベントを
Python へ読み込んでいたこと (98,943 件 / 72MB を毎秒 json.loads)。**契約は正しく、テストも
緑だった** — テストが規模の次元を持っていなかった (CORE doc `scale-contract-principle`)。

判断記録 (decision_events) の read は同じ形をしていた: `_query` が project の全行を Python に
読み、そのあとで窓 (kind / session / target / since / 直近 limit 件) を適用していた。
append-only で無制限に伸びる流れなので、件数が増えた時点で同型の負荷になる。

## 設計上の要点 (ここが壊れやすい)

絞り込みを SQL に押し下げると、**同じ規則が Python と SQL の 2 箇所に存在する**ことになる。
片方だけ直すと「一覧には出るのに SQL では落ちる (逆も)」という最悪の drift になり、しかも
手元の規模テストは偽カーソルが WHERE を解釈しないので **誤りが緑で通る**
(`tests/scale_contract.py` の docstring が明言)。

そこで「どの項目を・どの JSON パスで・どう比べるか」を `decision_event._WINDOW_FILTERS`
の 1 つの仕様表に置き、Python 述語と SQL 片の両方をそこから導く。このファイルは
**仕様表が窓ヘルパーの絞り込みを漏れなく覆っていること**を構造で固定する (覆っていないと
その項目が SQL で絞られず、LIMIT が効いた結果 **本来返るべき行が返らない**)。

## このファイルが pin するもの

1. 表が 10 万行でも Python に渡るのは `limit` 件まで (規模の契約)。
2. 絞り込み・並び・件数制限が SQL に出ている (押し下げたことの確認)。
3. 仕様表が窓ヘルパーの絞り込み項目を漏れなく覆っている (drift ガード + test-the-test)。
4. 仕様表の Python 側読み出しが、既存のアクセサ (`_row_session_id` / `_row_target_id`) と
   同じ値を返す (SQL 側の COALESCE 式と対になる契約)。
5. 生成される SQL 文字列そのもの (将来の変更が差分に出る)。
"""
from __future__ import annotations

import inspect
import json
import os
import sys

import pytest

_SERVER = os.path.join(os.path.dirname(__file__), "..", "server")
sys.path.insert(0, _SERVER)

import decision_event as de  # noqa: E402
from scale_contract import fake_rows, measure_rows_into_python  # noqa: E402


def _decision_rows(n: int) -> list:
    """判断記録の形をした偽行を n 件。created_at は昇順、kind は交互。"""
    return fake_rows(
        n, sk_prefix="dec",
        kind=lambda i: "dm-send" if i % 2 else "log-backstop",
        created_at=lambda i: f"2026-09-01T00:00:{i % 60:02d}.{i:06d}Z",
        decision_id=lambda i: f"dec-{i:08d}",
        decision="x",
    )


# ---------------------------------------------------------------------------
# 1. 規模の契約
# ---------------------------------------------------------------------------

def test_表が10万行でもlimit件しか読まない(monkeypatch):
    import mysql_client
    _, stat = measure_rows_into_python(
        monkeypatch, mysql_client,
        lambda: mysql_client.list_decision_events("p1", limit=100),
        table_rows=_decision_rows(100_000))
    assert stat["rows_into_python"] <= 100, (
        f"全件読みに戻っている (読んだ行数 {stat['rows_into_python']})")


def test_絞り込み付きでもlimit件しか読まない(monkeypatch):
    # session / target 絞りも SQL に押し下げてあるので、絞った read でも読み込み量は
    # limit 件で収まる (Python 側で絞る実装だと全行受け取ってしまう)。
    import mysql_client
    for kw in ({"kind": "log-backstop"}, {"session": "sv-1"},
               {"target": "ms-9"}, {"since": "2026-09-01T00:00:00Z"},
               {"exclude_kinds": de.NON_DECISION_KINDS}):
        _, stat = measure_rows_into_python(
            monkeypatch, mysql_client,
            lambda kw=kw: mysql_client.list_decision_events("p1", limit=50, **kw),
            table_rows=_decision_rows(100_000))
        assert stat["rows_into_python"] <= 50, (kw, stat["rows_into_python"])


# ---------------------------------------------------------------------------
# 2. 押し下げたことの確認
# ---------------------------------------------------------------------------

def test_絞り込みと並びと件数制限がSQLに出ている(monkeypatch):
    import mysql_client
    _, stat = measure_rows_into_python(
        monkeypatch, mysql_client,
        lambda: mysql_client.list_decision_events(
            "p1", kind="log-backstop", since="2026-09-01T00:00:00Z",
            session="sv-1", target="ms-9", limit=10),
        table_rows=_decision_rows(100))
    sql = " ".join(stat["queries"])
    assert "WHERE pk=%s" in sql
    for path in ("$.kind", "$.created_at", "$.who.session_id",
                 "$.related.target_id", "$.target_id"):
        assert path in sql, f"{path} が SQL に出ていない (Python 側で絞っている)"
    assert "ORDER BY" in sql and "DESC" in sql, "並びを SQL に寄せていない"
    assert "LIMIT %s" in sql, "件数制限を SQL に寄せていない"


# ---------------------------------------------------------------------------
# 3. 仕様表が窓ヘルパーを漏れなく覆っている (drift ガード)
# ---------------------------------------------------------------------------

def test_仕様表が窓ヘルパーの絞り込みを漏れなく覆う():
    """窓ヘルパーに絞り込みを足して仕様表に足し忘れると、その項目は SQL で絞られない。

    結果は「落ちる」ではなく **本来返るべき行が返らない**: SQL は絞らずに最新 limit 件を
    返し、そのあと Python 側だけが絞るので、絞られた分だけ結果が足りなくなる。沈黙する
    欠陥なので構造で固定する。
    """
    params = set(inspect.signature(de.window_decision_events).parameters)
    filters = params - {"rows", "limit"}
    spec = {name for name, _paths, _op in de.window_filter_spec()}
    assert filters == spec, (
        f"窓ヘルパーの絞り込み {sorted(filters)} と仕様表 {sorted(spec)} がズレている — "
        f"仕様表に無い項目は SQL で絞られず、LIMIT が効いた結果が足りなくなる")


def test_この仕様表ガードはズレで実際に赤くなる():
    # test-the-test: 片方に項目が増えた状態を合成して、上の比較が本当に差を検出するか。
    filters = {"kind", "session", "target", "since", "exclude_kinds", "new_filter"}
    spec = {"kind", "session", "target", "since", "exclude_kinds"}
    assert filters != spec, "集合比較が差を検出できていない"


# ---------------------------------------------------------------------------
# 4. 仕様表の Python 読み出しが既存アクセサと一致する
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row,expected", [
    ({"related": {"target_id": "ms-9"}}, "ms-9"),
    ({"target_id": "ms-7"}, "ms-7"),                      # top-level fallback
    ({"related": {"target_id": None}, "target_id": "ms-7"}, "ms-7"),  # JSON null
    ({}, ""),
])
def test_対象の読み出しが既存アクセサと一致する(row, expected):
    paths = next(p for n, p, _ in de.window_filter_spec() if n == "target")
    assert de._row_value(row, paths) == expected
    assert de._row_target_id(row) == expected, (
        "仕様表の読み出しと _row_target_id がズレている — SQL 側の COALESCE 式も"
        "同じ値を返す契約なので、ここがズレると 3 者が食い違う")


@pytest.mark.parametrize("row,expected", [
    ({"who": {"session_id": "sv-1"}}, "sv-1"),
    ({"who": {}}, ""),
    ({}, ""),
])
def test_セッションの読み出しが既存アクセサと一致する(row, expected):
    paths = next(p for n, p, _ in de.window_filter_spec() if n == "session")
    assert de._row_value(row, paths) == expected
    assert de._row_session_id(row) == expected


# ---------------------------------------------------------------------------
# 5. 生成 SQL の固定 (変更が差分に出る)
# ---------------------------------------------------------------------------

def test_既定readのSQLを固定する():
    tail, params = de.mysql_window_sql(limit=5,
                                       exclude_kinds=de.NON_DECISION_KINDS)
    assert tail == (
        " AND COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(data, '$.kind')), 'null'), '')"
        " NOT IN (%s)"
        " ORDER BY COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(data, '$.created_at')),"
        " 'null'), '') DESC, sk DESC LIMIT %s")
    assert params == ["dm-send", 5]


def test_種別明示時は除外集合から自分を引く():
    # window_decision_events の AND 合成と同じ規則 (「その種別を明示すれば引ける」)。
    tail, params = de.mysql_window_sql(kind="dm-send", limit=5,
                                       exclude_kinds=de.NON_DECISION_KINDS)
    assert "NOT IN" not in tail, "種別自身を引いていない — 明示しても引けなくなる"
    assert params == ["dm-send", 5]


def test_欠損とJSON_nullを空文字に落としている():
    # これを忘れると NULL NOT IN (...) が NULL になり、種別を持たない行が SQL でだけ
    # 静かに消える。式の形を固定して、COALESCE / NULLIF の省略を差分で見つける。
    tail, _ = de.mysql_window_sql(target="ms-9", limit=1)
    assert "COALESCE(" in tail and "NULLIF(" in tail and ", 'null')" in tail
