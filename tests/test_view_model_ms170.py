"""ビュー用スキーマの単体テスト (ms-170 e-6342 / SPEC KIGmqQTIanqUypbtmrTs)。

このテストが守っているのは 1 つだけ: **盤の見た目を決めるデータに、どこから
読んだか (ローカル / クラウド) が漏れないこと**。

SPEC 受入条件 2「表示層のコードに取得元による分岐が 1 箇所も無い」は、表示層を
grep しても守られたことにならない (後から誰かが足せる)。守れるのは「そもそも
表示層に渡る形に取得元が入っていない」という構造だけなので、それをここで固定する。
"""

import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import view_model  # noqa: E402


def _dev_project(milestones=None):
    return {
        "name": "Beacon",
        "objective": "AI 開発の進捗を透明化する",
        "summary": "直近: ビューワーの変換層に着手",
        "profession": "dev",
        "milestones": milestones if milestones is not None else [],
    }


def _ms(id_, status="todo", entries=None):
    return {
        "id": id_,
        "status": status,
        "label": id_,
        "entries": entries if entries is not None else [],
    }


def _task(id_, status="todo"):
    return {"id": id_, "type": "task", "status": status, "description": id_}


class _FakeStore:
    """``Store`` プロトコルのうち、変換層が触る部分だけの替え玉。"""

    def __init__(self, data, *, cloud, project_id=""):
        self._data = data
        self._cloud = cloud
        self.project_id = project_id

    def load_project(self):
        return self._data

    def is_cloud(self):
        return self._cloud


class _PrivateIdStore(_FakeStore):
    """識別子を非公開属性でだけ持つクラウド替え玉 (実物の ``StoreApi`` と同じ形)。"""

    def __init__(self, data, project_id):
        super().__init__(data, cloud=True)
        del self.project_id
        self._project_id = project_id


# --- 中核: 取得元が変わっても盤は変わらない -------------------------------

def test_same_project_yields_identical_view_from_local_and_cloud():
    """同じプロジェクト辞書なら、ローカル由来でもクラウド由来でも
    ``source`` 以外は 1 バイト違わない。

    これが成立するのはデータ層 (``Store.load_project``) が両取得元で同じ形を
    返すからで、変換層が何かを揃えているからではない (SPEC 設計方針 1-2)。
    """
    data = _dev_project([
        _ms("ms-1", "done", [_task("e-1", "done")]),
        _ms("ms-2", "in_progress", [_task("e-2"), _task("e-3", "done")]),
    ])

    local = view_model.build_board_view(data, source=view_model.SOURCE_LOCAL)
    cloud = view_model.build_board_view(
        data, source=view_model.SOURCE_CLOUD, project_id="beacon-b95643")

    local_rest = {k: v for k, v in local.items() if k != "source"}
    cloud_rest = {k: v for k, v in cloud.items() if k != "source"}
    assert local_rest == cloud_rest

    # 違うのは出所ラベルだけ。
    assert local["source"] == {"kind": "local", "project_id": ""}
    assert cloud["source"] == {"kind": "cloud", "project_id": "beacon-b95643"}


def test_source_label_does_not_leak_into_any_other_field():
    """``source`` 以外のどこにも 'local' / 'cloud' / プロジェクト識別子が
    現れない。表示層が出所で分岐しようとしても、材料が無い状態を保つ。"""
    data = _dev_project([_ms("ms-1", "in_progress")])
    view = view_model.build_board_view(
        data, source=view_model.SOURCE_CLOUD, project_id="beacon-b95643")

    rest = {k: v for k, v in view.items() if k != "source"}
    blob = repr(rest)
    assert "beacon-b95643" not in blob
    assert "cloud" not in blob
    assert "local" not in blob


def test_store_wrapper_routes_both_backends_through_one_question():
    """``board_view_from_store`` は取得元を 1 箇所でだけ尋ねる。

    ローカル / クラウドの替え玉を通しても、出てくる盤は ``source`` 以外同一。
    """
    data = _dev_project([_ms("ms-1", "in_progress")])
    local = view_model.board_view_from_store(_FakeStore(data, cloud=False))
    cloud = view_model.board_view_from_store(
        _FakeStore(data, cloud=True, project_id="beacon-b95643"))

    assert {k: v for k, v in local.items() if k != "source"} \
        == {k: v for k, v in cloud.items() if k != "source"}
    assert local["source"]["kind"] == "local"
    assert cloud["source"]["kind"] == "cloud"
    # ローカルにプロジェクト識別子は無い (クラウド固有の概念を持ち込まない)。
    assert local["source"]["project_id"] == ""


def test_cloud_project_id_is_read_from_the_private_attribute_too():
    """実物の ``StoreApi`` は識別子を ``_project_id`` でだけ持つ。

    ``Store`` プロトコルに「自分がどのプロジェクトか」を尋ねる口が無いため、
    公開属性だけを見ると出所ラベルが空になる (実データで実際に空になった)。
    """
    view = view_model.board_view_from_store(
        _PrivateIdStore(_dev_project(), "beacon-b95643"))
    assert view["source"]["project_id"] == "beacon-b95643"


# --- 純粋性: 盤を見ただけで元データが変わらない ----------------------------

def test_build_does_not_mutate_input():
    data = _dev_project([_ms("ms-1", "in_progress", [_task("e-1")])])
    before = copy.deepcopy(data)
    view_model.build_board_view(data, source=view_model.SOURCE_LOCAL)
    assert data == before


def test_returned_detail_is_a_copy():
    """返した盤を表示層がいじっても、元のプロジェクト辞書に波及しない。"""
    data = _dev_project([_ms("ms-1", "in_progress")])
    view = view_model.build_board_view(data, source=view_model.SOURCE_LOCAL)
    if view["targets"]:
        view["targets"][0]["detail"]["progress"] = 999
    again = view_model.build_board_view(data, source=view_model.SOURCE_LOCAL)
    if again["targets"]:
        assert again["targets"][0]["detail"].get("progress") != 999


# --- 形の固定 ---------------------------------------------------------------

def test_top_level_shape_is_pinned():
    """表示層が依存する最上位のキーを固定する。増やすのは自由、消すのは版上げ。"""
    view = view_model.build_board_view(
        _dev_project(), source=view_model.SOURCE_LOCAL)
    assert set(view) == {
        "schema_version", "project", "progress", "targets",
        "deliverables", "documents", "sessions", "source",
    }
    assert view["schema_version"] == view_model.SCHEMA_VERSION
    assert view["project"]["name"] == "Beacon"
    assert view["project"]["profession"] == "dev"


def test_profession_defaults_to_dev_when_unset():
    """職種が書かれていないプロジェクトでも表示層が空文字を掴まない。"""
    data = _dev_project()
    del data["profession"]
    view = view_model.build_board_view(data, source=view_model.SOURCE_LOCAL)
    assert view["project"]["profession"] == "dev"


def test_target_row_shape_and_work_item_rollup():
    data = _dev_project([
        _ms("ms-1", "in_progress", [_task("e-1", "done"), _task("e-2")]),
    ])
    view = view_model.build_board_view(data, source=view_model.SOURCE_LOCAL)
    row = view["targets"][0]
    assert set(row) == {
        "id", "label", "status", "kind", "work_items",
        "is_done", "is_open", "detail",
    }
    assert row["id"] == "ms-1"
    # 消化数は入れ子に畳まれ、残数まで計算済みで渡る (表示層に算術をさせない)。
    assert set(row["work_items"]) == {"total", "done", "open"}
    assert row["work_items"]["open"] == \
        row["work_items"]["total"] - row["work_items"]["done"]
    assert row["work_items"]["open"] >= 0
    assert row["is_open"] is True
    assert row["is_done"] is False


def test_done_target_is_marked_done():
    data = _dev_project([_ms("ms-1", "done")])
    view = view_model.build_board_view(data, source=view_model.SOURCE_LOCAL)
    row = view["targets"][0]
    assert row["is_done"] is True
    assert row["is_open"] is False


def test_progress_counts_roll_up_over_targets():
    data = _dev_project([
        _ms("ms-1", "done"), _ms("ms-2", "in_progress"), _ms("ms-3", "todo"),
    ])
    view = view_model.build_board_view(data, source=view_model.SOURCE_LOCAL)
    assert view["progress"]["total"] == 3
    assert view["progress"]["done"] == 1
    assert view["progress"]["open"] == 2


# --- 盤に必要だがプロジェクト辞書に無いもの --------------------------------

def test_documents_are_normalised_from_either_backend_shape():
    """ローカルは前書き由来、クラウドは API 由来だが、同じキーで渡る。"""
    view = view_model.build_board_view(
        _dev_project(), source=view_model.SOURCE_LOCAL,
        documents=[
            {"doc_id": "d1", "title": "SPEC", "scope": "spec",
             "updated_at": "2026-09-09", "milestone": "ms-170"},
            {"id": "d2", "title": "全貌マップ", "scope": "core",
             "updated_at": "2026-09-01", "target": "ms-104"},
        ])
    docs = view["documents"]
    assert [d["id"] for d in docs] == ["d1", "d2"]
    # milestone / target のどちらで来ても、表示層は target だけを見ればよい。
    assert docs[0]["target"] == "ms-170"
    assert docs[1]["target"] == "ms-104"


def test_sessions_are_normalised_and_never_expose_raw_focus_nesting():
    view = view_model.build_board_view(
        _dev_project(), source=view_model.SOURCE_LOCAL,
        sessions=[{
            "session_id": "sv-77e8",
            "cwd": "/Users/x/tools/beacon",
            "actor": {"email": "a@example.com", "machine": "MACHINE-1"},
            "agent": {"kind": "claude-code"},
            "focus": {"milestone": {"id": "ms-121", "title": "バックオフィス"}},
            "live": True,
            "poll_health": {"healthy": True},
            "last_active": "2026-09-09T05:37:08Z",
        }])
    s = view["sessions"][0]
    assert s["id"] == "sv-77e8"
    assert s["who"] == "a@example.com"
    assert s["agent"] == "claude-code"
    assert s["target"] == "ms-121"
    assert s["target_label"] == "バックオフィス"
    assert s["live"] is True
    assert s["healthy"] is True


def test_absent_documents_and_sessions_are_empty_not_missing():
    """まだ取れていないものは空の一覧で渡す。表示層に有無の分岐をさせない。"""
    view = view_model.build_board_view(
        _dev_project(), source=view_model.SOURCE_LOCAL)
    assert view["documents"] == []
    assert view["sessions"] == []


def test_empty_project_still_produces_a_full_shape():
    """マイルストーンが 1 つも無い新規プロジェクトでも盤の形が崩れない。"""
    view = view_model.build_board_view(
        _dev_project(), source=view_model.SOURCE_LOCAL)
    assert view["targets"] == []
    assert view["progress"] == {"total": 0, "done": 0, "open": 0}
