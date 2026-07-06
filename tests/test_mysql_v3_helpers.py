"""ms-96 v3 (entry-level split) の pure-Python helper unit tests。

_v3_assemble / _v3_decompose / _v3_sig / _v3_entry_sort_key の 4 helper を検証。
MySQL 接続は不要 (= dict / string 変換だけの純関数)。

apply_project_op_v3 / replace_project_v3 / get_project_v3 / save_project_v3 の
integration test は Phase 2d で MySQL テストコンテナか mocking 経由で追加。
"""
import os
import sys

import pytest

THIS = os.path.dirname(__file__)
SERVER = os.path.normpath(os.path.join(THIS, "..", "server"))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)


@pytest.fixture
def unified_project():
    """Sample v3 project unified shape (= hydrate 後、op に渡す形)。

    milestone 3 個、各 milestone に entries 数個、うち task には子 commit も入る
    (= 深さ 2 のツリー、iruca3 が実データで観察した shape)。
    """
    return {
        "project_id": "sample-p123",
        "name": "Sample",
        "summary": "テスト用の project",
        "owner": "uid-A",
        "schema_version": 3,
        "milestones": [
            {
                "id": "ms-1",
                "title": "First",
                "status": "done",
                "progress": 100,
                "entries": [
                    {"id": "e-1", "type": "task", "status": "done",
                     "created_at": "2026-05-01T00:00:00Z", "description": "first task"},
                    {"id": "e-2", "type": "commit", "status": "done",
                     "created_at": "2026-05-02T00:00:00Z", "description": "first commit"},
                ],
            },
            {
                "id": "ms-2",
                "title": "Second",
                "status": "in_progress",
                "progress": 50,
                "entries": [
                    {"id": "e-3", "type": "task", "status": "in_progress",
                     "created_at": "2026-05-10T00:00:00Z",
                     "description": "task with children",
                     "entries": [
                         {"id": "e-4", "type": "commit", "status": "done",
                          "created_at": "2026-05-11T00:00:00Z",
                          "description": "child commit 1"},
                         {"id": "e-5", "type": "commit", "status": "done",
                          "created_at": "2026-05-12T00:00:00Z",
                          "description": "child commit 2"},
                     ]},
                ],
            },
            {
                "id": "ms-10",
                "title": "Later (numeric sort test)",
                "status": "todo",
                "progress": 0,
                "entries": [],
            },
        ],
    }


def test_v3_decompose_splits_meta_ms_entries(unified_project):
    """decompose は meta / ms_map / entry_map の 3 分割を作る。

    meta 側に milestones[] は残らず、ms_map 側に entries[] は残らないこと。
    子 entries (= 深さ 2 の commit 群) は親 entry の data に JSON 内包で残ること。
    """
    from mysql_client import _v3_decompose, SCHEMA_V3_ENTRY

    meta, ms_map, entry_map = _v3_decompose(unified_project)

    # meta には milestones が含まれない、schema_version=3 が stamp される。
    assert "milestones" not in meta
    assert meta["schema_version"] == SCHEMA_V3_ENTRY
    assert meta["name"] == "Sample"
    assert meta["owner"] == "uid-A"

    # ms_map の各値には entries が含まれない (= 独立行に切り離された)。
    assert set(ms_map.keys()) == {"ms-1", "ms-2", "ms-10"}
    for ms_id, ms_data in ms_map.items():
        assert "entries" not in ms_data, f"ms {ms_id} still has entries[]"
        assert ms_data["id"] == ms_id
        assert "title" in ms_data

    # entry_map の sk は composite "{ms_id}#{entry_id}"、子 entries は JSON 内包。
    assert set(entry_map.keys()) == {
        "ms-1#e-1", "ms-1#e-2",
        "ms-2#e-3",  # e-4, e-5 は e-3 の子 entries[] 内包 (独立行を作らない)
    }
    parent_task = entry_map["ms-2#e-3"]
    assert parent_task["id"] == "e-3"
    # 子 entries は subtree JSON として残る (= iruca3 提案の要点)。
    assert isinstance(parent_task.get("entries"), list)
    assert {c["id"] for c in parent_task["entries"]} == {"e-4", "e-5"}


def test_v3_assemble_hydrates_back_to_unified_shape(unified_project):
    """decompose → assemble の round-trip が元 shape に戻ることを検証。

    子 entries[] は subtree 内包で保持されているので、assemble 側は entries[]
    テーブルの各行を「そのまま」milestone[i].entries[] に注入するだけで再構築できる。
    """
    from mysql_client import _v3_assemble, _v3_decompose

    meta, ms_map, entry_map = _v3_decompose(unified_project)

    # MySQL 行の shape に変換 (= [(sk, data), ...] タプル)。
    ms_rows = list(ms_map.items())
    entry_rows = list(entry_map.items())

    rebuilt = _v3_assemble(meta, ms_rows, entry_rows)

    # トップレベル meta が復元される。
    assert rebuilt["project_id"] == "sample-p123"
    assert rebuilt["name"] == "Sample"
    assert rebuilt["owner"] == "uid-A"
    assert rebuilt["schema_version"] == 3

    # milestones が数値順で並ぶ (= ms-1, ms-2, ms-10、辞書順 ms-1 < ms-10 < ms-2 ではなく)。
    assert [m["id"] for m in rebuilt["milestones"]] == ["ms-1", "ms-2", "ms-10"]

    # ms-1 の entries が created_at 昇順で復元される。
    ms1 = rebuilt["milestones"][0]
    assert [e["id"] for e in ms1["entries"]] == ["e-1", "e-2"]

    # ms-2 の entries: 親 e-3 が subtree として復元、子 e-4/e-5 は内包のまま。
    ms2 = rebuilt["milestones"][1]
    assert len(ms2["entries"]) == 1
    parent = ms2["entries"][0]
    assert parent["id"] == "e-3"
    assert [c["id"] for c in parent.get("entries", [])] == ["e-4", "e-5"]

    # ms-10 は entries 空。
    ms10 = rebuilt["milestones"][2]
    assert ms10["entries"] == []


def test_v3_sig_stable_for_equivalent_dicts():
    """_v3_sig は key の並び順が違っても同じ dict は同じ hash を返す (= sort_keys)。

    diff 検出の基盤なので stability が命 (= 「変わってないのに書き直す」を防ぐ)。
    """
    from mysql_client import _v3_sig

    a = {"id": "ms-1", "title": "X", "status": "done"}
    b = {"status": "done", "title": "X", "id": "ms-1"}  # 同じ内容、順序違い

    assert _v3_sig(a) == _v3_sig(b)


def test_v3_sig_changes_when_content_differs():
    from mysql_client import _v3_sig

    a = {"id": "ms-1", "status": "todo"}
    b = {"id": "ms-1", "status": "done"}

    assert _v3_sig(a) != _v3_sig(b)


def test_v3_entry_sort_key_orders_by_created_at_then_id():
    """entries は created_at 昇順、tie は id 文字列比較。created_at 欠落は末尾。"""
    from mysql_client import _v3_entry_sort_key

    e_early = {"id": "e-100", "created_at": "2026-01-01T00:00:00Z"}
    e_late = {"id": "e-2", "created_at": "2026-02-01T00:00:00Z"}
    e_no_ts = {"id": "e-99"}  # created_at 欠落 → 末尾

    ordered = sorted([e_late, e_early, e_no_ts], key=_v3_entry_sort_key)
    assert [e["id"] for e in ordered] == ["e-100", "e-2", "e-99"]


def test_v3_decompose_drops_empty_or_invalid_ids():
    """id 欠落の milestone / entry は暗黙に skip される (= 不正 shape 対策)。"""
    from mysql_client import _v3_decompose

    bad = {
        "milestones": [
            {"id": "ms-1", "title": "OK", "entries": [
                {"id": "e-1", "description": "OK"},
                {"description": "no id, dropped"},  # id 欠落
            ]},
            {"title": "no id milestone, dropped"},  # ms id 欠落
        ],
    }
    meta, ms_map, entry_map = _v3_decompose(bad)
    assert list(ms_map.keys()) == ["ms-1"]
    assert list(entry_map.keys()) == ["ms-1#e-1"]


def test_v3_assemble_defends_against_legacy_milestones_in_meta():
    """meta 側に古い "milestones" residue が残っていても防御的に落とす。

    v1 → v3 直行 migration のような path で、projects 行の data に旧
    milestones[] が残っているケースを想定した防御。 assemble 出力は
    subcollection 由来の milestones[] のみを持つべき。
    """
    from mysql_client import _v3_assemble

    meta_with_residue = {
        "project_id": "p",
        "name": "N",
        "milestones": [  # residue: 無視されるべき
            {"id": "OLD-msX", "title": "should not appear"},
        ],
    }
    ms_rows = [("ms-1", {"id": "ms-1", "title": "Fresh"})]
    entry_rows: list = []

    result = _v3_assemble(meta_with_residue, ms_rows, entry_rows)
    assert [m["id"] for m in result["milestones"]] == ["ms-1"]  # residue 消える
    assert result["milestones"][0]["title"] == "Fresh"
