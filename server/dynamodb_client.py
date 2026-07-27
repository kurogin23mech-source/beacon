"""DynamoDB backend for the Beacon API (= profile=aws-ga 経路の DB レイヤー).

`firestore_client.py` と同じ public API を提供する skeleton。フェーズ 0 (本 PR)
では関数シグネチャだけを揃え、本体は NotImplementedError。フェーズ 1〜4 で
順次実装する (= SPEC doc 参照: ms-64 SPEC: server/store.py DynamoDB backend
(e-1544))。

DynamoDB layout は e-1540 で立てた 16 テーブル: PK = project_id (or user_id)、
SK = サブコレクションの各 ID。テーブル名は `beacon-{env}-{entity}` 形式。
"""
from __future__ import annotations

import os
from typing import Optional

# boto3 は requirements-lambda.txt にだけ載せている (Cloud Run requirements.txt
# には入れない)。BEACON_STORE_BACKEND != "dynamodb" 環境では import されないため、
# Cloud Run 既存経路にコストはかからない。
try:
    import boto3
    from boto3.dynamodb.conditions import Attr, Key
except ImportError:
    boto3 = None  # type: ignore[assignment]
    Attr = Key = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# DynamoDB resource (lazy initialization)
# ---------------------------------------------------------------------------
# boto3.resource は最初の呼び出しで AWS credentials を解決するので、import 時
# ではなく初回 _table 呼び出し時まで遅延させる (= Cloud Run で誤って boto3 が
# import されても credentials 解決を強制しない安全策)。
_RESOURCE = None


def _get_resource():
    global _RESOURCE
    if _RESOURCE is None:
        if boto3 is None:
            raise RuntimeError("boto3 is not installed (= DynamoDB backend cannot be used)")
        # BEACON_DYNAMODB_ENDPOINT (e-1987): ローカル検証で amazon/dynamodb-local
        # へ向けるための endpoint 上書き。未設定なら None を渡す (= boto3 既定の
        # 実 AWS DynamoDB エンドポイント解決にそのまま委ねる、本番経路は無影響)。
        _RESOURCE = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "ap-northeast-1"),
            endpoint_url=os.environ.get("BEACON_DYNAMODB_ENDPOINT") or None,
        )
    return _RESOURCE


def _table(entity_name: str):
    return _get_resource().Table(TABLES[entity_name])


# Subcollection の SK 名 (= terraform locals.dynamodb_tables の各 sk と一致)
_SUBCOLLECTION_SK_NAMES = {
    "documents": "doc_id",
    "document_revisions": "revision_id",
    "changelog": "change_id",
    "notes": "note_id",
    "bus_events": "event_id",
    "bus_cursors": "cursor_id",
    "bus_nonces": "nonce",
    "bus_audit": "audit_id",
    # ms-70 / e-1712: sidecar to bus_events carrying the receiver-side
    # approval decision_stamp. SK = parent event_id so the sidecar row
    # collocates with its source envelope but is updated independently
    # without touching the HMAC-signed payload.
    "bus_event_approvals": "event_id",
    "sessions": "session_id",
    "session_lookup": "lookup_key",
    "session_logs": "session_id",
    "operation_envelopes": "envelope_id",
    "retros": "week",
}


# ---------------------------------------------------------------------------
# Table name resolution
# ---------------------------------------------------------------------------
# terraform 側 (= iac/terraform/envs/dev/main.tf locals.dynamodb_tables) と
# 一致させる。entity 名は Firestore subcollection 名と同じにしている
# (= e-1540 設計時の意図的整合)。
ENV = os.environ.get("BEACON_ENV", "dev")
TABLE_PREFIX = f"beacon-{ENV}"

TABLES = {
    # top-level
    "projects": f"{TABLE_PREFIX}-projects",
    "users": f"{TABLE_PREFIX}-users",
    "treks": f"{TABLE_PREFIX}-treks",  # ms-69 / e-1652 (PK=trek_id)
    "organizations": f"{TABLE_PREFIX}-organizations",  # ms-113 / e-3731 (PK=org_id)
    # ms-111 / e-3620: 共有マスター identity (top-level, org 単位で cross-project 共有)
    "master_accounts": f"{TABLE_PREFIX}-master_accounts",   # PK=master_account_id
    "master_contacts": f"{TABLE_PREFIX}-master_contacts",   # PK=master_contact_id
    # projects/{pid}/* subcollections
    "retros": f"{TABLE_PREFIX}-retros",
    "documents": f"{TABLE_PREFIX}-documents",
    "document_revisions": f"{TABLE_PREFIX}-document_revisions",
    "changelog": f"{TABLE_PREFIX}-changelog",
    "notes": f"{TABLE_PREFIX}-notes",
    "bus_events": f"{TABLE_PREFIX}-bus_events",
    "bus_cursors": f"{TABLE_PREFIX}-bus_cursors",
    "bus_nonces": f"{TABLE_PREFIX}-bus_nonces",
    "bus_audit": f"{TABLE_PREFIX}-bus_audit",
    # ms-70 / e-1712: receiver-side approval decision_stamp sidecar.
    "bus_event_approvals": f"{TABLE_PREFIX}-bus_event_approvals",
    "sessions": f"{TABLE_PREFIX}-sessions",
    "session_logs": f"{TABLE_PREFIX}-session_logs",
    "operation_envelopes": f"{TABLE_PREFIX}-operation_envelopes",
    # ms-55 e-1730: per-project active claim subcollection (PK=project_id,
    # SK=claim_id). Mirrors Firestore's active_claims/ subcollection so
    # Web UI + restart-recovery work cross-backend.
    "active_claims": f"{TABLE_PREFIX}-active_claims",
    # users/{uid}/* subcollections
    "machines": f"{TABLE_PREFIX}-machines",
    "session_lookup": f"{TABLE_PREFIX}-session_lookup",
}


# Authoritative PK / SK schema per entity (= 単一の真値源)。
# 実 AWS では terraform が、ローカル検証では create_local_tables.py
# (e-1987) と tests の moto fixture がこの形でテーブルを作る。
# top-level エンティティは PK のみ (sk=None)、project / user 配下の
# subcollection は composite key を持つ。
# 不変条件: TABLES の全 entity が必ずここにも現れること
# (create_local_tables.py 起動時に検証する)。
TABLE_KEY_SCHEMA: dict[str, tuple[str, str | None]] = {
    # top-level (PK only, no sort key)
    "projects": ("project_id", None),
    "users": ("user_id", None),
    "treks": ("trek_id", None),
    # ms-111 / e-3620: 共有マスター identity (PK = record 自身の canonical id)
    "master_accounts": ("master_account_id", None),
    "master_contacts": ("master_contact_id", None),
    # users/{uid}/* subcollections
    "machines": ("user_id", "fingerprint_id"),
    # projects/{pid}/active_claims (_SUBCOLLECTION_SK_NAMES には載らないが
    # PK=project_id, SK=claim_id の subcollection — ms-55 e-1730)
    "active_claims": ("project_id", "claim_id"),
    # projects/{pid}/* subcollections (PK=project_id, SK は各 subcollection 固有)
    **{entity: ("project_id", sk) for entity, sk in _SUBCOLLECTION_SK_NAMES.items()},
}


def _not_implemented(fn_name: str) -> NotImplementedError:
    return NotImplementedError(
        f"{fn_name} not yet ported to DynamoDB. "
        f"See SPEC doc 'ms-64 SPEC: server/store.py DynamoDB backend (e-1544)' "
        f"for phase plan."
    )


# ---------------------------------------------------------------------------
# Projects (e-1544 Phase 1)
# ---------------------------------------------------------------------------

def get_project(project_id: str) -> dict | None:
    resp = _table("projects").get_item(Key={"project_id": project_id})
    return resp.get("Item")


def save_project(project_id: str, data: dict) -> None:
    # PK は project_id。data 側に同名キーが含まれていても上書きされるだけで害は無い。
    item = {**data, "project_id": project_id}
    _table("projects").put_item(Item=item)


def list_projects(user_id: str | None = None,
                  include_archived: bool = False) -> list[dict]:
    # Scan は全件読みで割高。dev 規模では問題なし。将来 user_id 検索を
    # 多用するようになったら owner / members[].user_id を別 GSI に出して
    # Query に切替える。
    items = _scan_all(_table("projects"))
    result = []
    for item in items:
        if not include_archived and item.get("archived"):
            continue
        if user_id:
            owner = item.get("owner")
            # owner 無し project は migration 期間のため全員に見える (= firestore 同挙動)
            if owner:
                members = [m.get("user_id") for m in item.get("members", [])]
                if owner != user_id and user_id not in members:
                    continue
        result.append({
            "project_id": item.get("project_id", ""),
            "name": item.get("name", ""),
            "objective": item.get("objective", ""),
            "archived": item.get("archived", False),
        })
    return result


def list_all_projects() -> list[dict]:
    items = _scan_all(_table("projects"))
    return [
        {
            "project_id": item.get("project_id", ""),
            "name": item.get("name", ""),
            "owner": item.get("owner", ""),
            "member_count": len(item.get("members", [])),
            "milestone_count": len(item.get("milestones", [])),
            "updated_at": item.get("updated_at", ""),
        }
        for item in items
    ]


def delete_project(project_id: str) -> bool:
    if get_project(project_id) is None:
        return False
    # Cascade delete: 14 個のサブコレクションテーブルから project_id を PK に持つ
    # アイテムを全部 batch_writer 経由で削除する。Firestore 側 _delete_subcollection
    # と同等の振る舞い (= Firestore は subcollection auto-cascade しないので明示削除、
    # DynamoDB も同様)。
    for entity, sk_name in _SUBCOLLECTION_SK_NAMES.items():
        table = _table(entity)
        items = _query_all(table, Key("project_id").eq(project_id))
        if not items:
            continue
        with table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={
                    "project_id": project_id,
                    sk_name: item[sk_name],
                })
    _table("projects").delete_item(Key={"project_id": project_id})
    return True


# ---------------------------------------------------------------------------
# Users (e-1544 Phase 1)
# ---------------------------------------------------------------------------

def get_user(user_id: str) -> dict | None:
    resp = _table("users").get_item(Key={"user_id": user_id})
    return resp.get("Item")


def get_or_create_user(user_id: str, email: str) -> dict:
    import datetime
    existing = get_user(user_id)
    if existing:
        if existing.get("email") != email:
            _table("users").update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET email = :e",
                ExpressionAttributeValues={":e": email},
            )
            existing["email"] = email
        return existing
    user_data = {
        "user_id": user_id,
        "email": email,
        "role": "user",
        "created_at": datetime.datetime.now().isoformat(),
    }
    _table("users").put_item(Item=user_data)
    return user_data


def list_users() -> list[dict]:
    # 戻り値の各 dict は user_id を含む (= DynamoDB item の PK が自然に入る)。
    # Firestore 版は doc.id を別途差し込んでいたが、DynamoDB は put 時に user_id
    # を Item に含めているので結果は同等。
    return _scan_all(_table("users"))


def update_user(user_id: str, updates: dict) -> bool:
    if get_user(user_id) is None:
        return False
    if not updates:
        return True
    # UpdateExpression を動的構築。DynamoDB は予約語ガード付きで attribute 名を
    # ExpressionAttributeNames 経由で渡すと安全。
    sets = []
    names = {}
    values = {}
    for i, (k, v) in enumerate(updates.items()):
        np = f"#k{i}"
        vp = f":v{i}"
        sets.append(f"{np} = {vp}")
        names[np] = k
        values[vp] = v
    _table("users").update_item(
        Key={"user_id": user_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    return True


def delete_user(user_id: str) -> bool:
    if get_user(user_id) is None:
        return False
    _table("users").delete_item(Key={"user_id": user_id})
    return True


def find_user_by_email(email: str) -> tuple[str, dict] | None:
    # GSI on email を v1 では作らないので Scan + FilterExpression で代用。
    # dev 規模 (user 数 < 数百) では問題なし。本番化前に email GSI を追加する。
    items = _scan_all(_table("users"), filter_expression=Attr("email").eq(email))
    if not items:
        return None
    user = items[0]
    return user.get("user_id", ""), user


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------
# DynamoDB Scan / Query は 1MB cap + LastEvaluatedKey 経由でページング。
# 既存呼び出し側は「全件返る」前提 (= Firestore stream 経由) なので、
# helper でページング吸収する。


def _scan_all(table, filter_expression=None) -> list[dict]:
    kwargs = {}
    if filter_expression is not None:
        kwargs["FilterExpression"] = filter_expression
    items = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def _query_all(table, key_condition) -> list[dict]:
    kwargs = {"KeyConditionExpression": key_condition}
    items = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


# ---------------------------------------------------------------------------
# Retros (subcollection: projects/{project_id}/retros/{week})  e-1544 Phase 2
# ---------------------------------------------------------------------------
# Table: beacon-{env}-retros, PK=project_id, SK=week ("YYYY-WNN")

def list_retros(project_id: str) -> list[dict]:
    items = _query_all(_table("retros"), Key("project_id").eq(project_id))
    # week DESC で揃える (= Firestore 版が order_by("week", DESCENDING))
    items.sort(key=lambda it: it.get("week", ""), reverse=True)
    return items


def get_retro(project_id: str, week: str) -> dict | None:
    resp = _table("retros").get_item(Key={"project_id": project_id, "week": week})
    return resp.get("Item")


def save_retro(project_id: str, week: str, content: str) -> None:
    import datetime
    _table("retros").put_item(Item={
        "project_id": project_id,
        "week": week,
        "content": content,
        "updated_at": datetime.datetime.now().isoformat(),
    })


# ---------------------------------------------------------------------------
# Documents (subcollection: projects/{project_id}/documents/{doc_id})  e-1544 Phase 2
# ---------------------------------------------------------------------------
# Tables:
#   beacon-{env}-documents           PK=project_id, SK=doc_id
#   beacon-{env}-document_revisions  PK=project_id, SK=revision_id
#
# document_revisions は 1 テーブルで全 doc の履歴を持つ。SK 形式を
# "{doc_id}#{rev:06d}" にすることで:
#   - begins_with(SK, f"{doc_id}#") で doc 単位の Query が効く
#   - rev 番号が 06d zero-pad なので SK 辞書順 = rev 昇順
#   - doc 削除時の cascade も begins_with で拾える

def _extract_frontmatter_field(content: str, field: str, default: str = "") -> str:
    if not content.startswith("---"):
        return default
    end = content.find("\n---", 3)
    if end == -1:
        return default
    for line in content[4:end].split("\n"):
        line = line.strip()
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return default


def _extract_scope(content: str) -> str:
    val = _extract_frontmatter_field(content, "scope", "memo")
    return val if val in ("core", "spec", "memo") else "memo"


def _generate_doc_id() -> str:
    # Firestore auto-id 互換の 20 文字英数字 (= 既存 UI / DB の見た目を保つ)
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20))


def list_documents(project_id: str) -> list[dict]:
    items = _query_all(_table("documents"), Key("project_id").eq(project_id))
    result = []
    for data in items:
        if data.get("deleted"):
            continue
        milestone = data.get("milestone") or _extract_frontmatter_field(
            data.get("content", ""), "milestone"
        )
        # e-1859: operation field, same row-first / frontmatter-fallback
        # pattern as milestone. Without this, `beacon doc list --op <op-id>`
        # always returned [] in cloud mode, and `/beacon-operation-review`
        # Step 2 (= SPEC discovery by --op) could not find op-scoped specs.
        operation = data.get("operation") or _extract_frontmatter_field(
            data.get("content", ""), "operation"
        )
        # ms-109 e-3754: canonical target-class-agnostic linkage (acc-/opp-/…)
        # so `doc list --account` etc. work in cloud mode; trek_id alongside
        # (previously omitted) so the tolerant fallback is not cloud-blind.
        target = data.get("target") or _extract_frontmatter_field(
            data.get("content", ""), "target"
        )
        trek_id = data.get("trek_id") or _extract_frontmatter_field(
            data.get("content", ""), "trek_id"
        )
        entry = {
            "doc_id": data.get("doc_id", ""),
            "title": data.get("title", ""),
            "scope": data.get("scope", "memo"),
            "updated_at": data.get("updated_at", ""),
        }
        if milestone:
            entry["milestone"] = milestone
        if operation:
            entry["operation"] = operation
        if target:
            entry["target"] = target
        if trek_id:
            entry["trek_id"] = trek_id
        result.append(entry)
    result.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
    return result


def get_document(project_id: str, doc_id: str) -> dict | None:
    resp = _table("documents").get_item(Key={"project_id": project_id, "doc_id": doc_id})
    return resp.get("Item")


def _last_revision_number(project_id: str, doc_id: str) -> int:
    # SK begins_with クエリで doc の全 revisions を取って max rev を返す。
    # ページング吸収のため _query_all を使う (件数が増えても正しい値が返る)。
    # boto3 / moto は数値を Decimal で返すので int() でまとめてキャスト。
    items = _query_all(
        _table("document_revisions"),
        Key("project_id").eq(project_id) & Key("revision_id").begins_with(f"{doc_id}#"),
    )
    last = 0
    for it in items:
        try:
            r = int(it.get("rev", 0))
        except (TypeError, ValueError):
            continue
        if r > last:
            last = r
    return last


def save_document(project_id: str, doc_id: str, title: str, content: str,
                  scope: str | None = None, updated_by: str = "unknown") -> str:
    import datetime
    resolved_scope = scope if scope in ("core", "spec", "memo") else _extract_scope(content)
    milestone = _extract_frontmatter_field(content, "milestone")
    # e-1859: operation field — extract from frontmatter and store as a
    # column so list_documents can hand it back without re-parsing the
    # full content blob. Symmetric to milestone above.
    operation = _extract_frontmatter_field(content, "operation")
    # ms-69 / e-1663: trek_id is optional (= 既存 doc 影響なし migration 不要)
    trek_id = _extract_frontmatter_field(content, "trek_id")
    now_iso = datetime.datetime.now().isoformat()

    if not doc_id:
        doc_id = _generate_doc_id()

    data = {
        "project_id": project_id,
        "doc_id": doc_id,
        "title": title,
        "content": content,
        "scope": resolved_scope,
        "updated_at": now_iso,
        "updated_by": updated_by,
    }
    if milestone:
        data["milestone"] = milestone
    if operation:
        data["operation"] = operation
    if trek_id:
        data["trek_id"] = trek_id

    # 既存があれば現行 content を revision に積んでから上書き
    existing = get_document(project_id, doc_id)
    if existing:
        next_rev = _last_revision_number(project_id, doc_id) + 1
        _table("document_revisions").put_item(Item={
            "project_id": project_id,
            "revision_id": f"{doc_id}#{next_rev:06d}",
            "doc_id": doc_id,
            "rev": next_rev,
            "content": existing.get("content", ""),
            "title": existing.get("title", ""),
            "ts": existing.get("updated_at", ""),
            "saved_by": existing.get("updated_by", "unknown"),
        })

    _table("documents").put_item(Item=data)
    return doc_id


def list_document_revisions(project_id: str, doc_id: str) -> list:
    items = _query_all(
        _table("document_revisions"),
        Key("project_id").eq(project_id) & Key("revision_id").begins_with(f"{doc_id}#"),
    )
    # rev DESC (= Firestore order_by("rev", DESCENDING) と一致)
    items.sort(key=lambda it: it.get("rev", 0), reverse=True)
    return [{"rev": it.get("rev"), "ts": it.get("ts"), "saved_by": it.get("saved_by")}
            for it in items]


def get_document_revision(project_id: str, doc_id: str, rev: int) -> dict | None:
    resp = _table("document_revisions").get_item(Key={
        "project_id": project_id,
        "revision_id": f"{doc_id}#{int(rev):06d}",
    })
    it = resp.get("Item")
    if not it:
        return None
    return {
        "rev": it.get("rev"),
        "content": it.get("content", ""),
        "title": it.get("title", ""),
        "ts": it.get("ts"),
        "saved_by": it.get("saved_by"),
    }


def delete_document(project_id: str, doc_id: str, deleted_by: str = "unknown",
                    reason: str = "") -> bool:
    """Soft-delete a document (sets deleted flag).

    Optional ``reason`` is stored as ``trash_reason`` for audit symmetry
    with local mode's frontmatter (ms-14 e-991). Clears any prior
    restore stamps so audit fields reflect the current trash event
    (= Firestore 版 DELETE_FIELD と等価、DynamoDB は REMOVE で表現)。
    Returns True if existed.
    """
    import datetime
    if get_document(project_id, doc_id) is None:
        return False
    set_parts = [
        "#deleted = :dt",
        "deleted_at = :dat",
        "deleted_by = :dby",
    ]
    remove_parts = ["restored_at", "restored_by", "restore_reason"]
    names = {"#deleted": "deleted"}
    values = {
        ":dt": True,
        ":dat": datetime.datetime.now().isoformat(),
        ":dby": deleted_by,
    }
    if reason:
        set_parts.append("trash_reason = :tr")
        values[":tr"] = reason
    else:
        remove_parts.append("trash_reason")
    update_expr = "SET " + ", ".join(set_parts) + " REMOVE " + ", ".join(remove_parts)
    _table("documents").update_item(
        Key={"project_id": project_id, "doc_id": doc_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    return True


def sweep_trashed_documents(project_id: str, *, days: int = 30,
                            dry_run: bool = False) -> list[str]:
    """Hard-delete soft-deleted docs older than ``days`` (ms-14 e-991).

    Docs with ``deleted=true`` but missing ``deleted_at`` are NOT swept
    (mirrors the MS / task rule — no timestamp, no proof the window has
    passed). Revisions are cascaded since DynamoDB has no native cascade.
    """
    import datetime
    cutoff_iso = (datetime.datetime.now()
                  - datetime.timedelta(days=max(1, days))).isoformat()
    items = _query_all(
        _table("documents"),
        Key("project_id").eq(project_id),
    )
    purged: list[str] = []
    docs_table = _table("documents")
    revs_table = _table("document_revisions")
    for it in items:
        if not it.get("deleted"):
            continue
        deleted_at = it.get("deleted_at", "")
        if not deleted_at or deleted_at >= cutoff_iso:
            continue
        doc_id = it.get("doc_id", "")
        if not doc_id:
            continue
        purged.append(doc_id)
        if dry_run:
            continue
        # Cascade revisions first then delete the doc itself
        rev_items = _query_all(
            revs_table,
            Key("project_id").eq(project_id) & Key("revision_id").begins_with(f"{doc_id}#"),
        )
        if rev_items:
            with revs_table.batch_writer() as batch:
                for rev in rev_items:
                    batch.delete_item(Key={
                        "project_id": project_id,
                        "revision_id": rev["revision_id"],
                    })
        docs_table.delete_item(Key={"project_id": project_id, "doc_id": doc_id})
    return purged


# ---------------------------------------------------------------------------
# Active claims (subcollection)  ms-55 e-1730
# ---------------------------------------------------------------------------
#
# PK=project_id, SK=claim_id. Mirrors Firestore subcollection (see
# server/firestore_client.py for the design rationale).

def list_active_claims(project_id: str) -> list[dict]:
    items = _query_all(_table("active_claims"), Key("project_id").eq(project_id))
    out: list[dict] = []
    for it in items:
        data = dict(it)
        # PK / SK are inlined onto every item; surface claim_id even if
        # the stored payload lost it (= legacy back-fill).
        data.setdefault("claim_id", data.pop("claim_id", "") or "")
        # Strip the storage PK from the wire shape — clients don't need it.
        data.pop("project_id", None)
        out.append(data)
    out.sort(key=lambda r: r.get("issued_at") or "")
    return out


def get_active_claim(project_id: str, claim_id: str) -> dict | None:
    resp = _table("active_claims").get_item(
        Key={"project_id": project_id, "claim_id": claim_id},
    )
    item = resp.get("Item")
    if not item:
        return None
    data = dict(item)
    data.pop("project_id", None)
    data.setdefault("claim_id", claim_id)
    return data


def save_active_claim(project_id: str, claim_id: str, payload: dict) -> str:
    if not claim_id:
        raise ValueError("claim_id is required")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    item = {**payload, "project_id": project_id, "claim_id": claim_id}
    _table("active_claims").put_item(Item=item)
    return claim_id


def delete_active_claim(project_id: str, claim_id: str) -> bool:
    table = _table("active_claims")
    existing = table.get_item(
        Key={"project_id": project_id, "claim_id": claim_id},
    ).get("Item")
    if not existing:
        return False
    table.delete_item(Key={"project_id": project_id, "claim_id": claim_id})
    return True


# ---------------------------------------------------------------------------
# Changelog (subcollection)
# ---------------------------------------------------------------------------

def append_changelog(project_id: str, entry: dict) -> str:
    raise _not_implemented("append_changelog")


def list_changelog(project_id: str, *, since: str | None = None,
                   limit: int = 100) -> list[dict]:
    raise _not_implemented("list_changelog")


# ---------------------------------------------------------------------------
# Notes (subcollection)
# ---------------------------------------------------------------------------

def add_note(project_id: str, note: dict) -> str:
    raise _not_implemented("add_note")


def list_notes(project_id: str) -> list[dict]:
    raise _not_implemented("list_notes")


def clear_notes(project_id: str) -> None:
    raise _not_implemented("clear_notes")


# ---------------------------------------------------------------------------
# Bus events / cursors / nonces / audit  (e-1544 Phase 3)
# ---------------------------------------------------------------------------
# Tables:
#   beacon-{env}-bus_events    PK=project_id, SK=event_id
#   beacon-{env}-bus_cursors   PK=project_id, SK=cursor_id (= recipient_id)
#   beacon-{env}-bus_nonces    PK=project_id, SK=nonce
#   beacon-{env}-bus_audit     PK=project_id, SK=audit_id
#
# event_id / audit_id は millisecond-precision timestamp prefix にして
# SK 辞書順 = 時系列順とする。SK Sort で created_at ORDER BY 相当が無料で
# 効くので、list_bus_events / list_bus_audit の since カーソルは
# event_id / audit_id 自体の KeyCondition で push できる
# (FilterExpression より安く帯域も食わない)。

def _mint_timestamp_id() -> str:
    """Millisecond-prefix sortable id (= "<epoch_ms:013>-<rand6>").

    辞書順 = 時系列順を保証しつつ、ms 同一書き込みが衝突しないよう
    後ろに 6 字のランダム英数字を付ける。Firestore auto-id (20 字
    base62) に近い形状。
    """
    import secrets
    import string
    import time
    ms = int(time.time() * 1000)
    alphabet = string.ascii_letters + string.digits
    rand = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"{ms:013d}-{rand}"


def append_bus_event(project_id: str, data: dict) -> str:
    """Append a bus event (auto-id) and return the event_id.

    呼び出し側が data に created_at (ISO8601) を含めることが前提
    (firestore_client と同じ契約)。event_id は ms-prefix なので
    SK 辞書順 = チャネル横断の時系列順になる。
    """
    event_id = _mint_timestamp_id()
    item = {**data, "project_id": project_id, "event_id": event_id}
    _table("bus_events").put_item(Item=item)
    return event_id


def get_bus_cursor(project_id: str, recipient_id: str) -> dict:
    resp = _table("bus_cursors").get_item(
        Key={"project_id": project_id, "cursor_id": recipient_id}
    )
    item = resp.get("Item")
    if not item:
        return {}
    # cursor_id (= recipient_id) は内部キー、callers には返さない
    return {k: v for k, v in item.items() if k not in ("project_id", "cursor_id")}


def advance_bus_cursor(project_id: str, recipient_id: str,
                       last_seen_at: str) -> dict:
    """Forward-only cursor advance. Returns the resulting cursor.

    既存カーソルより新しい last_seen_at だけ書き込む。同一 / 古い
    値の場合は既存を返して idempotent な no-op にする。
    structural 防御: stale クライアントが古い値で巻き戻しを起こせない。
    """
    import datetime
    table = _table("bus_cursors")
    existing_resp = table.get_item(
        Key={"project_id": project_id, "cursor_id": recipient_id}
    )
    existing = existing_resp.get("Item") or {}
    existing_seen = existing.get("last_seen_at", "")
    if existing_seen and last_seen_at <= existing_seen:
        return {k: v for k, v in existing.items() if k not in ("project_id", "cursor_id")}
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    table.update_item(
        Key={"project_id": project_id, "cursor_id": recipient_id},
        UpdateExpression="SET last_seen_at = :ls, updated_at = :ua",
        ExpressionAttributeValues={":ls": last_seen_at, ":ua": now},
    )
    return {"last_seen_at": last_seen_at, "updated_at": now}


def check_and_record_bus_nonce(project_id: str, nonce: str,
                               expires_at: str) -> bool:
    """Return True iff ``nonce`` is fresh; atomically record it on first use.

    ConditionExpression "attribute_not_exists" で put_item を行うことで
    Firestore transaction と同じ "最初の 1 回だけ True" を実現する。
    競合時には ConditionalCheckFailedException が出るので False を返す。
    """
    import datetime
    table = _table("bus_nonces")
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    try:
        table.put_item(
            Item={
                "project_id": project_id,
                "nonce": nonce,
                "expires_at": expires_at,
                "recorded_at": now,
            },
            ConditionExpression="attribute_not_exists(#n)",
            ExpressionAttributeNames={"#n": "nonce"},
        )
        return True
    except Exception as e:
        # boto3 のクライアントエラーは ClientError、moto も同様。
        # 名前で識別する (= import パスが boto3 / botocore で揺れない)。
        if e.__class__.__name__ == "ConditionalCheckFailedException":
            return False
        if hasattr(e, "response") and isinstance(getattr(e, "response", None), dict):
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
        raise


def set_bus_event_receipt(project_id: str, event_id: str, stage: str,
                          recipient_session_id: str) -> dict | None:
    """Stamp a per-event receipt timestamp (DM read receipt, e-1348).

    First-write-wins per stage (= delivered/opened). 既存 stamp があれば
    上書きせずに ``already_set=True`` で返す。ConditionExpression で
    "attribute_not_exists(<stage>_at)" を立てて二重書きを構造的に防ぐ。

    Returns ``None`` if the event does not exist.
    """
    import datetime
    if stage not in ("delivered", "opened"):
        raise ValueError(f"set_bus_event_receipt: invalid stage {stage!r}")
    ts_field = f"{stage}_at"
    by_field = f"{stage}_by"
    table = _table("bus_events")

    existing = table.get_item(
        Key={"project_id": project_id, "event_id": event_id}
    ).get("Item")
    if not existing:
        return None

    existing_ts = existing.get(ts_field)
    if existing_ts:
        return {
            "event_id": event_id,
            "stage": stage,
            "timestamp": existing_ts,
            "by": existing.get(by_field, ""),
            "already_set": True,
        }
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    try:
        table.update_item(
            Key={"project_id": project_id, "event_id": event_id},
            UpdateExpression="SET #ts = :ts, #by = :by",
            ConditionExpression="attribute_not_exists(#ts)",
            ExpressionAttributeNames={"#ts": ts_field, "#by": by_field},
            ExpressionAttributeValues={":ts": now, ":by": recipient_session_id},
        )
    except Exception as e:
        # 競合: 別 caller が先に stamp した。最新を読み直して already_set で返す
        if (e.__class__.__name__ == "ConditionalCheckFailedException"
                or (hasattr(e, "response")
                    and isinstance(getattr(e, "response", None), dict)
                    and e.response.get("Error", {}).get("Code", "") == "ConditionalCheckFailedException")):
            cur = table.get_item(
                Key={"project_id": project_id, "event_id": event_id}
            ).get("Item") or {}
            return {
                "event_id": event_id,
                "stage": stage,
                "timestamp": cur.get(ts_field, ""),
                "by": cur.get(by_field, ""),
                "already_set": True,
            }
        raise
    return {
        "event_id": event_id,
        "stage": stage,
        "timestamp": now,
        "by": recipient_session_id,
        "already_set": False,
    }


def find_bus_event(project_id: str, event_id: str) -> dict | None:
    resp = _table("bus_events").get_item(
        Key={"project_id": project_id, "event_id": event_id}
    )
    return resp.get("Item")


def append_bus_audit(project_id: str, record: dict) -> str:
    audit_id = _mint_timestamp_id()
    _table("bus_audit").put_item(
        Item={**record, "project_id": project_id, "audit_id": audit_id}
    )
    return audit_id


def list_bus_audit(project_id: str, *, since: str = "",
                   limit: int = 100) -> list[dict]:
    """List audit records ordered by received_at (most recent at end).

    since は received_at に対する > 比較。SK (audit_id) は ms-prefix で
    時系列順に並ぶので、since から派生する audit_id 範囲で KeyCondition を
    絞ることもできるが、received_at は caller が record に格納する任意
    フィールドなので「received_at と audit_id が必ず時系列一致する」とは
    限らない。安全側で SK は全 query、received_at は FilterExpression に
    して整合性を保つ。
    """
    kwargs = {"KeyConditionExpression": Key("project_id").eq(project_id)}
    if since:
        kwargs["FilterExpression"] = Attr("received_at").gt(since)
    items = []
    while True:
        resp = _table("bus_audit").query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    items.sort(key=lambda it: it.get("received_at", ""))
    if limit:
        items = items[:limit]
    return items


def list_bus_events(project_id: str, since: str = "", channel: str = "",
                    limit: int = 100) -> list[dict]:
    """List bus events ordered by created_at.

    Firestore 版と同じ契約:
      - since: created_at > since の event のみ返す
      - channel: equality filter (in-memory)
      - limit: 結果件数の上限

    実装ストラテジ:
      - event_id (SK) は ms-prefix のため辞書順 = 時系列順、Query 結果は
        昇順で並ぶ
      - since / channel は FilterExpression に push して帯域節約
      - limit は filter 後に caller 側で切る (Firestore 版と同じ意味論)
    """
    kwargs = {"KeyConditionExpression": Key("project_id").eq(project_id)}
    filters = []
    values = {}
    names = {}
    if since:
        filters.append("created_at > :since")
        values[":since"] = since
    if channel:
        filters.append("#chan = :chan")
        names["#chan"] = "channel"
        values[":chan"] = channel
    if filters:
        kwargs["FilterExpression"] = " AND ".join(filters)
        if values:
            kwargs["ExpressionAttributeValues"] = values
        if names:
            kwargs["ExpressionAttributeNames"] = names
    # Fetch with a soft cap so channel-narrow queries still satisfy `limit`.
    # 単一 query にすると LastEvaluatedKey が返らないので、必要なら pagination
    # で次を取りに行く (= Firestore 版が "fetch_cap = limit * 5" でやってた
    # オーバーフェッチの代替)。
    fetch_cap = (limit * 5) if (limit and channel) else (limit or 0)
    items: list[dict] = []
    while True:
        resp = _table("bus_events").query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        if fetch_cap and len(items) >= fetch_cap:
            break
        kwargs["ExclusiveStartKey"] = last
    # SK 辞書順 = ms-prefix の時系列順なので Query 結果は既に created_at 順に
    # 近い。但し caller が created_at を別タイムソースで埋めることがあるので
    # 念のため明示ソート (idempotent)。
    items.sort(key=lambda it: it.get("created_at", ""))
    if limit:
        items = items[:limit]
    return items


# ---------------------------------------------------------------------------
# Bus event approvals (ms-70 / e-1712)
# ---------------------------------------------------------------------------
# Table: beacon-{env}-bus_event_approvals, PK=project_id, SK=event_id
#
# Receiver-side decision_stamp sidecar mirrored byte-for-byte with the
# Firestore implementation (server/firestore_client.py). The bus_event
# envelope itself stays in beacon-{env}-bus_events untouched — flipping a
# field on it would break the HMAC signature (= ms-54 / e-1155 design).
# This sidecar table stores only the mutable receiver decision:
#
#   event_id           : str           (= SK, same id as parent bus_event)
#   approval_status    : str           (= "pending" | "approved" | "denied" | "auto")
#   decision_by        : str | None    (= user_id of decider, None while pending)
#   decision_at        : str | None    (= ISO8601 of decision, None while pending)
#   created_at         : str           (= ISO8601 of sidecar first write)
#   sender_user_id     : str
#   receiver_user_id   : str
#
# Migration: bus_events without a sidecar row are read as
# approval_status="auto" by callers (= same-user DMs / shared-Trek blanket
# allow). get_bus_event_approval returns None for that case; the caller
# must treat None == "auto" rather than "missing".

# Module-level alias mirrors the firestore_client.py constant so callers can
# import either backend and refer to the SK / subcollection name uniformly.
_BUS_EVENT_APPROVAL_STATUSES = ("pending", "approved", "denied", "auto")


def get_bus_event_approval(project_id: str, event_id: str) -> dict | None:
    """Return the sidecar approval doc for ``event_id`` or None if absent.

    None is the **legacy / auto-allow** signal — callers must treat None as
    ``approval_status="auto"`` for backwards compatibility with bus_events
    written before this sidecar landed (= ms-70 / e-1712).
    """
    resp = _table("bus_event_approvals").get_item(
        Key={"project_id": project_id, "event_id": event_id}
    )
    item = resp.get("Item")
    if not item:
        return None
    # project_id は内部キー、callers には返さない (= firestore 版が doc.id を
    # event_id にマップする shape と同じ surface にする)。
    return {k: v for k, v in item.items() if k != "project_id"}


def put_bus_event_approval(project_id: str, event_id: str, *,
                           approval_status: str,
                           sender_user_id: str,
                           receiver_user_id: str,
                           decision_by: str | None = None,
                           decision_at: str | None = None) -> dict:
    """Write or update the sidecar approval row for ``event_id``.

    First call for an event_id sets ``created_at`` to the server clock;
    subsequent calls (= pending → approved | denied) update the decision
    fields but preserve the original ``created_at``. This mirrors the
    Firestore implementation's lifecycle (= ms-70 / e-1712 design).

    Returns the resulting row as a 7-field dict (without the internal
    project_id key), matching :func:`get_bus_event_approval`'s shape.

    Raises ``ValueError`` for invalid ``approval_status``.
    """
    if approval_status not in _BUS_EVENT_APPROVAL_STATUSES:
        raise ValueError(
            f"put_bus_event_approval: invalid approval_status "
            f"{approval_status!r} (allowed: {_BUS_EVENT_APPROVAL_STATUSES})"
        )
    table = _table("bus_event_approvals")
    existing = table.get_item(
        Key={"project_id": project_id, "event_id": event_id}
    ).get("Item") or {}
    now = _now_iso_utc()
    created_at = existing.get("created_at") or now
    item = {
        "project_id": project_id,
        "event_id": event_id,
        "approval_status": approval_status,
        "decision_by": decision_by,
        "decision_at": decision_at,
        "created_at": created_at,
        "sender_user_id": sender_user_id,
        "receiver_user_id": receiver_user_id,
    }
    table.put_item(Item=item)
    return {k: v for k, v in item.items() if k != "project_id"}


def list_pending_approvals(project_id: str, *,
                           receiver_user_id: str | None = None,
                           limit: int = 100) -> list[dict]:
    """List sidecar rows in ``approval_status="pending"`` ordered by created_at.

    ``receiver_user_id`` (optional): restrict to rows where the receiver
    matches — applied in-memory after the FilterExpression on
    ``approval_status``. No GSI is provisioned (= dev scale, mirrors the
    Firestore version's "no composite index" trade-off).

    ``limit``: cap returned rows; defaults to 100. Applied after the
    receiver filter so a heavy other-receiver burst does not starve the
    caller's "my pending" query.
    """
    kwargs = {
        "KeyConditionExpression": Key("project_id").eq(project_id),
        "FilterExpression": Attr("approval_status").eq("pending"),
    }
    # Soft over-fetch to satisfy `limit` after in-memory receiver filter.
    fetch_cap = (limit * 5) if (limit and receiver_user_id) else (limit or 0)
    items: list[dict] = []
    while True:
        resp = _table("bus_event_approvals").query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        if fetch_cap and len(items) >= fetch_cap:
            break
        kwargs["ExclusiveStartKey"] = last
    items.sort(key=lambda it: it.get("created_at", ""))
    rows = [{k: v for k, v in it.items() if k != "project_id"} for it in items]
    if receiver_user_id:
        rows = [r for r in rows if r.get("receiver_user_id") == receiver_user_id]
    if limit:
        rows = rows[:limit]
    return rows


def list_decided_approvals(project_id: str, *, limit: int = 50) -> list[dict]:
    """List sidecar rows in approval_status in {"approved","denied"} (= ms-70 / e-1718).

    DynamoDB mirror of :func:`firestore_client.list_decided_approvals`. Same
    contract: pending / auto excluded, newest-first by ``decision_at``
    (fallback ``created_at``), cap at ``limit``.

    Single Query on the project_id PK with a FilterExpression matching
    approved OR denied. No GSI provisioned — dev scale only, mirrors the
    "no composite index" trade-off used by list_pending_approvals.
    """
    kwargs = {
        "KeyConditionExpression": Key("project_id").eq(project_id),
        "FilterExpression": (
            Attr("approval_status").eq("approved")
            | Attr("approval_status").eq("denied")
        ),
    }
    items: list[dict] = []
    fetch_cap = (limit * 2) if limit else 0
    while True:
        resp = _table("bus_event_approvals").query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        if fetch_cap and len(items) >= fetch_cap:
            break
        kwargs["ExclusiveStartKey"] = last
    rows = [{k: v for k, v in it.items() if k != "project_id"} for it in items]
    def _sort_key(r: dict) -> str:
        return r.get("decision_at") or r.get("created_at") or ""
    rows.sort(key=_sort_key, reverse=True)
    if limit:
        rows = rows[:limit]
    return rows


# ---------------------------------------------------------------------------
# Sessions (e-1544 Phase 4)
# ---------------------------------------------------------------------------
# Table: beacon-{env}-sessions, PK=project_id, SK=session_id

def _now_iso_utc() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _build_set_update(data: dict) -> tuple[str, dict, dict]:
    """Build a SET UpdateExpression that mirrors Firestore set(merge=True).

    data の各 key を SET <#k> = <:v> として並べる。予約語ガードのため
    必ず ExpressionAttributeNames 経由で渡す。
    """
    sets = []
    names = {}
    values = {}
    for i, (k, v) in enumerate(data.items()):
        np = f"#k{i}"
        vp = f":v{i}"
        sets.append(f"{np} = {vp}")
        names[np] = k
        values[vp] = v
    return ("SET " + ", ".join(sets), names, values)


def upsert_session(project_id: str, session_id: str, data: dict) -> None:
    """Upsert a session document by session_id (merge semantics).

    Firestore の set(merge=True) は「指定された key だけ書く、ほかは
    触らない」。DynamoDB の UpdateExpression SET も同じ意味論で動く。
    空 data は no-op (Firestore も実質同様)。
    """
    if not data:
        return
    expr, names, values = _build_set_update(data)
    _table("sessions").update_item(
        Key={"project_id": project_id, "session_id": session_id},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def stamp_session_actor_email(project_id: str, session_id: str,
                              email: str) -> None:
    """Stamp ``actor.email`` on a session document without touching siblings
    (= ms-54 e-1349; Firestore 版は dotted-path update に相当する)。

    DynamoDB は nested document path への部分 update をサポートする
    ("SET actor.email = :e")。親の actor map が存在しない場合は
    ValidationException になるので、その時は actor=map 全体を merge で書き直す。
    """
    if not email:
        return
    try:
        _table("sessions").update_item(
            Key={"project_id": project_id, "session_id": session_id},
            UpdateExpression="SET #actor.#email = :e",
            ExpressionAttributeNames={"#actor": "actor", "#email": "email"},
            ExpressionAttributeValues={":e": email},
        )
    except Exception:
        # 親 map が無いケース: actor 全体を書く (= heartbeat と race 中に
        # 一瞬 machine/agent が落ちる可能性があるが Firestore 版と同じ挙動)
        _table("sessions").update_item(
            Key={"project_id": project_id, "session_id": session_id},
            UpdateExpression="SET #actor = :a",
            ExpressionAttributeNames={"#actor": "actor"},
            ExpressionAttributeValues={":a": {"email": email}},
        )


def list_sessions(project_id: str) -> list[dict]:
    """List sessions for a project ordered by last_active desc."""
    items = _query_all(_table("sessions"), Key("project_id").eq(project_id))
    items.sort(key=lambda it: it.get("last_active", ""), reverse=True)
    # session_id (= SK) は item にも入っているが Firestore 版は最後に上書きする
    # ので key win 順序を揃える。
    return [{**it, "session_id": it.get("session_id", "")} for it in items]


# ---------------------------------------------------------------------------
# Machines + session minting (ms-62 / e-1509)  e-1544 Phase 4
# ---------------------------------------------------------------------------
# Tables:
#   beacon-{env}-machines        PK=user_id,    SK=fingerprint_id
#   beacon-{env}-session_lookup  PK=project_id, SK=lookup_key
#                                ("{machine_id}_{parent_pid}")

def _safe_doc_id(s: str) -> str:
    """Firestore safe id とほぼ同じ。/ を含むか長すぎる場合はハッシュ化。"""
    import hashlib
    if not s:
        return "_empty"
    if "/" in s or len(s) > 200:
        return "h-" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]
    return s


def get_or_mint_machine(user_id: str, fingerprint: str, *,
                        hostname: str = "", agent: str = "") -> tuple[str, bool]:
    """Get or mint machine_id for (user_id, fingerprint).

    Returns ``(machine_id, minted)``. The fingerprint is sanitised for use
    as the SK; the returned machine_id is a fresh server-side nonce so the
    value itself never leaks the fingerprint.
    """
    import secrets
    if not user_id or not fingerprint:
        raise ValueError("user_id and fingerprint are required")

    fingerprint_id = _safe_doc_id(fingerprint)
    table = _table("machines")
    now = _now_iso_utc()

    existing = table.get_item(
        Key={"user_id": user_id, "fingerprint_id": fingerprint_id}
    ).get("Item")
    if existing:
        table.update_item(
            Key={"user_id": user_id, "fingerprint_id": fingerprint_id},
            UpdateExpression="SET last_seen_at = :ls",
            ExpressionAttributeValues={":ls": now},
        )
        return existing.get("machine_id", ""), False

    machine_id = f"mc-{secrets.token_hex(8)}"
    table.put_item(Item={
        "user_id": user_id,
        "fingerprint_id": fingerprint_id,
        "machine_id": machine_id,
        "fingerprint": fingerprint,
        "hostname": hostname or fingerprint,
        "agent": agent,
        "created_at": now,
        "last_seen_at": now,
    })
    return machine_id, True


def get_or_mint_session_by_tuple(project_id: str, machine_id: str,
                                 parent_pid: int, *, user_id: str,
                                 cwd: str = "",
                                 metadata: dict | None = None) -> dict:
    """Server-side identity tuple → session_id resolver (= ms-62 e-1509)。

    Resolve (project_id, machine_id, parent_pid) to a session_id. Lookup
    miss → mint, register lookup doc + session doc, return.

    DynamoDB transaction (TransactWriteItems) は put + put の組合せだけ
    なら使えるが、Firestore 版も実は「race で 2 つ mint されたら新しい方が
    勝つ」程度の保証しかない (lookup_ref が後勝ち)。同じ挙動になるよう
    sequential put にする。
    """
    import secrets
    import time

    if not project_id or not machine_id or not isinstance(parent_pid, int):
        raise ValueError("project_id, machine_id, parent_pid are required")

    lookup_key = _safe_doc_id(f"{machine_id}_{parent_pid}")
    sessions_table = _table("sessions")
    lookup_table = _table("session_lookup")
    now = _now_iso_utc()

    existing_lookup = lookup_table.get_item(
        Key={"project_id": project_id, "lookup_key": lookup_key}
    ).get("Item")
    if existing_lookup:
        sid = existing_lookup.get("session_id", "")
        if sid:
            # Refresh last_heartbeat_at + metadata on the session doc itself
            heartbeat_data = {
                "last_heartbeat_at": now,
                "machine_id": machine_id,
                "parent_pid": parent_pid,
                "cwd": cwd,
            }
            if metadata:
                heartbeat_data.update(metadata)
            expr, names, values = _build_set_update(heartbeat_data)
            sessions_table.update_item(
                Key={"project_id": project_id, "session_id": sid},
                UpdateExpression=expr,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            existing_session = sessions_table.get_item(
                Key={"project_id": project_id, "session_id": sid}
            ).get("Item") or {}
            return {
                "session_id": sid,
                "minted": False,
                "last_heartbeat_at": now,
                "created_at": existing_session.get("created_at", ""),
            }

    # Mint a fresh session_id (= Firestore 実装と同じ形式)
    epoch_ms = int(time.time() * 1000)
    prefix = machine_id[3:11] if machine_id.startswith("mc-") else machine_id[:8]
    new_sid = f"sv-{prefix}-{epoch_ms}-{secrets.token_hex(4)}"

    session_data = {
        "project_id": project_id,
        "session_id": new_sid,
        "user_id": user_id,
        "machine_id": machine_id,
        "parent_pid": parent_pid,
        "cwd": cwd,
        "created_at": now,
        "last_heartbeat_at": now,
        "last_active": now,
        "source": "server_minted",
    }
    if metadata:
        session_data.update(metadata)
    sessions_table.put_item(Item=session_data)
    lookup_table.put_item(Item={
        "project_id": project_id,
        "lookup_key": lookup_key,
        "session_id": new_sid,
        "machine_id": machine_id,
        "parent_pid": parent_pid,
        "created_at": now,
    })
    return {
        "session_id": new_sid,
        "minted": True,
        "last_heartbeat_at": now,
        "created_at": now,
    }


def list_user_machines(user_id: str) -> list[dict]:
    return _query_all(_table("machines"), Key("user_id").eq(user_id))


# ---------------------------------------------------------------------------
# Session logs (ms-57 / e-1037)  e-1544 Phase 4
# ---------------------------------------------------------------------------
# Table: beacon-{env}-session_logs, PK=project_id, SK=session_id

def upsert_session_log(project_id: str, session_id: str, data: dict) -> None:
    """Upsert a session log entry. merge semantics same as upsert_session."""
    if not data:
        return
    expr, names, values = _build_set_update(data)
    _table("session_logs").update_item(
        Key={"project_id": project_id, "session_id": session_id},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def list_session_logs(project_id: str,
                      limit: int | None = None) -> list[dict]:
    """List session logs ordered by last_aggregated_at desc."""
    items = _query_all(_table("session_logs"), Key("project_id").eq(project_id))
    items.sort(key=lambda it: it.get("last_aggregated_at", ""), reverse=True)
    if limit:
        items = items[:limit]
    return items


def get_session_log(project_id: str, session_id: str) -> dict | None:
    resp = _table("session_logs").get_item(
        Key={"project_id": project_id, "session_id": session_id}
    )
    return resp.get("Item")


# ---------------------------------------------------------------------------
# Operation envelopes (Tier 2) — ms-60 / e-1339  e-1544 Phase 4
# ---------------------------------------------------------------------------
# Table: beacon-{env}-operation_envelopes, PK=project_id, SK=envelope_id
#
# Invariant: at most one envelope per op_id has status="active". issue が
# 先に既存 active を revoke してから新 envelope を put する。Firestore 版は
# 1 つの transaction でやるが、DynamoDB は TransactWriteItems で同じ
# atomicity が取れる範囲なら使う。ここでは active が 0 or 1 件のみと
# いう前提なので sequential update でも race window は十分狭い (= Firestore
# 版が transaction で守りたかった "2 件 active" がそもそも発生確率が
# 極小)。万一二重 active が観測されたときの read 側 fallback は Firestore
# 版と同じく「created_at 最新を採用」。

def get_active_operation_envelope(project_id: str,
                                  op_id: str) -> dict | None:
    items = _query_all(
        _table("operation_envelopes"),
        Key("project_id").eq(project_id),
    )
    actives = [it for it in items
               if it.get("op_id") == op_id and it.get("status") == "active"]
    if not actives:
        return None
    if len(actives) > 1:
        actives.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return actives[0]


def issue_operation_envelope(project_id: str, op_id: str, spec_doc_id: str,
                             spec_revision_id: str, envelope_dict: dict,
                             approved_actions: list[str],
                             created_by: str) -> dict:
    """Store a freshly minted T2 envelope. Auto-revokes any prior active."""
    nonce = envelope_dict.get("nonce")
    if not nonce:
        raise ValueError("envelope_dict missing nonce")
    now = _now_iso_utc()
    table = _table("operation_envelopes")

    # 既存 actives を revoke
    items = _query_all(table, Key("project_id").eq(project_id))
    for it in items:
        if it.get("op_id") == op_id and it.get("status") == "active":
            table.update_item(
                Key={"project_id": project_id,
                     "envelope_id": it.get("envelope_id", "")},
                UpdateExpression=(
                    "SET #status = :revoked, revoked_at = :ra, "
                    "revoked_by = :rb, revoke_reason = :rr"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":revoked": "revoked",
                    ":ra": now,
                    ":rb": created_by,
                    ":rr": "superseded by new approve",
                },
            )

    new_doc = {
        "project_id": project_id,
        "envelope_id": nonce,
        "op_id": op_id,
        "spec_doc_id": spec_doc_id,
        "spec_revision_id": spec_revision_id,
        "envelope": envelope_dict,
        "approved_actions": list(approved_actions),
        "status": "active",
        "created_at": now,
        "created_by": created_by,
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
    }
    table.put_item(Item=new_doc)
    return new_doc


def revoke_operation_envelope(project_id: str, envelope_id: str,
                              revoked_by: str, reason: str) -> dict | None:
    """Mark envelope as revoked. Idempotent (already-revoked → return as-is)."""
    table = _table("operation_envelopes")
    existing = table.get_item(
        Key={"project_id": project_id, "envelope_id": envelope_id}
    ).get("Item")
    if not existing:
        return None
    if existing.get("status") == "revoked":
        return existing
    now = _now_iso_utc()
    table.update_item(
        Key={"project_id": project_id, "envelope_id": envelope_id},
        UpdateExpression=(
            "SET #status = :revoked, revoked_at = :ra, "
            "revoked_by = :rb, revoke_reason = :rr"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":revoked": "revoked",
            ":ra": now,
            ":rb": revoked_by,
            ":rr": reason,
        },
    )
    existing.update({
        "status": "revoked",
        "revoked_at": now,
        "revoked_by": revoked_by,
        "revoke_reason": reason,
    })
    return existing


def list_operation_envelopes(project_id: str, op_id: str | None = None,
                             status: str | None = None) -> list[dict]:
    items = _query_all(
        _table("operation_envelopes"),
        Key("project_id").eq(project_id),
    )
    if op_id:
        items = [it for it in items if it.get("op_id") == op_id]
    if status:
        items = [it for it in items if it.get("status") == status]
    items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return items


def get_operation_envelope(project_id: str,
                           envelope_id: str) -> dict | None:
    resp = _table("operation_envelopes").get_item(
        Key={"project_id": project_id, "envelope_id": envelope_id}
    )
    return resp.get("Item")


# ---------------------------------------------------------------------------
# Treks (ms-69 / e-1652)  — top-level entity, PK=trek_id
#
# Same Schema as firestore_client. Table: beacon-{env}-treks.
# Subcollections (activity / summaries / claims) land in later tasks
# (e-1663 / e-1657); they will be separate tables with PK=trek_id, SK=*.
# ---------------------------------------------------------------------------

def get_trek(trek_id: str) -> dict | None:
    resp = _table("treks").get_item(Key={"trek_id": trek_id})
    return resp.get("Item")


def save_trek(trek_id: str, data: dict) -> None:
    # PK is trek_id; caller-supplied trek_id in ``data`` is overwritten on
    # purpose (= same shape as save_project / save_document).
    item = {**data, "trek_id": trek_id}
    _table("treks").put_item(Item=item)


def list_treks(actor_id: str | None = None, *,
               status: str | None = None,
               include_archived: bool = False) -> list[dict]:
    """List treks. See firestore_client.list_treks for semantics.

    Scan is fine at dev scale; once trek counts grow, owner / member GSI
    will let us push the visibility filter to DynamoDB.
    """
    items = _scan_all(_table("treks"))
    result: list[dict] = []
    for item in items:
        if not include_archived and item.get("status") == "archived":
            continue
        if status and item.get("status") != status:
            continue
        if actor_id:
            creator = (item.get("creator_actor") or {}).get("user_id")
            members = [m.get("user_id")
                       for m in item.get("members", []) or []]
            if creator != actor_id and actor_id not in members:
                continue
        result.append(item)
    # Newest first (= matches firestore_client ordering).
    result.sort(key=lambda t: (t.get("created_at", ""), t.get("trek_id", "")),
                reverse=True)
    return result


def delete_trek(trek_id: str) -> bool:
    if get_trek(trek_id) is None:
        return False
    # Phase 1: top-level delete only. Subcollection cascade follows the
    # delete_project pattern once e-1663 lands.
    _table("treks").delete_item(Key={"trek_id": trek_id})
    return True


# ---------------------------------------------------------------------------
# Organizations (ms-113 / e-3731) — top-level entity, PK=org_id.
# Same schema as firestore_client. Scan is fine at dev scale.
# ---------------------------------------------------------------------------

def get_org(org_id: str) -> dict | None:
    resp = _table("organizations").get_item(Key={"org_id": org_id})
    return resp.get("Item")


def save_org(org_id: str, data: dict) -> None:
    item = {**data, "org_id": org_id}
    _table("organizations").put_item(Item=item)


def list_orgs_for_user(user_id: str | None = None) -> list[dict]:
    """List organizations. See firestore_client.list_orgs_for_user semantics."""
    items = _scan_all(_table("organizations"))
    result: list[dict] = []
    for item in items:
        if user_id:
            members = [m.get("user_id") for m in item.get("members", []) or []]
            if user_id not in members:
                continue
        result.append(item)
    result.sort(key=lambda o: (o.get("created_at", ""), o.get("org_id", "")),
                reverse=True)
    return result


def delete_org(org_id: str) -> bool:
    if get_org(org_id) is None:
        return False
    _table("organizations").delete_item(Key={"org_id": org_id})
    return True


# ---------------------------------------------------------------------------
# Master identity store (ms-111 / e-3620) — 汎用プリミティブ。
# 問い合わせ・書き込み意味論は lib/master_store.BeaconDefaultAdapter が持つので、
# ここは raw get/put/scan のみ。PK 属性名は entity ごとに record の canonical id
# (master_account_id / master_contact_id) を使う (= 余計な "pk" field を record に
# 混ぜず、再 put 時の identity-only 検証を壊さない)。
# ---------------------------------------------------------------------------

_MASTER_KEY_ATTR = {
    "master_accounts": "master_account_id",
    "master_contacts": "master_contact_id",
}


def master_get(entity: str, pk: str) -> dict | None:
    key_attr = _MASTER_KEY_ATTR[entity]
    resp = _table(entity).get_item(Key={key_attr: pk})
    return resp.get("Item")


def master_put(entity: str, pk: str, data: dict) -> None:
    key_attr = _MASTER_KEY_ATTR[entity]
    _table(entity).put_item(Item={**data, key_attr: pk})


def master_scan(entity: str) -> list[dict]:
    return _scan_all(_table(entity))


# ---------------------------------------------------------------------------
# Trek structured logs (ms-97 Phase 7-C, AC26 / AC27, e-2603)
# DynamoDB MVP: in-process dict, keyed by trek_id. The cloud-shaped store
# is Firestore (= subcollection). DynamoDB users get a best-effort backend
# until a `trek_logs` table lands as a follow-up; tests mock these methods
# directly so the production path is exercised in the Firestore branch.
# ---------------------------------------------------------------------------

_TREK_LOGS_FALLBACK: dict[str, list[dict]] = {}


def _mint_trek_log_id() -> str:
    import secrets as _secrets
    return f"log-{_secrets.token_hex(8)}"


def append_trek_log(trek_id: str, log_entry: dict) -> str:
    payload = dict(log_entry or {})
    log_id = payload.get("log_id") or _mint_trek_log_id()
    payload["log_id"] = log_id
    payload["trek_id"] = trek_id
    _TREK_LOGS_FALLBACK.setdefault(trek_id, []).append(payload)
    return log_id


def list_trek_logs(trek_id: str, *, limit: int = 100,
                   since: str = "") -> list[dict]:
    rows = list(_TREK_LOGS_FALLBACK.get(trek_id) or [])
    if since:
        rows = [r for r in rows if (r.get("created_at") or "") > since]
    rows.sort(key=lambda r: (r.get("created_at", ""), r.get("log_id", "")))
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


# ---------------------------------------------------------------------------
# Decision events (ms-90 e-3242): 意思決定の統一 append-only ストリーム。
# DynamoDB MVP: in-process dict keyed by project_id (= trek_logs と同じ
# best-effort。cloud-shaped store は Firestore / MySQL)。schema builder は
# server/decision_event.py。
# ---------------------------------------------------------------------------

_DECISION_EVENTS_FALLBACK: dict[str, list[dict]] = {}


def _mint_decision_event_id() -> str:
    import secrets as _secrets
    return f"dec-{_secrets.token_hex(8)}"


def append_decision_event(project_id: str, data: dict) -> str:
    try:
        from decision_event import assert_no_outcome
        assert_no_outcome(data or {})
    except ImportError:
        pass
    payload = dict(data or {})
    decision_id = payload.get("decision_id") or _mint_decision_event_id()
    payload["decision_id"] = decision_id
    payload.setdefault("created_at", _now_iso_utc())
    _DECISION_EVENTS_FALLBACK.setdefault(project_id, []).append(payload)
    return decision_id


def list_decision_events(project_id: str, *, limit: int = 100,
                         since: str = "") -> list[dict]:
    rows = list(_DECISION_EVENTS_FALLBACK.get(project_id) or [])
    if since:
        rows = [r for r in rows if (r.get("created_at") or "") > since]
    rows.sort(key=lambda r: (r.get("created_at", ""), r.get("decision_id", "")))
    if limit and limit > 0:
        rows = rows[:limit]
    return rows
