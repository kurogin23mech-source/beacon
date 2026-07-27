"""server/master_adapter.py — server 側でマスター adapter を組み立てる橋渡し (ms-111 / e-3621 part2 後半 chunk2a).

lib/master_store の `BeaconDefaultAdapter` は保存を `MasterPersistence`
(entity 単位 get/put/scan) に注入で委ねる。本モジュールはその保存 interface を
**現在の backend** (store_router が選ぶ MySQL / Firestore / DynamoDB) の
汎用プリミティブ (master_get / master_put / master_scan) で満たし、server の
endpoint / 内部処理から使える 1 つの adapter を提供する。

責務分界:
  - lib (master_store): adapter 契約 + 問い合わせ / 書き込みの意味論 (backend 非依存)。
  - server (本モジュール): 契約が要求する保存プリミティブを実 backend に配線するだけ。
  これで「投影の read を master 経由にする」endpoint 配線 (次 chunk) が、backend を
  意識せず `get_master_adapter()` 1 つで master を読み書きできる。
"""
from __future__ import annotations

import master_store  # lib: adapter 契約 + Beacon-default 実装
import store_router   # server: backend 選択済みの master_get / master_put / master_scan


class _StoreRouterMasterPersistence:
    """store_router の master_* プリミティブを MasterPersistence 契約に適合させる。"""

    def get(self, entity: str, pk: str):
        return store_router.master_get(entity, pk)

    def put(self, entity: str, pk: str, data: dict) -> None:
        store_router.master_put(entity, pk, data)

    def scan(self, entity: str) -> list:
        return store_router.master_scan(entity)


_ADAPTER: master_store.BeaconDefaultAdapter | None = None


def get_master_adapter() -> master_store.BeaconDefaultAdapter:
    """現在の backend に配線された Beacon-default master adapter を返す (プロセス内で 1 個)。

    プロセス内で使い回す (backend 選択は store_router import 時に固定なので、adapter を
    毎回作り直す必要はない)。テストは `reset_master_adapter()` で state を落とせる。
    """
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = master_store.BeaconDefaultAdapter(_StoreRouterMasterPersistence())
    return _ADAPTER


def reset_master_adapter() -> None:
    """キャッシュした adapter を破棄する (= テストの isolation 用)。"""
    global _ADAPTER
    _ADAPTER = None
