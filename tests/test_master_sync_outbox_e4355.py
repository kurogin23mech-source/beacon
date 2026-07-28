"""投影 rename の emit 失敗 → lost edit を塞ぐ outbox + pending マーカー (ms-111 / e-4355).

背景 (lost-edit vector): e-3622 で account_rename は投影を先に更新し、その rename を master
へ透過する master-sync event を best-effort emit する。emit が silent 失敗 (bus 不通 / 未
login / 認証エラー) すると、投影は新名 (cache) を持つが master は旧名のまま (local-only)。
以後 master が別経由で変わって inbound fan-out が走ると、master-wins で投影が master の旧名
(この rename を含まない stale name) に上書きされ、user の rename が **revert = lost edit**。

本 test は 2 本の防御を pure-lib レベルで固定する:
  (c) revert 防御: pending マーカー中の投影は、その edit を含まない stale master name で
      上書きされない (sync_master_name_into_projection が no-op)。
  (b) outbox: pending マーカーを源に再発行 (drain) すれば at-least-once で master に届く。

特に **中核の回帰テスト** は test_lost_edit_path_is_closed_by_pending_marker: 「emit 失敗 →
後続 fan-out で revert」を実際に再現し、pending マーカーがその revert を塞ぐことを assert する。
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


# ---------------------------------------------------------------------------
# pending マーカー helper の基本挙動
# ---------------------------------------------------------------------------
def test_mark_and_clear_pending():
    acc = _linked_acc("a1", "macc-1", "Acme")
    assert mp.is_sync_pending(acc) is False
    mp.mark_sync_pending(acc, "Acme Inc.")
    assert mp.is_sync_pending(acc) is True
    assert mp.sync_pending_name(acc) == "Acme Inc."
    mp.clear_sync_pending(acc)
    assert mp.is_sync_pending(acc) is False
    assert mp.sync_pending_name(acc) == ""


def test_mark_pending_unlinked_is_noop():
    acc = {"id": "a1", "name": "Acme", "label": "Acme"}  # 未 link
    mp.mark_sync_pending(acc, "New")
    assert mp.is_sync_pending(acc) is False  # 未 link は同期概念が無い → 印を立てない


def test_mark_pending_empty_name_is_noop():
    acc = _linked_acc("a1", "macc-1", "Acme")
    mp.mark_sync_pending(acc, "   ")
    assert mp.is_sync_pending(acc) is False


def test_pending_sync_accounts_lists_only_pending():
    a1 = _linked_acc("a1", "macc-1", "Acme")
    a2 = _linked_acc("a2", "macc-2", "Beta")
    mp.mark_sync_pending(a1, "Acme Inc.")
    data = {"accounts": [a1, a2]}
    pend = mp.pending_sync_accounts(data)
    assert [a["id"] for a in pend] == ["a1"]


# ---------------------------------------------------------------------------
# 中核回帰: lost-edit 経路が pending マーカーで塞がれる
# ---------------------------------------------------------------------------
def test_lost_edit_reproduced_without_marker():
    """マーカーが無い (= 従来の best-effort) と、stale master name の fan-out が rename を revert する。

    このテストは『対策を入れなければ lost edit が起きる』ことを明示的に再現する
    (= 回帰の基準線)。マーカーを立てずに fan-out すると投影は master の旧名に戻る。
    """
    acc = _linked_acc("a1", "macc-1", "Acme")
    # user が rename → 投影 cache だけ更新 (emit は失敗したと仮定、マーカー無し)。
    acc["name"] = acc["label"] = "Acme Renamed"
    # master は rename を受け取れておらず旧名のまま。別経由の fan-out が旧名で来る。
    data = {"accounts": [acc]}
    changed = mp.sync_master_name_into_projection(data, "macc-1", "Acme")
    assert changed is True                        # 上書きが起きた
    assert data["accounts"][0]["name"] == "Acme"  # lost edit: user の "Acme Renamed" が消えた


def test_lost_edit_path_is_closed_by_pending_marker():
    """★中核: emit 失敗で pending マーカーが立っていれば、stale master fan-out は revert しない。

    データフロー:
      1. user rename → 投影 cache = "Acme Renamed"、master 透過 emit は失敗。
      2. emit 失敗なので pending マーカーを立てる (outbox)。
      3. 別経由で master が (この rename を含まない) 旧名 "Acme" のまま fan-out。
      4. pending 中なので上書きを拒否 → 投影は "Acme Renamed" を保つ = lost edit 塞がれた。
    """
    acc = _linked_acc("a1", "macc-1", "Acme")
    acc["name"] = acc["label"] = "Acme Renamed"
    mp.mark_sync_pending(acc, "Acme Renamed")     # emit 失敗 → outbox マーカー
    data = {"accounts": [acc]}
    # stale master name ("Acme") が fan-out されても pending 中は上書きしない。
    changed = mp.sync_master_name_into_projection(data, "macc-1", "Acme")
    assert changed is False                              # revert 拒否 (no-op)
    assert data["accounts"][0]["name"] == "Acme Renamed"  # user の edit が保たれた
    assert mp.is_sync_pending(data["accounts"][0]) is True  # まだ未配送 → マーカー残置


def test_pending_cleared_when_own_edit_finally_lands():
    """pending edit と一致する master name が fan-out で来たら、自分の edit が反映済 → 印を回収。

    outbox retry でついに master に届き、fan-out が同名 ("Acme Renamed") で戻ってきた場合、
    それは自分の edit の反映なので受け入れて pending を解消する (stranded を作らない)。
    """
    acc = _linked_acc("a1", "macc-1", "Acme")
    acc["name"] = acc["label"] = "Acme Renamed"
    mp.mark_sync_pending(acc, "Acme Renamed")
    data = {"accounts": [acc]}
    changed = mp.sync_master_name_into_projection(data, "macc-1", "Acme Renamed")
    assert changed is True                               # 状態が変わった (印が消えた)
    assert data["accounts"][0]["name"] == "Acme Renamed"
    assert mp.is_sync_pending(data["accounts"][0]) is False  # edit 反映済 → 印回収


def test_pending_blocks_only_referencing_account():
    """pending 防御は該当 master を参照する投影だけに効く (無関係 account は通常どおり)。"""
    a1 = _linked_acc("a1", "macc-1", "Acme")
    a1["name"] = a1["label"] = "Acme Renamed"
    mp.mark_sync_pending(a1, "Acme Renamed")
    a2 = _linked_acc("a2", "macc-2", "Beta")
    data = {"accounts": [a1, a2]}
    # macc-2 の fan-out は pending でない a2 を普通に更新する。
    assert mp.sync_master_name_into_projection(data, "macc-2", "Beta Corp.") is True
    assert data["accounts"][1]["name"] == "Beta Corp."
    # macc-1 の stale fan-out は pending の a1 を守る。
    assert mp.sync_master_name_into_projection(data, "macc-1", "Acme") is False
    assert data["accounts"][0]["name"] == "Acme Renamed"
