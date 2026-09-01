"""完遂 verdict の decision write 失敗が silent に消えないことの検証 (ms-166 e-5978)。

背景: completion-verdict (目的達成の判定) を decision arm に書く 2 経路
``cmd_target._record_completion_verdict_decision`` (milestone/target approve の主経路) と
``target_completion._record_completion_decision`` (全 target-class 共通の完遂 seam) は、
どちらも ``except BaseException: pass`` で write 失敗を握りつぶしていた。endpoint が落ちて
いても呼び出し側に一切現れず、完遂は起きているのに監査 arm は空に見えた
(= e-5970 の read 窓と併せて「completion-verdict が横断的に消失」した実害の write 側)。

契約:
  * write が失敗しても完遂フローは壊さない (best-effort は維持)。
  * ただし失敗は WARNING で必ず可視化する (silent に落とさない)。
  * narrow 化により KeyboardInterrupt / SystemExit は伝播する (握りつぶさない)。
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_target          # noqa: E402
import target_completion   # noqa: E402
import commands_shared     # noqa: E402


class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc

    def record_decision(self, project_id, decision):
        raise self._exc


def _cloud(monkeypatch, exc):
    client = _RaisingClient(exc)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    monkeypatch.setattr(commands_shared, "_session_kind_is_human", lambda: False)
    monkeypatch.setattr(cmd_target, "_session_kind_is_human", lambda: False)


def _approval_entry():
    return {"id": "e-app", "meta": {"target_id": "ms-9", "intent": "全 AC 達成",
                                    "evidence": ["commit:abc1234"], "review_evidence": []}}


# --- target_completion._record_completion_decision (generic seam) -----------

def test_generic_seam_write失敗はwarnして飲まない(monkeypatch, caplog):
    _cloud(monkeypatch, RuntimeError("502 endpoint down"))
    with caplog.at_level(logging.WARNING):
        # 例外が伝播しないこと (= 完遂フローを壊さない)。
        target_completion._record_completion_decision({"id": "ms-9"}, "done", "理由")
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "completion-verdict decision write failed" in msgs
    assert "ms-9" in msgs


def test_generic_seam_KeyboardInterruptは伝播する(monkeypatch):
    _cloud(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        target_completion._record_completion_decision({"id": "ms-9"}, "done", "理由")


def test_generic_seam_SystemExitは飲むがwarnする(monkeypatch, caplog):
    # _get_api_client の sys.exit で完遂フローを壊さない (既存契約) が、silent にしない。
    def _boom():
        raise SystemExit(1)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client", _boom)
    monkeypatch.setattr(commands_shared, "_session_kind_is_human", lambda: False)
    with caplog.at_level(logging.WARNING):
        target_completion._record_completion_decision({"id": "ms-9"}, "done", "理由")
    assert "write skipped" in " ".join(r.getMessage() for r in caplog.records)


# --- cmd_target._record_completion_verdict_decision (approve 主経路) ---------

def test_approve主経路_write失敗はwarnして飲まない(monkeypatch, caplog):
    _cloud(monkeypatch, RuntimeError("502 endpoint down"))
    with caplog.at_level(logging.WARNING):
        cmd_target._record_completion_verdict_decision(
            "ms-9", "done", _approval_entry(), "レビュー合格につき承認")
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "completion-verdict decision write failed" in msgs
    assert "ms-9" in msgs


def test_approve主経路_KeyboardInterruptは伝播する(monkeypatch):
    _cloud(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        cmd_target._record_completion_verdict_decision(
            "ms-9", "done", _approval_entry(), "承認")


def test_approve主経路_SystemExitは飲むがwarnする(monkeypatch, caplog):
    def _boom():
        raise SystemExit(1)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client", _boom)
    monkeypatch.setattr(cmd_target, "_session_kind_is_human", lambda: False)
    with caplog.at_level(logging.WARNING):
        cmd_target._record_completion_verdict_decision(
            "ms-9", "done", _approval_entry(), "承認")
    assert "write skipped" in " ".join(r.getMessage() for r in caplog.records)
