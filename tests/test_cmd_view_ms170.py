"""`beacon view` の変換層入口 build_view の単体テスト (ms-170)。

**Go 一本化 (e-6518)**: Python 素朴盤 (serve / PAGE / host / expose / 起動出力) は
撤去され、盤 / 運用室の描画は Go 版 beacon-view が唯一の実装になった。よって serve の
HTTP 挙動・画面・待ち受け境界のテストはここから外し (それらの挙動は
tests/test_go_viewer_parity_ms170.py が Go 側で担う)、Python 側に残る唯一の盤ロジック =
``build_view`` (= ``beacon view --json`` の headless 経路) の不変条件だけを守る:

- 変換層が組み立てた形がそのまま出ること (schema / project / targets / progress)
- 取得元 (source) 以外に「ローカル / クラウド」の痕跡が漏れないこと (親 SPEC 受入条件 2)
- 同じデータならローカルでもクラウドでも中身が同一になること
- ドキュメント / 名簿が取れなくても盤は成立すること、名簿は差し替え可能なこと

ネットワークには出ない。替え玉の store を渡す。
"""

import os
import sys

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


# --- build_view = 変換層の出力そのもの (--json / headless 経路) ---------------

def test_build_view_is_the_view_schema():
    board = cmd_view.build_view(_FakeStore(), sessions=[])
    # 変換層が組み立てたものが、加工されずそのまま出ている。
    assert board["schema_version"] == view_model.SCHEMA_VERSION
    assert board["project"]["name"] == "Beacon"
    assert [t["id"] for t in board["targets"]] == ["ms-1", "ms-2"]
    assert board["progress"] == {"total": 2, "done": 1, "open": 1}


# --- 取得元が source 以外に漏れない (親 SPEC 受入条件 2) ----------------------

def test_board_carries_no_source_trace_outside_source():
    board = cmd_view.build_view(_FakeStore(cloud=True), sessions=[])
    rest = {k: v for k, v in board.items() if k != "source"}
    blob = repr(rest)
    assert "beacon-test" not in blob
    assert "cloud" not in blob


def test_local_and_cloud_produce_the_same_board():
    """同じデータなら、ローカルでもクラウドでも盤の中身 (source 以外) は同一。"""
    local = cmd_view.build_view(_FakeStore(cloud=False), sessions=[])
    cloud = cmd_view.build_view(_FakeStore(cloud=True), sessions=[])
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

    ここが固定できないと「盤を組み立てる」試験が実通信に依存する。
    """
    given = [{
        "session_id": "sv-1", "actor": {"email": "a@example.com"},
        "agent": {"kind": "claude-code"}, "live": True,
    }]
    view = cmd_view.build_view(_FakeStore(cloud=True), sessions=given)
    assert [s["id"] for s in view["sessions"]] == ["sv-1"]


# --- Python 素朴盤は撤去された (e-6518) — 退行ガード -------------------------

def test_python_simple_board_is_removed():
    """serve / PAGE / _handler_class 等の Python 素朴盤が復活していないこと。

    e-6518 で盤描画は Go 一本化した。これらが戻ると畳んだ二重メンテが再建される。
    """
    for gone in ("serve", "PAGE", "_handler_class", "_pick_port",
                 "_is_loopback"):
        assert not hasattr(cmd_view, gone), \
            f"撤去したはずの Python 素朴盤 {gone} が cmd_view に復活している"
