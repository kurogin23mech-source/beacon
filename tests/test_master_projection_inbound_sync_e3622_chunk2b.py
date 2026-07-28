"""master name 変更を投影 cache へ反映する inbound sync core (ms-111 / e-3622 chunk2b).

master が真値 (chunk2a の consumer で更新済)。同 org の各投影は master の写し (cache) なので、
master-changed を受けて投影 account の cached name を更新する。CLI は adapter=None で投影
cache を読むため、この write-back で古い名前を見ないようにする (hybrid sync-cache)。
純関数レベルで hermetic に固定する。
"""
from __future__ import annotations

import os
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

import master_projection as mp  # noqa: E402


def _linked_acc(acc_id, master_id, name):
    acc = {"id": acc_id, "name": name, "label": name, "contacts": []}
    mp.link_account_to_master(acc, master_id)
    return acc


def test_updates_linked_account_cache():
    data = {"accounts": [_linked_acc("a1", "macc-1", "Acme")]}
    changed = mp.sync_master_name_into_projection(data, "macc-1", "Acme Inc.")
    assert changed is True
    acc = data["accounts"][0]
    assert acc["name"] == "Acme Inc." and acc["label"] == "Acme Inc."


def test_only_matching_master_id_is_updated():
    data = {"accounts": [
        _linked_acc("a1", "macc-1", "Acme"),
        _linked_acc("a2", "macc-2", "Beta"),
    ]}
    mp.sync_master_name_into_projection(data, "macc-2", "Beta Corp.")
    assert data["accounts"][0]["name"] == "Acme"          # 無関係は不変
    assert data["accounts"][1]["name"] == "Beta Corp."     # 対象のみ更新


def test_no_matching_account_is_false():
    data = {"accounts": [_linked_acc("a1", "macc-1", "Acme")]}
    assert mp.sync_master_name_into_projection(data, "macc-nope", "X") is False
    assert data["accounts"][0]["name"] == "Acme"


def test_already_same_value_is_noop_false():
    data = {"accounts": [_linked_acc("a1", "macc-1", "Acme")]}
    assert mp.sync_master_name_into_projection(data, "macc-1", "Acme") is False


def test_empty_name_is_false():
    data = {"accounts": [_linked_acc("a1", "macc-1", "Acme")]}
    assert mp.sync_master_name_into_projection(data, "macc-1", "   ") is False
    assert data["accounts"][0]["name"] == "Acme"


def test_unlinked_account_is_not_touched():
    data = {"accounts": [{"id": "a1", "name": "Acme", "label": "Acme"}]}  # 未 link
    assert mp.sync_master_name_into_projection(data, "macc-1", "X") is False
    assert data["accounts"][0]["name"] == "Acme"
