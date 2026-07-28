"""CLI account_rename の emit → outbox 配線 (ms-111 / e-4355).

commands._emit_master_sync_account_rename と _drain_master_sync_outbox を、実 bus 配送を
stub して固定する。実発行の可否 (_post_master_sync_event) だけを差し替え、pure な配線
(発行成功で pending 回収 / 発行失敗で pending 残置 = outbox / drain で再発行) を hermetic に
検証する。live cloud には一切触れない。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import master_projection as mp  # noqa: E402


def _linked_data(name="Acme"):
    acc = {"id": "a1", "name": name, "label": name, "contacts": []}
    mp.link_account_to_master(acc, "macc-1")
    return {"accounts": [acc]}


def test_emit_failure_marks_pending(monkeypatch):
    """emit が配送失敗 (bus 不通等) → 投影に pending マーカーを立てる (outbox、changed=True)。"""
    monkeypatch.setattr(commands, "_post_master_sync_event", lambda payload: False)
    data = _linked_data("Acme")
    data["accounts"][0]["name"] = data["accounts"][0]["label"] = "Acme Inc."
    delivered, changed = commands._emit_master_sync_account_rename(data, "a1", "Acme Inc.")
    assert delivered is False and changed is True
    assert mp.sync_pending_name(data["accounts"][0]) == "Acme Inc."


def test_emit_success_clears_pending(monkeypatch):
    """emit 成功 → 既存 pending マーカーを回収する (配送済)。"""
    monkeypatch.setattr(commands, "_post_master_sync_event", lambda payload: True)
    data = _linked_data("Acme Inc.")
    mp.mark_sync_pending(data["accounts"][0], "Acme Inc.")  # 過去の未配送を残置
    delivered, changed = commands._emit_master_sync_account_rename(data, "a1", "Acme Inc.")
    assert delivered is True and changed is True
    assert mp.is_sync_pending(data["accounts"][0]) is False


def test_emit_unlinked_is_noop(monkeypatch):
    """未 link account は同期対象外 → 発行も pending も無し。"""
    monkeypatch.setattr(commands, "_post_master_sync_event", lambda payload: False)
    data = {"accounts": [{"id": "a1", "name": "Acme", "label": "Acme"}]}
    delivered, changed = commands._emit_master_sync_account_rename(data, "a1", "New")
    assert delivered is False and changed is False


def test_drain_resends_pending(monkeypatch):
    """drain: pending を抱える account の master-sync を再発行し、成功したら印を回収する。"""
    calls = []

    def _post(payload):
        calls.append(payload["new_name"])
        return True

    monkeypatch.setattr(commands, "_post_master_sync_event", _post)
    data = _linked_data("Acme Inc.")
    mp.mark_sync_pending(data["accounts"][0], "Acme Inc.")
    changed = commands._drain_master_sync_outbox(data)
    assert changed is True
    assert calls == ["Acme Inc."]                       # 再発行された
    assert mp.is_sync_pending(data["accounts"][0]) is False  # 回収済


def test_drain_keeps_pending_on_failure(monkeypatch):
    """drain 時も配送失敗なら pending を残す (次回持ち越し = at-least-once)。"""
    monkeypatch.setattr(commands, "_post_master_sync_event", lambda payload: False)
    data = _linked_data("Acme Inc.")
    mp.mark_sync_pending(data["accounts"][0], "Acme Inc.")
    changed = commands._drain_master_sync_outbox(data)
    assert changed is False
    assert mp.is_sync_pending(data["accounts"][0]) is True   # 残置


def test_drain_noop_when_nothing_pending(monkeypatch):
    called = []
    monkeypatch.setattr(commands, "_post_master_sync_event",
                        lambda payload: called.append(1) or True)
    data = _linked_data("Acme")
    assert commands._drain_master_sync_outbox(data) is False
    assert called == []   # pending が無ければ発行しない
