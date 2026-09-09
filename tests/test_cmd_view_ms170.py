"""`beacon view` の単体テスト (ms-170 e-6344 / SPEC KIGmqQTIanqUypbtmrTs)。

守っているもの:
- 手元起動の受け口が、盤の JSON と画面を返し、知らない道は 404 で断ること
- 待ち受けが **自分の機械の中だけ** に閉じていること (手元起動で外に開かない)
- 表示層が取得元を知らないこと — 画面のコードに「ローカルなら / クラウドなら」の
  分岐が無く、盤の JSON にも source 以外の痕跡が無いこと (親 SPEC 受入条件 2)

ネットワークには出ない。替え玉の store を渡して受け口だけを立てる。
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_view  # noqa: E402
import view_model  # noqa: E402


def _project():
    return {
        "name": "Beacon",
        "objective": "AI 開発の進捗を透明化する",
        "summary": "直近: 手元ビューワーに着手",
        "profession": "dev",
        "milestones": [
            {"id": "ms-1", "status": "done", "label": "ms-1", "entries": []},
            {"id": "ms-2", "status": "in_progress", "label": "ms-2",
             "entries": [{"id": "e-1", "type": "task", "status": "todo",
                          "description": "e-1"}]},
        ],
    }


class _FakeStore:
    def __init__(self, *, cloud=False, docs=None):
        self._cloud = cloud
        self._docs = docs if docs is not None else []
        self.project_id = "beacon-test" if cloud else ""

    def load_project(self):
        return _project()

    def is_cloud(self):
        return self._cloud

    def list_documents(self):
        return self._docs


class _ServedBoard:
    """受け口を立てて、終わったら必ず畳む入れ物。

    ``sessions`` は既定で空を渡す。省略すると名簿を取りに行って **実際の通信が
    発生する** ため、試験がネットワークと本番データに依存してしまう (この落とし穴は
    実際に踏んで、``build_view`` に差し替え口を足す設計変更につながった)。
    """

    def __init__(self, store, sessions=()):
        self._store = store
        self._sessions = list(sessions)

    def __enter__(self):
        self._server, self.url = cmd_view.serve(
            port=0, open_browser=False, store=self._store, forever=False,
            sessions=self._sessions)
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def get(self, path=""):
        with urllib.request.urlopen(self.url + path) as r:
            return r.status, r.read().decode("utf-8")


# --- 受け口の振る舞い -------------------------------------------------------

def test_board_json_is_the_view_schema():
    with _ServedBoard(_FakeStore()) as s:
        status, body = s.get("api/board")
    assert status == 200
    board = json.loads(body)
    # 変換層が組み立てたものが、加工されずそのまま出ている。
    assert board["schema_version"] == view_model.SCHEMA_VERSION
    assert board["project"]["name"] == "Beacon"
    assert [t["id"] for t in board["targets"]] == ["ms-1", "ms-2"]
    assert board["progress"] == {"total": 2, "done": 1, "open": 1}


def test_root_serves_the_page():
    with _ServedBoard(_FakeStore()) as s:
        status, body = s.get("")
    assert status == 200
    assert "<title>Beacon 盤</title>" in body
    # 画面は盤を自分で取りに行く (埋め込みではない = 再読み込みで最新が出る)。
    assert "/api/board" in body


def test_unknown_path_is_404():
    with _ServedBoard(_FakeStore()) as s:
        try:
            s.get("nope")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        else:
            raise AssertionError("404 になっていない")


# --- 手元起動の境界 ---------------------------------------------------------

def test_listens_on_loopback_only():
    """手元起動は自分の機械の中だけに閉じる。外に開いたら事故になる。"""
    store = _FakeStore()
    server, url = cmd_view.serve(
        port=0, open_browser=False, store=store, forever=False, sessions=[])
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert url.startswith("http://127.0.0.1:")
    finally:
        server.shutdown()
        server.server_close()


def test_port_falls_back_when_preferred_is_busy():
    """既定の口が塞がっていても、空きを借りて立ち上がる (併走できる)。"""
    first, url1 = cmd_view.serve(
        port=0, open_browser=False, store=_FakeStore(), forever=False,
        sessions=[])
    try:
        busy = first.server_address[1]
        second, url2 = cmd_view.serve(
            port=busy, open_browser=False, store=_FakeStore(), forever=False,
            sessions=[])
        try:
            assert second.server_address[1] != busy
        finally:
            second.shutdown()
            second.server_close()
    finally:
        first.shutdown()
        first.server_close()


# --- 表示層が取得元を知らない (親 SPEC 受入条件 2) --------------------------

def test_page_has_no_source_branching():
    """画面のコードに『ローカルなら / クラウドなら』の分岐が無い。

    ここが破られると、畳もうとしている二重メンテが表示層の中に再建される。
    """
    page = cmd_view.PAGE
    # 出所は見出しに表示するだけ (source.kind をそのまま出す) で、
    # 値を比較して描き分けてはいけない。
    for forbidden in ('=== "cloud"', "=== 'cloud'", '== "cloud"',
                      '=== "local"', "=== 'local'", '== "local"',
                      'is_cloud', 'isCloud'):
        assert forbidden not in page, f"表示層に取得元の分岐がある: {forbidden}"


def test_board_json_carries_no_source_trace_outside_source():
    with _ServedBoard(_FakeStore(cloud=True)) as s:
        _, body = s.get("api/board")
    board = json.loads(body)
    rest = {k: v for k, v in board.items() if k != "source"}
    blob = repr(rest)
    assert "beacon-test" not in blob
    assert "cloud" not in blob


def test_local_and_cloud_serve_the_same_board():
    """同じデータなら、ローカル起動でもクラウド起動でも盤の中身は同一。"""
    with _ServedBoard(_FakeStore(cloud=False)) as s:
        local = json.loads(s.get("api/board")[1])
    with _ServedBoard(_FakeStore(cloud=True)) as s:
        cloud = json.loads(s.get("api/board")[1])
    assert {k: v for k, v in local.items() if k != "source"} \
        == {k: v for k, v in cloud.items() if k != "source"}


# --- 壊れていても盤は出す ---------------------------------------------------

def test_documents_failure_does_not_break_the_board():
    """ドキュメントが取れなくても盤は出す (見えないことより出ないほうが困る)。"""

    class _BrokenDocs(_FakeStore):
        def list_documents(self):
            raise RuntimeError("読めません")

    view = cmd_view.build_view(_BrokenDocs(), sessions=[])
    assert view["documents"] == []
    assert len(view["targets"]) == 2


def test_local_store_has_no_session_roster():
    """ローカルには他セッションの名簿が無い。空で通す (ms-171 の領域)。"""
    view = cmd_view.build_view(_FakeStore(cloud=False))
    assert view["sessions"] == []


def test_sessions_can_be_injected_so_the_board_does_not_reach_the_network():
    """名簿は差し替えられる。

    ここが固定できないと「盤を組み立てる」試験が実通信に依存する。実際、最初の
    実装では替え玉の store を渡しても本番のセッション名簿を拾ってしまい、
    ローカル / クラウドの盤が一致しない形で表面化した。
    """
    given = [{
        "session_id": "sv-1", "actor": {"email": "a@example.com"},
        "agent": {"kind": "claude-code"}, "live": True,
    }]
    view = cmd_view.build_view(_FakeStore(cloud=True), sessions=given)
    assert [s["id"] for s in view["sessions"]] == ["sv-1"]


# --- サーバ設置 (e-6345) ---------------------------------------------------

def test_default_stays_loopback_without_host():
    """既定は今まで通り自分の機械の中だけ。設置機能を足しても変わらない。"""
    server, url = cmd_view.serve(
        port=0, open_browser=False, store=_FakeStore(), forever=False,
        sessions=[])
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()


def test_exposing_without_acknowledgement_is_refused():
    """外に開くには明示が要る。盤は認証を持たないので黙って公開させない。"""
    try:
        cmd_view.serve(port=0, open_browser=False, store=_FakeStore(),
                       forever=False, sessions=[], host="0.0.0.0")
    except ValueError as e:
        # 何が起きるかと、どうすればよいかが文面に出ていること。
        assert "全員が盤を読めます" in str(e)
        assert "--expose" in str(e)
    else:
        raise AssertionError("外に開く指定が素通りした")


def test_exposing_with_acknowledgement_binds_to_the_given_host():
    server, url = cmd_view.serve(
        port=0, open_browser=False, store=_FakeStore(), forever=False,
        sessions=[], host="0.0.0.0", expose=True)
    try:
        assert server.server_address[0] == "0.0.0.0"
    finally:
        server.shutdown()
        server.server_close()


def test_localhost_is_treated_as_loopback():
    """名前で書いても自分の機械の中なら明示は要らない。"""
    server, url = cmd_view.serve(
        port=0, open_browser=False, store=_FakeStore(), forever=False,
        sessions=[], host="localhost")
    try:
        assert url.startswith("http://localhost:")
    finally:
        server.shutdown()
        server.server_close()


def test_server_install_serves_the_identical_page():
    """設置モードでも表示層は同一。ここが違ったら『同じ画面』が嘘になる。"""
    local, _ = cmd_view.serve(port=0, open_browser=False, store=_FakeStore(),
                              forever=False, sessions=[])
    exposed, _ = cmd_view.serve(port=0, open_browser=False,
                                store=_FakeStore(cloud=True), forever=False,
                                sessions=[], host="0.0.0.0", expose=True)
    try:
        a = urllib.request.urlopen(
            f"http://127.0.0.1:{local.server_address[1]}/").read()
        b = urllib.request.urlopen(
            f"http://127.0.0.1:{exposed.server_address[1]}/").read()
        assert a == b  # 1 バイトも違わない
    finally:
        for s in (local, exposed):
            s.shutdown()
            s.server_close()
