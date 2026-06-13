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
    import boto3  # noqa: F401  (= フェーズ 1+ で resource client を作る)
except ImportError:
    boto3 = None  # type: ignore[assignment]


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
# Projects
# ---------------------------------------------------------------------------

def get_project(project_id: str) -> dict | None:
    raise _not_implemented("get_project")


def save_project(project_id: str, data: dict) -> None:
    raise _not_implemented("save_project")


def list_projects(user_id: str | None = None, include_archived: bool = False) -> list[dict]:
    raise _not_implemented("list_projects")


def list_all_projects() -> list[dict]:
    raise _not_implemented("list_all_projects")


def delete_project(project_id: str) -> bool:
    raise _not_implemented("delete_project")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user(user_id: str) -> dict | None:
    raise _not_implemented("get_user")


def get_or_create_user(user_id: str, email: str) -> dict:
    raise _not_implemented("get_or_create_user")


def list_users() -> list[dict]:
    raise _not_implemented("list_users")


def update_user(user_id: str, updates: dict) -> bool:
    raise _not_implemented("update_user")


def delete_user(user_id: str) -> bool:
    raise _not_implemented("delete_user")


def find_user_by_email(email: str) -> tuple[str, dict] | None:
    raise _not_implemented("find_user_by_email")


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
