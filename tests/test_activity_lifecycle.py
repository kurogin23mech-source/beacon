"""Activity のライフサイクル動詞 (done / cancel / update) と status 整合 —
ms-139 e-4950.

営業の準備活動(会食・打診など)を、削除せずに『完了』『取消』『後追い更新』できる
ようにした。あわせて cancelled を Activity の正当な状態語彙に加え、cockpit の
「cancelled は未消化に出さない」除外前提との齟齬 (SPEC P6) を閉じる。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import sales_entities as se  # noqa: E402
import deadline  # noqa: E402
import work_model  # noqa: E402


def _data_with_activity(deadline_str="2026-08-07", ball="self"):
    data = {"opportunities": [{"id": "opp-1", "phase": "初回", "activities": []}]}
    aid = se.activity_add(data, "opp-1", "8/7会食", deadline=deadline_str,
                          who_has_the_ball=ball, created_at="2026-08-01T00:00:00Z")
    return data, aid


# --- status 語彙の整合 (P6) --------------------------------------------------

def test_valid_activity_status_includes_cancelled():
    # cancelled は activity_cancel が作る正当な terminal 状態。cockpit の除外前提と
    # 揃えるため vocabulary に含める。
    assert se.VALID_ACTIVITY_STATUS == {
        work_model.TODO_STATUS, work_model.DONE_STATUS, work_model.CANCELLED_STATUS}


def test_set_status_rejects_cancelled_routes_to_cancel():
    # set_status で直接 cancelled にはできない (監査印を伴うため activity_cancel 経由)。
    data, aid = _data_with_activity()
    try:
        se.activity_set_status(data, aid, "cancelled")
        assert False, "should have raised"
    except ValueError as e:
        assert "activity_cancel" in str(e)


def test_set_status_still_sets_done_and_todo():
    data, aid = _data_with_activity()
    se.activity_set_status(data, aid, "done", at="2026-08-09T00:00:00Z")
    act = se.find_activity(data, aid)[1]
    assert act["status"] == "done"
    assert act.get("meta", {}).get("done_by")  # 基底経由で done_by が刻まれる


# --- cancel ------------------------------------------------------------------

def test_activity_cancel_stamps_and_excludes_from_overdue():
    data, aid = _data_with_activity(deadline_str="2026-08-01")
    se.activity_cancel(data, aid, reason="やらないことにした")
    act = se.find_activity(data, aid)[1]
    assert act["status"] == "cancelled"
    assert act["meta"]["cancel_reason"] == "やらないことにした"
    # cancelled は terminal → 期日を過ぎても overdue に出ない (催促が止まる)。
    acts = data["opportunities"][0]["activities"]
    assert deadline.overdue_work_items(acts, "2026-08-20") == []


# --- update ------------------------------------------------------------------

def test_activity_update_changes_deadline_ball_description():
    data, aid = _data_with_activity()
    se.activity_update(data, aid, deadline="2026-08-15",
                       who_has_the_ball="counterpart", description="8/7会食(再調整)")
    act = se.find_activity(data, aid)[1]
    assert act["deadline"] == "2026-08-15"
    assert act["who_has_the_ball"] == "counterpart"
    assert act["description"] == "8/7会食(再調整)"


def test_activity_update_empty_is_no_change():
    data, aid = _data_with_activity(deadline_str="2026-08-07", ball="self")
    se.activity_update(data, aid, description="説明だけ変更")
    act = se.find_activity(data, aid)[1]
    assert act["deadline"] == "2026-08-07"       # 触っていないので不変
    assert act["who_has_the_ball"] == "self"
    assert act["description"] == "説明だけ変更"


def test_activity_update_rejects_bad_ball():
    data, aid = _data_with_activity()
    try:
        se.activity_update(data, aid, who_has_the_ball="nobody")
        assert False, "should have raised"
    except ValueError as e:
        assert "ball must be" in str(e)


def test_activity_update_unknown_id_raises():
    data, _ = _data_with_activity()
    try:
        se.activity_update(data, "act-999", deadline="2026-08-15")
        assert False, "should have raised"
    except ValueError as e:
        assert "not found" in str(e).lower()


# --- deadline を更新すると overdue 判定が即追従する --------------------------

def test_update_deadline_moves_out_of_overdue():
    # 期日超過の活動を未来日に付け替えると overdue から外れる (状態は派生なので即反映)。
    data, aid = _data_with_activity(deadline_str="2026-08-05")
    acts = data["opportunities"][0]["activities"]
    assert [it["id"] for it, _ in deadline.overdue_work_items(acts, "2026-08-09")] == [aid]
    se.activity_update(data, aid, deadline="2026-08-20")
    assert deadline.overdue_work_items(acts, "2026-08-09") == []
