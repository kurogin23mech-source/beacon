"""ms-154 e-5650 — closing a task records a task-done decision.

The audit gap this closes: the CLI marks a task done locally and syncs via a
whole-document PUT, so it never reaches the server ``done_entry`` route where the
decision-arm recording lives. The most audit-critical decision (the AI's own done
judgment) was therefore captured for Web-UI dones only.

**ms-166 e-6997 — どこが書くかが変わった。** 当初この記録は CLI の handler
(``cmd_task._record_task_done_decision``) が書いていた。ところが同じ状態 (タスクが
done) に到達する経路はもう 1 つあり、``beacon pr approve`` の自動完了は
``core.task_done`` を直に呼ぶので **記録を 1 件も残さなかった** (本番の直近 300 件で
task-done 39 件のうち PR 承認由来は 0 件 —— 独立 judge 実測)。そこで記録を
**全ての経路が通る唯一の場所** = データ層の ``core.task_done`` → ``decision_outbox``
に移した。書き込みは保存後の flush で、保存後の data でそのタスクが本当に done かを
述語で確認してから走る。

この module が測る中身 (帰属が本当に捕まる / 根拠は実 link だけ / local mode では
書かない / 失敗は flow を壊さない) は e-5650 のまま。**測る先だけ** 新しい継ぎ目に
移した (outbox に積み → flush)。経路網羅そのものは
tests/test_task_done_decision_seam_e6997.py が見る。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402
import commands_shared  # noqa: E402
import decision_outbox  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_outbox():
    decision_outbox.discard()
    yield
    decision_outbox.discard()


def _project():
    return {"name": "p", "summary": "", "milestones": [
        {"id": "ms-9", "title": "t", "status": "in_progress", "progress": 0,
         "entries": [{"id": "e-1", "type": "task", "status": "todo",
                      "description": "a", "date": ""},
                     {"id": "e-2", "type": "task", "status": "todo",
                      "description": "b", "date": ""}]}]}


def _close_and_flush(entry_id, reason, *, decided_by="", evidence=None):
    """閉じて保存後の flush まで走らせる (= 実際の書き込み経路をそのまま通す)。"""
    data = _project()
    core.task_done(data, entry_id, reason=reason, decided_by=decided_by,
                   evidence=evidence)
    return decision_outbox.flush(data)


class _FakeClient:
    def __init__(self):
        self.posted = []

    def record_decision(self, project_id, decision):
        self.posted.append((project_id, decision))
        return {"decision_id": "dec-9", "kind": decision["kind"]}


def _clear_env(monkeypatch):
    for k in ("BEACON_DECIDED_BY", "BEACON_DONE_EVIDENCE", "BEACON_ACTOR"):
        monkeypatch.delenv(k, raising=False)


def test_records_task_done_decision_in_cloud(monkeypatch):
    fake = _FakeClient()
    _clear_env(monkeypatch)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    _close_and_flush("e-1", "AC 全達成と判断")
    assert len(fake.posted) == 1
    pid, rec = fake.posted[0]
    assert pid == "p1"
    assert rec["kind"] == "task-done"
    assert rec["decision"] == "done"
    assert rec["rationale"] == "AC 全達成と判断"
    assert rec["decided_by"] == "autonomous-AI"     # conservative default
    assert rec["evidence"] == []                     # honest empty (no link given)
    assert rec["related"]["task_id"] == "e-1"


def test_captures_decided_by_and_evidence_from_env(monkeypatch):
    # ms-154 e-5650: decided_by is really captured (human-directed vs autonomous
    # become distinguishable), and evidence carries real commit links, not a
    # self-reference.
    fake = _FakeClient()
    monkeypatch.setenv("BEACON_DECIDED_BY", "human-delegated")
    monkeypatch.setenv("BEACON_DONE_EVIDENCE", "commit:abc1234\ncommit:def5678")
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    # 帰属と根拠は **呼び出し元が渡す** (env を読むのは CLI handler の仕事で、
    # データ層の継ぎ目は受け取った値をそのまま載せる)。
    _close_and_flush("e-2", "user がクローズを指示",
                     decided_by="human-delegated",
                     evidence=["commit:abc1234", "commit:def5678"])
    rec = fake.posted[0][1]
    assert rec["decided_by"] == "human-delegated"
    assert rec["evidence"] == ["commit:abc1234", "commit:def5678"]


def test_noop_in_local_mode(monkeypatch):
    fake = _FakeClient()
    _clear_env(monkeypatch)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    _close_and_flush("e-1", "x")
    assert fake.posted == []  # local mode = no server decision stream


def test_best_effort_swallows_client_failure(monkeypatch):
    # a raising client (e.g. _get_api_client sys.exit on missing creds) must not
    # propagate — decision recording is a flag, never a gate on `task done`.
    _clear_env(monkeypatch)

    def _boom():
        raise SystemExit(1)

    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client", _boom)
    _close_and_flush("e-1", "x")  # no raise


def test_the_cli_handler_still_passes_the_env_attribution_down():
    """CLI handler が env の帰属 / 根拠を継ぎ目へ **渡している** こと。

    記録の場所をデータ層へ移したので、env を読む責務は handler 側に残る。読まなく
    なれば /beacon-log が渡す ``--decided-by`` / ``--evidence`` が黙って落ちるので、
    渡していることを構文木で固定する (文字列一致だと docstring の言及に反応する)。
    """
    import ast
    import io as _io
    src = _io.open(os.path.join(os.path.dirname(__file__), "..", "lib",
                                "cmd_task.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_task_done")
    calls = [c for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == "task_done"]
    assert calls, "cmd_task_done が core.task_done を呼んでいない"
    kw = {k.arg for k in calls[0].keywords}
    assert {"decided_by", "evidence"} <= kw, (
        f"帰属 / 根拠を継ぎ目へ渡していない (渡しているのは {sorted(kw)})")
