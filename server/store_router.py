"""Storage backend router for the Beacon API.

`BEACON_STORE_BACKEND` 環境変数で backend を切替える薄い router:

  - `firestore` (default) → `firestore_client` の関数を re-export
  - `dynamodb`            → `dynamodb_client` の関数を re-export

`server/app.py` は `import store as db` するだけで両対応になる (= db.get_project
等の呼び出しは router が backend に転送する)。

切替は import 時に 1 度だけ評価する (= プロセス内で backend が動的に変わる
ことはない、Lambda 1 invocation も Cloud Run 1 instance も backend 固定)。
"""
from __future__ import annotations

import os

_BACKEND = os.environ.get("BEACON_STORE_BACKEND", "firestore").lower()

if _BACKEND == "dynamodb":
    from dynamodb_client import (  # noqa: F401
        # Frontmatter helper (ms-60 / e-2306):
        # operation_approve calls `db._extract_frontmatter_field(...)`. Without
        # this re-export the call raises AttributeError, swallowed by the
        # catch-all exception handler as a generic 500. Both backends ship the
        # same pure-string helper, so the router exposes it under the same
        # private name to keep call sites symmetrical.
        _extract_frontmatter_field,
        # Projects
        get_project,
        save_project,
        list_projects,
        list_all_projects,
        delete_project,
        # Users
        get_user,
        get_or_create_user,
        list_users,
        update_user,
        delete_user,
        find_user_by_email,
        # Retros
        list_retros,
        get_retro,
        save_retro,
        # Documents
        list_documents,
        get_document,
        save_document,
        list_document_revisions,
        get_document_revision,
        delete_document,
        sweep_trashed_documents,
        # Changelog
        append_changelog,
        list_changelog,
        # Notes
        add_note,
        list_notes,
        clear_notes,
        # Bus events / cursors / nonces / audit
        append_bus_event,
        get_bus_cursor,
        advance_bus_cursor,
        check_and_record_bus_nonce,
        set_bus_event_receipt,
        find_bus_event,
        append_bus_audit,
        list_bus_audit,
        list_bus_events,
        # Bus event approvals sidecar (ms-70 / e-1712, e-1713 dispatcher gate)
        get_bus_event_approval,
        put_bus_event_approval,
        list_pending_approvals,
        # Sessions
        upsert_session,
        stamp_session_actor_email,
        list_sessions,
        # Machines + session minting
        get_or_mint_machine,
        get_or_mint_session_by_tuple,
        list_user_machines,
        # Session logs
        upsert_session_log,
        list_session_logs,
        get_session_log,
        # Operation envelopes
        get_active_operation_envelope,
        issue_operation_envelope,
        revoke_operation_envelope,
        list_operation_envelopes,
        get_operation_envelope,
        # Treks (ms-69 / e-1652)
        get_trek,
        save_trek,
        list_treks,
        delete_trek,
        # Active claims (ms-55 e-1730)
        list_active_claims,
        get_active_claim,
        save_active_claim,
        delete_active_claim,
    )
elif _BACKEND == "firestore":
    from firestore_client import (  # noqa: F401
        # Firestore-specific escape hatches (ms-95 / e-2407):
        # Several test files do `import firestore_client as db` then patch
        # `db._db` / `db.get_db`. Other test files alias
        # `sys.modules["firestore_client"] = store_router` at module scope to
        # share rebinds across pytest collection — so subsequent
        # `import firestore_client as db` resolves to store_router. Without
        # re-exporting `_db` / `get_db`, `monkeypatch.setattr(db, "_db", …)`
        # raises AttributeError. Asymmetric vs dynamodb branch is fine: these
        # are firestore-coupled by definition (firestore.Client singleton).
        _db,
        get_db,
        COLLECTION,
        USERS_COLLECTION,
        PROJECT_ID,
        # Frontmatter helper (ms-60 / e-2306):
        # operation_approve calls `db._extract_frontmatter_field(...)`. Without
        # this re-export the call raises AttributeError, swallowed by the
        # catch-all exception handler as a generic 500. Both backends ship the
        # same pure-string helper, so the router exposes it under the same
        # private name to keep call sites symmetrical.
        _extract_frontmatter_field,
        # Projects
        get_project,
        save_project,
        list_projects,
        list_all_projects,
        delete_project,
        # Users
        get_user,
        get_or_create_user,
        list_users,
        update_user,
        delete_user,
        find_user_by_email,
        # Retros
        list_retros,
        get_retro,
        save_retro,
        # Documents
        list_documents,
        get_document,
        save_document,
        list_document_revisions,
        get_document_revision,
        delete_document,
        sweep_trashed_documents,
        # Changelog
        append_changelog,
        list_changelog,
        # Notes
        add_note,
        list_notes,
        clear_notes,
        # Bus events / cursors / nonces / audit
        append_bus_event,
        get_bus_cursor,
        advance_bus_cursor,
        check_and_record_bus_nonce,
        set_bus_event_receipt,
        find_bus_event,
        append_bus_audit,
        list_bus_audit,
        list_bus_events,
        # Bus event approvals sidecar (ms-70 / e-1712, e-1713 dispatcher gate)
        get_bus_event_approval,
        put_bus_event_approval,
        list_pending_approvals,
        # Sessions
        upsert_session,
        stamp_session_actor_email,
        list_sessions,
        # Machines + session minting
        get_or_mint_machine,
        get_or_mint_session_by_tuple,
        list_user_machines,
        # Session logs
        upsert_session_log,
        list_session_logs,
        get_session_log,
        # Operation envelopes
        get_active_operation_envelope,
        issue_operation_envelope,
        revoke_operation_envelope,
        list_operation_envelopes,
        get_operation_envelope,
        # Treks (ms-69 / e-1652)
        get_trek,
        save_trek,
        list_treks,
        delete_trek,
        # Active claims (ms-55 e-1730)
        list_active_claims,
        get_active_claim,
        save_active_claim,
        delete_active_claim,
    )
else:
    raise RuntimeError(
        f"Unknown BEACON_STORE_BACKEND: {_BACKEND!r} "
        f"(expected 'firestore' or 'dynamodb')"
    )
