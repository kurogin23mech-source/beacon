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
# Retros (subcollection: projects/{project_id}/retros/{week})
# ---------------------------------------------------------------------------

def list_retros(project_id: str) -> list[dict]:
    raise _not_implemented("list_retros")


def get_retro(project_id: str, week: str) -> dict | None:
    raise _not_implemented("get_retro")


def save_retro(project_id: str, week: str, content: str) -> None:
    raise _not_implemented("save_retro")


# ---------------------------------------------------------------------------
# Documents (subcollection: projects/{project_id}/documents/{doc_id})
# ---------------------------------------------------------------------------

def list_documents(project_id: str) -> list[dict]:
    raise _not_implemented("list_documents")


def get_document(project_id: str, doc_id: str) -> dict | None:
    raise _not_implemented("get_document")


def save_document(project_id: str, doc_id: str, title: str, content: str,
                  scope: str | None = None, updated_by: str = "unknown") -> str:
    raise _not_implemented("save_document")


def list_document_revisions(project_id: str, doc_id: str) -> list:
    raise _not_implemented("list_document_revisions")


def get_document_revision(project_id: str, doc_id: str, rev: int) -> dict | None:
    raise _not_implemented("get_document_revision")


def delete_document(project_id: str, doc_id: str, deleted_by: str = "unknown",
                    reason: str = "") -> bool:
    raise _not_implemented("delete_document")


def sweep_trashed_documents(project_id: str, *, days: int = 30,
                            dry_run: bool = False) -> list[str]:
    raise _not_implemented("sweep_trashed_documents")


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
