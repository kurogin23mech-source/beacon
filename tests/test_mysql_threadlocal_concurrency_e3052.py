"""e-3052 — MySQL 接続の thread-local 分離の回帰テスト + 接続数観測。

背景: ms-96 の VPS cutover 後、uvicorn が sync endpoint を anyio スレッドプールで
並行実行するため、1 本のグローバル PyMySQL 接続を全スレッドで共有すると、あるスレッド
がソケットを読んでいる最中に別スレッドが操作して socket が None になり
`'NoneType' object has no attribute 'settimeout'` で 500/ハングする並行破損が起きた。
PR #349 で接続を **thread-local** 化して解消した。

このテストはその修正が **構造的に維持されているか** を DB 無しで検証する:
  - N スレッドが `_conn()` を呼ぶと各スレッドが別々の接続を得る (= thread-local)。
    もし誰かが module-global 共有に戻したら、多くのスレッドが同じ接続を掴むので
    distinct 数が N を下回り、このテストが fail する (= 回帰の forcing function)。
  - 同一スレッドの再呼び出しは同じ接続を再利用する (= 無駄な張り直しをしない)。
  - `connection_stats()` が live / 累計 open を観測できる (= AC2 の総接続数観測)。

pymysql / 実 MySQL は不要。`_connect` を fake に差し替えて接続の同一性だけを見る。
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import mysql_client


class _FakeConn:
    """最小の PyMySQL 接続スタブ (ping/close だけ)。"""

    def __init__(self, serial: int):
        self.serial = serial
        self.closed = False

    def ping(self, reconnect: bool = True):
        if self.closed:
            raise RuntimeError("connection is dead")

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch):
    # pymysql が入っていない環境でも _conn() が RuntimeError を投げないよう、
    # 非 None のセンチネルを差し込む (実際の接続は _connect の fake が返す)。
    monkeypatch.setattr(mysql_client, "pymysql", object())

    counter = {"n": 0}
    lock = threading.Lock()

    def _fake_connect():
        with lock:
            counter["n"] += 1
            serial = counter["n"]
        return _FakeConn(serial)

    monkeypatch.setattr(mysql_client, "_connect", _fake_connect)

    # 接続レジストリを毎テスト初期化 (プロセス跨ぎの相互汚染を防ぐ)。
    with mysql_client._CONN_LOCK:
        mysql_client._LIVE_CONNS.clear()
        mysql_client._OPENED_TOTAL = 0
    # 主スレッドの thread-local 接続もクリア (= 前テストの接続を継承しない)。
    # ワーカースレッドは新規なので各々 None から始まる。
    if hasattr(mysql_client._LOCAL, "conn"):
        del mysql_client._LOCAL.conn
    yield
    with mysql_client._CONN_LOCK:
        mysql_client._LIVE_CONNS.clear()
        mysql_client._OPENED_TOTAL = 0
    if hasattr(mysql_client._LOCAL, "conn"):
        del mysql_client._LOCAL.conn


def test_each_thread_gets_a_distinct_connection():
    """N スレッド並行で _conn() を呼ぶと、各スレッドが別々の接続を得る。

    これが thread-local 分離の核心。module-global 共有に戻すと distinct < N になり
    fail する (= PR #349 回帰の検知)。
    """
    n = 16
    conns: dict[int, object] = {}
    conns_lock = threading.Lock()
    barrier = threading.Barrier(n)  # 全スレッドを同時に走らせて並行性を最大化

    def worker(i: int):
        barrier.wait()
        c = mysql_client._conn()
        with conns_lock:
            conns[i] = c

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(conns) == n
    distinct = {id(c) for c in conns.values()}
    assert len(distinct) == n, (
        f"expected {n} distinct thread-local connections, got {len(distinct)} "
        "— 接続がスレッド間で共有されている (PR #349 の thread-local 化が壊れた疑い)"
    )

    stats = mysql_client.connection_stats()
    assert stats["backend"] == "mysql"
    assert stats["live_connections"] == n
    assert stats["opened_total"] >= n


def test_same_thread_reuses_its_connection():
    """同一スレッドの再呼び出しは同じ接続を返し、追加で張らない。"""
    c1 = mysql_client._conn()
    c2 = mysql_client._conn()
    assert c1 is c2
    stats = mysql_client.connection_stats()
    assert stats["live_connections"] == 1
    assert stats["opened_total"] == 1  # 2 回目は再利用 (ping のみ、open は増えない)


def test_dead_connection_is_reopened_and_counted():
    """ping が落ちた接続は張り直され、累計 open が増える (live は増えない)。"""
    c1 = mysql_client._conn()
    c1.close()  # 次の _conn() で ping が失敗 → 張り直し
    c2 = mysql_client._conn()
    assert c2 is not c1
    stats = mysql_client.connection_stats()
    assert stats["live_connections"] == 1   # 同一スレッドなので live は 1 のまま
    assert stats["opened_total"] == 2        # 累計 open は 2 (再接続を記録)


def test_connection_stats_shape_when_idle():
    """接続を張る前でも connection_stats は例外を投げず 0 を返す (fail-safe)。"""
    stats = mysql_client.connection_stats()
    assert stats == {"backend": "mysql", "live_connections": 0, "opened_total": 0}
