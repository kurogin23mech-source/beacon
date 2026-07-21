"""lib/target_disclosure.py — 任意の Target を cross-project 開示する汎用層
(ms-113 generalization).

開示 (project_links に project を足す/外す) は **どの Target でも同一の純粋操作**
なのに、当初 Account 専用の CLI (`beacon account link`) として露出してしまっていた。
これを id で任意の Target を引いて開示する汎用動詞 (`beacon disclose` /
`beacon undisclose`) に持ち上げる。Account はその最初の consumer にすぎない。

resolver は `occupation.TARGET_DECOMPOSITION` (= 全 Target collection の登録簿:
milestones / opportunities / accounts / acquisitions …) を走査するので、prefix に
依存せず、将来 Target 種が増えても自動で開示対象になる。実際の link/unlink は
ms-113 で本番着地した `disclosure` プリミティブ (fail-closed / 剥奪即時) が担う。
"""
from __future__ import annotations

from typing import Optional

import occupation
import work_model
import disclosure


def find_target(data: dict, target_id: str) -> Optional[dict]:
    """任意の Target collection から id で resource を引く (全 collection scan)。

    見つからなければ None。prefix 判定はしない (= 登録簿にある collection を順に
    探すので、`acc-` / `opp-` / `ms-` 等の種別を呼び出し側が知らなくてよい)。
    """
    if not target_id:
        return None
    for collection in occupation.TARGET_DECOMPOSITION:
        found = work_model.find_by_id(data.get(collection, []), target_id)
        if found is not None:
            return found
    return None


def disclose_target(data: dict, target_id: str, project_id: str) -> bool:
    """Target を別 project に開示リンクする。追加したら True、既存なら False。

    その project のメンバーが、この Target を参照できるようになる (cross-project
    開示の opt-in 付与)。Target が見つからなければ ValueError。
    """
    target = find_target(data, target_id)
    if target is None:
        raise ValueError(f"Target not found: {target_id}")
    return disclosure.link_resource_to_project(target, project_id)


def undisclose_target(data: dict, target_id: str, project_id: str) -> bool:
    """Target の project 開示リンクを外す。外したら True、無ければ False。

    外した瞬間から、その project のメンバーは Target を参照できなくなる
    (= query 時評価なので剥奪即時)。Target が見つからなければ ValueError。
    """
    target = find_target(data, target_id)
    if target is None:
        raise ValueError(f"Target not found: {target_id}")
    return disclosure.unlink_resource_from_project(target, project_id)
