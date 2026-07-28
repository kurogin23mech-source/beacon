"""操作を待たずに master-sync outbox を drain する定期経路 (ms-111 / e-4399).

背景: e-4355 の outbox (= 投影 Account の rename を master へ透過できなかった時に立てる
「未配送」pending マーカー) は、従来 cmd_account_rename の末尾でしか drain されず「次の
CLI 操作」駆動だった。rename 後に別操作が無いと未配送分が master に届かない。

e-4399 は drain を account_rename から独立させ、操作非依存の entrypoint
``cmd_master_sync_drain`` (= `beacon master-sync drain`) を足す。session-start helper
(scripts/session-start-master-sync-drain.py) がこれを呼び、定期経路から未配送を掃く。

本 test が固定する不変条件:
  - rename を一切呼ばず (= 操作非依存) に drain だけ走らせて、pending を抱える投影の
    master-sync が再発行され、pending マーカーが回収される。
  - 発行に失敗する環境 (未 login / bus 不通) では pending を残したまま no-op で落ちる
    (= at-least-once、次回に持ち越す)。
  - 書き込みは load_project → save_project を経由する (= cloud lost-update guard の傘に
    入る)。drain が何も変えなければ save しない。
"""
from __future__ import annotations

import os
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

import commands  # noqa: E402
import master_projection as mp  # noqa: E402


def _linked_pending_acc(acc_id, master_id, name, pending_name):
    acc = {"id": acc_id, "name": name, "label": name, "contacts": []}
    mp.link_account_to_master(acc, master_id)
    mp.mark_sync_pending(acc, pending_name)
    return acc


@pytest.fixture
def wired(monkeypatch):
    """load_project / save_project を in-memory に差し替え、posted event を記録する。"""
    state = {"data": None, "saved": 0, "posted": [], "emit_ok": True}

    def _load():
        return state["data"]

    def _save(data, op=None):
        state["saved"] += 1

    def _post(payload):
        if not state["emit_ok"]:
            return False  # 未 login / bus 不通 を模す
        state["posted"].append(payload)
        return True

    monkeypatch.setattr(commands, "load_project", _load)
    monkeypatch.setattr(commands, "save_project", _save)
    monkeypatch.setattr(commands, "_post_master_sync_event", _post)
    return state


def test_drain_runs_without_any_rename_operation(wired, capsys):
    """★中核: rename を一切呼ばず、定期 drain だけで未配送が master に届き pending が消える。"""
    acc = _linked_pending_acc("a1", "macc-1", "Acme Renamed", "Acme Renamed")
    wired["data"] = {"accounts": [acc]}
    assert mp.is_sync_pending(acc) is True

    # 操作 (rename) を挟まず、独立 entrypoint を直接叩く。
    commands.cmd_master_sync_drain()

    # master-sync が再発行され、pending マーカーが回収された。
    assert len(wired["posted"]) == 1
    assert wired["posted"][0]["master_account_id"] == "macc-1"
    assert wired["posted"][0]["new_name"] == "Acme Renamed"
    assert mp.is_sync_pending(wired["data"]["accounts"][0]) is False
    assert wired["saved"] == 1  # 変化したので save した
    assert "再送" in capsys.readouterr().out


def test_drain_noop_when_no_pending(wired):
    """未配送が無ければ発行も save もしない (= 空振りで副作用ゼロ)。"""
    acc = {"id": "a1", "name": "Acme", "label": "Acme", "contacts": []}
    mp.link_account_to_master(acc, "macc-1")  # link 済だが pending 無し
    wired["data"] = {"accounts": [acc]}

    commands.cmd_master_sync_drain()

    assert wired["posted"] == []
    assert wired["saved"] == 0


def test_drain_keeps_pending_when_emit_fails(wired):
    """発行できない環境 (未 login / bus 不通) では pending を残し save しない (at-least-once)。"""
    acc = _linked_pending_acc("a1", "macc-1", "Acme Renamed", "Acme Renamed")
    wired["data"] = {"accounts": [acc]}
    wired["emit_ok"] = False  # emit 失敗を模す

    commands.cmd_master_sync_drain()

    assert wired["posted"] == []
    assert wired["saved"] == 0  # 変化なし → save しない
    assert mp.is_sync_pending(wired["data"]["accounts"][0]) is True  # 次回に持ち越す


def test_drain_entrypoint_is_independent_of_rename():
    """drain は account_rename とは別の callable entrypoint である (操作非依存の裏付け)。"""
    assert commands.cmd_master_sync_drain is not commands.cmd_account_rename
    assert callable(commands.cmd_master_sync_drain)


def test_drain_command_is_registered_in_dispatch():
    """lib/commands.py の main dispatch に "master_sync_drain" が登録されている。

    dispatch dict は ``if __name__ == "__main__"`` 内のローカルなので import では引けない。
    ソースを読んで登録行の存在を固定する (= `beacon master-sync drain` が実配線される保証)。
    """
    import pathlib
    src = pathlib.Path(commands.__file__).read_text(encoding="utf-8")
    assert '"master_sync_drain": cmd_master_sync_drain' in src
