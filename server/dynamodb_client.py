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
        _RESOURCE = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "ap-northeast-1"),
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
    "sessions": "session_id",
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
    "sessions": f"{TABLE_PREFIX}-sessions",
    "session_logs": f"{TABLE_PREFIX}-session_logs",
    "operation_envelopes": f"{TABLE_PREFIX}-operation_envelopes",
    # users/{uid}/* subcollections
    "machines": f"{TABLE_PREFIX}-machines",
    "session_lookup": f"{TABLE_PREFIX}-session_lookup",
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
        entry = {
            "doc_id": data.get("doc_id", ""),
            "title": data.get("title", ""),
            "scope": data.get("scope", "memo"),
            "updated_at": data.get("updated_at", ""),
        }
        if milestone:
            entry["milestone"] = milestone
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
# Bus events / cursors / nonces / audit
# ---------------------------------------------------------------------------

def append_bus_event(project_id: str, data: dict) -> str:
    raise _not_implemented("append_bus_event")


def get_bus_cursor(project_id: str, recipient_id: str) -> dict:
    raise _not_implemented("get_bus_cursor")


def advance_bus_cursor(project_id: str, recipient_id: str,
                       last_seen_at: str) -> dict:
    raise _not_implemented("advance_bus_cursor")


def check_and_record_bus_nonce(project_id: str, nonce: str,
                               expires_at: str) -> bool:
    raise _not_implemented("check_and_record_bus_nonce")


def set_bus_event_receipt(project_id: str, event_id: str, stage: str,
                          recipient_session_id: str) -> dict | None:
    raise _not_implemented("set_bus_event_receipt")


def find_bus_event(project_id: str, event_id: str) -> dict | None:
    raise _not_implemented("find_bus_event")


def append_bus_audit(project_id: str, record: dict) -> str:
    raise _not_implemented("append_bus_audit")


def list_bus_audit(project_id: str, *, since: str = "",
                   limit: int = 100) -> list[dict]:
    raise _not_implemented("list_bus_audit")


def list_bus_events(project_id: str, since: str = "", channel: str = "",
                    limit: int = 100) -> list[dict]:
    raise _not_implemented("list_bus_events")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def upsert_session(project_id: str, session_id: str, data: dict) -> None:
    raise _not_implemented("upsert_session")


def stamp_session_actor_email(project_id: str, session_id: str,
                              email: str) -> None:
    raise _not_implemented("stamp_session_actor_email")


def list_sessions(project_id: str) -> list[dict]:
    raise _not_implemented("list_sessions")


# ---------------------------------------------------------------------------
# Machines + session minting
# ---------------------------------------------------------------------------

def get_or_mint_machine(user_id: str, fingerprint: str, *,
                        hostname: str = "", agent: str = "") -> tuple[str, bool]:
    raise _not_implemented("get_or_mint_machine")


def get_or_mint_session_by_tuple(project_id: str, machine_id: str,
                                 parent_pid: int, *, user_id: str,
                                 cwd: str = "",
                                 metadata: dict | None = None) -> dict:
    raise _not_implemented("get_or_mint_session_by_tuple")


def list_user_machines(user_id: str) -> list[dict]:
    raise _not_implemented("list_user_machines")


# ---------------------------------------------------------------------------
# Session logs
# ---------------------------------------------------------------------------

def upsert_session_log(project_id: str, session_id: str, data: dict) -> None:
    raise _not_implemented("upsert_session_log")


def list_session_logs(project_id: str,
                      limit: int | None = None) -> list[dict]:
    raise _not_implemented("list_session_logs")


def get_session_log(project_id: str, session_id: str) -> dict | None:
    raise _not_implemented("get_session_log")


# ---------------------------------------------------------------------------
# Operation envelopes (Tier 2)
# ---------------------------------------------------------------------------

def get_active_operation_envelope(project_id: str,
                                  op_id: str) -> dict | None:
    raise _not_implemented("get_active_operation_envelope")


def issue_operation_envelope(project_id: str, op_id: str, spec_doc_id: str,
                             spec_revision_id: str, envelope_dict: dict,
                             approved_actions: list[str],
                             created_by: str) -> dict:
    raise _not_implemented("issue_operation_envelope")


def revoke_operation_envelope(project_id: str, envelope_id: str,
                              revoked_by: str, reason: str) -> dict | None:
    raise _not_implemented("revoke_operation_envelope")


def list_operation_envelopes(project_id: str, op_id: str | None = None,
                             status: str | None = None) -> list[dict]:
    raise _not_implemented("list_operation_envelopes")


def get_operation_envelope(project_id: str,
                           envelope_id: str) -> dict | None:
    raise _not_implemented("get_operation_envelope")
