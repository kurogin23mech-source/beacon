"""lib/master_projection.py — 投影 (projection) 側から共有マスターへの参照層 (ms-111 / e-3621 part2 前半).

各 project の Account / Contact は、共有マスター (= 顧客 identity の真値源) の **投影
(写し)** である (SPEC ms-111 §1)。本モジュールは投影 → マスターの ID 参照 (external_ref)
を張る additive な層を提供する:

  - 投影 Account/Contact に `master_ref` (= マスター行への参照鍵) を付与・読み出す。
  - 投影の identity フィールド (name 等) から、org_id 束縛のマスター record を組み立てる
    (= projection → master mapper。lib/master_identity の薄い schema を再利用)。

**この段は非破壊 (additive)**: 既存の Account/Contact の identity フィールド (name /
contacts …) は残したまま、マスターへの参照だけを足す。「投影が identity 実体を持たず
マスターだけを読む」最終状態 (= 読み出しの master 一本化) は後続スライスで、まず参照の
配線と projection↔master の対応関係をこの層で確立する (house の dual-write 移行と同型)。

束縛軸は org_id (SPEC §8)。どの org のマスターに写すかは project の master_binding
(lib/master_binding) が宣言し、その org_id をマスター record に刻む。開示 (誰が読めるか)
は participation-only の別層 (e-3733) が決め、本参照層は関与しない。
"""
from __future__ import annotations

from typing import Optional

import master_identity as mi

# 投影 Account/Contact 上に置く「マスター行への参照鍵」フィールド名。
# master record 側の `external_ref` (= マスター→外部 system of record) とは向きが逆:
# こちらは projection → master への内向き参照なので別名 `master_ref` で混同を避ける。
MASTER_REF_FIELD = "master_ref"

_SYSTEM_KEY = "system"
_ACCOUNT_ID_KEY = "master_account_id"
_CONTACT_ID_KEY = "master_contact_id"

BEACON_DEFAULT_SYSTEM = mi.BEACON_DEFAULT_SYSTEM


# ---------------------------------------------------------------------------
# link / read: 投影 → マスターの参照鍵
# ---------------------------------------------------------------------------
def link_account_to_master(account: dict, master_account_id: str, *,
                           system: str = BEACON_DEFAULT_SYSTEM) -> dict:
    """投影 Account にマスター会社への参照を張る (非破壊、既存フィールドは保つ)。"""
    if not master_account_id:
        raise ValueError("master_account_id is required to link an account projection")
    account[MASTER_REF_FIELD] = {
        _SYSTEM_KEY: (system or BEACON_DEFAULT_SYSTEM),
        _ACCOUNT_ID_KEY: master_account_id,
    }
    return account[MASTER_REF_FIELD]


def link_contact_to_master(contact: dict, master_contact_id: str,
                           master_account_id: str, *,
                           system: str = BEACON_DEFAULT_SYSTEM) -> dict:
    """投影 Contact にマスター担当者 (+ 親会社) への参照を張る (非破壊)。"""
    if not master_contact_id:
        raise ValueError("master_contact_id is required to link a contact projection")
    contact[MASTER_REF_FIELD] = {
        _SYSTEM_KEY: (system or BEACON_DEFAULT_SYSTEM),
        _CONTACT_ID_KEY: master_contact_id,
        _ACCOUNT_ID_KEY: master_account_id or "",
    }
    return contact[MASTER_REF_FIELD]


def account_master_ref(account: dict) -> Optional[dict]:
    """投影 Account のマスター参照を返す (未 link なら None)。"""
    ref = (account or {}).get(MASTER_REF_FIELD)
    return dict(ref) if isinstance(ref, dict) and ref.get(_ACCOUNT_ID_KEY) else None


def contact_master_ref(contact: dict) -> Optional[dict]:
    """投影 Contact のマスター参照を返す (未 link なら None)。"""
    ref = (contact or {}).get(MASTER_REF_FIELD)
    return dict(ref) if isinstance(ref, dict) and ref.get(_CONTACT_ID_KEY) else None


def is_account_linked(account: dict) -> bool:
    """投影 Account がマスターに link 済か。"""
    return account_master_ref(account) is not None


def linked_master_account_id(account: dict) -> str:
    """投影 Account が指すマスター会社 id (未 link なら "")。"""
    ref = account_master_ref(account)
    return ref[_ACCOUNT_ID_KEY] if ref else ""


# ---------------------------------------------------------------------------
# projection → master mapper: 投影の identity からマスター record を組み立てる
# ---------------------------------------------------------------------------
def project_account_to_master(account: dict, *, org_id: str, now: str,
                              system: str = BEACON_DEFAULT_SYSTEM,
                              master_account_id: str = "") -> dict:
    """投影 Account の identity 部分から、org_id 束縛のマスター会社 record を作る。

    薄いマスター schema (lib/master_identity) を再利用するので work データ (phase /
    health / contacts …) は自動的に落ちる (= 投影から identity だけを抽出してマスター化)。
    external_ref は Beacon-default 自己参照 (外部 CRM 連携時は別途上書き)。
    """
    name = str((account or {}).get("name") or (account or {}).get("label") or "").strip()
    return mi.new_master_account(
        name, org_id=org_id, now=now, master_account_id=master_account_id,
        external_ref=mi.new_external_ref(system, ""),
    )


def project_contact_to_master(contact: dict, *, org_id: str, master_account_id: str,
                              now: str, system: str = BEACON_DEFAULT_SYSTEM,
                              master_contact_id: str = "") -> dict:
    """投影 Contact の identity 部分から、org_id 束縛のマスター担当者 record を作る。

    name / email / phone / role (= 人の identity 属性) だけを写し、親会社 (master_account_id)
    に紐付ける。work データは持ち込まない。
    """
    c = contact or {}
    name = str(c.get("name") or "").strip()
    return mi.new_master_contact(
        name, org_id=org_id, master_account_id=master_account_id, now=now,
        email=str(c.get("email") or ""), phone=str(c.get("phone") or ""),
        role=str(c.get("role") or ""), master_contact_id=master_contact_id,
        external_ref=mi.new_external_ref(system, ""),
    )
