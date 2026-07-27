"""lib/master_identity.py — 共有マスターの薄い identity schema (ms-111 / e-3619).

顧客 (Account = 会社) と担当者 (Contact = 人) の **canonical identity (= 正準の
「誰が誰か」)** だけを持つ薄いマスター (= 真値源) の schema を定義する。work データ
(= 商談・活動・フェーズ・健全度など「何をやるか」) は一切持たない — それらは各
project (= インスタンス) 固有で、マスターを ID 参照 (external_ref) で結ぶ投影
(projection = 写し) 側に残る (SPEC ms-111 §2 WHO/WORK 分離・§3 薄いマスター)。

なぜ薄く保つか (SPEC §3):
  - マスターに work を載せると職種依存になり pluggable (差し替え可能) 性を失う。
  - 薄く保てば外部 CRM (顧客管理システム) も同型で刺さる (= adapter 契約が成立)。

束縛軸は org_id (SPEC §8 案 B 確定、2026-07-27):
  - identity は org (組織) 単位で共有する。所有軸は user (owner) ではなく org_id。
  - solo 運用でも「1 人だけが属す org」= 実質 user スコープとして破綻せず回る。
  - lib/org.py の org 語彙 (org-p-{user_id} / org-t-{hex}) にそのまま乗る。

真値源の宣言 = external_ref (SPEC §1・§4・§5):
  - 各マスター行は「この identity の system of record (= 記録の権威) はどこか」を
    external_ref で宣言する。Beacon-default master なら system="beacon-default"。
  - 外部 CRM 接続時は system=外部システム名 + ref_id=向こうの id を指す。これが
    adapter (接続アダプタ) が同型で刺さる継ぎ目 (= e-3620 で store/契約を立てる)。

本モジュールは **純関数のみ** (dict を受けて dict を返す、I/O 無し)。store への
永続化 (MySQL 別 schema + /master namespace) と adapter interface は e-3620 が、
投影側の external_ref 化は e-3621 が担う (= lib/server / schema/store の責務分離、
lib/org.py と同型)。

薄さ (= identity only) は「お願い」でなくコードで強制する: builder が生成する
field を allowlist で固定し、`assert_identity_only` が allowlist 外 (= work data
の混入) を ValueError で弾く。work field が schema に漏れ込む経路を構造的に塞ぐ
(= 受入条件「work データを含まず identity のみ」を物理的に担保する)。
"""
from __future__ import annotations

import secrets
from typing import Optional

# ---------------------------------------------------------------------------
# ID prefix (lib/org.py の org-t-/org-p- と同じ「prefix で種別を区別」方式)。
#   マスターは project 横断の共有 store なので、project 内 sequential (acc-{N}) は
#   採れない (= 数える単一 doc が無い)。org team id と同じく minted hex を使う。
# ---------------------------------------------------------------------------
_MASTER_ACCOUNT_PREFIX = "macc-"
_MASTER_CONTACT_PREFIX = "mcon-"

# Beacon-default master (= 外部 CRM 未接続時の既定の system of record) の system 名。
# adapter を差し替えるとここが外部システム名に変わる (SPEC §4・§5)。
BEACON_DEFAULT_SYSTEM = "beacon-default"

# schema field 名の単一真実源 (投影側 e-3621 / store 側 e-3620 が同じ名前で参照する)。
MASTER_ACCOUNT_ID_FIELD = "master_account_id"
MASTER_CONTACT_ID_FIELD = "master_contact_id"
ORG_ID_FIELD = "org_id"          # 束縛軸 (SPEC §8 案 B)
EXTERNAL_REF_FIELD = "external_ref"

# 薄いマスターが持ってよい field の allowlist (= これ以外は work data とみなし弾く)。
# name / email / phone / role は「誰か」を成す identity 属性、org_id は束縛軸、
# external_ref は真値源宣言、created_at/updated_at は変更追跡 (§7 write-through)。
MASTER_ACCOUNT_ALLOWED_FIELDS = frozenset({
    MASTER_ACCOUNT_ID_FIELD,
    ORG_ID_FIELD,
    "name",
    EXTERNAL_REF_FIELD,
    "created_at",
    "updated_at",
})
MASTER_CONTACT_ALLOWED_FIELDS = frozenset({
    MASTER_CONTACT_ID_FIELD,
    MASTER_ACCOUNT_ID_FIELD,   # 親会社への identity 関係 (WHO の relation、SPEC §2)
    ORG_ID_FIELD,
    "name",
    "email",
    "phone",
    "role",
    EXTERNAL_REF_FIELD,
    "created_at",
    "updated_at",
})


# ---------------------------------------------------------------------------
# ID mint / 判定
# ---------------------------------------------------------------------------
def mint_master_account_id() -> str:
    """マスター Account (会社) の新しい canonical id を発行する (`macc-{hex}`)。"""
    return f"{_MASTER_ACCOUNT_PREFIX}{secrets.token_hex(6)}"


def mint_master_contact_id() -> str:
    """マスター Contact (人) の新しい canonical id を発行する (`mcon-{hex}`)。"""
    return f"{_MASTER_CONTACT_PREFIX}{secrets.token_hex(6)}"


def is_master_account_id(value: str) -> bool:
    """value がマスター Account id の形式か。"""
    return isinstance(value, str) and value.startswith(_MASTER_ACCOUNT_PREFIX)


def is_master_contact_id(value: str) -> bool:
    """value がマスター Contact id の形式か。"""
    return isinstance(value, str) and value.startswith(_MASTER_CONTACT_PREFIX)


# ---------------------------------------------------------------------------
# external_ref (真値源宣言) — {"system": str, "ref_id": str}
# ---------------------------------------------------------------------------
def new_external_ref(system: str = BEACON_DEFAULT_SYSTEM, ref_id: str = "") -> dict:
    """system of record を宣言する external_ref を組み立てる。

    - `system` : identity の記録権威 (既定 = "beacon-default")。空は不可。
    - `ref_id` : その system 内での id。Beacon-default では空でよい (= master 行の
                 canonical id 自体が真値なので自己参照は冗長)。外部 CRM 接続時は
                 向こうの id を入れる (= adapter が突き合わせる鍵)。
    """
    if not system or not system.strip():
        raise ValueError("external_ref.system is required")
    return {"system": system.strip(), "ref_id": (ref_id or "").strip()}


def normalize_external_ref(value: object) -> dict:
    """external_ref を寛容に読む (tolerant reader、house style)。

    dict ならそのまま正規化、旧式に str が来たら Beacon-default system 下の ref_id
    とみなす、None/空なら Beacon-default 自己参照にフォールバックする。schema 進化
    (str → 構造化) の途中でも読める互換経路 (memo pnhATs37xgIxEkpFI8uR の互換契約)。
    """
    if isinstance(value, dict):
        system = str(value.get("system") or BEACON_DEFAULT_SYSTEM).strip()
        ref_id = str(value.get("ref_id") or "").strip()
        return {"system": system or BEACON_DEFAULT_SYSTEM, "ref_id": ref_id}
    if isinstance(value, str) and value.strip():
        return {"system": BEACON_DEFAULT_SYSTEM, "ref_id": value.strip()}
    return {"system": BEACON_DEFAULT_SYSTEM, "ref_id": ""}


# ---------------------------------------------------------------------------
# builder (I/O 無し、未永続化 — org.py の new_org と同方針)
# ---------------------------------------------------------------------------
def new_master_account(
    name: str,
    *,
    org_id: str,
    now: str,
    external_ref: Optional[dict] = None,
    master_account_id: str = "",
) -> dict:
    """マスター Account (会社の canonical identity) の doc を組み立てる。

    - `name`              : 会社の正準表示名 (identity)。空は不可。
    - `org_id`            : 束縛軸 (SPEC §8 案 B)。どの org がこの identity を共有
                            するか。空は不可 (= 束縛軸なしの orphan identity を作らない)。
    - `now`               : ISO8601 timestamp を呼び出し側から渡す (決定的テスト用、
                            org.py と同方針)。created_at/updated_at 両方に入る。
    - `external_ref`      : system of record 宣言。省略時は Beacon-default 自己参照。
    - `master_account_id` : 明示指定があれば使う (テストの決定性用)、無ければ発行。
    """
    if not name or not name.strip():
        raise ValueError("master account name is required")
    if not org_id or not org_id.strip():
        raise ValueError("org_id is required to bind a master account (SPEC §8)")
    ref = normalize_external_ref(external_ref) if external_ref is not None \
        else new_external_ref(BEACON_DEFAULT_SYSTEM, "")
    record = {
        MASTER_ACCOUNT_ID_FIELD: master_account_id or mint_master_account_id(),
        ORG_ID_FIELD: org_id.strip(),
        "name": name.strip(),
        EXTERNAL_REF_FIELD: ref,
        "created_at": now,
        "updated_at": now,
    }
    assert_identity_only(record, kind="account")
    return record


def new_master_contact(
    name: str,
    *,
    org_id: str,
    master_account_id: str,
    now: str,
    email: str = "",
    phone: str = "",
    role: str = "",
    external_ref: Optional[dict] = None,
    master_contact_id: str = "",
) -> dict:
    """マスター Contact (人の canonical identity) の doc を組み立てる。

    - `name`              : 人の正準表示名 (identity)。空は不可。
    - `org_id`            : 束縛軸 (SPEC §8)。親 Account と揃える。空は不可。
    - `master_account_id` : 所属する会社 (親) の canonical id。空は不可 (= 会社に
                            紐づかない浮いた Contact を作らない、WHO の relation)。
    - `email` / `phone`   : 連絡先 = 人の identity 属性 (work ではない)。
    - `role`              : 肩書 / 職務 = その人が「誰か」の一部。任意。
    - `now`               : ISO8601 timestamp を呼び出し側から渡す。
    - `external_ref`      : system of record 宣言。省略時は Beacon-default 自己参照。
    - `master_contact_id` : 明示指定があれば使う、無ければ発行。
    """
    if not name or not name.strip():
        raise ValueError("master contact name is required")
    if not org_id or not org_id.strip():
        raise ValueError("org_id is required to bind a master contact (SPEC §8)")
    if not master_account_id or not master_account_id.strip():
        raise ValueError("master_account_id (parent company) is required for a contact")
    ref = normalize_external_ref(external_ref) if external_ref is not None \
        else new_external_ref(BEACON_DEFAULT_SYSTEM, "")
    record = {
        MASTER_CONTACT_ID_FIELD: master_contact_id or mint_master_contact_id(),
        MASTER_ACCOUNT_ID_FIELD: master_account_id.strip(),
        ORG_ID_FIELD: org_id.strip(),
        "name": name.strip(),
        "email": (email or "").strip(),
        "phone": (phone or "").strip(),
        "role": (role or "").strip(),
        EXTERNAL_REF_FIELD: ref,
        "created_at": now,
        "updated_at": now,
    }
    assert_identity_only(record, kind="contact")
    return record


# ---------------------------------------------------------------------------
# 薄さの構造的強制 (= identity only を code で担保する)
# ---------------------------------------------------------------------------
def assert_identity_only(record: dict, *, kind: str) -> dict:
    """record が identity field だけで構成されているか検証する (allowlist)。

    allowlist 外の field (= 商談・フェーズ・健全度などの work data) が 1 つでも
    混じっていれば ValueError。「マスターは薄く保つ」を prompt (お願い) でなく
    schema 境界のコード制約として物理的に閉じる (受入条件: work データを含まない)。

    - `kind` : "account" / "contact"。allowlist を切り替える。
    """
    if kind == "account":
        allowed = MASTER_ACCOUNT_ALLOWED_FIELDS
    elif kind == "contact":
        allowed = MASTER_CONTACT_ALLOWED_FIELDS
    else:
        raise ValueError(f"unknown master identity kind '{kind}' (account or contact)")
    extra = set(record.keys()) - set(allowed)
    if extra:
        raise ValueError(
            f"master {kind} must be identity-only; got work/unknown fields "
            f"{sorted(extra)} (SPEC §3 薄いマスター). Keep work data in the projection."
        )
    return record


def is_identity_only(record: dict, *, kind: str) -> bool:
    """assert_identity_only の bool 版 (raise せず判定だけ)。"""
    try:
        assert_identity_only(record, kind=kind)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# tolerant accessor (house style: 読みは寛容に)
# ---------------------------------------------------------------------------
def master_account_org_id(record: dict) -> str:
    """マスター Account の束縛 org_id を返す (無ければ空文字)。"""
    return str((record or {}).get(ORG_ID_FIELD) or "")


def master_account_name(record: dict) -> str:
    """マスター Account の正準名を返す。"""
    return str((record or {}).get("name") or "")


def master_external_ref(record: dict) -> dict:
    """マスター行の system of record 宣言を正規化して返す。"""
    return normalize_external_ref((record or {}).get(EXTERNAL_REF_FIELD))


def master_contact_parent_id(record: dict) -> str:
    """マスター Contact の親会社 (master_account_id) を返す。"""
    return str((record or {}).get(MASTER_ACCOUNT_ID_FIELD) or "")
