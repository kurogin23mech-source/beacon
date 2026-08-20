"""件数が増えても読み込む量が増えないことの検証 (e-5370)。

契約テスト (何を返すか) とは別の軸で、**どれだけ読むか** を測る。2026-08-20 の
本番停止は、契約は正しいのに読む量が件数に比例する実装で起きた。詳細と使い方は
scale_contract.py の docstring を参照。

新しく list 系を足すときは、ここに 1 本足す。
"""
import os
import sys

import pytest

THIS = os.path.dirname(__file__)
SERVER = os.path.normpath(os.path.join(THIS, "..", "server"))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)
if THIS not in sys.path:
    sys.path.insert(0, THIS)

import mysql_client as mc          # noqa: E402
from scale_contract import fake_rows, measure_rows_into_python  # noqa: E402

HUGE = 100_000
SINCE = "2026-01-01T00:00:00.000000Z"


def test_バス取得は表が10万行でもlimit件しか読まない(monkeypatch):
    """本番停止の主因そのもの。従来は project の全イベントを読んでいた
    (beacon-b95643 で 98,943 件 / 72MB を毎秒 json.loads)。"""
    rows = fake_rows(HUGE, created_at=lambda i: f"2026-06-01T00:00:{i % 60:02d}.000000Z")
    _res, stat = measure_rows_into_python(
        monkeypatch, mc,
        lambda: mc.list_bus_events("p1", since=SINCE, channel="dm", limit=100),
        table_rows=rows)
    assert stat["rows_into_python"] <= 100, (
        f"表 {HUGE} 行に対し {stat['rows_into_python']} 行を Python へ読み込んでいる")


def test_バス取得は絞り込みをSQLへ押し下げている(monkeypatch):
    """『limit を付けただけで、絞り込みは Python 側』を防ぐ。"""
    rows = fake_rows(10)
    _res, stat = measure_rows_into_python(
        monkeypatch, mc,
        lambda: mc.list_bus_events("p1", since=SINCE, channel="dm", limit=100),
        table_rows=rows)
    sql = " ".join(stat["queries"]).upper()
    assert "WHERE" in sql and "LIMIT" in sql
    assert "ORDER BY" in sql, "並べ替えも DB 側で行うこと"


def test_重複チェックは表が10万行でも1件しか読まない(monkeypatch):
    """e-5369。窓を推測して走査するのではなく名指しで引く。"""
    rows = fake_rows(HUGE, client_event_id=lambda i: f"ce-{i}")
    _res, stat = measure_rows_into_python(
        monkeypatch, mc,
        lambda: mc.find_bus_event_by_client_id("p1", "ce-99999", channel="dm"),
        table_rows=rows)
    assert stat["rows_into_python"] <= 1, (
        f"1 件引くだけで {stat['rows_into_python']} 行を読み込んでいる")


def test_変更履歴は表が10万行でもlimit件しか読まない(monkeypatch):
    """incremental polling を謳う経路。since があるのに全件読んでいないこと。"""
    rows = fake_rows(HUGE, ts=lambda i: f"2026-06-01T00:00:{i % 60:02d}.000000Z")
    _res, stat = measure_rows_into_python(
        monkeypatch, mc,
        lambda: mc.list_changelog("p1", since=SINCE, limit=100),
        table_rows=rows)
    assert stat["rows_into_python"] <= 100, (
        f"表 {HUGE} 行に対し {stat['rows_into_python']} 行を Python へ読み込んでいる")


def test_セッション一覧は他プロジェクトを読まない(monkeypatch):
    """件数上限を持たない経路なので測る性質が違う。1 プロジェクト分に閉じて
    いれば正しく、表全体には比例しない (WHERE pk=... で閉じていること)。"""
    rows = fake_rows(50)
    _res, stat = measure_rows_into_python(
        monkeypatch, mc, lambda: mc.list_sessions("p1"), table_rows=rows)
    sql = " ".join(stat["queries"]).upper()
    assert "WHERE PK=" in sql.replace(" ", "").replace("WHEREPK=", "WHERE PK=") or "PK=%S" in sql, \
        "プロジェクトで絞らずに表全体を走査している"
