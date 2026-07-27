"""lib/master_store.py — マスター identity の adapter 契約 + Beacon-default 実装 (ms-111 / e-3620).

共有マスター (= 顧客 identity の真値源) を「どこに保存するか」から切り離すための
**adapter (= 接続アダプタ) 契約**を定義する。SPEC ms-111 §5 の核: identity 真値源は
Beacon-default master でも外部 CRM (顧客管理システム) でもよく、両者は同じ契約で刺さる。
本 MS では Beacon-default 実装 1 つを動かし、外部 CRM は契約だけ用意して実装は接続時に
回す (SPEC「やらない」= 外部 CRM adapter の実装)。

契約 (`MasterIdentityAdapter`) が要求するのは identity の CRUD だけ:
  - 会社 (Account) / 担当者 (Contact) の put / get / list / external_ref 逆引き
  - list は org_id 束縛軸 (SPEC §8 案 B) で絞る

Beacon-default 実装 (`BeaconDefaultAdapter`) は保存プリミティブ (`MasterPersistence`
= entity 単位の get/put/scan) に依存注入で乗る。これで:
  - lib 層は特定 backend (MySQL / Firestore / DynamoDB) に結合しない (= 純粋にテスト可)
  - server 側は本番 backend を satisfy する薄い wrapper を渡すだけ (= e-3620 の次スライス)
  - dev / local は同梱の `InMemoryMasterStore` で動く

master 権威 (SPEC §7): put は必ず updated_at を stamp し直し (= 書くたびにマスターが
真値を更新)、既存 id への put は created_at を保存して updated_at だけ進める。衝突解決
(master-wins) と bus 同期 (write-through) の配線は e-3622 が担い、本モジュールは
「マスター側が最終権威」の書き込み意味論だけを持つ。

薄さ (identity only) は schema 層 (lib/master_identity.assert_identity_only) が既に
allowlist で強制しているので、adapter は put のたびにそれを通し、work data が保存経路に
漏れ込むのを二重に塞ぐ。
"""
from __future__ import annotations

import abc
from typing import Optional, Protocol

import master_identity as mi
import work_base

# マスターの保存 entity 名 (server 側 ENTITIES 登録・table 名と揃える、e-3620 次スライス)。
ENTITY_MASTER_ACCOUNTS = "master_accounts"
ENTITY_MASTER_CONTACTS = "master_contacts"


# ---------------------------------------------------------------------------
# 保存プリミティブ契約 (= server backend が satisfy する薄い interface)
#   pk 設計: master_accounts は pk=master_account_id / sk='' の top-level entity、
#   master_contacts は pk=master_contact_id / sk='' の top-level entity。
#   org_id / 親会社での絞り込みは scan + filter (org.list_orgs_for_user と同型、
#   薄いマスターの現在規模では十分。indexed 逆引きは rule of three 後に別 task)。
# ---------------------------------------------------------------------------
class MasterPersistence(Protocol):
    """entity 単位の最小 CRUD。mysql_client の _get/_put/_scan にそのまま対応する。"""

    def get(self, entity: str, pk: str) -> Optional[dict]: ...

    def put(self, entity: str, pk: str, data: dict) -> None: ...

    def scan(self, entity: str) -> list[dict]: ...


class InMemoryMasterStore:
    """dev / test / local mode 用のインメモリ保存 (= MasterPersistence の 1 実装)。

    server backend を立てずに adapter を動かす・単体テストを hermetic (= 外部 live を
    触らない) に保つための実装。dict の deep-ish copy で参照汚染を避ける。
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {}

    def get(self, entity: str, pk: str) -> Optional[dict]:
        row = self._data.get(entity, {}).get(pk)
        return dict(row) if row is not None else None

    def put(self, entity: str, pk: str, data: dict) -> None:
        self._data.setdefault(entity, {})[pk] = dict(data)

    def scan(self, entity: str) -> list[dict]:
        return [dict(v) for v in self._data.get(entity, {}).values()]


# ---------------------------------------------------------------------------
# adapter 契約 (= Beacon-default / 外部 CRM が両方 implement する共通 interface)
# ---------------------------------------------------------------------------
class MasterIdentityAdapter(abc.ABC):
    """共有マスター identity の CRUD 契約 (SPEC §5)。

    Beacon-default master はこの 1 実装、外部 CRM (Salesforce / HubSpot 等) は接続時に
    別実装を足す。どちらに束ねられているかは project.json の master_binding が宣言する
    (e-3621)。契約は identity (誰が誰か) だけを扱い、work (商談・活動) は一切持たない。
    """

    @abc.abstractmethod
    def put_account(self, account: dict, *, now: str = "") -> dict:
        """会社 (Account) の canonical identity を保存し、保存後の record を返す。"""

    @abc.abstractmethod
    def get_account(self, master_account_id: str) -> Optional[dict]:
        """master_account_id で会社を引く。無ければ None。"""

    @abc.abstractmethod
    def list_accounts(self, org_id: str) -> list[dict]:
        """org_id 束縛軸 (SPEC §8) でその org が共有する会社を一覧する。"""

    @abc.abstractmethod
    def put_contact(self, contact: dict, *, now: str = "") -> dict:
        """担当者 (Contact) の canonical identity を保存し、保存後の record を返す。"""

    @abc.abstractmethod
    def get_contact(self, master_contact_id: str) -> Optional[dict]:
        """master_contact_id で担当者を引く。無ければ None。"""

    @abc.abstractmethod
    def list_contacts(self, master_account_id: str) -> list[dict]:
        """親会社 (master_account_id) に属す担当者を一覧する。"""

    @abc.abstractmethod
    def resolve_by_external_ref(self, system: str, ref_id: str) -> Optional[dict]:
        """外部システムの参照 (system, ref_id) から会社を逆引きする (adapter 突合の鍵)。"""


# ---------------------------------------------------------------------------
# Beacon-default 実装 (= adapter 契約の実装 #1、SPEC §5)
# ---------------------------------------------------------------------------
class BeaconDefaultAdapter(MasterIdentityAdapter):
    """Beacon 自身をマスターの system of record (記録権威) とする adapter。

    保存は注入された `MasterPersistence` (= server backend or InMemory) に委ね、
    識別子・薄さ・master 権威の書き込み意味論だけを担う。
    """

    def __init__(self, persistence: MasterPersistence) -> None:
        self._db = persistence

    # -- Account --------------------------------------------------------
    def put_account(self, account: dict, *, now: str = "") -> dict:
        record = self._prepare_write(account, kind="account", now=now)
        self._db.put(ENTITY_MASTER_ACCOUNTS, record[mi.MASTER_ACCOUNT_ID_FIELD], record)
        return record

    def get_account(self, master_account_id: str) -> Optional[dict]:
        if not master_account_id:
            return None
        return self._db.get(ENTITY_MASTER_ACCOUNTS, master_account_id)

    def list_accounts(self, org_id: str) -> list[dict]:
        if not org_id:
            return []
        rows = [r for r in self._db.scan(ENTITY_MASTER_ACCOUNTS)
                if mi.master_account_org_id(r) == org_id]
        return _sorted_by_created(rows)

    def resolve_by_external_ref(self, system: str, ref_id: str) -> Optional[dict]:
        if not system or not ref_id:
            return None
        for r in self._db.scan(ENTITY_MASTER_ACCOUNTS):
            ref = mi.master_external_ref(r)
            if ref.get("system") == system and ref.get("ref_id") == ref_id:
                return r
        return None

    # -- Contact --------------------------------------------------------
    def put_contact(self, contact: dict, *, now: str = "") -> dict:
        record = self._prepare_write(contact, kind="contact", now=now)
        self._db.put(ENTITY_MASTER_CONTACTS, record[mi.MASTER_CONTACT_ID_FIELD], record)
        return record

    def get_contact(self, master_contact_id: str) -> Optional[dict]:
        if not master_contact_id:
            return None
        return self._db.get(ENTITY_MASTER_CONTACTS, master_contact_id)

    def list_contacts(self, master_account_id: str) -> list[dict]:
        if not master_account_id:
            return []
        rows = [r for r in self._db.scan(ENTITY_MASTER_CONTACTS)
                if mi.master_contact_parent_id(r) == master_account_id]
        return _sorted_by_created(rows)

    # -- 内部: master 権威の書き込み意味論 (SPEC §7) ---------------------
    def _prepare_write(self, record: dict, *, kind: str, now: str) -> dict:
        """put 前の共通処理: 薄さ検証 + created_at 保存 + updated_at stamp。

        - allowlist 検証 (assert_identity_only) を保存経路でも通し、work data の
          混入を schema 層と二重に塞ぐ。
        - 既存 id への put は元の created_at を保存し (= 作成時刻は不変)、updated_at
          だけ進める (= マスターが最終権威、書くたびに真値を更新する write)。
        """
        mi.assert_identity_only(record, kind=kind)
        out = dict(record)
        stamp = now or work_base.now_iso()
        id_field = (mi.MASTER_ACCOUNT_ID_FIELD if kind == "account"
                    else mi.MASTER_CONTACT_ID_FIELD)
        entity = (ENTITY_MASTER_ACCOUNTS if kind == "account"
                  else ENTITY_MASTER_CONTACTS)
        existing = self._db.get(entity, out.get(id_field, ""))
        if existing and existing.get("created_at"):
            out["created_at"] = existing["created_at"]     # 作成時刻は不変
        elif not out.get("created_at"):
            out["created_at"] = stamp
        out["updated_at"] = stamp                          # 書くたびに更新 (master 権威)
        return out


def _sorted_by_created(rows: list[dict]) -> list[dict]:
    """作成時刻の新しい順に並べる (org.list_orgs_for_user と同じ並び規約)。"""
    return sorted(
        rows,
        key=lambda r: (r.get("created_at", ""), r.get("updated_at", "")),
        reverse=True,
    )
