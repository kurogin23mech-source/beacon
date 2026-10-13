"""優先度で「次の一手」を選ぶ — ms-166 e-6911.

この MS の主題 (配線はあるが実際には動かない) の典型例を 2 層で固める。

層 1: 直列点の serializer (``core.entries_to_json``)
    保存されているフィールドが機械可読面に出てくるか。e-6911 以前は手書きの
    許可リストで、名前の載っていないフィールドは黙って落ちていた。CLI・ローカル
    保存・SQLite 保存・サーバ 6 経路がこの 1 関数を通るので、落ちると全消費側で
    同時に落ちる。

層 2: 選び方 (``lib/next_action.py``)
    /beacon-session-start Step 2.9 規則 6 が文章で定めている順序を、実際に
    実行されるコードとして測る。規則が prompt にしか無い間は「お願い」であり、
    一度も働かなかった (2026-10-13 に実測)。

test-the-test: 本ファイルは「許可リストに戻すと赤くなる」「順序規則を壊すと
赤くなる」の両方を測った上で置いている。偽の緑を置かないため、境界 (0 件 /
priority 未設定 / terminal 状態) も赤に倒れる形で書く。
"""

import core
import next_action


def _task(tid, *, priority=None, status="todo", created_at="2026-01-01", **extra):
    entry = {"id": tid, "type": "task", "description": tid, "status": status,
             "created_at": created_at, "done_at": None, "meta": {}}
    if priority is not None:
        entry["meta"]["priority"] = priority
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# 層 1: serializer がフィールドを落とさない
# ---------------------------------------------------------------------------

class TestEntriesToJsonCarriesStoredFields:
    def test_priority_is_readable_top_level(self):
        """規則が読む面に priority が出る。

        e-6911 の核心。``meta.priority`` には昔から入っていたが、Step 2.9 が
        読むと書いてある top-level には出ていなかった。
        """
        out = core.entries_to_json([_task("e-1", priority="high")])[0]
        assert out["priority"] == "high"

    def test_meta_priority_is_kept_as_well(self):
        """既存の読み手 (``task show`` 等) が見ている場所を壊さない。"""
        out = core.entries_to_json([_task("e-1", priority="high")])[0]
        assert out["meta"]["priority"] == "high"

    def test_top_level_priority_wins_over_meta(self):
        """両方在る entry では top-level を正とする (二重真値を作らない)。"""
        entry = _task("e-1", priority="low")
        entry["priority"] = "highest"
        out = core.entries_to_json([entry])[0]
        assert out["priority"] == "highest"

    def test_a_field_nobody_listed_still_flows_through(self):
        """許可リスト方式そのものの回帰テスト (AC 4)。

        この名前はどこにも書かれていない。許可リストに戻すと落ちるので、
        「新しいフィールドを足すには serializer も直さねばならない」状態に
        逆戻りしたことが赤で分かる。
        """
        out = core.entries_to_json([_task("e-1", field_invented_by_this_test="x")])[0]
        assert out["field_invented_by_this_test"] == "x"

    def test_incident_fields_are_not_dropped(self):
        """許可リストが実際に落としていた具体例。

        incident entry の title / opened_at / resolution / resolved_at /
        linked_ms_task は e-6911 まで機械可読面に一度も出ていなかった
        (= 未解決 Incident を読む消費側が meta 以外から辿れなかった)。
        """
        incident = {"id": "e-9", "type": "incident", "description": "d",
                    "status": "open", "created_at": "2026-01-01", "done_at": None,
                    "title": "T", "opened_at": "2026-01-01T00:00:00Z",
                    "resolution": "R", "resolved_at": "2026-01-02T00:00:00Z",
                    "linked_ms_task": "e-5"}
        out = core.entries_to_json([incident])[0]
        for field in ("title", "opened_at", "resolution", "resolved_at",
                      "linked_ms_task"):
            assert out[field] == incident[field], field

    def test_empty_optional_fields_stay_absent(self):
        """空文字は「無い」と同じ扱い (e-6911 以前の挙動を保つ)。"""
        out = core.entries_to_json([_task("e-1", motivation="", behavior="b")])[0]
        assert "motivation" not in out
        assert out["behavior"] == "b"

    def test_child_entries_are_recursed_not_copied_raw(self):
        parent = _task("e-1", priority="high")
        parent["entries"] = [_task("e-2", priority="low")]
        out = core.entries_to_json([parent])[0]
        assert out["entries"][0]["priority"] == "low"
        assert out["entries"][0]["id"] == "e-2"

    def test_core_fields_are_present_even_when_unstored(self):
        """6 つの核フィールドは既定値つきで必ず出る (消費側が KeyError にならない)。"""
        out = core.entries_to_json([{"id": "e-1"}])[0]
        for field in core.ENTRY_JSON_ALWAYS:
            assert field in out, field
        assert out["status"] == "todo"


# ---------------------------------------------------------------------------
# 層 2: 選び方が優先度で並ぶ
# ---------------------------------------------------------------------------

class TestPriorityOrdering:
    def test_highest_wins_over_list_order(self):
        """規則 6 の第 1 節。並び順の先頭ではなく優先度で選ぶ。

        e-6911 以前に実際に起きていた挙動 (= 先頭が選ばれる) では e-first が
        返るので、このテストは壊れた状態で赤になる。
        """
        tasks = [_task("e-first"), _task("e-highest", priority="highest")]
        assert next_action.select_next_task(tasks)["id"] == "e-highest"

    def test_full_scale_orders_not_just_highest(self):
        """5 段階すべてで並ぶ (highest だけの特別扱いにしない)。"""
        tasks = [_task("e-lowest", priority="lowest"),
                 _task("e-medium", priority="medium"),
                 _task("e-highest", priority="highest"),
                 _task("e-low", priority="low"),
                 _task("e-high", priority="high")]
        assert [t["id"] for t in next_action.rank_open_tasks(tasks)] == [
            "e-highest", "e-high", "e-medium", "e-low", "e-lowest"]

    def test_in_progress_breaks_ties_within_a_priority(self):
        """規則 6 の第 2 節。同じ優先度なら着手済みが先。"""
        tasks = [_task("e-todo", priority="high"),
                 _task("e-doing", priority="high", status="in_progress")]
        assert next_action.select_next_task(tasks)["id"] == "e-doing"

    def test_priority_outranks_in_progress(self):
        """優先度が状態より強い (規則は priority を第 1 節に置いている)。"""
        tasks = [_task("e-doing-low", priority="low", status="in_progress"),
                 _task("e-todo-highest", priority="highest")]
        assert next_action.select_next_task(tasks)["id"] == "e-todo-highest"

    def test_untriaged_sorts_after_every_severity(self):
        """未 triage は severity ではない。人が low と判断した作業より後。"""
        tasks = [_task("e-untriaged", priority=core.UNTRIAGED_PRIORITY),
                 _task("e-lowest", priority="lowest")]
        assert next_action.select_next_task(tasks)["id"] == "e-lowest"

    def test_missing_priority_ranks_with_untriaged(self):
        a = next_action.priority_rank(None)
        b = next_action.priority_rank(core.UNTRIAGED_PRIORITY)
        assert a == b == len(core._PRIORITY_SCALE_ORDER)

    def test_deprecated_alias_is_normalized(self):
        """``middle`` は medium の旧名。別扱いして最後に落とさない。"""
        assert (next_action.priority_rank("middle")
                == next_action.priority_rank("medium"))

    def test_terminal_tasks_are_never_candidates(self):
        tasks = [_task("e-done", priority="highest", status="done"),
                 _task("e-cancelled", priority="highest", status="cancelled"),
                 _task("e-open", priority="lowest")]
        assert [t["id"] for t in next_action.rank_open_tasks(tasks)] == ["e-open"]

    def test_non_task_entries_are_ignored(self):
        """commit / pr / save は「次にやること」ではない。"""
        entries = [{"id": "e-c", "type": "commit", "status": "done"},
                   {"id": "e-p", "type": "pr", "status": "in_review"},
                   _task("e-t", priority="low")]
        assert [t["id"] for t in next_action.rank_open_tasks(entries)] == ["e-t"]

    def test_nested_tasks_are_found(self):
        parent = _task("e-parent", priority="low")
        parent["entries"] = [_task("e-child", priority="highest")]
        assert next_action.select_next_task([parent])["id"] == "e-child"

    def test_no_open_tasks_returns_none_not_a_stale_pick(self):
        """境界 (0 件)。緑のまま前回の候補を返す形にしない。"""
        assert next_action.select_next_task([]) is None
        assert next_action.select_next_task(
            [_task("e-done", status="done")]) is None

    def test_equal_keys_keep_stored_order(self):
        tasks = [_task("e-a", priority="high"), _task("e-b", priority="high")]
        assert [t["id"] for t in next_action.rank_open_tasks(tasks)] == ["e-a", "e-b"]

    def test_reads_priority_from_either_shape(self):
        """保存形 (meta) と JSON 形 (top-level) の両方で同じ順序になる。

        session-start は JSON を読み、他の呼び出し元は保存形を読む。片方だけ
        効く順序は「配線はあるが動かない」の再生産になる。
        """
        stored = [_task("e-low", priority="low"), _task("e-high", priority="high")]
        as_json = core.entries_to_json(stored)
        assert ([t["id"] for t in next_action.rank_open_tasks(stored)]
                == [t["id"] for t in next_action.rank_open_tasks(as_json)]
                == ["e-high", "e-low"])


class TestReasonTravelsWithThePick:
    def test_reason_names_the_priority(self):
        chosen = _task("e-1", priority="high")
        assert "high" in next_action.explain_choice(chosen)

    def test_reason_says_untriaged_rather_than_claiming_a_severity(self):
        chosen = _task("e-1", priority=core.UNTRIAGED_PRIORITY)
        reason = next_action.explain_choice(chosen)
        assert "triage" in reason

    def test_reason_mentions_in_progress(self):
        chosen = _task("e-1", priority="high", status="in_progress")
        assert "着手" in next_action.explain_choice(chosen)


class TestScaleOrderHasOneAddress:
    def test_next_action_does_not_define_its_own_severity_order(self):
        """順序の住所を 2 つにしない (= drift の作り方そのもの)。

        5 段階の順序は ``core._PRIORITY_SCALE_ORDER`` が唯一の持ち主。
        next_action.py が自前のリテラル順序表を持ったら赤にする。
        """
        import inspect
        src = inspect.getsource(next_action)
        for severity in core._PRIORITY_SCALE_ORDER:
            quoted = '"{0}"'.format(severity)
            single = "'{0}'".format(severity)
            assert quoted not in src and single not in src, (
                "next_action.py が severity 名 {0} をリテラルで持っている。"
                "順序は core._PRIORITY_SCALE_ORDER から読むこと".format(severity))
