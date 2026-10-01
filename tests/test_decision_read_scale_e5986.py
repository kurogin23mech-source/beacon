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
import decision_derive as dd  # noqa: E402  (lib/ は conftest が path に載せる)
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


# ---------------------------------------------------------------------------
# 6. 独立レビュー採否で足した分 (ax A-1/A-2, 保守性 M-1/M-2)
# ---------------------------------------------------------------------------

def test_除外集合の算出が単一関数に集約されている():
    """保守性 M-1: 「kind 自身を引く」規則を Python 側と SQL 側に 2 回書かない。

    片方だけ直すと「一覧には出るのに SQL では落ちる (逆も)」という、このモジュールが
    警告しているまさにその drift を再生産する。
    """
    import ast
    src = open(os.path.join(_SERVER, "decision_event.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for fname in ("window_decision_events", "mysql_window_sql"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        called = {s.func.id for s in ast.walk(fn)
                  if isinstance(s, ast.Call) and isinstance(s.func, ast.Name)}
        assert "effective_exclude_kinds" in called, (
            f"{fname} が除外集合の算出を共有関数に通していない")
    # 式の逐語コピーが残っていないこと (差分でも気付けるように形で固定)
    assert src.count("- ({kind} if kind else frozenset())") == 1, (
        "除外集合の式が複製されている — 共有関数 1 箇所に畳むこと")


# 手書きの golden SQL (保守性 M-2)。
#
# テスト用評価器 (mysql_window_eval) は SQL 生成側と同じ式組み立て関数を共有するので、
# **式そのものにバグが入ると生成 SQL と評価器の両方に同じ形で現れ、緑で通る** (循環)。
# そこで主要な組合せについて SQL 文字列を **手で書いて** 固定し、式組み立てへの依存を
# 切る。ここが赤くなったら、生成側の式が変わったということ (意図的な変更なら golden を
# 更新する。評価器経由のテストだけでは気付けない次元)。
_GOLDEN_KIND = ("COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(data, '$.kind')), 'null'), '')")
_GOLDEN_CREATED = ("COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(data, '$.created_at')),"
                   " 'null'), '')")
_GOLDEN_SESSION = ("COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(data, '$.who.session_id')),"
                   " 'null'), '')")
_GOLDEN_TARGET = ("COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(data, '$.related.target_id')),"
                  " 'null'), NULLIF(JSON_UNQUOTE(JSON_EXTRACT(data, '$.target_id')),"
                  " 'null'), '')")


def test_全項目を指定したSQLを手書きで固定する():
    tail, params = de.mysql_window_sql(
        kind="log-backstop", session="sv-1", target="ms-9",
        since="2026-09-01T00:00:00Z", limit=7)
    assert tail == (
        f" AND {_GOLDEN_KIND} = %s"
        f" AND {_GOLDEN_SESSION} = %s"
        f" AND {_GOLDEN_TARGET} = %s"
        f" AND {_GOLDEN_CREATED} > %s"
        f" ORDER BY {_GOLDEN_CREATED} DESC, sk DESC LIMIT %s")
    assert params == ["log-backstop", "sv-1", "ms-9", "2026-09-01T00:00:00Z", 7]


def test_対象のSQLが2つのパスをこの順で見る():
    # 本文解決側 (_row_value) と同じ順序 (related.target_id → top-level target_id)。
    tail, _ = de.mysql_window_sql(target="ms-9", limit=1)
    assert "$.related.target_id" in tail
    assert tail.index("$.related.target_id") < tail.index("$.target_id"), (
        "fallback の順序が逆 — Python 側の読み出しと食い違う")


def test_明示した対象が台帳の接頭辞で検証される():
    """ax A-2: 明示指定が本文解決より緩いと、綴り違いが「成功」のまま書き込まれる。

    書かれた対象 id は `decision list --target <正しい id>` では二度と見つからず、
    この MS が直している「記録はあるのに辿れない」を再生産する。2 経路を同じガードへ。
    """
    import work_model as wm
    for ok in [f"{p}9" for p in wm.known_target_prefixes()]:
        assert dd.is_known_target_id(ok), ok
    for bad in ("e-123", "garbage", "ms-", "", "   ", "ms-9 と ms-10"):
        assert not dd.is_known_target_id(bad), bad


def test_明示した対象が不正なら記録せず終了する():
    import importlib
    import cmd_decision
    importlib.reload(cmd_decision)
    posted = []

    class _Client:
        def record_decision(self, pid, payload):
            posted.append(payload)
            return {"decision_id": "dec-1"}

    env = {"BEACON_DECISION_WHAT": "決めた",
           "BEACON_DECISION_EVIDENCE": "commit:abc1234",
           "BEACON_DECISION_RELATED_TARGET": "e-123",  # タスク id = 対象ではない
           "BEACON_DECISION_RATIONALE": "", "BEACON_DECISION_DECIDED_BY": "",
           "BEACON_DECISION_RELATED_TASK": "", "BEACON_DECISION_KIND": "",
           "BEACON_JSON": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        cmd_decision._is_cloud_mode = lambda: True
        cmd_decision._get_api_client = lambda: (_Client(), {"project_id": "p"})
        with pytest.raises(SystemExit) as exc:
            cmd_decision.cmd_decision_record()
        assert exc.value.code == 1
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert posted == [], "不正な対象のまま記録してしまっている"


def test_両CLIフロントのusageが固定の既定値を宣伝しない():
    """ax A-1: 既定値が環境に依存するようになったのに usage が旧値を見せていた。

    文脈ゼロの AI は usage を読んで「省略すれば autonomous-AI」と信じる。実際は
    セッション種別で human-delegated に化けうるので、省略した呼び出しが環境ごとに
    違う監査値を黙って書く。
    """
    root = os.path.join(os.path.dirname(__file__), "..")
    for rel in ("bin/beacon", "beacon_cli/dispatch.py"):
        body = open(os.path.join(root, rel), encoding="utf-8").read()
        assert "[--decided-by autonomous-AI]" not in body, (
            f"{rel} の usage が固定の既定値を宣伝している")
        assert "セッション種別から導出" in body, (
            f"{rel} の usage が導出である事実を示していない")
