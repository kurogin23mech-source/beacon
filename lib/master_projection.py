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

import disclosure  # ms-111 e-3622: 投影 Account の所有 org (owner_org_id) を読む
import master_identity as mi
import work_model

# 投影 Account/Contact 上に置く「マスター行への参照鍵」フィールド名。
# master record 側の `external_ref` (= マスター→外部 system of record) とは向きが逆:
# こちらは projection → master への内向き参照なので別名 `master_ref` で混同を避ける。
MASTER_REF_FIELD = "master_ref"

_SYSTEM_KEY = "system"
_ACCOUNT_ID_KEY = "master_account_id"
_CONTACT_ID_KEY = "master_contact_id"

BEACON_DEFAULT_SYSTEM = mi.BEACON_DEFAULT_SYSTEM

# 投影 Account 上に置く「未同期 (unsynced) local edit の pending マーカー」フィールド名
# (ms-111 e-4355)。CLI rename が投影を先に更新した後、その rename を master へ透過する
# master-sync event の発行に **失敗** した (bus 不通 / 未 login / 認証エラー) とき、
# 「この投影の name はまだ master に届いていない local-only の編集だ」と印を残す。
# この印は 2 つの役割を持つ:
#   (1) fan-out revert 防御: master が別経由で変わり inbound fan-out が走っても、pending
#       中の投影を master の (この edit を含まない) stale name で上書きさせない
#       (= lost edit を構造で塞ぐ)。
#   (2) outbox retry の源: 次の CLI 操作でこの印を持つ account を拾い、master-sync event
#       を再発行する (at-least-once delivery)。発行成功で印を消す。
PENDING_SYNC_FIELD = "master_sync_pending"


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


def projection_account_org(account: dict) -> str:
    """投影 Account の所有 org (owner_org_id) を返す (無ければ空文字、e-3622).

    束縛軸の照合基準。master link の org はこの org と一致していなければならない
    (= same-org)。空 (= 所有 org 未定) の投影が master に link 済なのは異常なので、
    照合側は「照合できない = 信用しない」 (fail-closed) 扱いにする。
    """
    return str((account or {}).get(disclosure.OWNER_ORG_FIELD) or "")


def _org_matches(master_org: str, expected_org: str) -> bool:
    """master record の org と投影側 org が **両方非空で一致** するか (e-3622 fail-closed).

    どちらかが空、または不一致なら False → 呼び出し側は master を信用せず投影 fallback。
    「org-A の投影が org-B の master を指す」 poisoning を read 時に無効化する
    (link は org 必須生成で same-org by construction だが、参照鍵の取り違え / 将来の
    外部 adapter 混線に対する second line として read 側でも fail-closed に閉じる)。
    """
    return bool(master_org) and bool(expected_org) and master_org == expected_org


# ---------------------------------------------------------------------------
# 読み出しのマスター一本化 (= e-3621 part2 後半の核): master 経由で identity を読む
#   投影が master に link 済なら「誰が誰か」は master が真値なので master から読む。
#   未 link (= 既存 account / adapter 無し) は投影自身の値にフォールバックする
#   (work_model.target_label と同じ寛容な読み。local mode や移行途中でも壊れない)。
#   これにより「account.name の read が master 経由になる」 (SPEC「投影は identity 実体を
#   持たない」に向けた読み替え) を、既存データを壊さず段階導入する。
#   ms-111 e-3622: master を採用する条件に **org 一致** を足す (fail-closed)。org-A の
#   投影が org-B の master を指していたら master を無視して投影へ落とす (cross-org leak 防止)。
# ---------------------------------------------------------------------------
def resolve_account_identity(account: dict, adapter=None) -> str:
    """投影 Account の canonical name を master 経由で解決する (未 link / org 不一致は投影へ fallback)。

    - `adapter` : MasterIdentityAdapter (lib/master_store)。None なら常に fallback。
    master が真値源 (SPEC §7) なので、link 済 **かつ master の org が投影の所有 org と
    一致** する時だけ master の name を返す。org 不一致 (= cross-org poisoning の疑い) は
    master を採らず投影 fallback する (e-3622、fail-closed)。
    """
    if adapter is not None and is_account_linked(account):
        rec = adapter.get_account(linked_master_account_id(account))
        if rec and _org_matches(mi.master_account_org_id(rec),
                                projection_account_org(account)):
            return mi.master_account_name(rec)
    # fallback: 投影自身の tolerant read (label → title → name)
    return work_model.target_label(account or {})


def resolve_contact_identity(contact: dict, adapter=None, *, expected_org: str = "") -> dict:
    """投影 Contact の identity (name/email/phone/role) を master 経由で解決する。

    link 済 + adapter + **master の org が expected_org と一致** の時だけ master の値、
    それ以外は投影自身の値。Contact は自身に org を持たない (親 Account の束縛軸を継ぐ)
    ので、呼び出し側が親 Account の所有 org を ``expected_org`` で渡す。空 (= 照合材料
    なし) や不一致は fail-closed で投影 fallback (e-3622)。返り値は表示用の正規化 dict。
    """
    ref = contact_master_ref(contact)
    if adapter is not None and ref:
        rec = adapter.get_contact(ref[_CONTACT_ID_KEY])
        if rec and _org_matches(mi.master_contact_org_id(rec), expected_org):
            return {k: str(rec.get(k) or "") for k in ("name", "email", "phone", "role")}
    c = contact or {}
    return {k: str(c.get(k) or "") for k in ("name", "email", "phone", "role")}


def account_read_view(account: dict, adapter=None, extra: Optional[dict] = None) -> dict:
    """投影 Account を master 経由の identity で解決した read 用 dict にする (e-3621 chunk2b).

    **shape 不変**: 既存キーはすべて保ち、identity 由来の値の出所だけ master 経由に
    差し替える (= account の name/label、各 contact の name/email/phone/role)。link 済 +
    adapter あれば master が真値、未 link / adapter=None は投影 fallback (= 従来値、
    regression なし)。

    ``extra`` は home project 等の **identity 以外の付帯情報** を重ねる。identity キー
    (name/label/contacts) は resolver 値が **常に優先** され、extra では上書きできない
    (= identity 一本化の抜け穴を作らない。extra は identity キーより先に merge する)。

    対象は **dict を返す read 経路** (server endpoint 等) の 1 箇所化。CLI の表示は dict
    でなく整形印字なので本 helper を通さず ``resolve_account_identity`` /
    ``resolve_contact_identity`` を直接呼ぶ (どちらも同じ resolver を経由するので identity
    の出所ルールは 1 つ)。部分 swap で stale (投影) と master が混ざるバグを、resolver
    への一本化で塞ぐ。
    """
    # contact は自身に org を持たないので、親 Account の所有 org を org 照合基準に渡す
    # (e-3622 fail-closed: cross-org の master contact を read で採らない)。
    account_org = projection_account_org(account)
    name = resolve_account_identity(account, adapter)
    contacts = [
        {**c, **resolve_contact_identity(c, adapter, expected_org=account_org)}
        for c in ((account or {}).get("contacts") or [])
    ]
    # extra を先に、identity を後に merge → resolver 値が extra に勝つ (上書き不可)。
    return {**(account or {}), **(extra or {}),
            "name": name, "label": name, "contacts": contacts}


# ---------------------------------------------------------------------------
# 生成時の link: 投影を作った直後に master record を起こして参照を張る
#   pure 関数 (adapter への put も含むが I/O は adapter に委譲)。呼び出し側
#   (server endpoint / CLI-with-store) が account_add 後にこれを呼ぶ想定。
# ---------------------------------------------------------------------------
def link_new_account_to_master(account: dict, adapter, *, org_id: str, now: str,
                               system: str = BEACON_DEFAULT_SYSTEM) -> dict:
    """投影 Account から master record を起こし、adapter に保存して投影に参照を張る。

    戻り値は保存された master record。既に link 済なら二重起票せず既存を返す
    (= 冪等、重複 master を作らない)。

    ms-111 e-3622 (leader caveat 1 = same-org linking): 投影 Account が所有 org を
    宣言しているなら、link 先 org はそれと一致しなければならない。不一致は「org-A の
    account を org-B の master に link する」cross-org poisoning の入口なので構造で拒否
    する (= read 側 fail-closed の対になる write 側ガード)。所有 org 未宣言の account は
    照合材料が無いので拒否せず org_id をそのまま採る (read 側が fail-closed で守る)。
    """
    owner_org = projection_account_org(account)
    if owner_org and org_id and owner_org != org_id:
        raise ValueError(
            f"same-org linking violation: account owner_org={owner_org!r} != "
            f"link org_id={org_id!r} (ms-111 e-3622)")
    existing = account_master_ref(account)
    if existing:
        rec = adapter.get_account(existing[_ACCOUNT_ID_KEY])
        if rec:
            return rec
    master = project_account_to_master(account, org_id=org_id, now=now, system=system)
    saved = adapter.put_account(master, now=now)
    link_account_to_master(account, saved[mi.MASTER_ACCOUNT_ID_FIELD], system=system)
    return saved


def link_new_contact_to_master(contact: dict, adapter, *, org_id: str,
                               master_account_id: str, now: str,
                               system: str = BEACON_DEFAULT_SYSTEM) -> dict:
    """投影 Contact から master record を起こし、adapter に保存して投影に参照を張る。

    親会社 (master_account_id) に紐付ける。link 済なら冪等に既存を返す。
    """
    existing = contact_master_ref(contact)
    if existing:
        rec = adapter.get_contact(existing[_CONTACT_ID_KEY])
        if rec:
            return rec
    master = project_contact_to_master(contact, org_id=org_id,
                                       master_account_id=master_account_id,
                                       now=now, system=system)
    saved = adapter.put_contact(master, now=now)
    link_contact_to_master(contact, saved[mi.MASTER_CONTACT_ID_FIELD],
                           master_account_id, system=system)
    return saved


def link_project_accounts(project_data: dict, adapter, *, org_id: str, now: str,
                          system: str = BEACON_DEFAULT_SYSTEM) -> dict:
    """project の全 Account/Contact を master に一括 link する ingest choke point (e-3621 chunk2b).

    server の whole-project write ingest (put_project) が呼ぶ「全 site を漏れなく link」の
    一本化点。未 link の Account を master に起こし、配下 Contact も親 Account の master に
    紐付ける。link 済は冪等に skip (``link_new_*`` が既存を返し二重起票しない)。

    ``project_data`` を in-place で変更し (投影に external_ref を張る)、集計を返す。CLI は
    backend adapter を持てない (store_router は server 専用) ため linking は必ずこの server
    ingest に集約する = 「一部 site だけ master 経由」の部分 swap (stale と master の混在)
    を構造的に不能にする。

    per-account の same-org 違反 (``link_new_account_to_master`` の ValueError = cross-org
    poisoning の疑い) は、その account だけ skip して他は続ける (1 件の異常で project 全体の
    write を壊さない)。skip は戻り値 ``skipped`` で観測できる (silent にしない)。

    戻り値: ``{"linked_accounts": int, "linked_contacts": int, "skipped": [(id, reason), ...]}``
    """
    linked_accounts = 0
    linked_contacts = 0
    skipped: list = []
    for acc in ((project_data or {}).get("accounts") or []):
        was_linked = is_account_linked(acc)
        try:
            link_new_account_to_master(acc, adapter, org_id=org_id, now=now, system=system)
        except ValueError as exc:
            skipped.append((str((acc or {}).get("id") or ""), str(exc)))
            continue
        if not is_account_linked(acc):
            # 起票も既 link もされなかった (= 想定外)。配下 contact も張らず skip。
            continue
        if not was_linked:
            linked_accounts += 1
        master_account_id = linked_master_account_id(acc)
        for contact in ((acc.get("contacts") or [])):
            was_c_linked = bool(contact_master_ref(contact))
            try:
                link_new_contact_to_master(
                    contact, adapter, org_id=org_id,
                    master_account_id=master_account_id, now=now, system=system)
            except ValueError as exc:
                skipped.append((str((contact or {}).get("id") or ""), str(exc)))
                continue
            if not was_c_linked and bool(contact_master_ref(contact)):
                linked_contacts += 1
    return {"linked_accounts": linked_accounts,
            "linked_contacts": linked_contacts,
            "skipped": skipped}


# ---------------------------------------------------------------------------
# write-through: 投影 identity の編集を master へ透過する (master 権威、e-3622 chunk2a)
#   leader 合意 guard: user の rename は投影を直接 authoritative にせず、必ず master へ
#   透過する (投影 name は cache、master が唯一の真値 → divergence source を作らない)。
#   data-immutability (guard 1): master 側変更の追跡は上位の master-sync bus event log
#   (append-only) が担い、本関数は master record への適用 (master-wins) だけを持つ。
# ---------------------------------------------------------------------------
def write_through_account_name(account: dict, adapter, new_name: str, *,
                               now: str) -> Optional[dict]:
    """投影 Account の rename を master の canonical name へ write-through する。

    戻り値は更新後の master record。write-through 対象外なら None:
    - `adapter` が None / 未 link: master 実体が無いので透過先が無い → None
      (呼び出し側は投影のみ更新。master 化前の account は投影が唯一の source)。
    - link 済 + same-org: master の name を new_name に更新し put (master-wins)。
    - org 不一致 (= cross-org poisoning の疑い): fail-closed で拒否 (ValueError)。
      別 org の master を書き換えさせない (chunk1 read/write 境界と一貫)。
    - new_name が空: ValueError (identity を空にする write-through は無効)。
    """
    if adapter is None or not is_account_linked(account):
        return None
    name = str(new_name or "").strip()
    if not name:
        raise ValueError("write-through name cannot be empty")
    rec = adapter.get_account(linked_master_account_id(account))
    if not rec:
        return None
    if not _org_matches(mi.master_account_org_id(rec), projection_account_org(account)):
        raise ValueError(
            "cross-org write-through refused: master org != account owner_org "
            "(ms-111 e-3622)")
    return adapter.put_account({**rec, "name": name}, now=now)


def apply_master_name_sync(adapter, *, master_account_id: str, new_name: str,
                           sender_org_ids, now: str):
    """master-sync event を master へ適用する server 側 consumer core (e-3622 chunk2a part2).

    outbound write-through の bus 経路: CLI が rename 時に発行した master-sync event を
    server が受け、master の canonical name を更新する。``write_through_account_name`` が
    **投影 account を持つ context** (CLI / project doc) 用なのに対し、こちらは **event
    payload + 認証済み送信者しか持たない server consumer** 用の姉妹。

    **authz anchor (leader guard、e-3622)**: 権威は payload の org_id ではなく、**認証済み
    送信者 (JWT sub 等) が実際に member である org 集合** (``sender_org_ids``) に置く。
    master record 自身の org が ``sender_org_ids`` に含まれる時だけ適用する。payload に
    org を詐称しても、送信者がその org の member でなければ弾く (= crafted event による
    他 org master の poisoning を封じる)。payload の org_id は「どの master か」の hint に
    留め、authz には使わない (server 側は master record の org を真値に取る)。

    event 経路なので不適用は例外でなく **(None, reason)** を返す (= drop、event ループを
    壊さない)。``reason`` は audit/log 用 (leader minor guard: silent にせず観測可能に):
    ``''`` = 適用 / ``'empty_or_no_id'`` / ``'unknown_master'`` / ``'not_authorized'``。
    戻り値は ``(updated_record_or_None, reason)``。
    """
    name = str(new_name or "").strip()
    if not master_account_id or not name:
        return None, "empty_or_no_id"
    rec = adapter.get_account(master_account_id)
    if not rec:
        return None, "unknown_master"
    master_org = mi.master_account_org_id(rec)
    if not master_org or master_org not in (sender_org_ids or set()):
        return None, "not_authorized"   # 送信者が master の org の member でない
    return adapter.put_account({**rec, "name": name}, now=now), ""


def master_sync_payload(account: dict, new_name: str) -> Optional[dict]:
    """投影 rename から master-sync event の payload を組み立てる (CLI producer 用、chunk2a).

    投影 account の master 参照 (master_account_id) と所有 org (owner_org_id) を読み、
    server consumer (apply_master_name_sync) が同 org 検証して適用できる最小 payload に
    する。未 link (= master 実体なし) は None (= 発行不要、投影のみで完結)。
    """
    if not is_account_linked(account):
        return None
    name = str(new_name or "").strip()
    if not name:
        return None
    return {
        "kind": "master_sync",
        "entity": "account",
        "master_account_id": linked_master_account_id(account),
        "new_name": name,
        "org_id": projection_account_org(account),
    }


# ---------------------------------------------------------------------------
# outbox + revert 防御: 未同期 (unsynced) local edit の pending マーカー (e-4355)
#   linking を live にすると「emit 失敗 → master 反映漏れ → 後続 fan-out で revert
#   (lost edit)」が data-loss vector になる。この段は 2 本の防御を pure-lib に足す:
#     (b) outbox: emit 失敗時に pending マーカーを投影 account に残し、次操作で再発行
#         (at-least-once delivery)。発行成功でマーカーを消す。
#     (c) revert 防御: inbound fan-out (sync_master_name_into_projection) が pending 中の
#         投影を、その edit を含まない master の stale name で上書きするのを拒否する。
#   両者は同じマーカーを源にする (1 concept 1 field)。
# ---------------------------------------------------------------------------
def mark_sync_pending(account: dict, new_name: str) -> None:
    """投影 Account に「この local rename はまだ master へ届いていない」印を立てる (e-4355).

    link 済 account のみ対象 (未 link は master 実体が無く同期の概念が無い → no-op)。
    ``new_name`` は透過したい canonical name。同 name で再 mark は上書き (最新の意図を保持)。
    """
    name = str(new_name or "").strip()
    if not name or not is_account_linked(account):
        return
    account[PENDING_SYNC_FIELD] = {
        "new_name": name,
        "master_account_id": linked_master_account_id(account),
    }


def clear_sync_pending(account: dict) -> None:
    """投影 Account の未同期マーカーを消す (emit 成功時、e-4355)。"""
    if isinstance(account, dict):
        account.pop(PENDING_SYNC_FIELD, None)


def sync_pending_name(account: dict) -> str:
    """投影 Account に立っている未同期 local edit の name を返す (無ければ "")。"""
    p = (account or {}).get(PENDING_SYNC_FIELD)
    return str(p.get("new_name") or "").strip() if isinstance(p, dict) else ""


def is_sync_pending(account: dict) -> bool:
    """投影 Account が未同期 local edit を抱えているか (e-4355)。"""
    return bool(sync_pending_name(account))


def pending_sync_accounts(project_data) -> list:
    """project doc の中で未同期マーカーを持つ投影 account を列挙する (outbox drain 用)。"""
    return [acc for acc in ((project_data or {}).get("accounts") or [])
            if is_sync_pending(acc)]


# ---------------------------------------------------------------------------
# inbound sync: master の name 変更を投影 doc の cache に反映する (e-3622 chunk2b)
#   master が真値 (chunk2a の consumer で更新済)。同 org の各投影は master を写す cache な
#   ので、master-changed を受けて投影 account の cached name を更新する。server read は
#   resolve が master を live 読みするので本来不要だが、CLI は adapter=None で投影 cache を
#   読むため、この inbound write-back で新鮮さを保つ (hybrid sync-cache、leader 承認)。
#   guard 2 と整合: 投影 name は cache、master が真値。更新は master → 投影の一方向のみ。
# ---------------------------------------------------------------------------
def sync_master_name_into_projection(project_data, master_account_id: str,
                                     new_name: str) -> bool:
    """master name 変更を投影 doc の該当 account の cache に反映する (dict を mutate)。

    ``master_account_id`` に link された投影 account を ``project_data`` から探し、その
    cached name/label を ``new_name`` に更新する。更新したら True、該当 account 無し /
    空 name / 既に同値なら False (no-op)。投影は master の写しなので write-back は一方向。
    """
    name = str(new_name or "").strip()
    if not name or not master_account_id:
        return False
    for acc in (project_data or {}).get("accounts", []) or []:
        if linked_master_account_id(acc) == master_account_id:
            # e-4355 revert 防御: この投影が未同期 (unsynced) local edit を抱えている
            # なら、その edit を master へ届ける前の inbound fan-out が来ても上書きしない。
            # master の name がこの pending edit と一致する = 自分の edit がついに master に
            # 反映されて戻ってきた場合だけ、pending を解消して受け入れる。それ以外
            # (= master がこの edit を含まない stale name) は、上書きすると user の rename が
            # revert = lost edit になるので拒否する (no-op)。
            pending = sync_pending_name(acc)
            if pending and pending != name:
                return False   # 未同期 edit を stale master name で revert させない
            if pending and pending == name:
                clear_sync_pending(acc)   # 自分の edit が master に反映済 → 印を回収
                if work_model.target_label(acc) == name:
                    return True   # cache は既に pending 値と同じだが印は消えた → 変化あり
            if work_model.target_label(acc) == name:
                return False   # 既に同値 → no-op
            work_model.set_target_label(acc, name, dual_write=True)
            return True
    return False
