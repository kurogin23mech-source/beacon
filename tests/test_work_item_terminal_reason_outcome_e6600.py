"""活動の終端遷移が理由と結果を運ぶ — 職種で分岐しない (ms-166 e-6600)。

経緯:
  開発の `task done` は理由を必須で受け、記録に `meta.done_reason` を残していた。
  営業の活動は同じ共通 seam (``occupation.set_entry_state``) を通って閉じるのに、
  ``sales_entities.activity_set_status`` に reason の引数が無かったため、理由は
  **その 1 関数で落ちていた**。seam もその下の primitive (``work_model.mark_done``) も
  reason を受け取れたので、鎖は呼び出しの両側で繋がっていて、ちょうどそこだけ切れていた。
  取消 (``activity_cancel``) は reason を受けるが任意で、理由なしの取消が並んでいた
  (= 誤起票なのか意図的にやめたのか読み手に分からない)。

この test が守る状態:
  (1) 終端遷移 (done / cancel) は理由を必須で受ける。規則は開発と**同一の実装**
      (``commands_shared._require_reason_or_skip``) で、営業用の二つ目の規則を作らない。
  (2) 遷移が outcome (= 何が得られたか) を記録に運ぶ。活動や task の説明文は計画時の
      まま凍結されるので、結果を別に残さないと完了した項目が自分の予言のまま読まれる。
  (3) 規則と語彙が work-item class 共通 — 開発の task と営業の活動が同じ meta キーを使う。

意図的に gate しないもの:
  ``todo`` への差し戻し (= 再開) は終端でないので gate しない。開発側も再開を gate して
  いないため、ここで gate すると「職種で分岐しない」を自分で破る。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
sys.path.insert(0, _LIB)

import core  # noqa: E402
import occupation  # noqa: E402
import sales_entities  # noqa: E402
import work_base  # noqa: E402
import work_model  # noqa: E402


# --------------------------------------------------------------------------
# (2) primitive が outcome を運ぶ
# --------------------------------------------------------------------------

def test_mark_done_records_reason_and_outcome() -> None:
    item = {"id": "w1", "description": "計画の文面", "status": "todo"}
    work_model.mark_done(item, at="T1", actor="me",
                         reason="なぜ閉じたか", outcome="何が得られたか")
    meta = item["meta"]
    assert meta["done_reason"] == "なぜ閉じたか"
    assert meta["done_outcome"] == "何が得られたか"


def test_stamp_cancel_records_reason_and_outcome() -> None:
    rec = {"id": "w2", "status": "todo"}
    work_base.stamp_cancel(rec, reason="誤起票", actor="me", at="T1",
                           outcome="同内容が別にあるため不要")
    meta = rec["meta"]
    assert meta["cancel_reason"] == "誤起票"
    assert meta["cancel_outcome"] == "同内容が別にあるため不要"


@pytest.mark.parametrize("fn,key", [
    (lambda rec: work_model.mark_done(rec, reason="r"), "done_outcome"),
    (lambda rec: work_base.stamp_cancel(rec, reason="r"), "cancel_outcome"),
])
def test_outcome_absent_writes_no_key(fn, key) -> None:
    """outcome 未指定なら key を作らない (空文字の項目を記録に生やさない)。"""
    rec = {"id": "w3", "status": "todo"}
    fn(rec)
    assert key not in rec.get("meta", {})


# --------------------------------------------------------------------------
# (3) 鎖が端から端まで繋がる + 両職種が同じ語彙を使う
# --------------------------------------------------------------------------

def _sales_fixture():
    data = {"name": "t", "profession": "sales", "milestones": []}
    acc = sales_entities.account_add(data, "A社")
    opp = sales_entities.opportunity_add(data, "商談", account_id=acc)
    return data, opp


def test_activity_done_threads_reason_and_outcome_to_record() -> None:
    """activity_set_status が reason/outcome を seam へ渡す (ここが切れていた)。"""
    data, opp = _sales_fixture()
    act_id = sales_entities.activity_add(data, opp, "初回打診のメールを送る")
    act = sales_entities.activity_set_status(
        data, act_id, "done", at="T1",
        reason="送信の証跡から完了と判断", outcome="返信あり、来週面談へ")
    meta = act.get("meta", {})
    assert meta.get("done_reason") == "送信の証跡から完了と判断"
    assert meta.get("done_outcome") == "返信あり、来週面談へ"


def test_activity_cancel_threads_outcome_to_record() -> None:
    data, opp = _sales_fixture()
    act_id = sales_entities.activity_add(data, opp, "誤って入れた活動")
    act = sales_entities.activity_cancel(
        data, act_id, reason="誤起票", outcome="同内容が別活動に在るため不要")
    meta = act.get("meta", {})
    assert meta.get("cancel_reason") == "誤起票"
    assert meta.get("cancel_outcome") == "同内容が別活動に在るため不要"


def test_dev_and_sales_done_use_the_same_meta_vocabulary() -> None:
    """開発の task と営業の活動が同じ key で理由と結果を残す (分岐していない)。"""
    data, opp = _sales_fixture()
    act_id = sales_entities.activity_add(data, opp, "営業の活動")
    act = sales_entities.activity_set_status(
        data, act_id, "done", at="T1", reason="r-sales", outcome="o-sales")

    dev = {"name": "d", "milestones": [
        {"id": "ms-1", "title": "m", "status": "in_progress", "entries": []}]}
    eid = core.task_add(dev, "ms-1", "開発タスク", priority="medium")
    _ms, entry = core.task_done(dev, eid, date="T1", reason="r-dev", outcome="o-dev")

    for key in ("done_reason", "done_outcome"):
        assert key in act.get("meta", {}), f"営業側に {key} が無い"
        assert key in entry.get("meta", {}), f"開発側に {key} が無い"


def test_set_entry_state_accepts_outcome() -> None:
    """共通 seam が outcome を受ける (職種固有の関数に outcome を生やさない)。"""
    import inspect
    sig = inspect.signature(occupation.set_entry_state)
    assert "outcome" in sig.parameters
    assert "reason" in sig.parameters


# --------------------------------------------------------------------------
# (1) CLI 層の規則が開発と同一
# --------------------------------------------------------------------------

@pytest.fixture
def sales_project():
    """理由ゲートを子プロセスで試すための一時プロジェクト。

    env を継承した子プロセスで回す: ゲートは BEACON_REASON / BEACON_ACKNOWLEDGE を
    読むので、親の env を汚さずに 1 ケース 1 プロセスで測る。
    """
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".beacon"), exist_ok=True)
        pf = os.path.join(tmp, ".beacon", "project.json")
        data, opp = _sales_fixture()
        ids = [sales_entities.activity_add(data, opp, f"活動{i}") for i in range(4)]
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        yield tmp, pf, ids


def _run_verb(tmp, pf, verb, env):
    e = dict(os.environ)
    for k in ("BEACON_REASON", "BEACON_ACKNOWLEDGE", "BEACON_OUTCOME"):
        e.pop(k, None)
    e.update(env)
    e["BEACON_PROJECT_FILE"] = pf
    e.pop("BEACON_CLOUD", None)
    e["BEACON_OPERATIONS_BACKEND"] = "local"
    return subprocess.run([sys.executable, os.path.join(_LIB, "commands.py"), verb],
                          capture_output=True, text=True, env=e, cwd=tmp)


@pytest.mark.parametrize("verb,env_extra", [
    ("activity_done", {"BEACON_ACT_STATUS": "done"}),
    ("activity_cancel", {}),
])
def test_terminal_transition_refuses_without_reason(sales_project, verb, env_extra) -> None:
    tmp, pf, ids = sales_project
    env = {"BEACON_ACT_ID": ids[0]}
    env.update(env_extra)
    result = _run_verb(tmp, pf, verb, env)
    assert result.returncode == 1, (
        f"{verb} は理由なしで通ってはいけない (開発の task done と同一規則)。\n"
        f"stdout:{result.stdout}\nstderr:{result.stderr}")
    assert "requires an audit entry" in result.stderr, result.stderr
    # 拒否されたら記録は一切変えない。
    data = json.load(open(pf, encoding="utf-8"))
    act = occupation.find_target_entry(data, ids[0])[2]
    assert act["status"] == "todo", "拒否されたのに状態が変わっている"


@pytest.mark.parametrize("verb,env_extra,want_status", [
    ("activity_done", {"BEACON_ACT_STATUS": "done"}, "done"),
    ("activity_cancel", {}, "cancelled"),
])
def test_terminal_transition_accepts_acknowledge(sales_project, verb, env_extra,
                                                 want_status) -> None:
    """理由を書かないと決めた場合は --acknowledge で明示する (空文字で黙って通さない)。"""
    tmp, pf, ids = sales_project
    env = {"BEACON_ACT_ID": ids[1], "BEACON_ACKNOWLEDGE": "1"}
    env.update(env_extra)
    result = _run_verb(tmp, pf, verb, env)
    assert result.returncode == 0, result.stderr
    data = json.load(open(pf, encoding="utf-8"))
    act = occupation.find_target_entry(data, ids[1])[2]
    assert act["status"] == want_status


def test_activity_done_records_reason_and_outcome_through_cli(sales_project) -> None:
    tmp, pf, ids = sales_project
    result = _run_verb(tmp, pf, "activity_done", {
        "BEACON_ACT_ID": ids[2], "BEACON_ACT_STATUS": "done",
        "BEACON_REASON": "送信の証跡から完了", "BEACON_OUTCOME": "返信あり"})
    assert result.returncode == 0, result.stderr
    data = json.load(open(pf, encoding="utf-8"))
    meta = occupation.find_target_entry(data, ids[2])[2].get("meta", {})
    assert meta.get("done_reason") == "送信の証跡から完了"
    assert meta.get("done_outcome") == "返信あり"


def test_reopen_to_todo_is_not_gated(sales_project) -> None:
    """再開 (todo) は終端でないので理由を要求しない — 開発側も gate していない。"""
    tmp, pf, ids = sales_project
    result = _run_verb(tmp, pf, "activity_done", {
        "BEACON_ACT_ID": ids[3], "BEACON_ACT_STATUS": "todo"})
    assert result.returncode == 0, result.stderr


def test_gate_is_the_shared_dev_helper_not_a_sales_copy() -> None:
    """規則は開発と同じ関数を呼ぶ。営業用に二つ目の規則を書いていないことを見る。

    文字列の一致ではなく、営業のハンドラが実際にその関数を呼んでいるかを構文木で見る
    (コメントや docstring に名前が出ているだけでは通さない)。
    """
    import ast
    src = open(os.path.join(_LIB, "commands.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    wanted = {"cmd_activity_done", "cmd_activity_cancel"}
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            calls = {
                n.func.id for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            seen[node.name] = calls
    assert wanted <= set(seen), f"ハンドラが見つからない: {wanted - set(seen)}"
    for name, calls in seen.items():
        assert "_require_reason_or_skip" in calls, (
            f"{name} が共有の理由ゲート _require_reason_or_skip を呼んでいない "
            f"(営業用の二つ目の規則を書くと職種で分岐する)")
