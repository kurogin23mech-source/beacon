#!/usr/bin/env python3
"""Beacon CLI commands - thin adapter over core.py logic."""

__version__ = "0.63.1"

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from typing import Optional, Tuple

from store import get_store
import core
import transition_approval as _ta  # ms-119 e-3912: 目的達成レビュー primitive
import work_model  # ms-109 e-3559: 職種非依存の Target 正準ラベルアクセサ
import master_projection  # ms-111 e-3621: 投影 Account/Contact の identity を master 経由で解決
import occupation  # ms-108 e-3269: ③共有フレームの職種プロジェクション registry
import target_descriptor as _td  # ms-122 e-3954: data 定義 target-class 記述子
import target_engine as _te  # ms-122 e-3956: 記述子駆動 target の汎用機構

# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------
#
# ms-127 e-4316 (god-module split): these leaf CLI helpers were moved verbatim
# to lib/commands_shared.py so the per-family cmd_<family> modules can depend on
# a single small module instead of the whole commands.py. They are re-imported
# here so that (a) the remaining cmd_* functions in this file keep resolving the
# names locally, and (b) external callers that do `import commands;
# commands.load_project()` / `from commands import save_project` keep working —
# the import makes each name an attribute of the commands module, exactly as a
# top-level def did. See commands_shared.py for the one-directional dependency
# rule (commands_shared MUST NOT import commands / cmd_<family>).
from commands_shared import (  # noqa: F401  (re-exported for import-path stability)
    get_project_file,
    _resolve_session_id,
    _resolve_commit_source,
    _user_home,
    load_project,
    load_project_unsafe,
    _local_date,
    _append_changelog,
    _ACKNOWLEDGED_REASON,
    _require_reason_or_skip,
    _WRITE_AUTHORISED_STATUSES,
    _check_ms_status_for_write,
    save_project,
    save_project_unsafe,
    # ms-127 e-4317-foundation: cross-family shared helpers (cloud/api/token,
    # bus project-id resolution, persistence-poisoning defense, notes path,
    # occupation release). Re-exported so the remaining cmd_* here and external
    # `commands.X` callers keep resolving them after the move to commands_shared.
    _is_cloud_mode,
    _resolve_active_api_url,
    _get_cloud_config_path,
    _extract_token,
    _get_api_client,
    _resolve_bus_project_id,
    _canonicalize_project_ref,
    PERSISTENCE_POISONING_AUDIT_FILE,
    _BUS_ORIGIN_REFUSAL_MESSAGE,
    _is_bus_origin_input,
    _persistence_poisoning_audit_path,
    _record_persistence_poisoning_refusal,
    _refuse_if_bus_origin,
    _get_notes_path,
    # ms-127 e-4318-foundation: cross-family identity / cloud-project helpers
    # (used by org/member + deploy/cloud/trek/bus). Re-exported so the callers
    # remaining in commands.py and external `commands.X` keep resolving them.
    _project_id_for_ops,
    _read_credentials_for_identity,
    _resolve_creator_identity,
    _rename_local_project_json_for_cloud_cutover,
    # ms-127 e-4319-foundation: cross-family untriaged-gate / dup-report / author
    # helpers (used by task+entry + milestone/operation/sales). Re-exported so
    # the callers remaining in commands.py and external `commands.X` keep working.
    _HUMAN_UNTRIAGED_REFUSED_MSG,
    _caller_is_human_for_untriaged,
    _human_untriaged_bypass_refused,
    _print_residual_dups,
    _resolve_current_author,
)

# ms-127 e-4317b (god-module split): the `beacon session *` family lives in
# lib/cmd_session.py now. Re-imported so the dispatch dict below and external
# callers (`import commands; commands.cmd_session_end()` / `commands.
# _aggregate_and_persist`) keep resolving — the import makes each name an
# attribute of the commands module exactly as the top-level def did.
from cmd_session import (  # noqa: F401  (re-exported for dispatch + import-path stability)
    _session_logs_dir,
    _session_log_path,
    _read_local_session_log,
    _write_local_session_log,
    _push_session_log_to_cloud,
    _stamp_cloud_session_shutdown,
    _list_other_session_ids,
    _aggregate_and_persist,
    cmd_session_end,
    cmd_session_rescue,
    cmd_session_log_list,
    cmd_session_log_show,
    cmd_session_id,
    _resolve_current_session_id,
    cmd_session_focus,
    cmd_session_attention,
    cmd_session_fork,
    cmd_session_fork_list,
    _release_all_occupations_for_session,
)

# ms-127 e-4318b: the `beacon org *` + `beacon member *` families live in
# lib/cmd_org.py now. Re-imported for dispatch + external `commands.X` stability.
# Invariant across the split: re-export ALL moved names (public handlers AND
# private helpers), uniformly, so `import commands; commands.<name>` and the
# dispatch dict keep resolving regardless of which names external code happens to
# reference. The private ones are NOT a curated public API — they are re-export
# aliases; the canonical definition (and the correct monkeypatch target) is
# cmd_org. Patch cmd_org.<helper>, not commands.<helper> (a patch on the alias
# does not intercept calls made from within cmd_org's own namespace).
from cmd_org import (  # noqa: F401
    cmd_org_create,
    cmd_org_list,
    cmd_org_show,
    cmd_org_add_member,
    cmd_org_remove_member,
    cmd_org_delete,
    cmd_org_rehome,
    cmd_member_add,
    _member_identity,
    _build_owner_row,
    _annotate_external_guests,
    cmd_member_list,
    _resolve_cloud_project_id,
    cmd_member_remove,
    cmd_member_role,
    cmd_member_invite,
    cmd_member_invitation_list,
    cmd_member_invitation_cancel,
    cmd_member_join,
    cmd_member_whoami,
)

# ms-127 e-4319b: the `beacon task *` + `beacon entry *` families live in
# lib/cmd_task.py now. Re-imported for dispatch + external `commands.X` stability
# (uniform re-export of all moved names). These are re-export ALIASES: a
# monkeypatch on `commands.<name>` does NOT intercept calls made from inside
# cmd_task (which resolved the name in its own namespace at import time). To stub
# a helper a task/entry handler uses, patch `cmd_task.<name>` (or, for a shared
# helper, `commands_shared.<name>`) — not `commands.<name>`.
from cmd_task import (  # noqa: F401
    cmd_entry_purge,
    cmd_entry_move,
    cmd_task_add,
    cmd_task_done,
    cmd_task_list,
    cmd_task_show,
    cmd_task_detail,
    cmd_task_update,
    cmd_task_delete,
    cmd_task_cancel,
)

# ms-127 e-4320: more group-A families moved to their own modules (clean, no
# shared-helper promote needed). Re-export rule (ms-127, tightened per PR #586
# independent review): re-export ONLY the public cmd_* handlers — the dispatch
# dict below needs them, and external `import commands; commands.cmd_X()` callers
# rely on them. Family-PRIVATE helpers (`_foo`) are NOT re-exported: nothing in
# commands.py calls them post-move, and re-exporting a private would create a
# silent-no-op monkeypatch trap (patching commands._foo would not intercept the
# call made inside cmd_<family>, which resolves _foo in its own namespace). Their
# canonical home + patch target is cmd_<family>._foo; a missing commands._foo now
# fails loudly (AttributeError) instead of silently.
from cmd_note import cmd_note_add, cmd_note_list, cmd_note_clear  # noqa: F401
from cmd_decision import (cmd_decision_record, cmd_decision_list,  # noqa: F401  (ms-154 e-5594/e-5595)
                          cmd_decision_derive)  # noqa: F401  (ms-166 e-5972)
from cmd_incident import (  # noqa: F401
    cmd_incident_open, cmd_incident_close, cmd_incident_escalate, cmd_incident_list,
)
from cmd_issue import cmd_issue_import, cmd_issue_list, cmd_issue_sync  # noqa: F401
from cmd_log import cmd_log, cmd_log_prepare, cmd_log_finalize  # noqa: F401

# ms-127 e-4971: trigger command family + auto-fire/tick subsystem moved to
# lib/cmd_trigger.py. Re-export ALL 36 moved names (4 public cmd_trigger_*
# handlers + 32 private helpers) so `from commands import X` and `commands.X`
# keep resolving — tests import several `_auto_fire_*` / `_cleanup_*` directly.
# Canonical definition (and monkeypatch target) is cmd_trigger.<name>; patching
# commands.<name> does NOT intercept calls made from within cmd_trigger's namespace
# (see the e-4320 monkeypatch-trap note in cmd_note.py's header).
from cmd_trigger import (  # noqa: F401
    cmd_trigger_fire,
    cmd_trigger_check,
    cmd_trigger_tick,
    cmd_trigger_clear,
    _push_trigger_to_bus,
    _push_trek_trigger_to_bus,
    _push_operation_trigger_to_bus,
    _claim_operation_fire_for_bus_push,
    _resolve_operation_trigger_recipient,
    _cleanup_stale_triggers,
    _cleanup_review_due_triggers,
    _cleanup_spec_needed_triggers,
    _cleanup_old_release_marker_triggers,
    _maybe_auto_tick,
    _trigger_tick_stamp_path,
    _trigger_tick_lock_path,
    _trigger_tick_ttl_seconds,
    _auto_fire_map_drift_trigger,
    _auto_fire_operation_triggers,
    _auto_fire_pr_open_review_triggers_for_open_prs,
    _auto_fire_release_due_trigger,
    _auto_fire_release_marker_trigger,
    _auto_fire_retro_trigger,
    _auto_fire_untriaged_backlog_trigger,
    _clear_map_drift_trigger_if_exists,
    _clear_release_due_trigger_if_exists,
    _clear_untriaged_backlog_trigger_if_exists,
    _map_drift_trigger_path,
    _release_due_trigger_path,
    _untriaged_backlog_trigger_path,
    _count_commits_between_tags,
    _count_releases_since,
    _count_untriaged_active,
    _iso_week_string,
    _previous_v_tag,
    _pr_open_reviewed_marker_exists,
)

# ms-127 e-4321: sessions / push / claim families moved to their own modules.
# Public handlers only (private helpers stay canonical in cmd_<family>, patched
# there — see the e-4320 rule above).
from cmd_sessions import cmd_sessions_list  # noqa: F401
from cmd_push import cmd_push_list, cmd_push_record  # noqa: F401
from cmd_claim import (  # noqa: F401
    cmd_claim_request, cmd_claim_handoff, cmd_claim_post, cmd_claim_respond,
    cmd_claim_release, cmd_claim_list, cmd_claim_view,
)

# ms-127 e-4798: operation family moved to cmd_operation.py, plus its
# review/spec/gate leaf helpers promoted to commands_shared (foundation).
# Re-export the promoted helpers here so the many milestone / target handlers
# still in commands.py keep resolving them by bare name (and existing
# `monkeypatch.setattr(commands, "_X", ...)` in tests stay effective — those
# callers resolve _X in commands' namespace). Operation-handler callers resolve
# these inside cmd_operation instead, so tests driving cmd_operation_* patch at
# cmd_operation._X (see the e-4320 monkeypatch-trap note above).
from commands_shared import (  # noqa: F401
    _get_triggers_dir, _spec_doc_for_target, _spec_exists_for_op,
    _fire_review_due_trigger, _session_kind_is_human,
    _ai_session_direct_completion_ban_active, _gate_target_class,
)
# Public operation handlers only (family-private _fetch_active_operation_envelope
# stays canonical in cmd_operation, patched there — see the e-4320 rule above).
from cmd_operation import (  # noqa: F401
    cmd_operation_purge, cmd_operation_server_tick, cmd_operation_open,
    cmd_operation_set_status, cmd_operation_update, cmd_operation_task_add,
    cmd_operation_task_done, cmd_operation_task_list, cmd_operation_close,
    cmd_operation_pause, cmd_operation_resume,
    cmd_operation_list, cmd_operation_show, cmd_operation_approve,
    cmd_operation_envelope_verify, cmd_operation_revoke,
)

# ms-127 e-4803: bus family moved to cmd_bus.py, plus its budget/recipient/
# identity leaf helpers promoted to commands_shared (foundation). Re-export only
# the 5 promoted helpers that commands.py callers still use by bare name
# (_arm_for_trek / cmd_acquisition_attack_list_send / _check_recipient_live_health);
# the 6 identity/swap/budget-path helpers those pull in transitively live in
# commands_shared but are NOT referenced by commands.py, so they are not
# re-exported. Bus-handler callers resolve all of these inside cmd_bus, so tests
# driving cmd_bus_* patch at cmd_bus._X (monkeypatch-trap note above).
from commands_shared import (  # noqa: F401
    _read_bus_budget, _write_bus_budget, _bus_auto_execute_channels,
    _mirror_auto_execute_channels_to_local, _resolve_recipient_live,
)
# Public bus handlers only (family-private helpers stay canonical in cmd_bus,
# patched there — see the e-4320 rule above).
from cmd_bus import (  # noqa: F401
    cmd_bus_budget_grant, cmd_bus_budget_show, cmd_bus_budget_clear,
    cmd_bus_auto_execute_list, cmd_bus_auto_execute_add, cmd_bus_auto_execute_remove,
    cmd_bus_send, cmd_bus_listen, cmd_bus_receive, cmd_bus_ack,
    cmd_bus_status, cmd_bus_directory, cmd_dm_sent,
)

# ms-127 e-4809: retro family moved to cmd_retro.py, plus its retro-day / week /
# document / content-input leaf helpers promoted to commands_shared. Re-export the
# 5 promoted helpers so commands.py callers (_auto_fire_retro_trigger / cmd_search
# / cmd_doc_add / cmd_doc_update) keep resolving them by bare name. DAY_NAMES is
# only used by the promoted _get_retro_day, so it is NOT re-exported. Retro-handler
# callers resolve these inside cmd_retro, so tests driving cmd_retro_* patch at
# cmd_retro._X (the e-4320 monkeypatch-trap rule).
from commands_shared import (  # noqa: F401
    _get_retro_day, _last_reviewed_week, _most_recent_retro_day_on_or_before,
    _load_local_documents, _resolve_content_input,
)
# Public retro handlers only (family-private _retro_catch_up_block stays canonical
# in cmd_retro, patched there — see the e-4320 rule above).
from cmd_retro import (  # noqa: F401
    cmd_retro_prepare, cmd_retro_default_since, cmd_retro_save, cmd_retro_done,
)
# ms-155 e-5602: deliverable family (produced-value projection resolver + list).
from cmd_deliverable import cmd_deliverable_list  # noqa: F401
# ms-161 e-5902/e-5903: deliverable-changelog curation surface (add/retire/
# supersede) + derived-map render (map).
from cmd_deliverable import (  # noqa: F401
    cmd_deliverable_add, cmd_deliverable_retire,
    cmd_deliverable_supersede, cmd_deliverable_map,
)

# ms-127 e-4815: deploy family moved to cmd_deploy.py, plus the application-map
# applicability helper promoted to commands_shared. Re-export _application_map_applies
# so commands.py's _auto_fire_map_drift_trigger keeps resolving it by bare name.
# (ms-155 e-5599: the former _project_profession_safe helper was removed — the gate
# now derives from the deliverable declaration.) Deploy-handler callers resolve these
# inside cmd_deploy, so tests driving cmd_deploy_* patch at cmd_deploy._X (e-4320 rule).
from commands_shared import _application_map_applies  # noqa: F401
# ms-127 e-4831-foundation: doc family split — frontmatter / table-doc / link-
# validation helpers promoted to commands_shared so both cmd_doc.py (new home)
# and the acquisition / profile / briefing callers still in commands.py resolve
# them without importing cmd_doc (which would cycle).
from commands_shared import (  # noqa: F401
    _doc_slug,
    _add_frontmatter,
    _validate_link_target_exists,
    _now_iso,
    _split_frontmatter_raw,
    _load_table_model,
    _write_table_model,
    _persist_table_doc,
    _actor_str,
    _sales_skill_nudge,
    VALID_SCOPES,
)

# ms-127 e-4831: doc family handlers moved to lib/cmd_doc.py. Re-imported here
# so the dispatch table and external `commands.cmd_doc_*` callers keep resolving
# (import-path stability). Family-private helpers stay in cmd_doc (not re-exported).
from cmd_doc import (  # noqa: F401
    cmd_doc_list,
    cmd_doc_show,
    cmd_doc_add,
    cmd_doc_update,
    cmd_doc_table_create,
    cmd_doc_table_add_row,
    cmd_doc_table_set_cell,
    cmd_doc_table_rm_row,
    cmd_doc_table_show,
    cmd_doc_history,
    cmd_doc_restore,
    cmd_doc_delete,
    cmd_doc_image_upload,
)

# ms-127 e-4839-foundation: acquisition family split — generic date/number
# helpers shared by the acquisition handlers (moving to cmd_acquisition.py) and
# other commands.py callers (sales/opportunity). Re-exported for path stability.
from commands_shared import (  # noqa: F401
    _today_iso,
    _parse_number,
)

# ms-127 e-4839: acquisition handlers moved to lib/cmd_acquisition.py. Re-imported
# here so dispatch + external `commands.cmd_acquisition_*` keep resolving.
from cmd_acquisition import (  # noqa: F401
    cmd_acquisition_add,
    cmd_acquisition_list,
    cmd_acquisition_status,
    cmd_acquisition_delete,
    cmd_acquisition_attach_list,
    cmd_acquisition_lists,
    cmd_acquisition_attack_list_fill,
    cmd_acquisition_attack_list_send,
    cmd_acquisition_attack_list_send_record,
    cmd_acquisition_attack_list_awaiting_reply,
    cmd_acquisition_attack_list_reply_record,
    cmd_acquisition_attack_list_promote,
)
# Public deploy handlers only (family-private helpers stay canonical in cmd_deploy,
# patched there — see the e-4320 rule above).
from cmd_deploy import (  # noqa: F401
    cmd_deploy_record, cmd_deploy_list, cmd_deploy_delete,
    cmd_deploy_rollback, cmd_deploy_void,
)

# ms-127 e-4849: milestone family moved to lib/cmd_milestone.py. Re-imported
# here so dispatch + external `commands.cmd_milestone_*` keep resolving. Family-
# private helpers/constants stay canonical in cmd_milestone (patch there per the
# e-4320 rule).
from cmd_milestone import (  # noqa: F401
    cmd_milestone_add,
    cmd_milestone_list,
    cmd_milestone_start,
    cmd_milestone_done,
    cmd_milestone_observe,
    cmd_milestone_wait,
    cmd_milestone_release,
    cmd_milestone_occupations,
    cmd_milestone_join,
    cmd_milestone_show,
    cmd_milestone_update,
    cmd_milestone_delete,
    cmd_milestone_purge,
    cmd_milestone_depends,
    cmd_milestone_workspace,
    cmd_milestone_workspace_cleanup,
    cmd_milestone_graph,
)

# ms-127 e-4852: target family moved to lib/cmd_target.py. Re-imported here so
# dispatch + external `commands.cmd_target_*` keep resolving. Family-private
# helpers stay canonical in cmd_target (patch there per the e-4320 rule).
from cmd_target import (  # noqa: F401
    cmd_target_review_request,
    cmd_target_approve,
    cmd_target_attach_evidence,
    cmd_target_attach_disposition,
    cmd_target_reject,
    cmd_target_list,
    cmd_target_create,
    cmd_target_advance,
    cmd_target_close,
    cmd_target_instances,
    cmd_target_work_item,
    cmd_target_evidence,
    cmd_target_ball,
    cmd_target_class_add,
    cmd_target_class_update,
    cmd_target_purge,
    cmd_target_split,
    cmd_target_class_list,
    cmd_target_class_adopt,
)

# ms-127 e-4856: pr family moved to lib/cmd_pr.py. Re-imported here so dispatch
# + external `commands.cmd_pr_*` keep resolving. Family-private helpers stay
# canonical in cmd_pr (patch there per the e-4320 rule).
from cmd_pr import (  # noqa: F401
    cmd_pr_add,
    cmd_pr_approve,
    cmd_pr_close,
    cmd_pr_create,
    cmd_pr_merge,
    cmd_pr_reject,
    cmd_pr_request_changes,
    cmd_pr_request_review,
    cmd_pr_show,
    cmd_pr_sync,
)

# ms-127 e-4860: project family moved to lib/cmd_project.py. Re-imported here so
# dispatch + external `commands.cmd_project_*` keep resolving. Family-private
# helpers + _BACKUP_SCHEMA_VERSION stay canonical in cmd_project (patch there per
# the e-4320 rule).
from cmd_project import (  # noqa: F401
    cmd_project_archive,
    cmd_project_cleanup,
    cmd_project_dump,
    cmd_project_export,
    cmd_project_import,
    cmd_project_orphans,
    cmd_project_rename,
    cmd_project_unarchive,
)
# ms-127 e-4856: review-due trigger helpers promoted to commands_shared during
# the pr split (shared with the `beacon review` handlers still here). Re-import
# so those callers + tests patching commands._X keep resolving.
from commands_shared import (  # noqa: F401
    _clear_review_due_for_pr,
    _fire_pr_open_review_triggers,
    _clear_pr_open_review_triggers,
    _pending_review_types_for_pr,
    _pr_open_reviewed_marker_path,
    _fire_review_due_for_pr,
    _REVIEW_DUE_SUFFIX,
)
# ms-127 e-4849: 4 helpers promoted to commands_shared during the milestone
# split. They are shared across families: the milestone handlers (cmd_milestone.py,
# e-4849), the target handlers (cmd_target.py, e-4852), and the transition/backlog
# handlers still HERE in commands.py. Re-import so commands.py-internal callers
# keep resolving them by bare name. NOTE for test authors: patching `commands._X`
# only affects the commands.py call path — a test driving a cmd_milestone_* or
# cmd_target_* handler must patch `cmd_milestone._X` / `cmd_target._X` instead
# (each side binds an independent copy from commands_shared; see the e-4320 rule
# in cmd_milestone.py's / cmd_target.py's docstring).
from commands_shared import (  # noqa: F401
    _release_occupation_for_transition,
    _print_evidence_guidance,
    _spec_exists_for_ms,
    _spec_updated_at_for_target,
)

# ms-127 e-4820: trek family moved to cmd_trek.py, plus its docs/frontmatter/
# project-id leaf helpers + DEFAULT_SCOPE promoted to commands_shared. Re-export
# the 4 helpers + DEFAULT_SCOPE so the many commands.py callers (cmd_doc_* /
# _auto_fire_operation_triggers / account / table-doc) keep resolving them by
# bare name. Trek-handler callers resolve these inside cmd_trek, so tests driving
# cmd_trek_* patch at cmd_trek._X (the e-4320 monkeypatch-trap rule).
from commands_shared import (  # noqa: F401
    _current_project_id, _get_docs_dir, _parse_frontmatter, _read_local_doc,
    DEFAULT_SCOPE,
)
# Public trek handlers only (family-private helpers stay canonical in cmd_trek,
# patched there — see the e-4320 rule above).
from cmd_trek import (  # noqa: F401
    cmd_trek_create, cmd_trek_list, cmd_trek_review_verdicts, cmd_trek_show,
    cmd_trek_timeline, cmd_trek_start, cmd_trek_archive, cmd_trek_invite,
    cmd_trek_join, cmd_trek_stop, cmd_trek_resume, cmd_trek_pulse_ack,
    cmd_trek_kickoff, cmd_trek_take_over, cmd_trek_reconcile,
    cmd_trek_transfer_leader, cmd_trek_task_state, cmd_trek_block,
    cmd_trek_unblock, cmd_trek_blockers, cmd_trek_extend_ttl,
    cmd_trek_summary_sent, cmd_trek_plan, cmd_trek_slot_add, cmd_trek_slot_amend,
    cmd_trek_slot_claim, cmd_trek_slot_list, cmd_trek_scope_approve,
    cmd_trek_scope_reject, cmd_trek_blanket_approve, cmd_trek_blanket_revoke,
    cmd_trek_task_add, cmd_trek_leave,
)


# ---------------------------------------------------------------------------
# Init (CLI-specific: file creation, hook installation)
# ---------------------------------------------------------------------------

CLAUDE_MD_BEACON_SECTION = """\

## Beacon Project Management

This project uses [Beacon](https://github.com/kurogin23mech-source/beacon) for milestone-driven progress tracking.
このプロジェクトは Beacon でマイルストーンベースの進捗管理を行っています。

### Rules / ルール

- **Always advance the Target. A Target (milestone / opportunity / operation …) is a thing to push *forward*, not a bucket to sit in. "Making progress" means advancing the currently-open Target to its next phase/state. End every action by surfacing "the next move that advances this Target".**
  常に target を前進させる。target (= milestone / opportunity / operation 等の進める対象) は座って眺める箱ではなく前へ進めるもの。「仕事を進める」とは、いま進行中の target を次のフェーズ / 状態へ前進させること。どの操作も最後に「この target を次に前進させる一手」を提示して終える。
- **Never edit `.beacon/project.json` directly. Always use beacon CLI commands.**
  `.beacon/project.json` を直接編集しない。必ず beacon CLI を使うこと。
- Before starting work, check milestones (`beacon status`) and confirm which milestone the work targets.
  実装開始前にマイルストーンを確認し、どのマイルストーンに向かう作業かユーザーに確認すること。
- After committing, the PostToolUse hook will auto-trigger `/beacon-log` Skill for AI-evaluated progress recording.
  コミット後はPostToolUse hookが自動で `/beacon-log` Skillを起動し、AI評価付きで進捗を記録する。
- If 2+ commits address the same issue, suggest grouping them into a task.
  同じ課題に2回以上コミットが発生したら、タスクにまとめることを提案する。
- Update the project summary when direction changes: `beacon summary "text"`
  方向性が変わった時はサマリーを更新する。書くべきは経緯・判断・背景であり、進捗率やMS名ではない。
- When the user hints at ending the session, or before you suggest splitting/ending the session yourself, run `/beacon-session-end` Skill first.
  ユーザーがセッション終了を仄めかしたとき、または自分自身がセッション分割・終了を提案する前に、必ず `/beacon-session-end` Skill を実行する。
- When the user wants to implement multiple milestones in parallel ("parallel", "sub-agents", "dispatch", etc.), run `/beacon-dispatch` Skill. Do not call the Agent tool directly.
  ユーザーが複数MSの並列実装を求めた場合（「パラレル」「サブエージェント」「並列」等）、必ず `/beacon-dispatch` Skill を実行する。Agent toolを直接呼ばない。
- When the user asks to review a PR ("レビューして", "review this PR", etc.), or when `beacon trigger check` shows a PR review trigger, immediately invoke `/review`. Never call `beacon pr approve/reject` directly without running `/review` first.
  ユーザーがPRのレビューを依頼したとき、またはbeacon triggerにPRレビュー通知があるとき、必ず `/review` Skillを使う。`/review` を経ずに `beacon pr approve/reject` を直接呼ばない。
- When the user says "memo this", "remember this", "メモして", "覚えておいて", or when you find context that must survive compaction, use `/beacon-note` Skill (or `beacon note "text"`). Notes are cleared at session-end.
  ユーザーが「メモして」「覚えておいて」と言ったとき、またはコンパクション後に必要なコンテキストを見つけたときは `/beacon-note` Skill を使う。セッション終了時にクリアされる。

### Proactive Guidance / 自発的な提案

Act as a consultant, not just a status display. Use beacon data to proactively propose next steps:
ダッシュボード（状態を見せる）ではなくコンサルタント（解釈して提案する）として振る舞う。

- **No milestones yet**: Read the codebase and docs, then suggest a concrete first milestone.
  MSがゼロの場合: コードとドキュメントを読み、最初のマイルストーン候補を提案する。
- **After a milestone completes**: Propose what the next milestone should be.
  MS完了直後: 次のマイルストーンを提案する。
- **After adding a new milestone**: Proactively offer to create a SPEC document for it.
  MS追加直後: そのMSのSPECドキュメント作成を自発的に提案する。
- **Progress stalled** (no commits in a while): Acknowledge it and offer to break down the work.
  進捗が止まっている: 気づいて声をかけ、タスク分解を提案する。
- **After a retro**: Propose next-phase direction based on what was learned.
  振り返り後: 学びを踏まえた次フェーズの方向性を提案する。

Proposals should feel like "What if we tried X?" — not directives.
提案は指示ではなく「こういう方向はどうですか？」という姿勢で。

### CLI Quick Reference

| Command | Description |
|---------|-------------|
| `beacon status` | Show project status / ステータス表示 |
| `beacon milestone add "title"` | Add milestone / MS追加 |
| `beacon milestone start <id>` | Activate milestone / MS開始 |
| `beacon task add "desc" -m <ms-id>` | Add task / タスク追加 |
| `beacon task done <id>` | Complete task / タスク完了 |
| `beacon log "summary"` | Record commit (auto via hook) / コミット記録（hook経由で自動） |
| `beacon summary "text"` | Update summary / サマリー更新 |
| `beacon note "text"` | Add session note (ephemeral, cleared at session-end) / セッションメモ追加 |
| `beacon note list` | Show session notes / メモ一覧 |
| `beacon note clear` | Clear all session notes / メモ全削除 |

<!-- BEACON_ENTRY_WRITING_PRINCIPLE -->
### Entry Writing Principle / エントリ記述原則

When writing task / spec / doc entries (`description` / `motivation` / `acceptance_criteria`), follow these 4 principles. Beacon's readers include non-developers — the principles apply to all forward-going writes.
タスク・SPEC・ドキュメントを書くとき、以下 4 原則を守る。Beacon の読み手には非開発者が含まれるため、新しく書く全エントリに適用する。

1. **Reader-first 1-line description / 読み手目線 1 行**: `description` は「何が嬉しいか」をユーザーの言葉で 1 行。実装手段は含めない。
2. **3-tier loanword policy / 横文字 3 段階**: 固有名詞 (`Firestore` / `MCP`) はそのまま、技術概念 (`allowlist` / `opt-in`) は初出時に「(= 許可リスト)」のような日本語注、一般概念 (configure / receiver / audit) は日本語化。
3. **ID references with context / ID 参照に文脈**: `e-XXXX` / `ms-XX` / `UC?` の初出には必ず「(何の話か)」を 1 行添える。click-through 前提にしない。
4. **No truncated sentences / 尻切れトンボ禁止**: 主語・述語・論理関係を省略しない。開発者は文脈で補えるが非開発者は補えない。

Full principle and examples: `beacon doc show F3ZkqT0pKS6JpR8dn70n` (CORE doc `entry-writing-principle`).
原則の全文と実例: `beacon doc show F3ZkqT0pKS6JpR8dn70n` (CORE doc `entry-writing-principle`)。
<!-- /BEACON_ENTRY_WRITING_PRINCIPLE -->
"""


def _append_claude_md():
    claude_md = "CLAUDE.md"
    marker = "## Beacon Project Management"
    if os.path.exists(claude_md):
        with open(claude_md, "r", encoding="utf-8") as f:
            content = f.read()
        if marker in content:
            # Replace existing section with latest content
            idx = content.index(marker)
            prefix = content[:idx].rstrip('\n') + '\n\n'
            new_section = CLAUDE_MD_BEACON_SECTION.lstrip('\n')
            after = content[idx + len(marker):]
            next_h2 = after.find('\n## ')
            if next_h2 == -1:
                new_content = prefix + new_section
            else:
                new_content = prefix + new_section + after[next_h2:]
            if new_content != content:
                with open(claude_md, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"Updated {claude_md} with beacon rules")
            return
    with open(claude_md, "a", encoding="utf-8") as f:
        f.write(CLAUDE_MD_BEACON_SECTION)
    print(f"Updated {claude_md} with beacon rules")


POST_COMMIT_HOOK = """\
#!/usr/bin/env bash
# Beacon: auto-log commits to the active milestone
# Skip in Claude Code — AI handles logging with milestone/task judgment
if [ -n "$BEACON_CLAUDE_CODE" ]; then
    exit 0
fi
if [ -f ".beacon/project.json" ] && command -v beacon &>/dev/null; then
    beacon log 2>/dev/null || true
fi
"""

BEACON_HOOK_MARKER = "# Beacon: auto-log commits"


def _install_git_hook():
    hook_dir = os.path.join(".git", "hooks")
    if not os.path.isdir(hook_dir):
        return
    hook_path = os.path.join(hook_dir, "post-commit")
    if os.path.exists(hook_path):
        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()
        if BEACON_HOOK_MARKER in content:
            return
        with open(hook_path, "a", encoding="utf-8") as f:
            f.write("\n" + POST_COMMIT_HOOK)
    else:
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write(POST_COMMIT_HOOK)
    os.chmod(hook_path, 0o755)
    print("Installed git post-commit hook")


def _find_hook(name):
    """Locate a hook script. Brew install places hooks alongside this file
    (libexec/); dev repo has them at <repo>/bin/."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_bin = os.path.join(os.path.dirname(here), "bin", name)
    sibling = os.path.join(here, name)
    if os.path.exists(sibling):
        return sibling
    return repo_bin


CLAUDE_HOOK_SCRIPT = _find_hook("beacon-post-commit-hook.sh")
CLAUDE_SAVE_HOOK_SCRIPT = _find_hook("beacon-save-hook.sh")
CLAUDE_POSTCOMPACT_HOOK_SCRIPT = _find_hook("beacon-postcompact.sh")
# ms-44 e-854: Stop hook bash script (Mac/Linux). Python port lives at
# beacon_cli.hooks.context_monitor and is resolved via _resolve_hook_command
# when the user is on Windows or has the wheel installed.
CLAUDE_CONTEXT_MONITOR_HOOK_SCRIPT = _find_hook("context-usage-monitor.sh")


# ms-160 e-5806 — single source of truth for the Claude Code hooks beacon
# installs. Both install paths (`beacon init` = _install_claude_hook and
# `beacon skill install` = _install_claude_hooks) wire this exact set, and
# `beacon doctor` (e-5807) checks every entry, so a hook can no longer be wired
# on one path and silently missing on the other. That divergence is what left
# the MCP save hook off `beacon skill install` and the bus-inbox receive hook
# off BOTH installers (= a fresh install received no cross-session DMs at all).
#
# Each entry:
#   key          — stable id (doctor / messages)
#   events       — Claude Code hook events this command registers on
#   matcher      — tool matcher for PostToolUse ("Bash"/"mcp__"/"*"), else None
#   resolver     — basename for _resolve_hook_command (entry-point / python -m),
#                  OR
#   script       — basename of a bin/ script hook (resolved to `<python> <path>`)
#   identity     — command substrings that identify this hook (dedup / detect)
#   timeout, statusMessage — settings.json entry fields
HOOK_MANIFEST = [
    {"key": "post-commit", "events": ["PostToolUse"], "matcher": "Bash",
     "resolver": "beacon-post-commit-hook.sh",
     "identity": ("beacon-post-commit-hook", "beacon-hook-post-commit",
                  "beacon_cli.hooks.post_commit"),
     # e-5803 review (AX-11): this hook also detects PR-open / target-close now
     # (ms-160 e-5801), so the status text names lifecycle events, not just commit.
     "timeout": 10,
     "statusMessage": "Beacon: checking for commit / PR / target-close / deploy..."},
    {"key": "save", "events": ["PostToolUse"], "matcher": "mcp__",
     "resolver": "beacon-save-hook.sh",
     "identity": ("beacon-save-hook", "beacon-hook-save",
                  "beacon_cli.hooks.save_hook"),
     "timeout": 10, "statusMessage": "Beacon: checking MCP operation..."},
    # e-5803 review (AX-9): matcher "*" is Claude Code's settings.json glob for
    # "all tools". The Codex equivalent uses the regex ".*" (see
    # plugins/beacon/scripts/beacon-codex-bridge._halt_hook_entry) — the two hook
    # systems spell "every tool" differently; do NOT copy "*" into Codex config.
    {"key": "halt-check", "events": ["PostToolUse"], "matcher": "*",
     "resolver": "beacon-halt-check.sh",
     "identity": ("beacon-hook-halt-check", "beacon_cli.hooks.halt_check"),
     "timeout": 10, "statusMessage": "Beacon: checking for STOP signal..."},
    {"key": "postcompact", "events": ["PostCompact"], "matcher": None,
     "resolver": "beacon-postcompact.sh",
     "identity": ("beacon-postcompact", "beacon-hook-postcompact",
                  "beacon_cli.hooks.postcompact"),
     "timeout": 10, "statusMessage": "Beacon: post-compaction orientation..."},
    {"key": "context-monitor", "events": ["Stop"], "matcher": None,
     "resolver": "context-usage-monitor.sh",
     "identity": ("context-usage-monitor", "beacon-hook-context-monitor",
                  "beacon_cli.hooks.context_monitor"),
     "timeout": 10,
     "statusMessage": "Beacon: checking context-usage threshold..."},
    {"key": "session-start", "events": ["SessionStart"], "matcher": None,
     "resolver": "beacon-session-start.sh",
     "identity": ("beacon-hook-session-start", "beacon_cli.hooks.session_start"),
     "timeout": 15, "statusMessage": "Beacon: checking for updates..."},
    {"key": "bus-inbox", "events": ["SessionStart", "UserPromptSubmit"],
     "matcher": None, "script": "beacon-bus-inbox-hook.py",
     "identity": ("beacon-bus-inbox-hook",),
     "timeout": 15, "statusMessage": "Beacon: checking bus inbox..."},
]


def _resolve_manifest_hook_command(spec: dict) -> str:
    """Resolve a HOOK_MANIFEST entry to a settings.json command string, or "" if
    it can't be resolved on this install (caller skips it — best-effort, e.g. a
    wheel install with no bin/ script on disk)."""
    resolver = spec.get("resolver")
    if resolver:
        return _resolve_hook_command(resolver)
    script = spec.get("script")
    if script:
        path = _find_hook(script)
        if path and os.path.exists(path) and not _hook_unusable_on_windows(path):
            # No entry-point exists for the bin/ script, so invoke it through the
            # current interpreter (cross-platform: a bare .py path won't self-run
            # on Windows). Mirrors _resolve_hook_command's `python -m` fallback.
            return f"{_bash_safe(sys.executable)} {_bash_safe(path)}"
    return ""


def _manifest_hook_present(hooks_dict: dict, spec: dict) -> bool:
    """True iff the hook is registered on EVERY one of its events. `beacon
    doctor` (e-5807) uses this to detect a partially-wired install."""
    for event in spec["events"]:
        found = False
        for entry in hooks_dict.get(event, []):
            for h in entry.get("hooks", []):
                if any(s in h.get("command", "") for s in spec["identity"]):
                    found = True
                    break
            if found:
                break
        if not found:
            return False
    return True


def _install_manifest_hook(hooks_dict: dict, spec: dict) -> bool:
    """Idempotently register one HOOK_MANIFEST entry across all of its events.
    Drops stale same-identity entries whose command differs (path migration),
    then adds the resolved command wherever absent. Returns True if it changed
    anything. No-op (returns False) when the command can't be resolved."""
    command = _resolve_manifest_hook_command(spec)
    if not command:
        return False
    if _is_path_command(command) and not os.path.exists(command):
        return False
    changed = False
    for event in spec["events"]:
        event_list = hooks_dict.setdefault(event, [])
        # 1. drop stale same-identity entries whose command != the resolved one.
        cleaned = []
        for entry in event_list:
            kept = []
            for h in entry.get("hooks", []):
                existing = h.get("command", "")
                if any(s in existing for s in spec["identity"]) and existing != command:
                    changed = True
                    continue  # drop stale
                kept.append(h)
            entry["hooks"] = kept
            if kept:
                cleaned.append(entry)
        event_list[:] = cleaned
        # 2. add if the exact command is absent from this event.
        present = any(h.get("command", "") == command
                      for entry in event_list for h in entry.get("hooks", []))
        if not present:
            new_entry: dict = {"hooks": [{
                "type": "command", "command": command,
                "timeout": spec["timeout"],
                "statusMessage": spec["statusMessage"],
            }]}
            if spec.get("matcher") is not None:
                new_entry["matcher"] = spec["matcher"]
            event_list.append(new_entry)
            changed = True
    return changed


def _install_claude_hook():
    settings_path = os.path.join(_user_home(), ".claude", "settings.json")
    settings_dir = os.path.dirname(settings_path)
    os.makedirs(settings_dir, exist_ok=True)
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    hooks = settings.setdefault("hooks", {})

    # ms-160 e-5806: install every hook from the single-source-of-truth manifest
    # so `beacon init` and `beacon skill install` can no longer diverge. This
    # replaces the per-hook blocks that previously lived here (commit / save /
    # halt / postcompact / stop) and additionally wires the session-start and
    # bus-inbox receive hooks the init path was missing.
    for spec in HOOK_MANIFEST:
        _install_manifest_hook(hooks, spec)

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("Installed Claude Code hooks (commit / save / halt / postcompact / "
          "stop / session-start / bus-inbox)")


def _skill_profession_from_text(text: str) -> str:
    """Return the ``profession:`` frontmatter value of a skill markdown, or
    ``"core"`` when absent (ms-108 e-3364).

    Skills carry an optional ``profession`` tag so install can gate them by job
    template: a ``dev`` project should not receive the ``beacon-sales-*`` skills
    and vice versa. Untagged skills are ``core`` (= install for every
    profession). Companion files (``_*.md``) are not read here; they follow
    their owning skill's install decision.
    """
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return "core"
    pm = re.search(r"^profession:\s*(\S+)", m.group(1), re.M)
    return (pm.group(1).strip().lower() if pm else "core")


def _requested_professions() -> set:
    """Return the set of professions whose skills should be installed here.

    Always includes ``"core"``. Adds the current project's ``profession`` (read
    from the resolved project file, if any) and the ``BEACON_PROFESSION`` env
    override. Install is **additive** — copying skills never removes previously
    installed ones — so a user who works both a dev and a sales project ends up
    with the union across their setups (ms-108 e-3364).
    """
    profs = {"core"}
    env = (os.environ.get("BEACON_PROFESSION", "") or "").strip().lower()
    if env:
        profs.add(env)
    try:
        pf = get_project_file()
        if pf and os.path.exists(pf):
            with open(pf, encoding="utf-8") as f:
                data = json.load(f) or {}
            p = (data.get("profession") or "").strip().lower()
            if p:
                profs.add(p)
    except Exception:
        pass
    return profs


def _skill_is_installable(src_path: str, requested: set) -> bool:
    """True when the skill at ``src_path`` should install for ``requested``
    professions: its profession is ``core`` (always) or is in ``requested``."""
    try:
        prof = _skill_profession_from_text(
            open(src_path, encoding="utf-8").read())
    except Exception:
        return True  # unreadable frontmatter → fail open (install), never hide a skill by error
    return prof == "core" or prof in requested


def _install_skills():
    """Copy beacon skills to ~/.claude/skills/ for Claude Code integration.

    ms-108 e-3364: profession-gated. Only ``core`` skills plus the skills of
    the professions in ``_requested_professions()`` are installed, so a dev
    project does not get the ``beacon-sales-*`` skills (and vice versa).
    """
    skills_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "skills",
    )
    if not os.path.isdir(skills_src):
        return
    requested = _requested_professions()
    skills_dst = os.path.join(_user_home(), ".claude", "skills")
    os.makedirs(skills_dst, exist_ok=True)
    installed = []
    skipped = []
    for fname in os.listdir(skills_src):
        if not fname.endswith(".md"):
            continue
        skill_name = fname[:-3]  # beacon-log.md -> beacon-log
        if not _skill_is_installable(os.path.join(skills_src, fname), requested):
            skipped.append(skill_name)
            continue
        dst_dir = os.path.join(skills_dst, skill_name)
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(os.path.join(skills_src, fname), os.path.join(dst_dir, "SKILL.md"))
        installed.append(skill_name)
    if installed:
        print(f"Installed skills: {', '.join(sorted(installed))}")
    if skipped:
        print(f"Skipped {len(skipped)} off-profession skills "
              f"(professions installed: {', '.join(sorted(requested))})")


def _maybe_prompt_initial_profile() -> Optional[str]:
    """ms-64 e-1633: When the user has auth-logged into >= 2 Beacon backends
    and has not already committed this cwd to a profile, ask once which one
    this project should target. Returns the chosen profile name (or None if
    the prompt should be skipped — caller falls back to silent default).

    Skip rules (all silent, no prompt):
      - --profile / BEACON_PROFILE explicitly set (= user already chose)
      - .beacon/cloud.json already has a `profile` field (= prior choice persisted)
      - len(logged_in_profiles) < 2 (= no ambiguity)
      - stdin is not a TTY (= hook / CI / pipe — don't block on input)
    """
    if os.environ.get("BEACON_PROFILE"):
        return None
    cloud_path = _get_cloud_config_path()
    if os.path.exists(cloud_path):
        try:
            with open(cloud_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
            if existing.get("profile"):
                return None
        except Exception:
            pass
    try:
        import profile as _profile  # type: ignore[import-not-found]
        candidates = _profile.list_logged_in_profiles()
    except Exception:
        return None
    if len(candidates) < 2:
        return None
    if not sys.stdin.isatty():
        return None
    try:
        return _profile.prompt_choose_profile(candidates)
    except Exception:
        return None


def _persist_initial_profile_choice(profile_name: str) -> None:
    """Write {"profile": <name>} to .beacon/cloud.json so that all subsequent
    commands in this cwd resolve to that profile (= cwd-based auto-switch)."""
    cloud_path = _get_cloud_config_path()
    os.makedirs(os.path.dirname(cloud_path) or ".", exist_ok=True)
    existing: dict = {}
    if os.path.exists(cloud_path):
        try:
            with open(cloud_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    existing["profile"] = profile_name
    with open(cloud_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _build_disclosure_policy_from_env() -> dict:
    """Resolve the disclosure_policy for a new project from env vars.

    ms-63 / e-1441: ``beacon init --sensitivity {high,low}`` (forwarded via
    ``BEACON_SENSITIVITY``) lets the user pick the project posture at
    create-time. Default is ``high`` per SPEC § 設計方針 2 — forgetting to
    configure must fail closed (= AI clams up) rather than fail open (= AI
    leaks). The OSS dogfood (Beacon itself) opts in to ``low`` explicitly.

    Recognised values: ``high`` or ``low``. Anything else (typo / future
    value) silently degrades to ``high`` so a misconfiguration cannot
    accidentally produce an open project.
    """
    raw = os.environ.get("BEACON_SENSITIVITY", "").strip().lower()
    sensitivity = "low" if raw == "low" else "high"
    # The semantics mirror server/envelope.normalize_disclosure_contract:
    # high → schema-only T5 + no free text; low → free mode + free text on.
    if sensitivity == "low":
        return {
            "sensitivity": "low",
            "t5_response_mode": "free",
            "t5_free_text": True,
        }
    return {
        "sensitivity": "high",
        "t5_response_mode": "schema-only",
        "t5_free_text": False,
    }


def _application_map_box_content(objective: str) -> str:
    """ms-104 e-3153a: init が用意する『空の箱』本文。

    map は状態の反映なので t=0 では surface ゼロ = 空。ここで作るのはヘッダ (=
    契約 / 読み方) と『/beacon-map で埋めて』の指示だけの placeholder。実際の充填は
    /beacon-map 初回 + deploy reconcile が行う。project-vision と同格の常設 CORE doc。
    """
    obj_line = objective.strip() or "(未設定 — beacon summary / project-vision 参照)"
    return (
        "# アプリケーション全貌マップ\n\n"
        "> **現在地の断面** — 今このプロダクトに何ができるか (= 全機能の入口) を写した索引。\n"
        "> project-vision (目的地) / milestone 履歴 (軌跡) に続く 3 つ目の軸。\n"
        "> 新機能を足す前にここを引いて「近い既存機能があるか」を確認し、二重実装を防ぐ。\n"
        ">\n"
        "> **これは init が用意した空の箱です。** 中身 (= surface 一覧) はまだありません。\n"
        "> プロジェクトが育ったら `/beacon-map` を実行すると、現在の全 surface (CLI / API /\n"
        "> Skill 等) を列挙して初版を生成します。以降は deploy 時の reconcile と map-drift\n"
        "> trigger で自動的に鮮度が保たれます。\n"
        ">\n"
        "> **読み方 (生成後)**: 章 (価値エリア) → 節 (価値) → surface 行。各 surface 行末の\n"
        "> `` `type:ident` `` は照合の楔 (= 実在するかを機械で突く目印、cli/api/skill/file)。\n"
        ">\n"
        f"> 目的 (objective): {obj_line}\n\n"
        "## (未生成)\n\n"
        "`/beacon-map` で現在の全 surface から初版を生成してください。\n"
    )


def _seed_application_map_box(objective: str) -> None:
    """init 時に空の application-map CORE doc (固定 id) を local docs dir に seed。

    ms-104 e-3153a: 『必須インフラは init で箱を作る』統一パターン。既に存在すれば
    上書きしない (= 冪等)。cloud mode の seed は server 側の責務 (別 site、未配線)。
    IO 失敗は init を止めない (best-effort)。
    """
    try:
        docs_dir = _get_docs_dir()
        os.makedirs(docs_dir, exist_ok=True)
        fpath = os.path.join(docs_dir, "application-map.md")
        if os.path.exists(fpath):
            return
        content = _add_frontmatter(
            _application_map_box_content(objective), "core", "", "", ""
        )
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        return


def cmd_onboarding_plan():
    """Emit the onboarding plan (WHAT to ask + the role of the objective/vision)
    for BEACON_PROFESSION as JSON, then exit — writes nothing.

    ms-133 e-4648 / e-4408: this is the descriptor-driven seam that lets the
    /beacon-init and /beacon-vision Skills render the RIGHT questions per
    occupation WITHOUT encoding `if profession == "sales"` in the Skill markdown
    (the SPEC review's high#1). The Skill calls `beacon init --profession <p>
    --plan`, reads this JSON, renders `ask[]`, and frames the vision deep-dive
    with `vision_role`. Reached only via `init --plan`, so it needs no standalone
    verb / README / help_json entry."""
    profession = os.environ.get("BEACON_PROFESSION", "dev") or "dev"
    plan = occupation.onboarding_plan(profession)
    print(json.dumps(plan, ensure_ascii=False, indent=2))


def cmd_init():
    name = os.environ.get("BEACON_NAME", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    pf = get_project_file()
    os.makedirs(os.path.dirname(pf), exist_ok=True)
    retro_day = os.environ.get("BEACON_RETRO_DAY", "monday")
    # ms-63 / e-1428 + e-1441: persist the project disclosure_policy at
    # init time so subsequent envelope mints (server/envelope.issue_envelope)
    # can snapshot the contract. Default is high (safe). The field lives
    # next to the existing top-level project fields so legacy readers that
    # don't know about it just ignore it (= forward-compat append-only).
    disclosure_policy = _build_disclosure_policy_from_env()
    # ms-106 ① — 職種テンプレート選択. BEACON_PROFESSION picks the job-template
    # (= agent class instance, SPEC 設計方針 0). Default "dev" keeps the
    # existing schema byte-for-byte; "sales" emits the sales entity schema
    # (opportunities/accounts) instead of driving work through milestones.
    # Both carry milestones:[] so the shared validator passes unchanged.
    profession = (os.environ.get("BEACON_PROFESSION", "dev") or "dev").strip().lower()
    # ms-150 seam probe: the "profession → adopted target-classes → composed
    # project" cascade moved behind ONE seam (occupation.build_new_project), so
    # the composition has a single home future per-class catalog migrations plug
    # into instead of a 4-way branch here. cmd_init keeps only the I/O and the
    # profession-specific USER FEEDBACK (prints / map seed / Next hints) below.
    data = occupation.build_new_project(
        name, objective, profession,
        retro_day=retro_day, disclosure_policy=disclosure_policy)
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    # ms-148 e-5414: init writes project.json only; the first command migrates it
    # into SQLite (get_store, migrate-on-first-use). We deliberately do NOT create
    # the db here — doing so would make a re-run of `beacon init` reset the db but
    # also make "init then hand-edit project.json before the first command" a
    # no-op. Re-init on an existing project leaving a stale db is a known edge
    # (follow-up e-5441); it is rare and `beacon init` is a once-per-project step.
    print(f"Created {pf}")
    # ms-148 review (PR#668 AX finding, HIGH): re-init on a project whose SQLite
    # store already has data is a silent no-op — get_store only migrates when the
    # db is empty (migrate-on-first-use), so the project.json we just wrote is
    # never read and the old store wins. Printing only "Created" makes the user
    # believe the init took effect. We do NOT change the behaviour (fully
    # resolving re-init is the filed follow-up e-5441) — we just make the no-op
    # LOUD so it stops being an invisible trap.
    try:
        from store_sqlite import sqlite_db_path_for, db_has_data
        _existing_db = sqlite_db_path_for(pf)
        if db_has_data(_existing_db):
            print(
                f"⚠ 既存の SQLite store ({_existing_db}) に既にデータがあります。\n"
                f"  いま書いた {pf} は次回コマンドで読まれません — SQLite store が真値で、\n"
                f"  空の db のときだけ project.json から移行します (migrate-on-first-use)。\n"
                f"  この init を反映させたい場合は {_existing_db} を .trash/ へ mv してから再実行してください。\n"
                f"  (既存 store を保持したまま再 init するのは既知エッジ e-5441 の follow-up 対象)",
                file=sys.stderr,
            )
    except Exception:
        # Never let this advisory check break init itself.
        pass
    # ms-150 e-5465: the profession → user-feedback strings (schema label +
    # "Next:" hint) live in ONE place (occupation.init_display), the display
    # twin of build_new_project's composition branch, so cmd_init no longer
    # re-branches on profession literals — the alias set was drifting between
    # the composition seam and this print block (PR #669 保守性#2). cmd_init
    # keeps only the I/O around the returned strings.
    _display = occupation.init_display(profession)
    if _display["schema_label"]:
        print(f"  {_display['schema_label']}")
    # Visible feedback on the chosen posture (SPEC § acceptance 2 + 3):
    # default-high is silent-but-printed so the user notices, opt-in low
    # gets a single-line confirmation that the OSS-friendly mode is active.
    if disclosure_policy["sensitivity"] == "low":
        print("  disclosure_policy.sensitivity = low (open posture; free-text "
              "DM replies permitted)")
    else:
        print("  disclosure_policy.sensitivity = high (default-safe; T5 DM "
              "replies capped to schema)")

    # ms-104 e-3153a: 必須インフラは init で箱を作る統一パターン。空の
    # application-map CORE doc を seed する (中身は /beacon-map が後で埋める)。
    # ms-109 e-3404: application-map は開発インスタンスの surface 索引なので、
    # 開発 profession のときだけ箱を作る (営業等は map を持たない)。
    if profession in ("", "dev"):
        _seed_application_map_box(objective)

    chosen = _maybe_prompt_initial_profile()
    if chosen:
        _persist_initial_profile_choice(chosen)
        print(f"Profile pinned for this project: {chosen}")

    # Single-source "Next:" hint (see init_display note above). The data-defined
    # profession's schema-label line is now printed once, up with the other
    # schema labels, instead of a second time here.
    print(_display["next_hint"])


def cmd_common_setup():
    """Install Claude Code hooks, skills, and CLAUDE.md beacon section (idempotent)."""
    _append_claude_md()
    _install_git_hook()
    _install_claude_hook()
    _install_skills()
    print("Claude Code integration ready.")


def cmd_auth_check():
    """Exit 0 if authenticated, exit 1 if not."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("not_authenticated")
        sys.exit(1)
    email = getattr(creds, "email", "") or ""
    print(f"authenticated:{email}")
    sys.exit(0)


def cmd_cloud_check_project():
    """Exit 0 if project exists in cloud, exit 1 otherwise."""
    import sys as _sys
    project_id = _sys.argv[1] if len(_sys.argv) > 1 else os.environ.get("BEACON_CLOUD_PROJECT_ID", "")
    if not project_id:
        _sys.exit(1)
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        _sys.exit(1)
    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))
    try:
        client.get_project(project_id)
        _sys.exit(0)
    except Exception:
        _sys.exit(1)


def cmd_cloud_join():
    """Join an existing cloud project (no .beacon/ required)."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    project_id = os.environ.get("BEACON_CLOUD_PROJECT_ID", "")
    if not project_id:
        print("Error: project ID required")
        sys.exit(1)

    api_url = _resolve_active_api_url()
    token = _extract_token(creds)

    from api_client import ApiClient
    client = ApiClient(api_url, token)

    try:
        data = client.get_project(project_id)
    except RuntimeError as e:
        if "404" in str(e):
            print(f"Project '{project_id}' not found in cloud.")
        else:
            print(f"Error: {e}")
        sys.exit(1)

    core.validate_project(data)

    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    os.makedirs(beacon_dir, exist_ok=True)

    cloud_config_path = os.path.join(beacon_dir, "cloud.json")
    with open(cloud_config_path, "w", encoding="utf-8") as f:
        json.dump({"project_id": project_id, "api_url": api_url}, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # e-1861 (ms-61): No longer write `{"mode": "cloud"}` to config.json —
    # cloud.json existence is the sole source of truth. The previous dual
    # write created a silent-drift attack surface (= sub-agent overwriting
    # config.json could flip cloud → local without touching cloud.json).

    # ms-84 Phase 3 (e-2037): no longer write a local project.json on
    # cloud join. The CLI reads through Store → StoreApi in cloud mode,
    # so a local cache file just decays into a silent-drift source the
    # moment another writer (web UI / server-side scheduler / another
    # session) touches the cloud document. If a stale project.json
    # already exists at this path (= migrating an old install or a
    # leftover from a prior local-mode invocation), rename it to keep a
    # one-shot recovery copy and unblock the cut-over.
    pf = get_project_file()
    renamed = _rename_local_project_json_for_cloud_cutover(pf)

    print(f"Joined cloud project: {project_id}")
    print(f"Project: {data.get('name', 'unnamed')}")
    if renamed:
        print(f"  local cache: {pf} → {renamed} (ms-84 Phase 3 cut-over)")


# ms-126 / e-4222 — the untriaged-is-machine-only actor gate
# (_caller_is_human_for_untriaged / _human_untriaged_bypass_refused /
# _HUMAN_UNTRIAGED_REFUSED_MSG) moved to commands_shared.py in ms-127 e-4319b
# (they gate both cmd_task_add here-adjacent and cmd_milestone_add). The full
# design rationale now lives with those functions' docstrings in commands_shared.


# ---------------------------------------------------------------------------
# Milestone commands
# ---------------------------------------------------------------------------





























# --- 目的達成レビュー CLI surface (ms-119 / e-3912) ---
# 職種中立な target 遷移承認 primitive (lib/transition_approval.py + core.py の
# 永続化層) を CLI に露出する。開発 (milestone) / 運用 (operation) の完了主張遷移
# を、人間承認を挟んで確定させる。approve = 遷移実行、reject = 遷移せず記録。































# ---------------------------------------------------------------------------
# Descriptor-driven target verbs (ms-122 e-3956) — create / advance / close /
# instances for a data-defined target-class. These operate on the descriptor's
# own collection and are orthogonal to the ms-119 review gate above.
# ---------------------------------------------------------------------------













# ---------------------------------------------------------------------------
# Thick-frame verbs on a data-defined target (ms-124 e-4089): WorkItems,
# Evidence, ball. These let a descriptor target carry the same cognitive
# primitives (units of doing / records of what happened / whose turn) a
# milestone or opportunity does, instead of being a bare phase machine.
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Target-class authoring (ms-124 e-4091) — declare a new target-class into
# project.json via CLI, the no-code onboarding path. Hand-editing project.json
# is forbidden by the project rules, so a person who can't write Beacon code
# still needs a sanctioned way to add their own kind of work (契約 / 稟議 / …).
# ---------------------------------------------------------------------------











# Default command groups probed by the AX full-surface snapshot. These are
# *group* commands (they dispatch to subcommands), so probing them with an
# unknown subcommand exercises the usage / error / exit-code surface WITHOUT
# executing real logic (no cloud calls, no state change) — safe to run on demand.
_SURFACE_SNAPSHOT_COMMANDS = [
    "milestone", "task", "doc", "pr", "target", "review", "bus", "trigger",
    "operation", "note", "session", "member", "claim",
]


def _collect_surface_snapshot(commands_list=None) -> list:
    """Probe the CLI command surface for the AX full-surface audit (e-3987).

    For each command group, run it with an unknown subcommand and capture the
    usage/error output + exit code. A silent no-op (exit 0 with no guidance on a
    bogus subcommand) is exactly the AX defect this snapshot lets the judge see.
    Mechanical: no interpretation, just the raw surface. Best-effort — a probe
    that times out / errors is recorded with an ``error`` note rather than
    aborting the whole snapshot.
    """
    install_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    beacon_bin = os.path.join(install_root, "bin", "beacon")
    if not os.path.isfile(beacon_bin):
        beacon_bin = "beacon"  # fall back to PATH
    cmds = commands_list if commands_list is not None else _SURFACE_SNAPSHOT_COMMANDS
    probes = []
    bogus = "__ax_surface_probe__"
    for c in cmds:
        entry = {"cmd": c, "probe_argv": f"{c} {bogus}"}
        try:
            p = subprocess.run([beacon_bin, c, bogus], capture_output=True,
                               text=True, timeout=20)
            entry["exit_code"] = p.returncode
            entry["stdout"] = (p.stdout or "")[:2000]
            entry["stderr"] = (p.stderr or "")[:2000]
            # a bogus subcommand that exits 0 with no stderr is a silent no-op
            entry["silent_no_op"] = (p.returncode == 0 and not (p.stderr or "").strip())
        except (subprocess.TimeoutExpired, OSError) as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        probes.append(entry)
    return probes


# ms-119 / e-4196: character budget for the whole-target 思想 artifact — this is a
# len() over the text diff (chars), NOT bytes. A milestone can carry ~100 commits;
# attaching every full diff would overflow a judge's context. Per-commit diffs are
# attached oldest-first until this budget is spent, then the remaining commits carry
# subject-only and the artifact is flagged truncated (never silently short).
_WHOLE_TARGET_DIFF_BUDGET = 300_000

# The ledger records a commit's hash sometimes abbreviated (pr_add child) and
# sometimes full (beacon log), so whole-target dedup keys on this many leading chars
# to fold the two spellings of the SAME commit into one. Trade-off: two DISTINCT
# commits sharing a prefix of this length would collide (astronomically rare) — the
# collector records the folded hash in the surviving commit's ``deduped`` list so
# the fold is observable, never a silent loss.
_LEDGER_HASH_ABBREV = 7


def _collect_whole_target_artifact(target_id: str):
    """Collect a whole-target 思想 artifact from the target's recorded commits
    (ms-119 / e-4196).

    A target-close philosophy review had no artifact source: --pr / --diff-ref are
    change-scoped, so a whole-MS close reviewed against nothing (照合対象が空). A
    target owns its implementation ledger — every commit is logged under it — and
    that recorded set is the mechanically-collectable "what this target changed",
    independent of the implementer's narrative (git history + project ledger). This
    walks the target's commit entries (ledger order = oldest-first), collects each
    commit's diff via ``git show``, and shapes the whole-target artifact bounded by
    ``_WHOLE_TARGET_DIFF_BUDGET``. The commit *subject* comes from the ledger entry
    (already recorded), so only the diff needs a git call.

    Returns ``(artifact, gaps)``. Raises ``ValueError`` if the target id does not
    resolve — this collector never calls ``sys.exit`` so it stays reusable outside
    the CLI; the CLI wiring (cmd_review_context) converts the error to an exit.

    Completeness invariants (ms-119 / e-4196 AX review — the artifact must never let
    a commit's change vanish silently):
      * merge commits are diffed against their first parent (``-m --first-parent``);
        a bare ``git show`` of a merge emits an empty combined diff, which would drop
        merge-introduced changes from the whole-target artifact.
      * a successful-but-empty diff is tagged ``empty_reason`` so a judge does not
        read ``""`` as "this commit changed nothing".
      * a commit recorded but unfetchable (logged on another machine) is carried with
        an ``error`` note AND counted into a gap ("N 件中 M 件取得不能, git fetch で
        回復") — so "gaps empty = complete input" stays a valid judge inference.
    """
    import review_spine
    data = load_project()
    target = core._find_approval_target(data, target_id)  # raises ValueError; CLI exits
    gaps = []
    # Walk recorded commit entries. Dedup by _LEDGER_HASH_ABBREV-char prefix so a
    # commit logged twice (pr_add child + beacon log) counts once; keep first-seen
    # (ledger = oldest-first) order. seen[key] accumulates any folded duplicate full
    # hashes so the fold is observable in the artifact, not silent.
    seen = {}
    refs = []  # [(hash, subject)]
    for ent in core._iter_all_entries(target.get("entries", []) or []):
        if not isinstance(ent, dict) or ent.get("type") != "commit":
            continue
        h = ((ent.get("meta") or {}).get("hash") or "").strip()
        if not h:
            continue
        key = h[:_LEDGER_HASH_ABBREV]
        if key in seen:
            seen[key].append(h)  # fold the duplicate spelling, but record it
            continue
        seen[key] = []
        refs.append((h, ent.get("description", "") or ""))
    commits = []
    spent = 0
    truncated = False
    n_error = 0
    for h, subject in refs:
        entry = {"hash": h, "subject": subject}
        folded = seen.get(h[:_LEDGER_HASH_ABBREV]) or []
        if folded:
            entry["deduped"] = folded
        if spent >= _WHOLE_TARGET_DIFF_BUDGET:
            entry.update({"diff": "", "omitted": True})
            commits.append(entry)
            truncated = True
            continue
        # `--format=` suppresses the header (subject travels from the ledger). -U15
        # widens context for a repo-blind judge. `-m --first-parent` makes a MERGE
        # commit show its first-parent diff instead of the empty combined diff a bare
        # `git show` produces (which would silently drop merge-introduced changes);
        # it is a no-op for regular commits.
        proc = subprocess.run(
            ["git", "show", "--format=", "-U15", "-m", "--first-parent", h],
            capture_output=True, text=True)
        if proc.returncode != 0:
            entry["error"] = (proc.stderr or "").strip()[:200]
            commits.append(entry)
            n_error += 1
            continue
        diff = proc.stdout or ""
        spent += len(diff)
        entry["diff"] = diff
        if not diff.strip():
            # successful but empty — distinct from "no diff collected". Tag the
            # reason so "" is not misread as "changed nothing".
            entry["empty_reason"] = "no-textual-diff (空 / メタのみの commit の可能性)"
        commits.append(entry)
    if not refs:
        gaps.append(f"{target_id} に記録された commit がありません。whole-target の "
                    f"照合対象が空です — この target の実装が beacon log されていないか、"
                    f"変更を伴わない target です (SPEC § 方針5、hard-block しない)。")
    elif n_error:
        # some commits are recorded but their diff could not be collected (typically
        # logged on another machine, not fetched). Declaring it keeps "gaps empty =
        # complete input" a valid inference for the judge (ms-119 e-4196 AX finding).
        gaps.append(f"{len(refs)} 件中 {n_error} 件の commit の diff を取得できません"
                    f"でした (別マシンで記録され未 fetch の可能性)。`git fetch` 後に"
                    f"再実行すると照合対象が揃います。")
    if truncated:
        gaps.append(f"whole-target artifact が大きいため diff を "
                    f"{_WHOLE_TARGET_DIFF_BUDGET} 文字で打ち切りました。以降の commit は "
                    f"subject のみです — 全体の drift を精査するには PR 単位で `--pr` "
                    f"レビューしてください。")
    artifact = review_spine.whole_target_artifact(commits, target_id=target_id,
                                                  truncated=truncated)
    return artifact, gaps


def _collect_change_diff(argv: list, target_ref: str, gaps: list) -> str:
    """Run a change-scoped diff collector (gh pr diff / git diff) and return its text
    (ms-119 / e-4196 maint review — keeps each artifact-source branch self-contained).

    Exits the process on a collection failure and appends an empty-diff gap. This is
    CLI glue (bound to cmd_review_context), so exiting here is fine — unlike the
    reusable ``_collect_whole_target_artifact`` collector, which raises instead.
    """
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"Error: diff collection failed: {proc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    diff_text = proc.stdout
    if not diff_text.strip():
        gaps.append(f"{target_ref} の差分が空です (レビュー対象の変更がありません)。")
    return diff_text


def _model_independence_gap():
    """ms-119 / e-3988 wire-up (思想レビュー finding ②) — run the judge/implementer
    model independence check in the kernel and return a gap string if they match.

    The check used to be a callable with no caller (enforcement was skill prose).
    Running it here bakes the warning into the bundle's gaps, so the judge itself
    sees "judge model == implementer model" in its input — structural, not prose.
    Returns None when no judge model was supplied or the models differ.
    """
    judge_model = os.environ.get("BEACON_JUDGE_MODEL", "").strip()
    if not judge_model:
        return None
    import review_spine  # local import — matches this file's module-import idiom
    impl_model = os.environ.get("BEACON_IMPLEMENTER_MODEL", "").strip()
    verdict = review_spine.judge_model_independence(impl_model, judge_model)
    return None if verdict["ok"] else ("⚠ モデル独立性: " + verdict["reason"])


def _emit_attainment_context(target_id: str, *, pr: str = "", diff_ref: str = "") -> None:
    """Emit the 目的達成 evidence-generation bundle for a context-zero judge
    (ms-119 / e-4005).

    Resolves the target's SPEC 原典 + criteria mechanically and prints the bundle
    as JSON, so /beacon-review-run (attainment mode) can hand it to a fresh
    subagent that verifies each criterion against real code. The bundle carries
    no implementer narrative — the whole point is that the evidence is NOT the
    implementer's self-report.
    """
    import review_spine
    if not target_id:
        print("Error: --type attainment needs --target <ms-XX|op-X> (its 原典 is "
              "the target's SPEC, resolved automatically).", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        target = core._find_approval_target(data, target_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    kind = core._approval_target_kind(target_id)

    gaps = []
    spec_doc = _spec_doc_for_target(target_id, kind)
    if spec_doc:
        spec_origin_id = spec_doc.get("doc_id", "") or spec_doc.get("id", "")
        # list_documents() may return metadata without the body — fetch the full
        # doc so the judge gets the whole SPEC 原典, never a truncated one.
        spec_content = spec_doc.get("content", "") or ""
        if not spec_content.strip() and spec_origin_id:
            try:
                full = get_store().get_document(spec_origin_id)
                if full:
                    spec_content = full.get("content", "") or ""
            except Exception:
                pass
        if not spec_content.strip():
            gaps.append(f"SPEC 原典 {spec_origin_id} は本文が空です。目的達成の照合"
                        f"基準が弱く、judge は intent 推定に留まります (SPEC § 方針5)。")
    else:
        spec_origin_id = ""
        spec_content = ""
        gaps.append(f"{target_id} に SPEC 原典が紐づいていません。判定は target の "
                    f"objective / acceptance と実コードだけが根拠になります "
                    f"(hard-block しない、SPEC § 方針5)。")

    # criteria: the target's own written success conditions, if any. The SPEC
    # (origin) carries §やる / 受入条件 the judge extracts; these are extra
    # structured criteria surfaced explicitly.
    criteria = []
    if (target.get("objective") or "").strip():
        criteria.append({"source": "objective", "text": target["objective"].strip()})
    if (target.get("acceptance_criteria") or "").strip():
        criteria.append({"source": "acceptance_criteria",
                         "text": target["acceptance_criteria"].strip()})

    # optional supporting diff (attainment verifies against full code, so the
    # diff is supplementary, not the sole artifact).
    diff_text = ""
    target_ref = target_id
    if pr and diff_ref:
        print("Error: --pr and --diff-ref are mutually exclusive.", file=sys.stderr)
        sys.exit(1)
    if pr:
        target_ref = f"{target_id} (PR #{pr})"
        proc = subprocess.run(["gh", "pr", "diff", pr], capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"Error: diff collection failed: {proc.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        diff_text = proc.stdout
    elif diff_ref:
        target_ref = f"{target_id} ({diff_ref})"
        proc = subprocess.run(["git", "diff", diff_ref], capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"Error: diff collection failed: {proc.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        diff_text = proc.stdout

    _mi = _model_independence_gap()
    if _mi:
        gaps.append(_mi)

    # ms-119 / e-4579: hand the judge the UNSTARTED highest/high backlog so its
    # verdict must reckon with each important-but-untouched task (task e-4579: the
    # disposition is the judge's determination against the 原典, not the
    # implementer's self-report; this design lives in the task, not the ms-119 SPEC).
    # Each carries id / priority / description so the
    # judge can propose done / superseded[理由] / blocks-attainment per task.
    backlog = [
        {"id": t.get("id"),
         "priority": ((t.get("meta") or {}).get("priority") or ""),
         "description": (t.get("description") or "")}
        for t in core.unstarted_priority_tasks(target)
    ]
    if backlog:
        gaps.append(
            f"未着手の重要タスク (highest/high) が {len(backlog)} 件あります。各タスクに "
            f"done / superseded[理由] / blocks-attainment の disposition を判定して"
            f"ください (attainment は AC 軸だが、未着手の重要作業は目的未達の強い "
            f"prior — 掃除機がある≠掃除した)。")

    bundle = review_spine.assemble_attainment_context(
        target_id=target_id,
        spec_origin_id=spec_origin_id,
        spec_content=spec_content,
        criteria=criteria,
        target_ref=target_ref,
        diff_text=diff_text,
        gaps=gaps,
        backlog=backlog,
        implementer_model=os.environ.get("BEACON_IMPLEMENTER_MODEL", "").strip(),
    )
    print(json.dumps(bundle, ensure_ascii=False))


def cmd_review_context():
    """Assemble the review-kernel bundle for an independent judge (ms-119 e-3947).

    Emits ONLY the 原典 (origin) + a mechanically-collected diff (artifact) as
    JSON, so the /beacon-review-run Skill can hand it to a fresh subagent that
    inherits none of the implementer session's context. This is the structural
    form of AX 原典 §2 (計器の必然): the human running the review never launders
    their own intent into the judge's input.

    Env:
        BEACON_REVIEW_TYPE: "ax" | "maintainability" | "philosophy" | "attainment"
                            | "decision-verification" (ms-154 e-5595: an independent
                            AI checks declared decision rationale against the real
                            code; its artifact is the decision stream, not a diff —
                            see _emit_decision_verification_context).
        BEACON_DIFF_REF:    git ref range (e.g. "origin/main...HEAD"); or
        BEACON_PR:          a PR number (uses `gh pr diff`).
        BEACON_ORIGIN_DOC:  doc-id of the 原典 (required for philosophy; the SPEC
                            / vision the implementation is checked against;
                            rejected with --type ax, whose 原典 is fixed).
        BEACON_TARGET_ID:   target id (ms-XX / op-X). Required for attainment (its
                            原典 = the target's SPEC, resolved automatically). For a
                            target-close judge-run review (philosophy) it is the
                            whole-target artifact source (e-4196): a close 節目 has
                            no --pr / --diff-ref, so the target's recorded commit
                            ledger is collected and the mode is set to "whole-target"
                            automatically (not settable via BEACON_MODE). Rejected
                            for pr-open review types (ax / maintainability).
        BEACON_MODE:        artifact scope, user-settable to "diff" (default,
                            change-scoped) or "full-surface" (command-surface
                            snapshot). "whole-target" is derived from BEACON_TARGET_ID
                            and cannot be set here.
    """
    import review_spine
    review_type = os.environ.get("BEACON_REVIEW_TYPE", "").strip()
    diff_ref = os.environ.get("BEACON_DIFF_REF", "").strip()
    pr = os.environ.get("BEACON_PR", "").strip()
    origin_doc = os.environ.get("BEACON_ORIGIN_DOC", "").strip()
    target_id = os.environ.get("BEACON_TARGET_ID", "").strip()
    mode = os.environ.get("BEACON_MODE", "diff").strip() or "diff"

    gaps = []

    # ms-119 / e-4005: 目的達成 evidence generation. Unlike ax / philosophy
    # (advisory findings), attainment hands a context-zero judge the target's
    # SPEC + criteria so it verifies attainment against REAL code and produces
    # met/partial/not-met evidence — the human then owns the verdict (approve is
    # e-4006-gated). Handled as a self-contained branch (its 原典 is the target's
    # SPEC, resolved from --target, not --origin-doc).
    if review_type == review_spine.REVIEW_ATTAINMENT:
        _emit_attainment_context(target_id, pr=pr, diff_ref=diff_ref)
        return

    # ms-154 / e-5595: decision-verification. The artifact is the declared
    # decision stream (what / why / evidence), not a code diff — a context-zero
    # judge checks each rationale against the real code the evidence points to.
    # Self-contained (its 原典 is a fixed repo-file, its artifact is fetched from
    # the decision read path), so it bypasses the diff/pr/target validation below.
    if review_type == "decision-verification":
        _emit_decision_verification_context()
        return

    # --- early input validation (ms-119 e-3947 dogfood: close silent no-ops so
    # the review capability's own CLI doesn't ship the defects it exists to
    # catch). Each guard rejects with a clean `Error:` + exit 1 (never a silent
    # win, never a raw traceback), so an automation loop reading exit codes can
    # tell a mistake happened at the point it happened. ---
    # ms-119 / e-3987: full-surface is now supported — a surface-snapshot
    # collector probes each command's help / representative error / exit code so
    # the AX judge can audit the whole command surface, not only this PR's diff.
    if mode not in ("diff", "full-surface"):
        print(f"Error: --mode must be 'diff' or 'full-surface', got {mode!r}.",
              file=sys.stderr)
        sys.exit(1)
    if pr and diff_ref:
        print("Error: --pr and --diff-ref are mutually exclusive; pass exactly "
              "one (the Usage brackets mean 'one of', not 'both').",
              file=sys.stderr)
        sys.exit(1)
    # ms-119 / e-4196 (AX review): --target (whole-target scope) and --pr / --diff-ref
    # (change scope) are contradictory artifact sources. The old elif chain silently
    # let --pr win and dropped --target, shrinking the review scope without a word.
    # Reject the combination so the scope can never quietly narrow (full-surface
    # ignores all three by design, so it is exempt).
    if mode != review_spine.MODE_FULL_SURFACE and target_id and (pr or diff_ref):
        print("Error: --target (target 全体の whole-target レビュー) と "
              "--pr / --diff-ref (1 変更のレビュー) は同時指定できません。close 節目は "
              "--target 単独、1 変更は --pr / --diff-ref 単独で指定してください。",
              file=sys.stderr)
        sys.exit(1)

    # --- origin (原典) resolution: mechanical, never implementer prose ---
    # ms-119 / e-4009: the accepted types + how each resolves its 原典 come from
    # the data-driven registry (skills/*/review-type.json), not a hardcoded
    # if/elif whitelist. A new judge-run review type is added by dropping a
    # descriptor + 原典 + SKILL — this command needs no edit.
    registry = review_spine.judge_run_review_types()
    desc = registry.get(review_type)
    if not desc:
        allowed = ", ".join(sorted(registry.keys())) or "(none registered)"
        print(f"Error: --type must be one of: {allowed}; got {review_type!r}. "
              f"(目的達成 review is human-gated via `beacon target`, not a judge "
              f"run. Register a new type with a skills/<type>/review-type.json "
              f"descriptor.)", file=sys.stderr)
        sys.exit(1)
    # ms-119 / e-4196 (AX review): a whole-target (--target) artifact only makes
    # sense for a review type that fires at a target-close 節目 (philosophy). A
    # pr-open type (ax / maintainability = interface-diff) has no whole-target
    # question in its instrument, so route it away instead of handing a judge a mode
    # its instrument doesn't know (which would make its findings nondeterministic).
    if target_id and desc.get("fires_on") == "pr-open":
        print(f"Error: --type {review_type} は interface 変更レビュー (--pr / "
              f"--diff-ref) です。target 全体の close レビューには --type philosophy "
              f"を使ってください (--target は whole-target 専用)。", file=sys.stderr)
        sys.exit(1)
    origin_spec = desc.get("origin", {}) if isinstance(desc.get("origin"), dict) else {}
    origin_kind = origin_spec.get("kind", "")
    if origin_kind == "repo-file":
        # 原典 is a fixed repo file that travels with the capability (Layer 3).
        # --origin-doc does not apply; silently ignoring it would let the caller
        # believe their doc became the 原典 — reject instead.
        ref = origin_spec.get("ref", "")
        if not ref:
            print(f"Error: review type {review_type!r} descriptor has origin.kind"
                  f"=repo-file but no 'ref' (skills/<type>/review-type.json is "
                  f"malformed).", file=sys.stderr)
            sys.exit(1)
        if origin_doc:
            print(f"Error: --origin-doc is not valid for --type {review_type} "
                  f"(its 原典 is fixed to {ref}).", file=sys.stderr)
            sys.exit(1)
        install_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        origin_path = os.path.join(install_root, *ref.split("/"))
        origin_id = ref
        try:
            with open(origin_path, encoding="utf-8") as f:
                origin_content = f.read()
        except OSError as e:
            print(f"Error: 原典 not found at {origin_id}: {e}", file=sys.stderr)
            sys.exit(1)
    elif origin_kind == "doc":
        # 原典 is a document supplied at review time (a target's SPEC / vision).
        # Absence is itself a finding (SPEC § 方針5): surface as a gap, don't lie.
        if not origin_doc:
            print(f"Error: --type {review_type} needs --origin-doc <doc-id> "
                  f"(the SPEC / vision the implementation is checked against).",
                  file=sys.stderr)
            sys.exit(1)
        doc = get_store().get_document(origin_doc)
        if not doc:
            print(f"Error: origin doc not found: {origin_doc}", file=sys.stderr)
            sys.exit(1)
        origin_id = origin_doc
        origin_content = doc.get("content", "")
        if not origin_content.strip():
            gaps.append(f"原典 {origin_doc} は本文が空です。drift の照合基準が"
                        f"無いため、findings は intent 推定に留まります (SPEC § 方針5)。")
    else:
        print(f"Error: review type {review_type!r} descriptor has unknown "
              f"origin.kind {origin_kind!r} (expected 'repo-file' or 'doc').",
              file=sys.stderr)
        sys.exit(1)

    # --- artifact collection: mechanical, no interpretation ---
    diff_text = ""
    artifact = None
    if mode == "full-surface":
        # ms-119 / e-3987: probe the command surface (help / representative error
        # / exit code) instead of a diff, so the AX judge audits the whole
        # surface. --pr / --diff-ref are ignored here (surface, not change-scoped).
        probes = _collect_surface_snapshot()
        target_ref = "full-surface (CLI command surface)"
        artifact = review_spine.surface_snapshot_artifact(probes, target_ref=target_ref)
        if not probes:
            gaps.append("surface snapshot が空です (コマンド surface を採取できません"
                        "でした)。")
    else:
        # ms-119 / e-4196 (maint review): each artifact source closes its own
        # collection (returncode check + diff/empty handling) so there is no
        # cross-branch `proc` re-tested by a duplicated `if pr or diff_ref` guard.
        if pr:
            target_ref = f"PR #{pr}"
            diff_text = _collect_change_diff(["gh", "pr", "diff", pr], target_ref, gaps)
        elif diff_ref:
            target_ref = diff_ref
            # ms-119 / e-4096: -U25 widens the diff context so surrounding code
            # travels with the change (git defaults to 3 lines — too few for a
            # repo-blind judge to see the enclosing function without opening files).
            diff_text = _collect_change_diff(["git", "diff", "-U25", diff_ref],
                                             target_ref, gaps)
        elif target_id:
            # ms-119 / e-4196: whole-target 思想 artifact. A close 節目 has no
            # --pr / --diff-ref (it reviews the WHOLE target, not one change), so the
            # target's own commit ledger is the mechanically-collected change source.
            # Sets `artifact` directly (diff_text stays ""). The collector raises on a
            # bad target id; the CLI layer converts that to a clean exit.
            try:
                artifact, tgaps = _collect_whole_target_artifact(target_id)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            target_ref = f"{target_id} (whole-target)"
            gaps.extend(tgaps)
            # the artifact is the whole target ledger, not this-PR's diff — label the
            # mode so a judge isn't told "diff" while handed a whole-target series.
            mode = review_spine.MODE_WHOLE_TARGET
        else:
            print("Error: pass --pr <n> or --diff-ref <base...head> or --target "
                  "<ms-XX|op-X> to collect the artifact (or --mode full-surface to "
                  "snapshot the command surface).", file=sys.stderr)
            sys.exit(1)

    _mi = _model_independence_gap()
    if _mi:
        gaps.append(_mi)

    # ms-119 / e-4096: attach the application-map surface index so the judge can
    # raise accuracy (spot a diff that duplicates an existing surface) WITHOUT
    # reaching into the repo — the sanctioned implementer-independent context.
    external_references = []
    surface_ref = _review_surface_index_reference()
    if surface_ref is not None:
        external_references.append(surface_ref)
    else:
        gaps.append("application-map (全 surface 索引) が未生成のため、judge は "
                    "『既存機能と重複していないか』を repo 参照なしには確認できません "
                    "(/beacon-map で生成すると review 精度が上がります)。")

    bundle = review_spine.assemble_review_context(
        review_type,
        origin_id=origin_id,
        origin_content=origin_content,
        diff_text=diff_text,
        mode=mode,
        target_ref=target_ref,
        gaps=gaps,
        known_judge_types=set(registry.keys()),
        implementer_model=os.environ.get("BEACON_IMPLEMENTER_MODEL", "").strip(),
        artifact=artifact,
        external_references=external_references,
    )
    print(json.dumps(bundle, ensure_ascii=False))


def _emit_decision_verification_context():
    """Assemble a decision-verification review kernel (ms-154 e-5595).

    The independent-verification path: fetch the declared decisions (what / why /
    evidence) from the decision arm and shape them as the review artifact, with
    the fixed 原典 (skills/decision-verification/principles.md) as origin. A
    context-zero judge then checks each declared rationale against the real code
    the evidence points to (catches AI post-hoc rationalization — P4).
    """
    import review_spine
    registry = review_spine.load_review_types()
    desc = registry.get("decision-verification")
    if not desc:
        print("Error: decision-verification review type not registered "
              "(skills/decision-verification/review-type.json missing).",
              file=sys.stderr)
        sys.exit(1)
    origin_id, origin_content = _repo_file_origin(desc)

    kind = os.environ.get("BEACON_DECISION_KIND", "").strip()
    limit_raw = os.environ.get("BEACON_DECISION_LIMIT", "").strip()
    limit = int(limit_raw) if limit_raw.isdigit() else 100

    decisions = []
    fetch_error = ""
    try:
        from commands_shared import _is_cloud_mode, _get_api_client
        if _is_cloud_mode():
            client, config = _get_api_client()
            pid = config.get("project_id", "")
            if pid:
                res = client.list_decisions(pid, kind=kind, limit=limit)
                decisions = res.get("decisions", []) if isinstance(res, dict) else []
    except BaseException as exc:  # best-effort: never crash the kernel assembly
        fetch_error = str(exc)

    artifact = {
        "kind": "decisions",
        "ref": kind or "all",
        "content": json.dumps(decisions, ensure_ascii=False, indent=2),
    }
    gaps = []
    if not decisions:
        gaps.append(
            "検証対象の decision がありません "
            f"({'cloud 未接続 / 記録ゼロ' if not fetch_error else 'fetch error: ' + fetch_error})。"
            "beacon decision record で記録が積まれてから再実行してください。")

    bundle = review_spine.assemble_review_context(
        "decision-verification",
        origin_id=origin_id,
        origin_content=origin_content,
        diff_text="",
        mode=review_spine.MODE_DECISION_AUDIT,
        target_ref=(kind or "decisions"),
        gaps=gaps,
        known_judge_types=set(registry.keys()),
        artifact=artifact,
    )
    print(json.dumps(bundle, ensure_ascii=False))


def _repo_file_origin(desc):
    """(origin_id, origin_content) for a repo-file 原典 descriptor, or exit with a
    clean error. Used by the BATCH review-context path (e-4125); the single-type
    path (cmd_review_context) resolves its origin inline because it also handles
    the doc-origin kind. Extracting the full single-path collection into a shared
    helper is a worthwhile follow-up (e-4125 maint review)."""
    origin_spec = desc.get("origin", {}) if isinstance(desc.get("origin"), dict) else {}
    ref = origin_spec.get("ref", "")
    if origin_spec.get("kind") != "repo-file" or not ref:
        print(f"Error: batch は原典が repo-file の review type のみ対象です "
              f"(descriptor: {desc.get('id')})。", file=sys.stderr)
        sys.exit(1)
    install_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    origin_path = os.path.join(install_root, *ref.split("/"))
    try:
        with open(origin_path, encoding="utf-8") as f:
            return ref, f.read()
    except OSError as e:
        print(f"Error: 原典 not found at {ref}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_review_batch_context():
    """Emit ALL review bundles that fire together at a 節目, in one call
    (ms-119 / e-4125).

    beacon review context --batch --pr <N>

    A 節目 fires a PAIR of reviews (a PR-open fires AX + maintainability). Running
    them as two separate one-off calls means two judge invocations the caller
    must orchestrate by hand and two disconnected reports. This emits one
    envelope carrying every judge-run bundle bound to the node — the
    /beacon-review-run Skill fans the judges out in parallel over one shared diff
    + surface index, then folds their findings into one deduped, consensus-scored
    report (review_spine.aggregate_review_reports).

    Today only the PR-open node (--pr) is a pure judge-run fan-out. The
    target-close pair (思想 + 目的達成) mixes a judge-run review with a
    human-gated one, so it is orchestrated per-type by the Skill using
    review_spine.batch_review_types_for_node("target-close").
    """
    import review_spine
    pr = os.environ.get("BEACON_PR", "").strip()
    if not pr:
        print("Usage: beacon review context --batch --pr <N>  "
              "(PR-open の節目に発火する全レビュー[AX+保守性]の bundle を1回で出力)",
              file=sys.stderr)
        sys.exit(1)
    node = "pr-open"
    due = review_spine.batch_review_types_for_node(
        node, registry=review_spine.load_review_types())
    due = [d for d in due if d["judge_run"]]
    if not due:
        print("Error: PR-open の節目に発火する judge-run review type がありません "
              "(skills/*/review-type.json の fires_on=pr-open を確認)。",
              file=sys.stderr)
        sys.exit(1)

    target_ref = f"PR #{pr}"
    proc = subprocess.run(["gh", "pr", "diff", pr], capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"Error: diff collection failed: {proc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    diff_text = proc.stdout
    shared_gaps = []
    if not diff_text.strip():
        shared_gaps.append(f"{target_ref} の差分が空です (レビュー対象の変更がありません)。")

    external_references = []
    surface_ref = _review_surface_index_reference()
    if surface_ref is not None:
        external_references.append(surface_ref)
    else:
        shared_gaps.append("application-map (全 surface 索引) が未生成のため、judge は "
                           "既存機能との重複を repo 参照なしには確認できません "
                           "(/beacon-map で生成すると review 精度が上がります)。")

    registry = review_spine.load_review_types()
    implementer_model = os.environ.get("BEACON_IMPLEMENTER_MODEL", "").strip()
    reviews = []
    for d in due:
        rtype = d["review"]
        origin_id, origin_content = _repo_file_origin(registry.get(rtype, {}))
        bundle = review_spine.assemble_review_context(
            rtype, origin_id=origin_id, origin_content=origin_content,
            diff_text=diff_text, mode="diff", target_ref=target_ref,
            gaps=list(shared_gaps),
            known_judge_types=set(review_spine.judge_run_review_types().keys()),
            implementer_model=implementer_model,
            external_references=external_references)
        reviews.append({"review_type": rtype, "judge_run": True, "bundle": bundle})

    print(json.dumps({
        "node": node,
        "target_ref": target_ref,
        "reviews": reviews,
        "aggregate_hint": ("各 review を独立 judge に並列で走らせ、findings を "
                           "beacon が aggregate_review_reports で dedup + consensus "
                           "して1レポートに畳んでください (review_spine)。"),
    }, ensure_ascii=False))


def _review_surface_index_reference():
    """Read the application-map CORE doc and shape it as the judge bundle's
    surface-index external reference (ms-119 / e-4096), or None when absent.

    Best-effort: a transport failure / missing map returns None (the caller then
    records a gap), never crashes a review."""
    import review_spine
    try:
        doc = get_store().get_document("application-map")
    except Exception:
        return None
    if not doc:
        return None
    # Stale when the map-drift trigger says the surface moved since it was mapped.
    stale = False
    try:
        stale = os.path.isfile(os.path.join(_get_triggers_dir(), "map-drift.json"))
    except OSError:
        stale = False
    return review_spine.build_surface_index_reference(
        doc.get("content", ""), updated_at=doc.get("updated_at", ""), stale=stale)


















# ---------------------------------------------------------------------------
# Cloud-mode purge helpers (e-1030)
#
# Local purge mutates `.beacon/project.json` directly via load_project_unsafe
# + save_project_unsafe. Cloud purge instead calls the server's `POST
# .../purge` endpoint, which enforces owner-only access. The pre-flight
# duplicate listing is kept symmetric with local UX so the operator sees the
# same "re-run with --index <n>" hint regardless of mode.
# ---------------------------------------------------------------------------

def _cloud_purge_403_message(role_word: str) -> str:
    """Translate a server 403 into a CLI-friendly error string."""
    return (
        f"Error: only the project owner can purge {role_word} "
        "(owner access required). Ask the project owner to run "
        "this command, or transfer ownership first."
    )


def _cloud_fetch_project_or_exit(client, project_id: str) -> dict:
    """Fetch the cloud project for pre-flight; exit 1 on error."""
    try:
        return client.get_project(project_id)
    except (RuntimeError, ConnectionError) as e:
        print(f"Error: cannot read cloud project: {e}", file=sys.stderr)
        sys.exit(1)


def _cloud_purge_dispatch(action_label: str, fn, *,
                          json_mode: bool, success_fmt) -> None:
    """Call a purge API method and render success / failure output.

    Args:
        action_label: short label for error messages ("milestone", etc).
        fn: zero-arg callable that performs the API request.
        json_mode: BEACON_JSON=1 toggle.
        success_fmt: callable(result) → list[str] of lines for human output.
    """
    try:
        result = fn()
    except RuntimeError as e:
        msg = str(e)
        if "403" in msg:
            print(_cloud_purge_403_message(action_label + "s"), file=sys.stderr)
            sys.exit(1)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ConnectionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if json_mode:
        out = dict(result)
        out["purged"] = True
        print(json.dumps(out, ensure_ascii=False))
    else:
        for line in success_fmt(result):
            print(line)




# Log commands — moved to lib/cmd_log.py (ms-127 e-4320); re-imported at top.


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def cmd_sync():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    # ms-143: profession-generic target resolution (not the dev-concrete symbol).
    target = occupation.resolve_target(data, ms_id)

    entries = target.setdefault("entries", [])
    existing_hashes = set()
    for entry in entries:
        if entry.get("type") == "commit":
            h = entry.get("meta", {}).get("hash", "")
            if h:
                existing_hashes.add(h[:7])

    result = subprocess.run(
        ["git", "log", "--oneline", "-20", "--pretty=format:%h|%s|%ci"],
        capture_output=True, text=True,
    )

    added = 0
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        h, msg, date_str = parts
        if h not in existing_hashes:
            commit_date = date_str.split(" ")[0]
            entries.insert(0, {
                "id": core.next_entry_id(data),
                "type": "commit",
                "description": msg,
                "date": commit_date,
                "created_at": commit_date,
                "done_at": commit_date,
                "status": "done",
                "meta": {"hash": h, "message": msg},
            })
            added += 1

    if added:
        save_project(data)
        print(f"Synced {added} new commits to: {work_model.target_label(target)}")
    else:
        print("No new commits to sync.")


# Task + entry commands — moved to lib/cmd_task.py (ms-127 e-4319b). Add new
# task/entry handlers there, not here.


# ---------------------------------------------------------------------------
# Persistence poisoning defense (ms-54 / e-1293)
#
# Defense in depth on top of the bus envelope verify layer (e-1155 Phase 1).
# Even though the envelope tier blocks an inbound DM from auto-executing a
# Skill, an AI that *reads* a DM and then interprets the content as a user
# request and calls ``note add`` / ``doc add`` / ``session log`` itself is a
# different attack surface — the envelope guard never sees that call. To
# close the gap, the handlers themselves refuse any write whose source is
# marked as bus-origin via ``BEACON_BUS_ORIGIN=1`` (or ``--bus-origin``).
#
# Producers in the bus path SHOULD set this flag when forwarding
# bus-derived content into a persistence handler. The current codebase has
# no such producer yet — the flag is in place as a structural defense for
# future code paths and as a tripwire if attack-surface code ever appears.
# Legitimate user / AI-internal flows do not pass the flag, so this never
# fires for ordinary use.
#
# Rejected attempts are logged to a local audit jsonl
# (``.beacon/persistence_poisoning_audit.jsonl``) so the attempt remains
# forensically visible even without cloud connectivity. The local file is
# the source of truth for this defense — by design we do NOT round-trip
# through the cloud audit collection (a bus-origin caller is, by
# definition, not trusted to write audit records remotely).
# ---------------------------------------------------------------------------


# Session Notes (note commands) — moved to lib/cmd_note.py (ms-127 e-4320);
# re-imported at top.


def _resolve_channel_root() -> "Path | None":
    """Find the directory containing channel/bus.mjs across all install paths.

    Walks a fixed candidate list in priority order so dev clone, Homebrew, and
    pypi (pipx/pip) installs all locate the bundled channel assets without a
    hard-coded layout. Returns None if no candidate exists (caller surfaces a
    clean error). ms-54 e-1169.

    Candidates tried in order:
      1. ``$BEACON_CHANNEL_DIR`` (env override — used by tests / Win users
         who manually copied channel/ to a non-standard location).
      2. ``<commands.py>/../../channel``           — dev clone layout
         (lib/commands.py + channel/ siblings under <repo>).
      3. ``<commands.py>/../channel``              — Homebrew libexec layout
         (libexec/commands.py + libexec/channel/).
      4. ``<commands.py>/../../_bundled_channel``  — pypi wheel layout
         (beacon_cli/_bundled_lib/commands.py + beacon_cli/_bundled_channel/).
    """
    from pathlib import Path  # local import — pathlib not yet at module level
    import os as _os
    override = _os.environ.get("BEACON_CHANNEL_DIR", "").strip()
    if override:
        cand = Path(override).resolve()
        if (cand / "bus.mjs").exists():
            return cand
    commands_path = Path(__file__).resolve()
    candidates = [
        commands_path.parent.parent / "channel",           # dev clone
        commands_path.parent / "channel",                  # Homebrew libexec
        commands_path.parent.parent / "_bundled_channel",  # pypi wheel
    ]
    for c in candidates:
        if (c / "bus.mjs").exists():
            return c
    return None


def _build_channel_server_entry(bus_path: "Path") -> dict:
    """Construct the `.mcp.json` `beacon-bus` server entry, OS-aware.

    Cross-platform pitfalls this handles (ms-54 e-1159):

    - **Windows ``node`` bare command**: Claude Code on Windows shells out
      via CreateProcess (not cmd.exe), so a bare ``"node"`` fails to spawn
      when node.exe is on PATH but not in CWD. We resolve the absolute
      node.exe path via ``shutil.which("node")`` and fall back to
      ``cmd.exe /c node`` so the shell does the PATH lookup. Mac/Linux
      keep the bare ``"node"`` form (shell does fork+exec lookup).
    - **`/tmp` log path**: ``/tmp`` doesn't exist on Windows; the file is
      silently never written and bus.mjs loses its diagnostic log. We use
      ``%TEMP%\\beacon-bus-channel.log`` on Windows (via ``tempfile.
      gettempdir()`` which resolves env vars cross-platform). Mac/Linux
      keep ``/tmp/beacon-bus-channel.log`` for compatibility with the
      existing dogfood debug pipeline.
    """
    from pathlib import Path  # local import — pathlib not yet at module level
    import platform as _platform
    import shutil as _shutil
    import tempfile as _tempfile
    is_windows = _platform.system() == "Windows"

    if is_windows:
        node_abs = _shutil.which("node") or _shutil.which("node.exe")
        if node_abs:
            command = node_abs
            args = [str(bus_path)]
        else:
            # Fallback: have cmd.exe walk PATH. Works even if node was
            # installed after this .mcp.json was generated.
            command = "cmd.exe"
            args = ["/c", "node", str(bus_path)]
        log_path = str(Path(_tempfile.gettempdir()) / "beacon-bus-channel.log")
    else:
        command = "node"
        args = [str(bus_path)]
        # Keep /tmp on Unix so existing dogfood log paths (e.g.
        # `/tmp/beacon-bus-channel.log` referenced in memo docs) stay
        # stable; tempfile.gettempdir() would return /var/folders/... on
        # macOS which breaks the muscle-memory `tail -f /tmp/...` flow.
        log_path = "/tmp/beacon-bus-channel.log"

    return {
        "command": command,
        "args": args,
        "env": {
            "BEACON_CHANNEL_ALLOWLIST": "dm",
            "BEACON_BUS_LOG": log_path,
        },
    }


def _ensure_channel_node_modules(channel_root: "Path") -> bool:
    """Make sure channel/node_modules/ exists (run `npm install` if not).

    Most install paths leave node_modules to runtime first-use because:
      - dev clone: contributor never runs npm install
      - pypi wheel: shipping node_modules in a wheel is platform-fragile
      - Homebrew: depends_on "node" + brew's install step should generate it,
        but a partial install / brew formula bug can leave it missing
    Returns True if node_modules is present (existed or created); False if
    install failed. ms-54 e-1169 + e-1191.

    Windows PATHEXT semantics (e-1191): the npm CLI on Windows ships as
    ``npm.cmd`` (a batch shim around node), not ``npm.exe``. Python's
    ``subprocess`` without ``shell=True`` does NOT consult PATHEXT, so
    ``subprocess.call(["npm", ...])`` raises FileNotFoundError on Windows
    even when Node.js is installed and ``shutil.which("node")`` finds it.
    The fix: resolve the absolute path via ``shutil.which`` (which DOES
    consult PATHEXT) FIRST, then call subprocess with the resolved path.
    """
    import subprocess as _subprocess
    import shutil as _shutil
    import platform as _platform

    nm = channel_root / "node_modules"
    if nm.exists():
        return True
    pkg_json = channel_root / "package.json"
    if not pkg_json.exists():
        return False

    is_windows = _platform.system() == "Windows"
    # Try canonical names first, then Windows-specific extensions. shutil.which
    # consults PATHEXT so on Win it will pick up npm.cmd via the bare "npm"
    # form too, but listing the explicit extensions makes the contract clear.
    candidates = ["npm"]
    if is_windows:
        candidates += ["npm.cmd", "npm.exe", "npm.bat"]
    npm_path = None
    for cand in candidates:
        resolved = _shutil.which(cand)
        if resolved:
            npm_path = resolved
            break

    if not npm_path:
        print("Error: `npm` not found on PATH.", file=sys.stderr)
        if is_windows:
            print("Install Node.js on Windows: `winget install OpenJS.NodeJS.LTS`",
                  file=sys.stderr)
            print("  or download from https://nodejs.org/ (LTS Windows Installer).",
                  file=sys.stderr)
        elif _platform.system() == "Darwin":
            print("Install Node.js on macOS: `brew install node` (or via nvm).",
                  file=sys.stderr)
        else:
            print("Install Node.js: your package manager (e.g. `apt install nodejs npm`)",
                  file=sys.stderr)
            print("  or via nvm (https://github.com/nvm-sh/nvm).", file=sys.stderr)
        return False

    print(f"channel/node_modules not found — running `npm install` in {channel_root}",
          file=sys.stderr)
    print(f"  using npm at: {npm_path}", file=sys.stderr)
    try:
        rc = _subprocess.call([npm_path, "install", "--silent"], cwd=str(channel_root))
    except OSError as e:
        print(f"Error: failed to launch npm at {npm_path}: {e}", file=sys.stderr)
        return False
    if rc != 0:
        print(f"Error: `npm install` exited {rc}.", file=sys.stderr)
        return False
    return nm.exists()


# ---------------------------------------------------------------------------
# Channel lifecycle: opt-out / opt-in / uninstall / status (ms-54 e-1266)
# ---------------------------------------------------------------------------
#
# The DM ("beacon-bus") channel has four user states:
#   (a) never installed — fresh user
#   (b) installed and active
#   (c) installed but paused (MCP entry removed, node_modules retained)
#   (d) opted out (no install will succeed, env / project / global flag set)
#
# The four CLI verbs that move between these states are:
#   beacon channel install               (a|c|d-without-opt-out) → b
#   beacon channel uninstall             b → c (default) or a (with --purge-files)
#   beacon channel opt-out [--global|--project]   any → d
#   beacon channel opt-in  [--global|--project]   d → previous
#
# `beacon channel status` prints the current state and predicts whether the
# next auto-install attempt will run. The whole surface is designed so that
# install paths (this CLI, the e-1238 auto-install, the e-1167 bclaude
# wrapper) all share `_is_bus_opted_out()` as the single gate — if any of
# (env BEACON_NO_BUS=1, project flag, global flag) is set, install is
# refused and the user sees a single consistent explanation.

def _user_beacon_config_path() -> str:
    """Absolute path to the user-global beacon config (~/.beacon/config.json).

    Distinct from project-local .beacon/config.json. We mint the directory
    on demand so the first `beacon channel opt-out --global` call doesn't
    fail because ~/.beacon does not yet exist.
    """
    return os.path.join(_user_home(), ".beacon", "config.json")


def _read_user_beacon_config() -> dict:
    """Read ~/.beacon/config.json, returning {} if missing or unreadable.

    Unreadable / corrupt files surface as {} so opt-out checks don't crash
    install paths; the broken file is preserved on disk so the user can
    diagnose it manually.
    """
    p = _user_beacon_config_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_user_beacon_config(d: dict) -> None:
    """Atomically write ~/.beacon/config.json, minting parent dir as needed."""
    p = _user_beacon_config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _read_project_bus_disabled() -> bool:
    """True if the active project sets `bus.disabled`.

    Project-scoped opt-out lives under a top-level `bus` object:
        { "bus": {"disabled": true}, ... }
    This shape mirrors the global config schema so a user inspecting
    either source (cloud doc or local project.json) sees the same idiom.

    ms-84 Phase 3 (e-2037): cloud mode reads the project document via
    Store → StoreApi instead of the local project.json, so the bus flag
    survives cloud truth-model cut-over. Local mode keeps reading the
    file. Missing or unreadable project → not opted out (consistent with
    how install requires a Beacon root anyway).
    """
    try:
        store = get_store()
        data = store.load_project()
    except Exception:
        return False
    bus = (data or {}).get("bus") or {}
    return bool(bus.get("disabled"))


def _write_project_bus_flag(disabled: bool) -> bool:
    """Set or clear the project-local `bus.disabled` flag.

    Returns True on write. False if no project document is available
    (caller decides whether to fail or treat as no-op). When
    ``disabled=False``, the bus object is removed entirely if empty so
    the document stays minimal.

    ms-84 Phase 3 (e-2037): routes through Store so cloud mode writes
    the flag to the cloud document (= survives across sessions / web UI),
    matching where ``_read_project_bus_disabled`` now reads from.
    """
    try:
        store = get_store()
        data = store.load_project()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    bus = data.get("bus") or {}
    if disabled:
        bus["disabled"] = True
        data["bus"] = bus
    else:
        bus.pop("disabled", None)
        if not bus:
            data.pop("bus", None)
        else:
            data["bus"] = bus
    try:
        store.save_project(data)
    except Exception:
        return False
    return True


def _bus_opt_out_status() -> dict:
    """Probe all three opt-out sources and report which (if any) is active.

    Returns {"env": bool, "project": bool, "global": bool, "any": bool}.
    The order matches the precedence we present to the user: env wins
    (transient, easy to flip), project next (scoped intent), global last
    (broad). `any` is the OR used by install gates.
    """
    env_flag = os.environ.get("BEACON_NO_BUS", "").strip() in ("1", "true", "yes", "on")
    project_flag = _read_project_bus_disabled()
    user_cfg = _read_user_beacon_config()
    global_flag = bool((user_cfg.get("bus") or {}).get("disabled"))
    return {
        "env": env_flag,
        "project": project_flag,
        "global": global_flag,
        "any": env_flag or project_flag or global_flag,
    }


def _is_bus_opted_out() -> "tuple[bool, str]":
    """Single gate every install path consults.

    Returns (is_opted_out, human_reason). The reason string is suitable
    for printing in CLI warnings / audit log lines without further
    formatting. Designed to be the only check sites need to make so the
    rule "install never silently overrides opt-out" holds globally.
    """
    s = _bus_opt_out_status()
    if not s["any"]:
        return False, ""
    sources = []
    if s["env"]:
        sources.append("env BEACON_NO_BUS=1")
    if s["project"]:
        sources.append("project (.beacon/project.json bus.disabled)")
    if s["global"]:
        sources.append("global (~/.beacon/config.json bus.disabled)")
    return True, "DM opt-out active: " + " + ".join(sources)


def cmd_channel_install():
    """Write .mcp.json with the Beacon bus Channel MCP server entry.

    Project-level opt-in for ms-54 channel/bus.mjs. Resolves the bundled
    bus.mjs absolute path via _resolve_channel_root() so dev clone /
    Homebrew / pypi installs all work. Refuses to overwrite an existing
    .mcp.json that already has unrelated mcpServers entries; merges
    instead when safe. ms-54 e-1152 / e-1169.
    """
    from pathlib import Path  # local import — pathlib not yet at module level
    cwd = Path.cwd()
    # ms-84 Phase 3 (e-2037): accept either marker — cloud mode has no
    # local project.json after cut-over.
    if not (
        (cwd / ".beacon" / "project.json").exists()
        or (cwd / ".beacon" / "cloud.json").exists()
    ):
        print("Error: no Beacon project in this directory "
              "(looked for .beacon/project.json or .beacon/cloud.json). "
              "Run `beacon init` (local) or `beacon cloud join <project-id>` (cloud) first.",
              file=sys.stderr)
        sys.exit(1)

    # ms-54 e-1266: refuse to install if any opt-out source is set. This is
    # the single gate every install path consults (manual install, e-1238
    # auto-install, e-1167 bclaude wrapper). Refusing here — rather than
    # silently no-op'ing — gives the user feedback that opt-out is in force
    # and tells them how to lift it.
    opted_out, reason = _is_bus_opted_out()
    if opted_out:
        print(f"Refusing to install: {reason}", file=sys.stderr)
        print("Lift the opt-out first:", file=sys.stderr)
        print("  beacon channel opt-in [--project|--global]   "
              "(or unset BEACON_NO_BUS)", file=sys.stderr)
        print("Or inspect current state with:  beacon channel status",
              file=sys.stderr)
        sys.exit(1)

    channel_root = _resolve_channel_root()
    if channel_root is None:
        print("Error: channel/bus.mjs not found in any expected location.", file=sys.stderr)
        print("Looked for:", file=sys.stderr)
        print("  - $BEACON_CHANNEL_DIR (env override)", file=sys.stderr)
        print("  - <repo>/channel        (dev clone)", file=sys.stderr)
        print("  - <libexec>/channel     (Homebrew)", file=sys.stderr)
        print("  - <beacon_cli>/_bundled_channel  (pypi wheel)", file=sys.stderr)
        print("Reinstall Beacon (brew reinstall / pipx reinstall) or set "
              "BEACON_CHANNEL_DIR to point at your channel/ directory.",
              file=sys.stderr)
        sys.exit(1)
    bus_path = channel_root / "bus.mjs"

    # Ensure node_modules so the MCP server actually starts on first launch.
    # Best-effort: surface a warning if missing but still write .mcp.json so
    # the user sees the next-steps text and can fix node manually.
    if not _ensure_channel_node_modules(channel_root):
        print(f"Warning: channel/node_modules at {channel_root} is missing or "
              f"`npm install` failed.", file=sys.stderr)
        print("The beacon-bus MCP server will fail to start until this is "
              "resolved.", file=sys.stderr)

    server_entry = _build_channel_server_entry(bus_path)

    mcp_path = cwd / ".mcp.json"
    config: dict
    if mcp_path.exists():
        try:
            with mcp_path.open("r", encoding="utf-8") as f:
                config = json.load(f) or {}
        except (json.JSONDecodeError, OSError) as e:
            print(f"Error: existing .mcp.json is not valid JSON ({e}).", file=sys.stderr)
            print("Resolve the file by hand, or remove it and re-run.", file=sys.stderr)
            sys.exit(1)
    else:
        config = {}

    mcp_servers = config.setdefault("mcpServers", {})
    existed = "beacon-bus" in mcp_servers
    mcp_servers["beacon-bus"] = server_entry

    with mcp_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"{'Updated' if existed else 'Created'} {mcp_path.relative_to(cwd)}")
    print(f"  beacon-bus server → {bus_path}")
    print()
    print("Next steps:")
    # ms-54 e-1167: bclaude wrapper is the recommended launcher because it
    # hides the long `--dangerously-load-development-channels` flag and
    # honors the opt-out gate. The raw flag form and shell alias stay as
    # fallbacks for users who don't have the wrapper on PATH yet.
    print("  1. Start Claude Code via the bundled wrapper (recommended):")
    print("     bclaude")
    print("     (forwards all args to `claude`; refuses the channel flag if "
          "DM opt-out is active)")
    print("  2. Or directly with the long flag (research preview):")
    print("     claude --dangerously-load-development-channels server:beacon-bus")
    print("  3. Or define a shell alias:")
    print("     alias bclaude='claude --dangerously-load-development-channels server:beacon-bus'")
    print()
    print("Channels are research preview. See:")
    print("  https://code.claude.com/docs/en/channels.md")
    print()
    print("To stop using DM later:")
    print("  beacon channel uninstall              # remove MCP entry, keep node_modules")
    print("  beacon channel uninstall --purge-files # also remove channel/node_modules")
    print("  beacon channel opt-out                # block all future auto-installs")
    print("  beacon channel status                 # check current state")


def cmd_channel_uninstall():
    """Remove the beacon-bus MCP entry; optionally also wipe node_modules.

    Modes (selected via env from bin/beacon):
      - default / --keep-files: only remove `beacon-bus` from .mcp.json.
        node_modules/ stays so a later `beacon channel install` is fast
        (this is the "pause" path).
      - --purge-files: remove the MCP entry AND delete channel/node_modules
        so the next install does a fresh `npm install`. Useful when the
        user wants the disk back or suspects a corrupt install.

    The MCP-entry removal mirrors install: if other mcpServers entries
    exist alongside beacon-bus, only beacon-bus is removed and the rest
    stay intact. If beacon-bus is the only entry, the mcpServers object
    is left empty (we don't delete .mcp.json itself — other tools may
    add their own entries later).

    Always prints what was actually removed so the user can audit.
    """
    from pathlib import Path
    cwd = Path.cwd()
    # ms-84 Phase 3 (e-2037): cloud mode has no local project.json after
    # cut-over; cloud.json is an equivalent root marker.
    if not (
        (cwd / ".beacon" / "project.json").exists()
        or (cwd / ".beacon" / "cloud.json").exists()
    ):
        print("Error: no Beacon project in this directory "
              "(looked for .beacon/project.json or .beacon/cloud.json). "
              "Run from a project root.", file=sys.stderr)
        sys.exit(1)

    purge = os.environ.get("BEACON_CHANNEL_PURGE_FILES", "") == "1"
    removed_anything = False

    # ---- Phase 1: strip the MCP entry --------------------------------------
    mcp_path = cwd / ".mcp.json"
    if mcp_path.exists():
        try:
            with mcp_path.open("r", encoding="utf-8") as f:
                config = json.load(f) or {}
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error: .mcp.json is not valid JSON ({e}).", file=sys.stderr)
            print("Resolve the file by hand, or remove it and re-run.",
                  file=sys.stderr)
            sys.exit(1)
        servers = config.get("mcpServers", {}) or {}
        if "beacon-bus" in servers:
            del servers["beacon-bus"]
            config["mcpServers"] = servers
            with mcp_path.open("w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(f"Removed beacon-bus from {mcp_path.relative_to(cwd)}")
            removed_anything = True
        else:
            print(f"(beacon-bus was not present in {mcp_path.relative_to(cwd)})")
    else:
        print("(no .mcp.json found — nothing to remove)")

    # ---- Phase 2: optionally wipe node_modules ----------------------------
    if purge:
        channel_root = _resolve_channel_root()
        if channel_root is None:
            print("Warning: channel/ root not located — skipping node_modules "
                  "purge.", file=sys.stderr)
        else:
            nm = channel_root / "node_modules"
            if nm.exists():
                # Move to .trash/ in the project root rather than rm -rf — this
                # follows the global "no rm" convention and lets the user
                # restore if --purge-files was a mistake.
                import time as _time
                trash_dir = cwd / ".trash"
                trash_dir.mkdir(exist_ok=True)
                stamp = _time.strftime("%Y%m%d-%H%M%S")
                target = trash_dir / f"channel-node_modules-{stamp}"
                try:
                    nm.rename(target)
                    print(f"Moved {nm} → {target.relative_to(cwd)}")
                    print("  (kept in .trash/ rather than deleted — "
                          "`mv` it back if you want it restored)")
                    removed_anything = True
                except OSError as e:
                    print(f"Warning: failed to move {nm}: {e}",
                          file=sys.stderr)
            else:
                print(f"(no node_modules at {nm} — nothing to purge)")

    if not removed_anything:
        print("Nothing to do — beacon-bus was already uninstalled.")

    # ---- Helpful follow-up text --------------------------------------------
    print()
    if purge:
        print("Re-install later with:  beacon channel install")
        print("  (will trigger a fresh `npm install`)")
    else:
        print("Re-install later with:  beacon channel install")
        print("  (fast — node_modules retained)")
    print("To block ALL future installs (incl. auto-install): "
          "beacon channel opt-out")


def cmd_channel_opt_out():
    """Set the persistent opt-out flag at project or global scope.

    Default scope is --project (set via env from bin/beacon). The flag
    blocks every install path: manual `beacon channel install`, the
    e-1238 auto-install in `beacon setup` / Skill flows, and the e-1167
    bclaude wrapper. Idempotent — setting twice is a no-op.

    Env BEACON_NO_BUS=1 is *also* honored (transient, per-shell) but
    needs no command; it is documented in `beacon channel status`.
    """
    scope = os.environ.get("BEACON_CHANNEL_SCOPE", "project")

    if scope == "global":
        cfg = _read_user_beacon_config()
        bus = cfg.get("bus") or {}
        already = bool(bus.get("disabled"))
        bus["disabled"] = True
        cfg["bus"] = bus
        _write_user_beacon_config(cfg)
        p = _user_beacon_config_path()
        if already:
            print(f"Global opt-out already set in {p}")
        else:
            print(f"Global opt-out written to {p}")
            print("  → DM auto-install will be skipped in every project.")
    else:
        # project scope (default). ms-84 Phase 3 (e-2037): accept either
        # marker — cloud mode keeps the bus flag in the cloud document
        # via Store, so a local project.json may not exist.
        from pathlib import Path as _Path
        cwd = _Path.cwd()
        has_marker = (
            (cwd / ".beacon" / "project.json").exists()
            or (cwd / ".beacon" / "cloud.json").exists()
        )
        if not has_marker:
            print("Error: no Beacon project in this directory "
                  "(looked for .beacon/project.json or .beacon/cloud.json). "
                  "Run from a project root, or use --global.",
                  file=sys.stderr)
            sys.exit(1)
        already = _read_project_bus_disabled()
        if not _write_project_bus_flag(True):
            print("Error: failed to persist project bus flag", file=sys.stderr)
            sys.exit(1)
        location = "cloud project document" if (cwd / ".beacon" / "cloud.json").exists() else get_project_file()
        if already:
            print(f"Project opt-out already set in {location}")
        else:
            print(f"Project opt-out written to {location}")
            print("  → DM auto-install will be skipped in this project only.")

    print()
    print("Lift later with:  beacon channel opt-in "
          f"[--{scope}]")


def cmd_channel_opt_in():
    """Clear the opt-out flag at project or global scope (idempotent)."""
    scope = os.environ.get("BEACON_CHANNEL_SCOPE", "project")

    if scope == "global":
        cfg = _read_user_beacon_config()
        bus = cfg.get("bus") or {}
        had = bool(bus.get("disabled"))
        if had:
            bus.pop("disabled", None)
            if bus:
                cfg["bus"] = bus
            else:
                cfg.pop("bus", None)
            _write_user_beacon_config(cfg)
            print(f"Global opt-out cleared from {_user_beacon_config_path()}")
        else:
            print(f"Global opt-out was not set (nothing to clear).")
    else:
        # ms-84 Phase 3 (e-2037): accept either marker — cloud mode
        # stores the flag in the cloud document via Store.
        from pathlib import Path as _Path
        cwd = _Path.cwd()
        has_marker = (
            (cwd / ".beacon" / "project.json").exists()
            or (cwd / ".beacon" / "cloud.json").exists()
        )
        if not has_marker:
            print("Error: no Beacon project in this directory "
                  "(looked for .beacon/project.json or .beacon/cloud.json). "
                  "Run from a project root, or use --global.",
                  file=sys.stderr)
            sys.exit(1)
        had = _read_project_bus_disabled()
        location = "cloud project document" if (cwd / ".beacon" / "cloud.json").exists() else get_project_file()
        if had:
            _write_project_bus_flag(False)
            print(f"Project opt-out cleared from {location}")
        else:
            print(f"Project opt-out was not set (nothing to clear).")

    # Surface remaining opt-out sources so the user understands why DM may
    # still be off even after this opt-in.
    status = _bus_opt_out_status()
    remaining = [k for k in ("env", "project", "global") if status[k]]
    if remaining:
        print()
        print("Note: opt-out is still active via: " + ", ".join(remaining))
        print("  (run `beacon channel status` to see details)")


def cmd_channel_status():
    """Print a 4-block summary of the DM channel lifecycle state.

    Blocks (in this order):
      1. Install state — is `beacon-bus` in .mcp.json?
      2. Files state — does channel/node_modules/ exist?
      3. Opt-out state — env / project / global, with source paths.
      4. Prediction — would the next auto-install run, and why?

    Read-only. Never modifies project.json, .mcp.json, or config.json.
    Designed so a user troubleshooting "why is DM not working?" gets the
    full picture in one screen.
    """
    from pathlib import Path
    cwd = Path.cwd()

    # ---- 1. Install state -------------------------------------------------
    mcp_path = cwd / ".mcp.json"
    mcp_has_entry = False
    if mcp_path.exists():
        try:
            with mcp_path.open("r", encoding="utf-8") as f:
                config = json.load(f) or {}
            mcp_has_entry = "beacon-bus" in (config.get("mcpServers") or {})
        except (OSError, json.JSONDecodeError):
            pass

    # ---- 2. Files state ---------------------------------------------------
    channel_root = _resolve_channel_root()
    nm_exists = False
    nm_path_str = "(channel/ root not located)"
    if channel_root is not None:
        nm = channel_root / "node_modules"
        nm_exists = nm.exists()
        nm_path_str = str(nm)

    # ---- 3. Opt-out state -------------------------------------------------
    status = _bus_opt_out_status()

    # ---- 4. Prediction ----------------------------------------------------
    if status["any"]:
        sources = []
        if status["env"]:
            sources.append("env BEACON_NO_BUS")
        if status["project"]:
            sources.append("project flag")
        if status["global"]:
            sources.append("global flag")
        prediction = "would be SKIPPED (opt-out via " + ", ".join(sources) + ")"
    elif mcp_has_entry:
        prediction = "would be a NO-OP (already installed, MCP entry present)"
    else:
        prediction = "would RUN (no opt-out, no MCP entry present)"

    # ---- Render ------------------------------------------------------------
    print("Beacon DM channel — current state")
    print("=" * 50)
    print()
    print(f"[1] Install state ({mcp_path.relative_to(cwd) if mcp_path.exists() else '.mcp.json'}):")
    if mcp_has_entry:
        print("    ✓ beacon-bus MCP entry present")
    elif mcp_path.exists():
        print("    × beacon-bus MCP entry missing (.mcp.json exists but no entry)")
    else:
        print("    × no .mcp.json (never installed in this project)")
    print()
    print(f"[2] Files state:")
    print(f"    path: {nm_path_str}")
    if nm_exists:
        print("    ✓ channel/node_modules/ present")
    else:
        print("    × channel/node_modules/ missing")
    print()
    print("[3] Opt-out state:")
    if not status["any"]:
        print("    (no opt-out set)")
    else:
        if status["env"]:
            print(f"    ✓ env BEACON_NO_BUS=1 (transient, this shell only)")
        if status["project"]:
            try:
                pf = get_project_file()
            except Exception:
                pf = ".beacon/project.json"
            print(f"    ✓ project flag in {pf}")
        if status["global"]:
            print(f"    ✓ global flag in {_user_beacon_config_path()}")
    print()
    print("[4] Next auto-install:")
    print(f"    → {prediction}")
    print()

    # ---- 5. Receive capability (send/receive asymmetry) -------------------
    # ms-93 recipient-stability follow-up: `bus send` is a CLI push and works
    # without a bridge, so a cwd with no `.mcp.json` (e.g. a hand-`cd`'d git
    # worktree) silently falls into a send-only state — outgoing DMs work,
    # incoming ones never live-wake. Make that asymmetry loud here rather than
    # letting the user infer "connected" from a working send.
    print("[5] Receive capability (受信は送信と非対称):")
    if mcp_has_entry:
        print("    ✓ 受信 bridge 経路あり — この cwd で起動した session は "
              "他セッションからの DM を live-wake で受信できます")
    else:
        print("    ⚠ 送信専用の恐れ — この cwd に beacon-bus MCP entry が無い")
        print("      送信 (`bus send`) は CLI push なので効きますが、他セッション"
              "からの DM は live-wake せず、次回 prompt の catch-up でのみ届きます。")
        print("      (git worktree に手で cd した等で起動 cwd と別 .beacon "
              "session になっている場合に起きがち)")
    print()

    # Action hints based on current state.
    if status["any"]:
        print("To re-enable DM: beacon channel opt-in [--project|--global] "
              "(or unset BEACON_NO_BUS)")
    elif not mcp_has_entry:
        print("To install: beacon channel install")
    else:
        print("To uninstall: beacon channel uninstall [--purge-files]")
        print("To block future auto-installs: beacon channel opt-out")


# ---------------------------------------------------------------------------
# Member management (e-624)
# ---------------------------------------------------------------------------
#
# All write paths go through lib/operations.apply_operation so the ms-39
# lost-update protection covers concurrent member edits (two maintainers
# inviting different people at the same moment, etc.). Reads are direct
# load_project, which is consistent with task_list / milestone_list and
# does not need atomicity.


# Trek CLI (ms-69) moved to lib/cmd_trek.py (ms-127 e-4820) — handlers are
# re-imported for dispatch at the top of this file; family-private helpers and
# trek-only constants (TREK_AUTO_ARM_CHANNELS etc.) live canonically there.


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def cmd_summary():
    # ms-57 / e-1040 deprecation completed: `beacon summary "text"` writes
    # are now a no-op. Reads still work for legacy CLI surfaces that
    # haven't migrated to project-vision / session_logs yet — they get
    # whatever was last written before the soft-deprecation ended.
    # Cross-session hand-off → `beacon session log`. Human narrative →
    # project-vision CORE doc.
    text = os.environ.get("BEACON_SUMMARY_TEXT", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    if text:
        # No write. Surface the deprecation so the caller knows their
        # input was ignored, but keep JSON callers quiet (mirrors the
        # pattern in cmd_log_finalize).
        if not json_mode and not os.environ.get("BEACON_SUPPRESS_DEPRECATION"):
            sys.stderr.write(
                "[deprecated] `beacon summary <text>` is no longer a write "
                "(ms-57 / e-1040 completed). Cross-session hand-off → "
                "`beacon session log`; human narrative → `project-vision` "
                "CORE doc. Your input was IGNORED. This command will be "
                "removed in a future release.\n"
            )
        if json_mode:
            print(json.dumps(
                {"summary": data.get("summary", ""),
                 "write_ignored": True,
                 "deprecated_since": "e-1040"},
                ensure_ascii=False,
            ))
        else:
            print("Summary write ignored (deprecated; see stderr).")
    elif json_mode:
        print(json.dumps({"summary": data.get("summary", "")}, ensure_ascii=False))
    else:
        print(data.get("summary", "(未設定)"))


# ---------------------------------------------------------------------------
# Save (ms-16)
# ---------------------------------------------------------------------------

def cmd_save():
    description = os.environ.get("BEACON_DESCRIPTION", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    source = os.environ.get("BEACON_SOURCE", "")
    url = os.environ.get("BEACON_URL", "")
    revision_id = os.environ.get("BEACON_REVISION_ID", "")
    hash_val = os.environ.get("BEACON_HASH", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    date = os.environ.get("BEACON_DATE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not source:
        print("Error: --source is required", file=sys.stderr)
        sys.exit(1)
    if not description:
        print("Error: description is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    # ms-143: record the side-effect changelog through the profession-generic
    # occupation.record_target_entry (NOT the dev-concrete core.save_entry), so
    # the save verb no longer symbol-reaches a PROFESSION_CONCRETE_SYMBOL. Bad-id /
    # multi-active still RAISE identically (record_target_entry propagates
    # find_target_milestone's errors). Only the empty-ms_id-no-active case differs:
    # record_target_entry no-ops (recorded=False) where save_entry raised — so the
    # frontend re-raises to PRESERVE the observable "No active milestone" error
    # (parity-first, leader 握り; abstraction=occupation, no-milestone UX=frontend).
    outcome = occupation.record_target_entry(
        data, ms_id, description=description, source=source, date=date, url=url,
        revision_id=revision_id, hash=hash_val, progress=progress)
    if not outcome.get("recorded"):
        raise ValueError("No active milestone. Run: beacon milestone start <ms-id>")
    result = outcome["result"]
    save_project(data)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["status"] == "duplicate":
            print(f"Duplicate save skipped (source={source}, ms={result['milestone']})")
        else:
            print(f"Saved [{result['entry_id']}] to {result['milestone']}: {description}")


# ---------------------------------------------------------------------------
# Milestone depends / workspace / graph (ms-17)
# ---------------------------------------------------------------------------









# ---------------------------------------------------------------------------
# Retro
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Triggers — moved to lib/cmd_trigger.py (ms-127 e-4971); re-imported at top.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Review-gate helpers + review done/skip commands. These live here (NOT in
# cmd_trigger) — they are review-flow, not the trigger auto-fire subsystem.
# Restored after ms-127 e-4971 extraction over-removed them (they were
# interleaved near the trigger block). They may call trigger fns, which
# resolve via commands.py's cmd_trigger re-import at top.
# ---------------------------------------------------------------------------

def _review_skips_log_path() -> str:
    """Durable append-only audit of human-intended review skips (ms-119 e-4124).

    Lives in .beacon/ (not the ephemeral triggers/ dir) so a waiver survives the
    trigger that it cleared — the skip decision is a permanent governance record,
    the trigger is a transient 'still owed' marker."""
    return os.path.join(os.path.dirname(get_project_file()), "review-skips.jsonl")


def _review_skip_gate_record() -> dict:
    """Capture HOW a review skip passed the human-intent guard (ms-119 e-4124).

    Mirrors _approval_gate_record (e-4006): the skip is meant to be a human's
    deliberate waiver, but the env signal is a self-report — so record which
    signal opened it (explicit user override vs human session) rather than
    trusting it. An AI-recorded skip then leaves a grep-able footprint (an
    ``ai-session-unguarded`` signal) instead of looking identical to a human's.
    This is a DIFFERENT env var from the AI-ban / merge-override flags on
    purpose: skipping a review is a distinct act from forcing a merge past one
    that is still owed (BEACON_PR_REVIEW_OVERRIDE)."""
    override = os.environ.get("BEACON_REVIEW_SKIP_USER_OVERRIDE", "") == "1"
    if override:
        signal = "user-override"
    elif _session_kind_is_human():
        signal = "human-session"
    else:
        signal = "ai-session-unguarded"
    return {
        "signal": signal,
        "session_kind": (os.environ.get("BEACON_SESSION_KIND", "") or "").strip() or "unset",
    }


def _fire_ax_review_due_trigger(pr_number: str, pr_title: str, pr_url: str) -> None:
    _fire_review_due_for_pr("ax", "AX (AI-Experience interface drift)",
                            pr_number, pr_title, pr_url)


def _clear_ax_review_due_trigger(pr_number: str) -> None:
    _clear_review_due_for_pr("ax", pr_number)


# ---------------------------------------------------------------------------
# Review-as-merge-gate (ms-119 e-4060).
#
# Before this, a PR-open fired a review-due trigger into a file, but NOTHING
# forced it to be consumed: unlike commit/push/deploy (each woken by the
# post-commit hook's "MUST run" nudge), the review trigger only re-surfaced on a
# voluntary `beacon trigger check`, buried among spec-needed noise — so AX /
# maintainability reviews fired into a void and were skipped. Two structural
# closes, mirroring the loops that already work:
#   * WAKE — the post-commit hook now emits a MUST-run on PR-open (bin change).
#   * GATE — the review-due trigger is repurposed as the "review still owed"
#     signal: `beacon review done` clears it (called by /beacon-review-run when
#     a judge produces its verdict), and beacon pr approve/merge REFUSE while any
#     remain. The trigger already survives approve (only close/merge cleaned it),
#     so it is a faithful "outstanding" marker. Override is possible but leaves
#     an audit line (e-4006 「隠せなくする」 pattern), never a silent bypass.
# ---------------------------------------------------------------------------


def _target_review_due_has_binding(target_id: str, review_type: str) -> bool:
    """True if the target's review-due trigger currently owes ``review_type``
    (ms-119 e-4124). Read-only peek (no mutation)."""
    try:
        with open(os.path.join(_get_triggers_dir(),
                               f"review-due-{target_id}.json"), encoding="utf-8") as f:
            return review_type in (json.load(f).get("bindings") or [])
    except (OSError, ValueError):
        return False


def _remove_binding_from_target_review_due(target_id: str, review_type: str) -> bool:
    """Remove ONE review type's binding from a target's review-due trigger,
    preserving the others (ms-119 e-4124).

    The target review-due trigger is a single file whose ``bindings`` list holds
    every review that fired at the completion 節目. A per-type skip must drop only
    ``review_type``: rewrite the file with the reduced bindings, and delete it
    only when the skipped type was the sole binding. Returns True when the type
    was present (an obligation was actually waived). Best-effort: IO errors leave
    the trigger as-is and return False (the gate stays honest — a failed clear
    does not look like a success)."""
    path = os.path.join(_get_triggers_dir(), f"review-due-{target_id}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    bindings = data.get("bindings") or []
    if review_type not in bindings:
        return False
    remaining = [b for b in bindings if b != review_type]
    try:
        if remaining:
            data["bindings"] = remaining
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.write("\n")
        else:
            os.remove(path)
    except OSError:
        return False
    return True


def _ci_flip_review_gate_success(pr_number: str) -> None:
    """Best-effort: set the `beacon-review-gate` commit status to success for the
    PR head (ms-119 e-4073). No-op unless BEACON_REVIEW_GATE_CI=1 (default OFF —
    the CI gate is opt-in scaffolding). Never raises."""
    if os.environ.get("BEACON_REVIEW_GATE_CI", "") != "1":
        return
    try:
        sha = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "headRefOid",
             "--jq", ".headRefOid"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if not sha:
            return
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "review-gate-ci.py")
        subprocess.run(["python3", script, "set", "--state", "success",
                        "--sha", sha, "--pr", pr_number],
                       capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return


def cmd_review_skip():
    """Record a HUMAN's deliberate decision to skip an owed review (ms-119 e-4124).

    beacon review skip --type <ax|maintainability|philosophy|...> \\
        (--pr <N> | --target <ms-XX|op-X>) --reason "<なぜ省くか>"

    First-class, reason-bearing, audited — semantically distinct from
    BEACON_PR_REVIEW_OVERRIDE (force a merge PAST a review that is still owed).
    A skip *waives* the obligation with an owner and a reason; it clears the
    review-due trigger (so approve/merge proceeds) AND appends a durable record
    to .beacon/review-skips.jsonl. --reason is mandatory: a waiver with no
    recorded reason is exactly the silent skip this MS exists to prevent."""
    review_type = os.environ.get("BEACON_REVIEW_TYPE", "").strip()
    pr_number = os.environ.get("BEACON_PR_NUMBER", "").strip()
    target_id = os.environ.get("BEACON_TARGET_ID", "").strip()
    reason = (os.environ.get("BEACON_REASON", "") or "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not review_type or not (pr_number or target_id):
        print("Usage: beacon review skip --type <ax|maintainability|philosophy|...> "
              "(--pr <N> | --target <ms-XX|op-X>) --reason \"<なぜ省くか>\"",
              file=sys.stderr)
        sys.exit(1)
    if pr_number and target_id:
        print("Error: --pr と --target は同時に指定できません (どちらか一方)。",
              file=sys.stderr)
        sys.exit(1)
    if not reason:
        print("Error: review skip には --reason が必須です。人間が『なぜこのレビューを"
              "省くか』を残さない waiver は、この仕組みが防ごうとしている silent skip "
              "そのものです。", file=sys.stderr)
        sys.exit(1)

    import review_spine
    import datetime
    # e-4124 AX/maint review: reject an unknown --type. Without this a typo'd type
    # (e.g. "maintainabilty") matches no review-due, clears nothing, yet prints
    # success — a silent no-op in the very command meant to make skipping visible.
    valid_types = (set(review_spine.load_review_types().keys())
                   | {review_spine.REVIEW_ATTAINMENT})
    if review_type not in valid_types:
        print(f"Error: 未知の review type: {review_type!r} "
              f"(有効: {', '.join(sorted(valid_types))})。typo の skip は義務を消さない"
              f"のに成功に見える silent no-op になるため拒否します。", file=sys.stderr)
        sys.exit(1)

    # Peek BEFORE mutating: does a matching owed review actually exist? A skip of a
    # non-existent obligation (wrong PR / target id) must not look successful.
    if pr_number:
        owed = os.path.isfile(os.path.join(
            _get_triggers_dir(), f"{review_type}-review-due-{pr_number}.json"))
    else:
        owed = _target_review_due_has_binding(target_id, review_type)

    gate = _review_skip_gate_record()
    ref_kind = "pr" if pr_number else "target"
    ref = pr_number or target_id
    record = review_spine.build_review_skip_record(
        review_type=review_type, ref_kind=ref_kind, ref=ref, reason=reason,
        actor=_actor_str(), gate=gate, at=datetime.datetime.now().isoformat())
    record["waived_obligation"] = owed  # honest audit: was anything actually owed?

    # Durable audit first (the record must survive even if trigger clearing
    # races); then clear ONLY this type's review-due obligation.
    try:
        with open(_review_skips_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"Error: skip 監査ログを書けませんでした: {e}", file=sys.stderr)
        sys.exit(1)
    if pr_number:
        # One file per (type, PR); removing it waives exactly this type.
        _clear_review_due_for_pr(review_type, pr_number)
    else:
        # The target review-due trigger is ONE file carrying ALL bindings that
        # fired at the completion 節目 (e.g. philosophy + attainment). A per-type
        # skip must remove only this type's binding — deleting the whole file
        # would silently waive the others too (e-4124 AX/maint review finding).
        _remove_binding_from_target_review_due(target_id, review_type)

    where = f"PR #{pr_number}" if pr_number else target_id
    if json_mode:
        print(json.dumps(record, ensure_ascii=False))
    else:
        print(f"レビュー skip を記録 (人間の意図的省略): {review_type} / {where}")
        print(f"  理由: {reason}")
        print(f"  監査: .beacon/review-skips.jsonl (signal={gate['signal']}, "
              f"actor={record['actor'] or 'unknown'})")
        if gate["signal"] == "ai-session-unguarded":
            print("  ⚠ このセッションは human と申告していません "
                  "(BEACON_SESSION_KIND=human も BEACON_REVIEW_SKIP_USER_OVERRIDE=1 も無し)。"
                  "skip は記録されましたが、監査に ai-session-unguarded として残ります。",
                  file=sys.stderr)
    if not owed:
        # Recorded, but nothing matched — say so (both output modes) instead of
        # implying an obligation was cleared. Non-zero exit so an automation loop
        # can tell the skip hit no target.
        print(f"⚠ 一致する review-due が見つかりませんでした ({review_type} / {where})。"
              f"skip は監査に記録しましたが、解消した義務はありません "
              f"(型 / PR番号 / target-id の typo を確認してください)。", file=sys.stderr)
        sys.exit(3)


def _record_review_adjudication_decision(review_type: str, pr_number: str,
                                         summary: str, adjudications: list) -> None:
    """ms-166 e-5971 — weld the finding-level review adjudication onto the decision
    arm at the SAME choke point that clears the review gate (``beacon review done``),
    so recording a review structurally captures WHAT was decided about its findings —
    not just THAT it ran. No AI-volition ``beacon decision record`` in between: the
    call that unblocks the merge is the call that records the adjudication.

    ``summary`` = the overall 採否 summary (what); ``adjudications`` = list of
    ``{finding, disposition, rationale}`` (why), disposition ∈ the single-source
    vocab ``commands_shared.ADJUDICATION_DISPOSITIONS``. Unknown dispositions are
    WARNED, not silently mis-counted (an AX review found ``deferred`` / typos vanished
    from the synthesized count). Nothing is recorded when both are empty. ``decided_by``
    follows the session kind via the single source ``commands_shared.decided_by_for_review``.
    cloud-only; best-effort but LOGGED on failure (with a re-run recovery hint) so it
    never breaks ``review done``."""
    if not summary and not adjudications:
        return
    from commands_shared import (best_effort_decision_write, _is_cloud_mode,
                                 _get_api_client, ADJUDICATION_DISPOSITIONS,
                                 decided_by_for_review)
    with best_effort_decision_write(
            f"review-adjudication for PR #{pr_number} ({review_type})",
            recovery_hint="re-run `beacon review done` with the same flags — the gate "
                          "clear is idempotent and only the adjudication is re-recorded"):
        if not _is_cloud_mode():
            return
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if not project_id:
            return
        # 集計と rationale を 1 パスで。未知 disposition は warn (silent 誤集計を防ぐ)。
        counts = {d: 0 for d in ADJUDICATION_DISPOSITIONS}
        parts = []
        for a in (adjudications or []):
            if not isinstance(a, dict):
                continue
            disp = (a.get("disposition") or "").strip()
            finding = (a.get("finding") or "").strip()
            why = (a.get("rationale") or "").strip()
            if disp in counts:
                counts[disp] += 1
            elif disp:
                print(f"  ⚠ 未知の disposition '{disp}' "
                      f"(既知: {', '.join(ADJUDICATION_DISPOSITIONS)}) "
                      "— rationale には残りますが件数集計から外れます", file=sys.stderr)
            seg = f"{disp}[{finding}]" if finding else disp
            if why:
                seg = f"{seg}: {why}"
            if seg:
                parts.append(seg)
        rationale = " / ".join(parts) or None
        # what (decision) = 明示 summary、無ければ採否件数から合成 (全 disposition を含む)。
        if summary:
            decision = summary
        else:
            decision = f"{review_type}: " + " / ".join(
                f"{counts[d]} {d}" for d in ADJUDICATION_DISPOSITIONS)
        client.record_decision(project_id, {
            "kind": "review-adjudication",
            "decision": decision,
            "rationale": rationale,
            "decided_by": decided_by_for_review(),
            # PR / review-type linkage lives in evidence as real link refs — the
            # server's related schema (_RELATED_KEYS) only keeps task/target/trek/
            # event ids, so `related.pr_number` was silently dropped (found by
            # dogfooding this very seam; a review-adjudication is about a PR, not a
            # task/target). "pr:<n>" makes "which採否 for PR N" queryable via evidence.
            "evidence": [f"review:{review_type}", f"pr:{pr_number}"],
        })


def _adjudications_from_env() -> list:
    """Parse ``BEACON_REVIEW_ADJUDICATIONS`` into a list of adjudication dicts — ms-166 e-5971.

    Best-effort but never SILENT (AX review finding): a malformed value warns and yields
    ``[]``; a single ``{...}`` object is auto-promoted to ``[{...}]`` (the natural shape
    when adjudicating ONE finding — a valid JSON that must not vanish); any other non-list
    (string / number / null) warns and yields ``[]``. In every drop case the gate still
    clears and the skip is announced, so a well-formed-but-wrong-shape input never
    silently loses the adjudication."""
    raw = os.environ.get("BEACON_REVIEW_ADJUDICATIONS", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        print("  ⚠ --adjudications の JSON 解析に失敗しました "
              "(review gate は解消しますが adjudication decision は記録しません)",
              file=sys.stderr)
        return []
    if isinstance(parsed, dict):
        return [parsed]                      # 単一 finding の自然形を昇格 (AX-1)
    if isinstance(parsed, list):
        return parsed
    print(f"  ⚠ --adjudications は list または object である必要があります "
          f"(受け取り: {type(parsed).__name__}) "
          "— review gate は解消しますが adjudication decision は記録しません", file=sys.stderr)
    return []


def cmd_review_done():
    """Mark an independent review as RUN for a PR — clears its review-due trigger
    so beacon pr approve/merge no longer blocks on it (ms-119 e-4060).

    beacon review done --type <ax|maintainability|...> --pr <N>
        [--adjudication-summary <text>]
        [--adjudications '[{"finding":..,"disposition":accepted|declined|deferred,"rationale":..}]']

    Called by /beacon-review-run after a judge produces its verdict, so running
    the review is what unblocks the PR (the loop closes on the review, not on the
    approve). Idempotent: clearing an absent trigger is a no-op.

    ms-166 e-5971: ``--adjudication-summary`` / ``--adjudications`` weld the
    finding-level 採否 (what was accepted/declined and why) onto the decision arm here,
    at the same choke point that clears the gate — so the adjudication is captured
    structurally, not by a separate AI-volition ``beacon decision record`` that is easy
    to skip. (``--adjudication-summary`` is named apart from the completion / PR-approve
    "verdict" concepts — it is the 採否 summary, not an approval verdict.)"""
    review_type = os.environ.get("BEACON_REVIEW_TYPE", "").strip()
    pr_number = os.environ.get("BEACON_PR_NUMBER", "").strip()
    if not review_type or not pr_number:
        print("Usage: beacon review done --type <ax|maintainability|...> --pr <N> "
              "[--adjudication-summary <text>] "
              "[--adjudications '[{\"finding\":..,\"disposition\":accepted|declined|deferred,"
              "\"rationale\":..}]']", file=sys.stderr)
        sys.exit(1)
    _clear_review_due_for_pr(review_type, pr_number)
    _adj_summary = os.environ.get("BEACON_REVIEW_ADJUDICATION_SUMMARY", "").strip()
    _adjudications = _adjudications_from_env()
    _record_review_adjudication_decision(
        review_type, pr_number, _adj_summary, _adjudications)
    if not _adj_summary and not _adjudications:
        # ms-166 e-5971 (AX review): the weld is opt-in and the command cannot know
        # how many findings the judge produced — so make the OMISSION visible rather
        # than let the採否 judgment-trail vanish silently.
        print("  ℹ 採否 (adjudication) は未記録です。findings があったなら "
              "--adjudication-summary / --adjudications 付きで再実行すると判断軌跡が "
              "decision arm に残ります。", file=sys.stderr)
    remaining = _pending_review_types_for_pr(pr_number)
    print(f"レビュー実施を記録: {review_type} / PR #{pr_number} (review-due 解消)")
    if remaining:
        print(f"  残りの未実施レビュー: {', '.join(remaining)}")
    else:
        # All reviews for this PR have run — stamp a done-marker so the tick
        # anchor does NOT re-fire review-due for this (now review-less) PR.
        try:
            os.makedirs(_get_triggers_dir(), exist_ok=True)
            with open(_pr_open_reviewed_marker_path(pr_number), "w",
                      encoding="utf-8") as f:
                f.write(review_type)
        except OSError:
            pass
        print(f"  PR #{pr_number} の独立レビューは全て実施済み "
              f"— approve/merge の gate を通過できます。")
        # ms-119 e-4073 (scaffold, opt-in): flip the CI gate status to success so
        # a branch-protection required check unblocks the merge button on ALL
        # routes (gh/UI/beacon), not just `beacon pr merge`. Guarded by
        # BEACON_REVIEW_GATE_CI=1 + a resolvable head SHA so default behavior and
        # tests are unchanged; best-effort (a failed gh post never breaks `done`).
        _ci_flip_review_gate_success(pr_number)



# ---------------------------------------------------------------------------
# Document commands
# ---------------------------------------------------------------------------


def _ensure_cloud_config():
    config_path = _get_cloud_config_path()
    existing: dict = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
        # Fast path: fully materialized cloud.json (= post-first-push state).
        if existing.get("project_id"):
            return existing
        # Partial cloud.json (e.g. init wrote {"profile": <name>} only) falls
        # through to the materialization step below, preserving existing fields.

    data = load_project()
    name = data.get("name", "project")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    import hashlib
    h = hashlib.md5(os.path.abspath(get_project_file()).encode()).hexdigest()[:6]
    project_id = f"{slug}-{h}"

    # ms-64 e-1633: if profile wasn't pinned at init time (= first push without
    # a prior `beacon init` having materialized a profile field), prompt once
    # when the user has multi-account auth state. Skip silently if the choice
    # was already made (existing.profile), explicitly set (env/--profile), or
    # the env can't take a prompt (non-TTY).
    profile_name = existing.get("profile")
    if not profile_name:
        chosen = _maybe_prompt_initial_profile()
        if chosen:
            profile_name = chosen
            # Persist immediately so _resolve_active_api_url below sees it
            # via the cwd cloud.json.profile precedence rule (lib/profile._resolve_api_url).
            _persist_initial_profile_choice(chosen)
        else:
            try:
                import profile as _profile  # type: ignore[import-not-found]
                profile_name = _profile.resolve_active_profile().name
            except Exception:
                profile_name = "default"

    api_url = _resolve_active_api_url()
    config = {
        "project_id": project_id,
        "api_url": api_url,
        "profile": profile_name,
    }
    # Preserve any unknown fields a partial cloud.json may have already carried.
    for k, v in existing.items():
        config.setdefault(k, v)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Created {config_path} (project_id: {project_id}, profile: {profile_name})")
    return config




def cmd_cloud_list():
    """List cloud projects via API."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    # cloud list may be called before cloud.json exists; the profile resolver
    # already honors the env > cwd cloud.json > profile.json > default chain,
    # so we just use it directly here (e-1458).
    api_url = _resolve_active_api_url()

    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))

    try:
        projects = client.list_projects()
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if json_mode:
        print(json.dumps(projects, ensure_ascii=False))
    else:
        if not projects:
            print("No cloud projects found.")
            print("Run 'beacon cloud upload-initial' to upload a project.")
            return
        for i, p in enumerate(projects, 1):
            # ms-95 / e-2411 — render owner email so cross-project work can
            # see "who do I ask about this?" at a glance. Silently skipped
            # when server didn't resolve one (= migration-era projects).
            owner_email = (p.get("owner_email") or "").strip()
            owner_suffix = f"  (owner: {owner_email})" if owner_email else ""
            print(f"  {i}. {p['project_id']}: {p['name']}{owner_suffix}")
            if p.get('objective'):
                print(f"     {p['objective'][:60]}")


def cmd_cloud_push():
    force = os.environ.get("BEACON_FORCE", "") == "1"

    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    # Capture the cloud/local state BEFORE _ensure_cloud_config() materializes
    # cloud.json. e-1861 (ms-61) switched _is_cloud_mode() to key on cloud.json
    # existence; since _ensure_cloud_config() writes cloud.json, checking
    # _is_cloud_mode() *after* it always returns True and made the first-run
    # migration abort with "already in cloud mode" (and silently skipped the
    # initial docs/retros push below). first_run distinguishes the genuine
    # local→cloud migration from a re-run after cut-over.
    first_run = not _is_cloud_mode()
    config = _ensure_cloud_config()
    project_id = config["project_id"]
    api_url = _resolve_active_api_url()

    # A re-run in cloud mode would overwrite cloud state with a stale local
    # project.json (data loss). The first-run migration is allowed through.
    if not first_run:
        if not force:
            print("Error: already in cloud mode.")
            print("")
            print("  In cloud mode, all CLI changes go directly to the cloud.")
            print("  The local project.json (if any) is a stale recovery copy.")
            print("")
            print("  upload-initial is a one-shot local→cloud migration only;")
            print("  re-running it after cut-over would overwrite cloud state.")
            print("  Use --force to override (cloud → local round-trip was")
            print("  retired in ms-84 Phase 4).")
            sys.exit(1)
        print("Warning: --force specified. Overwriting cloud project data with local file.")
        print("  documents and retros will NOT be pushed (they are managed in cloud).")

    from store_local import LocalStore
    local = LocalStore(get_project_file())
    data = local.load_project()
    core.validate_project(data)

    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))

    # Preserve cloud-only fields (deployments, releases) that are written directly
    # to Firestore and never synced back to local project.json.
    try:
        remote = client.get_project(project_id)
        for field in ("deployments", "releases"):
            if remote.get(field):
                data.setdefault(field, remote[field])
    except RuntimeError:
        pass  # new project or unreachable — proceed with local data only

    try:
        client.put_project(project_id, data)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    if force:
        _append_changelog({"op": "cloud_push_force", "project_id": project_id, "warning": "stale_local"})
    print(f"Pushed to cloud: projects/{project_id}")

    # Push local documents and retros only on the initial push (local → cloud).
    # In cloud mode they are managed via the API; pushing local files would
    # silently overwrite any edits made through the Web UI or CLI. Uses
    # first_run (captured before cloud.json was materialized) — _is_cloud_mode()
    # here is now always True, which previously skipped this block entirely.
    if first_run:
        docs_dir = os.path.join(os.path.dirname(get_project_file()) or ".beacon", "documents")
        if os.path.isdir(docs_dir):
            import glob
            md_files = glob.glob(os.path.join(docs_dir, "*.md"))
            for fpath in md_files:
                doc_info = _read_local_doc(fpath)
                try:
                    client.put_document(
                        project_id, doc_info["doc_id"],
                        doc_info["title"], doc_info["content"],
                        doc_info.get("scope"),
                    )
                    print(f"  doc: {doc_info['doc_id']} ({doc_info.get('scope', 'memo')})")
                except RuntimeError as e:
                    print(f"  doc error [{doc_info['doc_id']}]: {e}")

        retros_dir = os.path.join(os.path.dirname(get_project_file()) or ".beacon", "retro")
        if os.path.isdir(retros_dir):
            import glob
            retro_files = glob.glob(os.path.join(retros_dir, "*.md"))
            for fpath in retro_files:
                week = os.path.basename(fpath)[:-3]  # e.g. "2026-W19"
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                try:
                    client.save_retro(project_id, week, content)
                    print(f"  retro: {week}")
                except RuntimeError as e:
                    print(f"  retro error [{week}]: {e}")

    # e-1861 (ms-61): cloud.json existence already marks us as cloud-mode
    # (written above during the push setup), so the legacy config.json
    # ``{"mode": "cloud"}`` write was retired. Single source of truth = cloud.json.

    # ms-84 Phase 3 (e-2037): after the upload succeeds, rename the local
    # project.json to ``.before-cloud-YYYYMMDD`` so the cut-over to a
    # cloud-only truth model is final. The local file becomes the silent
    # drift source from this point onward (any later cloud write does not
    # propagate to it), so leaving it in place would re-introduce the
    # exact failure mode ms-84 is closing. Helper is idempotent + never
    # deletes — it keeps a one-shot recovery copy on disk.
    pf = get_project_file()
    renamed = _rename_local_project_json_for_cloud_cutover(pf)
    if renamed:
        print(f"  local cache: {pf} → {renamed}")
    print("Switched to cloud mode.")


# ms-84 Phase 4 (e-2038): cmd_cloud_pull was removed structurally. The
# cloud → local round-trip is now impossible (= no local cache to refresh
# into), so the function and its dispatcher entry are gone. Any operator
# script that called it should be deleted — `beacon status` reads cloud
# directly and replaces every legitimate use of pull.


def cmd_cloud_status():
    config_path = _get_cloud_config_path()
    if not os.path.exists(config_path):
        print("Cloud: not configured")
        print("Run 'beacon cloud upload-initial' to bootstrap a new cloud project.")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    from auth import load_credentials
    creds = load_credentials()
    logged_in = creds is not None

    print(f"Cloud: {config['project_id']}")
    print(f"API: {_resolve_active_api_url()}")
    print(f"Auth: {'logged in' if logged_in else 'not logged in'}")


def _local_vs_cloud_pre_flight(local: dict, remote: dict) -> list[str]:
    """Compare local project.json against the cloud-side project snapshot.

    Returns a list of human-readable issues. Empty list = pre-flight pass
    (= cloud has at least everything local has, safe to retire local).

    ms-95 / e-2339 (= 旧 local モード由来 orphan project.json の migration):
    cloud モード以降の writes は cloud のみに行くので、 local は通常 stale
    snapshot にしかならない。 risk は逆向き — local に cloud が知らない
    entry が残っていると、 retire (= rename to .trash/-ish) でその痕跡が
    cold storage に押しやられる。 pre-flight でそれを 1 回 catch する。

    Compared dimensions:

      * project name / objective text mismatch (= 別 project を間違って
        join した可能性)
      * milestones[].id: local ⊆ cloud (完全な MS 欠落のみ fail)
      * milestones[].entries[].id: local の各 entry id が cloud の
        **project 全体 (= 任意 MS) の entry id 集合** に含まれること
      * operations[].id: local ⊆ cloud
      * operations[].entries[].id: local の各 entry id が cloud の
        **project 全体 (= 任意 operation) の entry id 集合** に含まれること

    ms-95 / e-2405 (= MS 移動 entries の false-positive 解消):
    以前は entry の MS 所属まで一致を要求していたので、 dogfood で wave 1/2
    に整理した MS 間移動 (= e-1454 / e-1668 / e-2007 等 10 件) が
    「local の ms-A に居る entry が cloud の ms-A に無い」 = missing と
    false-positive 判定され、 user に余計な force-after-review 確認を
    強要していた。 cloud には entry 自体は別 MS に存在しており data loss
    リスクはゼロ。 そこで entry の所在判定は project 全体の id 集合に
    平坦化し、 「entry id がどこかに存在すれば OK」 に緩めた。 完全な MS
    自体の欠落は依然 fail として残す (= MS 構造そのものが消えるのは別問題)。
    """
    issues: list[str] = []

    local_name = (local.get("name") or "").strip()
    remote_name = (remote.get("name") or "").strip()
    if local_name and remote_name and local_name != remote_name:
        issues.append(
            f"project name differs: local={local_name!r} vs cloud={remote_name!r}"
        )

    def _id_set(items, key="id"):
        out = set()
        for it in items or []:
            v = it.get(key)
            if isinstance(v, str) and v:
                out.add(v)
        return out

    def _flat_entry_ids(containers):
        """Flatten entry ids across every milestone or operation."""
        flat: set[str] = set()
        for c in containers or []:
            for e in (c.get("entries") or []):
                v = e.get("id") if isinstance(e, dict) else None
                if isinstance(v, str) and v:
                    flat.add(v)
        return flat

    local_ms = _id_set(local.get("milestones", []))
    cloud_ms = _id_set(remote.get("milestones", []))
    missing_ms = sorted(local_ms - cloud_ms)
    if missing_ms:
        issues.append(
            f"milestones present locally but missing in cloud: {missing_ms}"
        )

    # ms-95 / e-2405: entries lookup is project-wide so that MS-moved entries
    # are not false-positively reported as missing (= they still exist in
    # cloud, just under a different milestone).
    cloud_ms_entry_ids = _flat_entry_ids(remote.get("milestones", []))
    for m in local.get("milestones", []) or []:
        mid = m.get("id")
        local_entries = _id_set(m.get("entries", []))
        missing = sorted(local_entries - cloud_ms_entry_ids)
        if missing:
            issues.append(
                f"milestone {mid}: entries present locally but missing in cloud (anywhere): {missing}"
            )

    local_ops = _id_set(local.get("operations", []))
    cloud_ops = _id_set(remote.get("operations", []))
    missing_ops = sorted(local_ops - cloud_ops)
    if missing_ops:
        issues.append(
            f"operations present locally but missing in cloud: {missing_ops}"
        )

    cloud_op_entry_ids = _flat_entry_ids(remote.get("operations", []))
    for o in local.get("operations", []) or []:
        oid = o.get("id")
        local_entries = _id_set(o.get("entries", []))
        missing = sorted(local_entries - cloud_op_entry_ids)
        if missing:
            issues.append(
                f"operation {oid}: entries present locally but missing in cloud (anywhere): {missing}"
            )

    return issues


def cmd_cloud_migrate_from_local():
    """Retire an orphan local project.json that survived the cloud cut-over.

    ms-95 / e-2339 (= cloud モード移行済プロジェクトに残った旧 local project.json
    を .beacon/.trash/-ish に退避する): ms-84 Phase 3 (e-2037) で
    ``cloud upload-initial`` 経路に追加された ``_rename_local_project_json_
    for_cloud_cutover`` は新規 cut-over 経路にしか効かず、 それ以前に cloud
    モードに移行した historical project は ``.beacon/project.json`` (=
    cloud 側と divergent な stale snapshot) を持ち続けていた。 本 CLI は
    その orphan を pre-flight check 経由で安全に退避する。

    Flow:

      1. ``.beacon/cloud.json`` の存在 (= cloud モード判定) を確認
      2. ``.beacon/project.json`` の存在 (= orphan condition) を確認
      3. ``BEACON_CONFIRM`` (= ``--confirm <project_id>`` 経路) と cloud.json
         の project_id を照合 (= silent invocation 防御、 ``cloud off`` と
         同じ二段確認パターン、 e-1776 incident 由来)
      4. local project.json + cloud project を比較する pre-flight check
         (``_local_vs_cloud_pre_flight``)。 何かが local-only なら abort
         (= ``--force-after-review`` で override 可)
      5. ``_rename_local_project_json_for_cloud_cutover`` を呼んで rename
         (= 既存ヘルパー再利用、 ``.before-cloud-YYYYMMDD`` suffix 付き、
         再実行 idempotent + 既存 backup を上書きしない)

    Override (``BEACON_FORCE`` = ``--force-after-review``): pre-flight で
    local-only entry を catch したあと、 user がそれを確認したうえで本 fix
    が必要な場合の脱出口。 例えば「local-only entry は意図的な未 sync 残骸
    (= 削除予定)」 のケース。 fail-close 設計だが、 user 監査後の override
    は許す。
    """
    expected_pid = os.environ.get("BEACON_CONFIRM", "").strip()
    force_after_review = os.environ.get("BEACON_FORCE", "") == "1"

    config_path = _get_cloud_config_path()
    if not os.path.exists(config_path):
        print("Not in cloud mode (no .beacon/cloud.json found).")
        print("This command retires a local project.json that survived a")
        print("prior cloud cut-over — without cloud.json there is no cut-over")
        print("to retire.")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    project_id = config.get("project_id", "")
    if not project_id:
        print("Error: .beacon/cloud.json is malformed (no project_id).")
        sys.exit(1)

    pf = get_project_file()
    if not os.path.exists(pf):
        print(f"No local project.json at {pf} — nothing to migrate.")
        print("(= already retired, or this project was created cloud-first.)")
        return

    if not expected_pid or expected_pid != project_id:
        print("Refusing to retire local project.json without two-factor confirmation.")
        print("")
        print(f"  Run: beacon cloud migrate-from-local --confirm {project_id!r}")
        print("")
        print("  This guard mirrors `cloud off` (e-1776): a sub-agent or stray")
        print("  command should not be able to silently move historical state")
        print("  out of the working tree.")
        sys.exit(1)

    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    print("Pre-flight check: comparing local project.json against cloud state...")
    from store_local import LocalStore
    local = LocalStore(pf).load_project()

    from api_client import ApiClient
    api_url = _resolve_active_api_url()
    client = ApiClient(api_url, _extract_token(creds))
    try:
        remote = client.get_project(project_id)
    except RuntimeError as exc:
        print(f"Error: could not fetch cloud project: {exc}")
        sys.exit(1)

    issues = _local_vs_cloud_pre_flight(local, remote)
    if issues:
        print("")
        print(f"Pre-flight failed — local has {len(issues)} issue(s) that cloud is missing:")
        for line in issues:
            print(f"  - {line}")
        if not force_after_review:
            print("")
            print("Refusing to retire local project.json: cloud is missing data that")
            print("only exists locally. Either:")
            print("")
            print("  (a) Investigate the missing entries (a stale milestone? a")
            print("      partial sync from before cloud cut-over?). Recover the")
            print("      value you want into cloud via the CLI / Web UI, then re-run.")
            print("  (b) If the local-only entries are intentional residue (= dead")
            print("      cleanup pending), confirm by re-running with both flags:")
            print("        BEACON_CONFIRM=<pid> BEACON_FORCE=1 beacon cloud migrate-from-local --confirm <pid> --force-after-review")
            print("      (or via the CLI wrapper). The renamed copy stays under")
            print("      .beacon/project.json.before-cloud-YYYYMMDD so recovery")
            print("      remains possible.")
            sys.exit(1)
        print("")
        print("Override accepted (--force-after-review). Proceeding to rename.")

    renamed = _rename_local_project_json_for_cloud_cutover(pf)
    if not renamed:
        print(f"Rename failed (see warning above). The orphan file is still at {pf}.")
        sys.exit(1)

    print("")
    print(f"Retired: {pf} → {renamed}")
    print(f"  cloud project: {project_id}")
    print(f"  pre-flight issues: {len(issues)}{' (override)' if issues else ''}")
    print("")
    print("From here on, cloud is the sole truth source for this working tree.")
    print(f"To recover the old snapshot: mv {renamed} {pf}")


def cmd_migrate_target_labels():
    """`beacon migrate target-labels` — run the ms-109 target-label backfill
    (e-3695) over this project and record the execution trail.

    Sweeps every stored Target (development milestone/operation via
    ``core.backfill_target_labels``; sales account/opportunity via
    ``sales_entities.backfill_target_labels``), stamping the canonical ``label``
    on any record that only carries the legacy ``title`` / ``name``. The sweep
    is ADDITIVE — it never removes the legacy key — so it is safe to run
    regardless of which readers are deployed (task e-3695 AC3). It is also
    idempotent: a second run stamps nothing and reports 0.

    On success it records ``work_model.BACKFILL_MARKER`` on the project, the
    structural gate the contract step (e-3626) checks before it drops the legacy
    fallback. Without this verb the two backfill functions had no production
    caller, so no stored data was ever migrated (fable review A-1)."""
    import sales_entities
    data = load_project()
    already = work_model.target_labels_backfilled(data)
    dev = core.backfill_target_labels(data)
    sales = sales_entities.backfill_target_labels(data)
    work_model.stamp_target_labels_backfill(
        data, dev_count=dev, sales_count=sales, version=__version__)
    save_project(data, op={
        "action": "migrate_target_labels",
        "dev_count": dev,
        "sales_count": sales,
    })
    total = dev + sales
    if total:
        print(f"target-label backfill: stamped canonical 'label' on {total} "
              f"record(s) ({dev} development + {sales} sales).")
    elif already:
        print("target-label backfill: already complete — nothing to stamp "
              "(idempotent re-run). Execution trail refreshed.")
    else:
        print("target-label backfill: every Target already carries a canonical "
              "'label' — nothing to stamp. Execution trail recorded.")
    print(f"  recorded: work_model.BACKFILL_MARKER (version {__version__}). "
          f"The contract step (e-3626) gates on this before dropping the "
          f"legacy fallback.")


# ---------------------------------------------------------------------------
# PR commands (ms-15)
# ---------------------------------------------------------------------------









































# GitHub Issue import (ms-28) — moved to lib/cmd_issue.py (ms-127 e-4320);
# re-imported at top.


# ---------------------------------------------------------------------------
# Skill install
# ---------------------------------------------------------------------------

def _resolve_skills_src() -> str:
    """Locate the skills source directory.

    Resolution order (first existing wins):
      1. ``<beacon_root>/skills/`` — source / editable / brew layouts.
         beacon_root = directory two parents up from commands.py.
      2. ``<beacon_cli>/_bundled_skills/`` — wheel/pipx layout where this
         file is at ``<site-packages>/beacon_cli/_bundled_lib/commands.py``
         and skills were remapped via ``setuptools.package-dir`` into
         ``<site-packages>/beacon_cli/_bundled_skills/``.

    Returns empty string when neither exists so the caller can produce a
    targeted error.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # 1) source layout: <repo>/skills/
    candidate = os.path.join(os.path.dirname(here), "skills")
    if os.path.isdir(candidate):
        return candidate
    # 2) wheel layout: <site-packages>/beacon_cli/_bundled_skills/
    candidate = os.path.join(here, "..", "_bundled_skills")
    candidate = os.path.normpath(candidate)
    if os.path.isdir(candidate):
        return candidate
    return ""


def _hook_unusable_on_windows(cmd: str) -> bool:
    """True if ``cmd`` is a bare ``.sh`` script while running on Windows.

    Claude Code's hook runner cannot execute a ``.sh`` on Windows (there is no
    shell association that yields the expected stdout JSON), so such a hook
    silently no-ops and ``/beacon-log`` never fires. Used to (a) keep
    ``_resolve_hook_command`` from resolving to a ``.sh`` on Windows and
    (b) let ``beacon doctor`` flag an already-broken config. (ms-44 e-853)
    """
    return os.name == "nt" and cmd.strip().lower().endswith(".sh")


def _bash_safe(path: str) -> str:
    """Normalize a path for use inside a Claude Code hook command (e-1043).

    Claude Code runs hook commands through bash (``/usr/bin/bash``) even on
    Windows. A backslash absolute path like
    ``C:\\Users\\me\\.local\\bin\\beacon-hook-post-commit.EXE`` has its
    backslashes eaten as escapes by bash, yielding
    ``C:Usersme.localbin...: command not found`` — so the hook silently fails
    on every Bash tool call. Forward slashes with the drive letter
    (``C:/Users/...``) are interpreted correctly by bash/msys, so rewrite them
    on Windows. POSIX paths are returned unchanged.
    """
    if os.name == "nt" and path:
        return path.replace("\\", "/")
    return path


def _resolve_hook_command(hook_basename: str) -> str:
    """Return a cross-platform command string for the named hook.

    Resolution order:
      1. ``shutil.which("beacon-hook-<name>")`` — the console-script entry-point
         installed by pipx / setuptools. Absolute path so Claude Code can
         spawn it without PATH dependence.
      2. Module-level ``CLAUDE_*_HOOK_SCRIPT`` constant when it points at an
         existing file. This preserves the bash-installed source/brew layout
         AND honors test-time ``monkeypatch.setattr(commands, "CLAUDE_…", …)``.
      3. ``<beacon_root>/bin/<hook_basename>`` — source / brew layout where
         the bash script lives next to the bin/beacon launcher.
      4. ``python -m beacon_cli.hooks.<name>`` — last-resort fallback that
         works as long as the Python interpreter that ran setup can still
         import the module. Used inside CI / wheel install when entry-points
         haven't been added to PATH yet (rare).

    ``hook_basename`` is the bash filename (e.g. ``beacon-post-commit-hook.sh``);
    we map it to the matching entry-point / module name.
    """
    # bash filename → (entry-point name, module name, module constant name)
    mapping = {
        "beacon-post-commit-hook.sh": (
            "beacon-hook-post-commit",
            "beacon_cli.hooks.post_commit",
            "CLAUDE_HOOK_SCRIPT",
        ),
        "beacon-postcompact.sh": (
            "beacon-hook-postcompact",
            "beacon_cli.hooks.postcompact",
            "CLAUDE_POSTCOMPACT_HOOK_SCRIPT",
        ),
        "beacon-save-hook.sh": (
            "beacon-hook-save",
            "beacon_cli.hooks.save_hook",
            "CLAUDE_SAVE_HOOK_SCRIPT",
        ),
        # ms-44 e-854: Stop hook (context-usage threshold monitor). Python
        # port lives at beacon_cli.hooks.context_monitor; bash stays at
        # bin/context-usage-monitor.sh for Mac/Linux source / brew users.
        "context-usage-monitor.sh": (
            "beacon-hook-context-monitor",
            "beacon_cli.hooks.context_monitor",
            "CLAUDE_CONTEXT_MONITOR_HOOK_SCRIPT",
        ),
        # ms-103: SessionStart 自動アップデート hook。bash 版は無く Python 専用。
        "beacon-session-start.sh": (
            "beacon-hook-session-start",
            "beacon_cli.hooks.session_start",
            "CLAUDE_SESSION_START_HOOK_SCRIPT",
        ),
        # ms-160 e-5798: PostToolUse halt-check hook (remote STOP kill-switch).
        # bash 版は無く Python 専用 (session-start と同型)。エントリポイント
        # beacon-hook-halt-check、fallback は python -m beacon_cli.hooks.halt_check。
        "beacon-halt-check.sh": (
            "beacon-hook-halt-check",
            "beacon_cli.hooks.halt_check",
            "CLAUDE_HALT_CHECK_HOOK_SCRIPT",
        ),
    }
    entry_name, module_name, const_name = mapping.get(
        hook_basename, ("", "", "")
    )

    # NOTE: every path-returning branch below goes through _bash_safe() so the
    # command written into settings.json is bash-safe on Windows (e-1043).
    if entry_name:
        resolved = shutil.which(entry_name)
        if resolved:
            # e-1170: write the bare entry-point name (not the absolute
            # path) so the hook command survives `beacon` upgrades that
            # relocate the binary (e.g. pipx → pip --user, ~/.local/bin →
            # AppData/Roaming/Python/.../Scripts). Claude Code re-resolves
            # via PATH at hook fire time. We still call shutil.which here
            # to *validate* the entry-point exists at install time — if it
            # doesn't, we fall through to the bash / module fallback.
            return entry_name

    # Honor the module-level constant (set at import time, but tests
    # routinely monkeypatch them — keeping this lookup means existing
    # test_install_hooks.py contracts continue to hold).
    if const_name:
        const_val = globals().get(const_name, "")
        if const_val and os.path.exists(const_val) and not _hook_unusable_on_windows(const_val):
            return _bash_safe(const_val)

    # Source / brew: bash next to the launcher.
    bash_path = _find_hook(hook_basename)
    if bash_path and os.path.exists(bash_path) and not _hook_unusable_on_windows(bash_path):
        return _bash_safe(bash_path)

    # Final fallback: invoke the module via the current interpreter.
    if module_name:
        return f"{_bash_safe(sys.executable)} -m {module_name}"
    return ""


def cmd_skill_install():
    """Install beacon Claude Code Skills into ~/.claude/skills/, update CLAUDE.md, and configure hooks."""
    import shutil
    from pathlib import Path
    converter_target = os.environ.get("BEACON_SKILL_TARGET", "").strip()
    converter_name = os.environ.get("BEACON_SKILL_NAME", "").strip()
    if converter_target or converter_name:
        from skill_converter import (
            SkillConversionError,
            install_skill,
            prune_skill,
            resolve_canonical_root,
        )

        if not converter_target or not converter_name:
            print("Error: converter install requires both --target and --name", file=sys.stderr)
            sys.exit(2)
        targets = ("claude", "codex") if converter_target == "both" else (converter_target,)
        try:
            common = {
                "targets": targets,
                "home": Path(_user_home()),
                "dry_run": os.environ.get("BEACON_DRY_RUN") == "1",
                "force": os.environ.get("BEACON_FORCE") == "1",
            }
            if os.environ.get("BEACON_PRUNE") == "1":
                if os.environ.get("BEACON_ADOPT") == "1":
                    raise SkillConversionError("--prune and --adopt are mutually exclusive")
                results = prune_skill(converter_name, **common)
            else:
                results = install_skill(
                    resolve_canonical_root() / converter_name,
                    adopt=os.environ.get("BEACON_ADOPT") == "1",
                    **common,
                )
        except SkillConversionError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if os.environ.get("BEACON_JSON") == "1":
            print(json.dumps(results, ensure_ascii=False))
        else:
            for result in results:
                print(
                    f"{result['target']}: {result['action']} "
                    f"{result['destination']}"
                )
                for warning in result.get("warnings", []):
                    print(f"  warning: {warning}")
        return

    _append_claude_md()

    skills_src = _resolve_skills_src()
    if not skills_src:
        print("Error: skills directory not found (looked in source layout and wheel _bundled_skills).")
        sys.exit(1)
    beacon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Destination: ~/.claude/skills/
    home = _user_home()
    claude_skills = os.path.join(home, ".claude", "skills")
    os.makedirs(claude_skills, exist_ok=True)

    # Separate skills from companion files.
    # Companion convention: filename starts with `_` and is NOT installed as a
    # standalone Skill. Instead it's placed in the related Skill's directory
    # as a doc that the Skill can Read at runtime.
    # Filename: _<skill-name>-<companion-suffix>.md
    #   The <skill-name> portion is matched against existing skill names
    #   (longest match wins). The <companion-suffix> becomes the destination
    #   filename inside that Skill's directory.
    skill_files = []
    companion_files = []
    for src_file in sorted(os.listdir(skills_src)):
        if not src_file.endswith(".md"):
            continue
        if src_file.startswith("_"):
            companion_files.append(src_file)
        else:
            skill_files.append(src_file)

    skill_names = [f[:-3] for f in skill_files]
    # ms-108 e-3364: gate by profession so a dev project doesn't receive the
    # beacon-sales-* skills (and vice versa). Additive across setups.
    requested = _requested_professions()

    installed = []
    skipped = []
    for src_file in skill_files:
        skill_name = src_file[:-3]
        if not _skill_is_installable(os.path.join(skills_src, src_file), requested):
            skipped.append(skill_name)
            continue
        dest_dir = os.path.join(claude_skills, skill_name)
        os.makedirs(dest_dir, exist_ok=True)
        dest_file = os.path.join(dest_dir, "skill.md")
        shutil.copy2(os.path.join(skills_src, src_file), dest_file)
        installed.append(skill_name)
    installed_set = set(installed)

    companion_installed = []
    for src_file in companion_files:
        base = src_file[1:-3]  # strip leading _ and trailing .md
        # Find longest matching skill name as the owning skill. A companion
        # follows its owning skill's install decision (ms-108 e-3364): if the
        # parent was gated out by profession, its companion is skipped too.
        match = ""
        for s in skill_names:
            if s not in installed_set:
                continue
            if (base == s or base.startswith(s + "-")) and len(s) > len(match):
                match = s
        if not match:
            print(f"  ! Companion file {src_file} doesn't match any installed skill (skipping)")
            continue
        # Companion suffix: everything after `<skill-name>-`
        suffix = base[len(match) + 1:] if len(base) > len(match) else "companion"
        if not suffix:
            suffix = "companion"
        dest_dir = os.path.join(claude_skills, match)
        os.makedirs(dest_dir, exist_ok=True)
        dest_file = os.path.join(dest_dir, f"{suffix}.md")
        shutil.copy2(os.path.join(skills_src, src_file), dest_file)
        companion_installed.append(f"{match}/{suffix}.md")

    if installed:
        print(f"Installed {len(installed)} Skills to {claude_skills}:")
        for name in installed:
            print(f"  /{name}")
    else:
        print("No skills found to install.")
    if skipped:
        print(f"Skipped {len(skipped)} off-profession skill(s) "
              f"(professions installed: {', '.join(sorted(requested))}). "
              f"Set BEACON_PROFESSION or init that profession's project to include them.")
    if companion_installed:
        print(f"Installed {len(companion_installed)} companion file(s):")
        for path in companion_installed:
            print(f"  {path}")

    # Configure Claude Code PostToolUse hooks.
    # ms-44 e-777: cross-platform — _resolve_hook_command returns a console-
    # script entry-point path (pipx install) or the bash .sh path (source /
    # brew layout) or a `python -m beacon_cli.hooks.<name>` fallback.
    hook_script = _resolve_hook_command("beacon-post-commit-hook.sh")
    settings_path = (
        os.environ.get("BEACON_SETTINGS_PATH", "")
        or os.path.join(home, ".claude", "settings.json")
    )
    _install_claude_hooks(hook_script, settings_path)

    # ms-46 e-725 follow-up: install git pre-commit hook for beacon dev clones.
    # Only acts when running inside the beacon source repo (has .git/ AND
    # scripts/hooks/pre-commit). Brew-installed users don't have a .git/ here
    # so this is a no-op for them.
    _install_dev_precommit_hook(beacon_root)


def _install_dev_precommit_hook(beacon_root: str) -> None:
    """Install scripts/hooks/pre-commit as a symlink to .git/hooks/pre-commit.

    Idempotent. Safe to call from any environment — does nothing if not a git clone.
    Backs up any existing non-symlink pre-commit hook to .git/hooks/pre-commit.bak.
    """
    git_dir = os.path.join(beacon_root, ".git")
    if not os.path.isdir(git_dir):
        return  # brew install / tarball, no .git
    src_rel = os.path.join("scripts", "hooks", "pre-commit")
    src_abs = os.path.join(beacon_root, src_rel)
    if not os.path.isfile(src_abs):
        return  # source not present (e.g., very old checkout)

    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    target = os.path.join(hooks_dir, "pre-commit")
    # Compute relative path from .git/hooks/ back to scripts/hooks/pre-commit
    # (.git/hooks/ → .git/ → repo root → scripts/hooks/pre-commit)
    link_target = os.path.join("..", "..", src_rel)

    # If already symlinked correctly, nothing to do.
    if os.path.islink(target):
        try:
            current = os.readlink(target)
            if current == link_target:
                return  # already correct
        except OSError:
            pass

    # Back up existing non-symlink hook so we don't clobber custom logic.
    if os.path.exists(target) and not os.path.islink(target):
        import time
        backup = target + f".bak.{int(time.time())}"
        try:
            os.rename(target, backup)
            print(f"  [hook] backed up existing pre-commit hook → {os.path.basename(backup)}")
        except OSError as e:
            print(f"  [hook] WARN: couldn't back up existing pre-commit ({e}), skipping")
            return
    elif os.path.islink(target):
        # Stale symlink (pointing elsewhere) — remove
        try:
            os.unlink(target)
        except OSError:
            return

    # Make sure source is executable (chmod 0o755). No-op on Windows where
    # file modes aren't used in the Unix sense; safe to ignore failures.
    try:
        os.chmod(src_abs, 0o755)
    except OSError:
        pass

    # Try symlink first (POSIX, atomic, tracks source updates automatically).
    # On Windows without Developer Mode / admin, symlink() raises OSError —
    # fall back to a plain file copy so the hook still fires. The copy
    # version becomes stale if the source is updated later; print a hint so
    # the user knows to re-run `beacon skill install` after pulling.
    try:
        os.symlink(link_target, target)
        print(f"  [hook] installed pre-commit hook (symlink → {src_rel})")
        return
    except OSError as e:
        symlink_err = e

    try:
        import shutil as _shutil
        _shutil.copyfile(src_abs, target)
        try:
            os.chmod(target, 0o755)
        except OSError:
            pass
        print(
            f"  [hook] installed pre-commit hook (copy → {src_rel}; "
            f"re-run 'beacon skill install' after updating the hook source)"
        )
    except OSError as copy_err:
        print(
            f"  [hook] WARN: couldn't install pre-commit hook — "
            f"symlink: {symlink_err}; copy: {copy_err}"
        )


# ---------------------------------------------------------------------------
# Self-update (`beacon update`)
# ---------------------------------------------------------------------------

def _detect_install_method() -> dict:
    """Detect how beacon was installed.

    Returns a dict:
      {
        "method": "brew" | "git" | "unknown",
        "prefix": str | None,           # brew prefix or git repo root
        "current_version": str,         # beacon --version
      }

    "brew": realpath of the running bin/beacon lives under `brew --prefix`.
    "git":  beacon root has a `.git/` directory (developer / manual clone install).
    "unknown": none of the above.
    """
    info = {"method": "unknown", "prefix": None, "current_version": __version__}

    # Resolve the running beacon root (lib/commands.py is at <root>/lib/commands.py)
    beacon_root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # 1) Homebrew detection
    try:
        result = subprocess.run(
            ["brew", "--prefix", "beacon"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            brew_prefix = result.stdout.strip()
            if brew_prefix:
                brew_real = os.path.realpath(brew_prefix)
                # beacon root may be the brew prefix itself, or sit under Cellar/...
                if beacon_root == brew_real or beacon_root.startswith(brew_real + os.sep):
                    info["method"] = "brew"
                    info["prefix"] = brew_prefix
                    return info
                # Cellar path: brew_prefix is a symlink to Cellar/beacon/<v>/...
                # If beacon_root is under any HOMEBREW Cellar, treat as brew.
                if "/Cellar/beacon/" in beacon_root:
                    info["method"] = "brew"
                    info["prefix"] = brew_prefix
                    return info
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # 2) Git checkout detection
    if os.path.isdir(os.path.join(beacon_root, ".git")):
        info["method"] = "git"
        info["prefix"] = beacon_root
        return info

    return info


def cmd_update():
    """Self-update beacon: `brew upgrade beacon` (or git pull) then `beacon skill install`.

    Flags (via environment):
      BEACON_UPDATE_CHECK=1     Dry-run; report what would happen, no changes.
      BEACON_UPDATE_SKILL_ONLY=1  Skip brew/git step, only refresh skills.
      BEACON_UPDATE_YES=1       Skip interactive confirmation.

    Exit codes:
      0  success (or no-op when already up to date)
      1  failure (network / brew / git error)
    """
    check_only = os.environ.get("BEACON_UPDATE_CHECK", "") == "1"
    skill_only = os.environ.get("BEACON_UPDATE_SKILL_ONLY", "") == "1"
    auto_yes = os.environ.get("BEACON_UPDATE_YES", "") == "1"

    info = _detect_install_method()
    current = info["current_version"]
    method = info["method"]

    print(f"Current: beacon {current}  (install method: {method})")

    if skill_only:
        print("→ Skipping CLI upgrade (--skill-only); refreshing Claude Code Skills only.")
        if check_only:
            print("[dry-run] would run: beacon skill install")
            sys.exit(0)
        cmd_skill_install()
        return

    if method == "brew":
        # 1. Probe latest available version (best-effort).
        latest = None
        try:
            r = subprocess.run(
                ["brew", "info", "--json=v2", "beacon"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                try:
                    payload = json.loads(r.stdout)
                    formulae = payload.get("formulae", [])
                    if formulae:
                        latest = formulae[0].get("versions", {}).get("stable")
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        if latest:
            print(f"Latest:  beacon {latest}")
            if latest == current:
                print("✓ Already up to date.")
                if not check_only:
                    print("→ Re-installing Skills to ensure they match the running CLI.")
                    cmd_skill_install()
                sys.exit(0)
        else:
            print("Latest:  (could not determine — proceeding anyway)")

        if check_only:
            print("[dry-run] would run:")
            print("  brew update")
            print("  brew upgrade beacon")
            print("  beacon skill install")
            sys.exit(0)

        if not auto_yes:
            try:
                ans = input("Proceed with upgrade? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans and ans not in ("y", "yes", ""):
                print("Aborted.")
                sys.exit(0)

        # 2. brew update (best-effort; do not abort on network blip)
        print("→ brew update ...")
        r = subprocess.run(["brew", "update"])
        if r.returncode != 0:
            print("  (brew update failed; continuing to upgrade anyway)")

        # 3. brew upgrade beacon
        print("→ brew upgrade beacon ...")
        r = subprocess.run(["brew", "upgrade", "beacon"])
        if r.returncode != 0:
            # Detect the common "already up to date" case via re-check
            after = subprocess.run(
                ["beacon", "--version"], capture_output=True, text=True
            )
            if after.returncode == 0 and current in after.stdout:
                print("  No upgrade available (already at latest).")
            else:
                print("  brew upgrade failed.")
                sys.exit(1)

        # 4. Reinstall skills (the new CLI may have new/changed Skills)
        print("→ beacon skill install ...")
        # IMPORTANT: invoke the freshly-installed CLI, not this in-memory module.
        r = subprocess.run(["beacon", "skill", "install"])
        if r.returncode != 0:
            print("  Skill install failed.")
            sys.exit(1)

        print("✓ Update complete.")
        return

    if method == "git":
        prefix = info["prefix"]
        print(f"Detected developer / git install at: {prefix}")
        print("→ This install is not managed by Homebrew. Recommended manual steps:")
        print(f"    git -C {prefix} pull --ff-only")
        print(f"    beacon skill install")
        if check_only:
            print("[dry-run] no changes applied.")
            sys.exit(0)

        if not auto_yes:
            try:
                ans = input("Attempt automatic `git pull --ff-only` + skill install? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans and ans not in ("y", "yes", ""):
                print("Aborted. Run the commands above manually when ready.")
                sys.exit(0)

        r = subprocess.run(["git", "-C", prefix, "pull", "--ff-only"])
        if r.returncode != 0:
            print("  git pull failed (uncommitted changes or non-fast-forward). Resolve manually.")
            sys.exit(1)

        r = subprocess.run(["beacon", "skill", "install"])
        if r.returncode != 0:
            print("  Skill install failed.")
            sys.exit(1)
        print("✓ Update complete.")
        return

    # Unknown install method
    print("⚠ Could not determine how beacon was installed.")
    print("  If installed via Homebrew, ensure `brew` is on PATH.")
    print("  Otherwise, re-run the installer for your setup.")
    print(f"  You can refresh Skills only with: beacon update --skill-only")
    sys.exit(1)


def _is_path_command(cmd: str) -> bool:
    """Return True when ``cmd`` looks like a single executable path.

    ms-44 e-777: ``_install_claude_hooks`` originally received a bash .sh
    path and gated registration on ``os.path.exists``. The cross-platform
    refactor may pass back a multi-token command like
    ``/usr/bin/python3 -m beacon_cli.hooks.post_commit`` — that's not a
    file path, so we skip the existence check for those.

    ms-44 e-1170: distinguish absolute / relative paths (which have
    separators or a leading ``./``) from bare entry-point names (which
    are PATH-resolved by the shell, not file-existence-checked). After
    e-1170 we write the bare ``beacon-hook-post-commit`` to settings.json
    rather than the absolute path so the hook survives install relocation.
    """
    if not cmd or " " in cmd.strip():
        return False
    s = cmd.strip()
    # Path-like if it contains separators or starts with relative-path prefix.
    has_sep = "/" in s or "\\" in s
    has_dot_prefix = s.startswith(("./", ".\\"))
    return has_sep or has_dot_prefix


def _install_claude_hooks(hook_script: str, settings_path: str) -> None:
    """Add beacon PostToolUse + PostCompact hooks to Claude Code settings.json.

    ms-43 e-672: previously this only registered the PostToolUse commit hook,
    so users who installed via `beacon skill install` ended up missing the
    PostCompact orientation hook (registered by the legacy `_install_claude_hook`
    code path used by `beacon init`). The two install paths must produce the
    same end state.

    ms-44 e-777: ``hook_script`` may now be a console-script absolute path
    (pipx install) or a ``python -m ...`` fallback command, not just a .sh.
    We only run the disk-existence guard for single-token commands.
    """
    if not hook_script:
        print("Warning: could not resolve a PostToolUse hook command.")
        return
    if _is_path_command(hook_script) and not os.path.exists(hook_script):
        print(f"Warning: hook script not found at {hook_script}")
        return

    # Load existing settings
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, IOError, UnicodeDecodeError):
            # #21 follow-up: tolerate cp932 settings.json from legacy builds.
            pass

    hooks = settings.setdefault("hooks", {})
    post_tool_use = hooks.setdefault("PostToolUse", [])

    # Dedup by hook *identity*: remove stale entries pointing to old install
    # paths (e.g. previous brew Cellar versions that no longer exist after
    # upgrade) or the obsolete bash .sh path after a pipx-style upgrade to the
    # Python entry-point.
    #
    # Identity rules:
    #   - Same basename (e.g. ``beacon-post-commit-hook.sh``) and different
    #     full path → stale.
    #   - Same "kind" — either references ``beacon-post-commit-hook`` /
    #     ``beacon-hook-post-commit`` / ``beacon_cli.hooks.post_commit`` —
    #     and different full command → stale.
    hook_basename = (
        os.path.basename(hook_script) if _is_path_command(hook_script) else hook_script
    )
    identity_substrings = (
        "beacon-post-commit-hook",
        "beacon-hook-post-commit",
        "beacon_cli.hooks.post_commit",
    )
    removed_stale = False
    cleaned_post = []
    for entry in post_tool_use:
        new_hooks_in_entry = []
        for h in entry.get("hooks", []):
            existing = h.get("command", "")
            same_basename = (
                _is_path_command(existing)
                and os.path.basename(existing) == hook_basename
            )
            same_kind = any(s in existing for s in identity_substrings)
            stale = (same_basename or same_kind) and existing != hook_script
            if stale:
                removed_stale = True
                continue  # drop stale
            new_hooks_in_entry.append(h)
        entry["hooks"] = new_hooks_in_entry
        if new_hooks_in_entry:
            cleaned_post.append(entry)
    post_tool_use[:] = cleaned_post

    # Check exact-path presence
    already_present = any(
        h.get("command", "") == hook_script
        for entry in post_tool_use
        for h in entry.get("hooks", [])
    )

    posttooluse_dirty = False
    if not already_present:
        # Add beacon PostToolUse hook (commit / PR / target-close / deploy detection).
        # e-5803 review (AX-11): keep this statusMessage identical to the
        # HOOK_MANIFEST post-commit entry so the init and skill-install paths write
        # the same text.
        post_tool_use.append({
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                "command": hook_script,
                "timeout": 10,
                "statusMessage": "Beacon: checking for commit / PR / target-close / deploy..."
            }]
        })
        posttooluse_dirty = True

    # ms-160 e-5806: install the remaining hooks from the single-source-of-truth
    # HOOK_MANIFEST (save / halt / postcompact / stop / session-start / bus-inbox).
    # The commit hook stays param-driven above for back-compat; everything else is
    # manifest-driven so `beacon skill install` reaches the SAME end state as
    # `beacon init` — this is what finally wires the MCP save hook and the
    # bus-inbox receive hook that skill-install previously left off (a fresh
    # install used to receive no cross-session DMs at all). _install_manifest_hook
    # folds the same stale-path migration (e-1043) the per-hook blocks used to do.
    manifest_dirty = False
    for spec in HOOK_MANIFEST:
        if spec["key"] == "post-commit":
            continue  # installed param-driven above
        if _install_manifest_hook(hooks, spec):
            manifest_dirty = True

    if removed_stale or posttooluse_dirty or manifest_dirty:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        if removed_stale and not (posttooluse_dirty or manifest_dirty):
            print(f"Hooks: cleaned stale {hook_basename} entries; current path active")
        else:
            print(f"Hooks: synced Claude Code hooks (manifest) in {settings_path}")
    else:
        print("Hooks: already configured in ~/.claude/settings.json")














# ms-127 e-4860: the project export / import family (ms-14 e-828, full-snapshot
# backup) moved to lib/cmd_project.py — see that module's docstring for the ZIP
# layout and the local/cloud-mode notes. Public handlers are re-imported above.


def cmd_cycle_status():
    """Emit a per-cycle activation snapshot for the current project.

    Output (JSON mode is default for AI consumers):

      {
        "push":     {"active": true,  "last_action_date": "2026-05-10T00:00:00Z"},
        "deploy":   {"active": false, "last_action_date": null},
        "retro":    {"active": false, "last_action_date": null},
        "operation":{"active": true,  "last_action_date": "..."},
        "release":  {"active": false, "last_action_date": null}
      }

    Skill consumers (/beacon-log Step 7, /beacon-push Step 2.5, etc.) call
    this once and branch their rhythm-suggestion logic on the result. Centra-
    lizing it here means every Skill agrees on activation semantics — see
    CORE doc `Cdg2zJtrOajm1q8adMa1` and SPEC `LqXEEbgsH712Z78KBELP`.

    Text mode is for human inspection and not load-bearing.
    """
    import cycle as cycle_mod  # local import: optional dependency outside of Skill use

    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()

    # ms-84 Phase 2: Store.list_documents() で local / cloud を統一。
    # 失敗時は空 list に degrade する best-effort 契約 (= push / deploy /
    # operation 系の cycle 判定は document に依存しないので、 ここで空でも
    # snapshot は正しく作れる)。
    try:
        documents = get_store().list_documents() or []
    except Exception:
        documents = []

    snapshot = cycle_mod.cycle_status_snapshot(data, documents=documents)

    if json_mode:
        print(json.dumps(snapshot, ensure_ascii=False))
        return

    # Human-readable mode. Used for spot-checking during development; Skills
    # always use --json.
    print("Cycle status:")
    for c, info in snapshot.items():
        flag = "active" if info["active"] else "inactive"
        last = info["last_action_date"] or "—"
        print(f"  {c:<10s} {flag:<10s} last: {last}")


def cmd_search():
    """Unified search across all Beacon entities (CORE doc 検索基盤の原則 / SPEC 3ne57ccZegYQXDQA03op).

    Delegates the actual work to ``lib/search.search_project`` for the
    canonical (= pre-ms-79) path, or to ``lib/retro_query.retro_query``
    when any ms-79 extension flag is present (source / actor / claim /
    include_*). retro_query wraps search_project and adds the post-filters
    plus extension entity merges (= bus archive / session_logs / Trek);
    see SPEC ms-79 §3 ‘設計の柱 6’ (基盤統合).
    """
    import search as _search  # noqa: PLC0415

    query = os.environ.get("BEACON_QUERY", "")
    ms_filter = os.environ.get("BEACON_MS_ID", "")
    op_filter = os.environ.get("BEACON_OPERATION_ID", "")
    id_filter = os.environ.get("BEACON_ENTRY_ID", "")
    scope_filter = os.environ.get("BEACON_SCOPE", "")
    assignee = os.environ.get("BEACON_ASSIGNEE", "")
    owner = os.environ.get("BEACON_OWNER", "")
    from_date = os.environ.get("BEACON_FROM", "")
    to_date = os.environ.get("BEACON_TO", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    # ms-79 extension knobs (= /beacon-retrospect Skill が opt-in で渡す).
    # 空のときは search_project の旧経路で動かして back-compat を保つ。
    source_list_raw = os.environ.get("BEACON_SOURCE", "").strip()
    actor_filter = os.environ.get("BEACON_ACTOR", "").strip()
    claimant_filter = os.environ.get("BEACON_CLAIMANT", "").strip()
    include_bus_dm = os.environ.get("BEACON_INCLUDE_BUS_DM", "") == "1"
    include_session_logs = os.environ.get("BEACON_INCLUDE_SESSION_LOGS", "") == "1"
    include_trek = os.environ.get("BEACON_INCLUDE_TREK", "") == "1"

    def _parse_list(env_key: str) -> list[str]:
        raw = os.environ.get(env_key, "").strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    type_list = _parse_list("BEACON_TYPE")
    status_list = _parse_list("BEACON_STATUS")
    priority_list = _parse_list("BEACON_PRIORITY")

    try:
        limit = int(os.environ.get("BEACON_LIMIT", "50"))
    except ValueError:
        limit = 50
    try:
        offset = int(os.environ.get("BEACON_OFFSET", "0"))
    except ValueError:
        offset = 0

    # Without any filters the search defaults to "give me the 50 most-recent
    # entities". This matches the SPEC: "all params omitted = ダッシュボード
    # 最近の動き相当". So an empty query is no longer an error.

    data = load_project()
    documents = _load_local_documents()

    source_list = [s.strip() for s in source_list_raw.split(",") if s.strip()] if source_list_raw else None
    use_retro_query = bool(
        source_list or actor_filter or claimant_filter
        or include_bus_dm or include_session_logs or include_trek
    )

    if use_retro_query:
        import retro_query as _rq  # noqa: PLC0415
        session_logs = _load_session_logs() if include_session_logs else None
        bus_archive = _load_bus_archive() if include_bus_dm else None
        trek_summaries = _load_trek_summaries() if include_trek else None
        result = _rq.retro_query(
            data,
            documents,
            q=query,
            type=type_list or None,
            status=status_list or None,
            priority=priority_list or None,
            scope=scope_filter,
            ms=ms_filter,
            op=op_filter,
            id=id_filter,
            assignee=assignee,
            owner=owner,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
            source=source_list,
            actor=actor_filter,
            claimant=claimant_filter,
            include_bus_dm=include_bus_dm,
            include_session_logs=include_session_logs,
            include_trek=include_trek,
            session_logs=session_logs,
            bus_archive=bus_archive,
            trek_summaries=trek_summaries,
        )
    else:
        result = _search.search_project(
            data,
            documents,
            q=query,
            type=type_list or None,
            status=status_list or None,
            priority=priority_list or None,
            scope=scope_filter,
            ms=ms_filter,
            op=op_filter,
            id=id_filter,
            assignee=assignee,
            owner=owner,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
        return

    results = result["results"]
    total = result["total"]

    if not results:
        if query:
            print(f"No results for: {query}")
        else:
            print("No results.")
        return

    type_icons = {
        "task": "□", "commit": "○", "pr": "PR", "milestone": "MS",
        "save": "→", "document": "📄", "push": "↑", "deploy": "▲",
        "operation": "⚙", "operation_task": "·", "run": "▷", "incident": "⚠",
    }
    header = f"{total} result(s)"
    if query:
        header += f" for: {query}"
    if total > len(results):
        header += f"  (showing {len(results)} from offset {result['offset']})"
    print(header)
    for r in results:
        icon = type_icons.get(r.get("entity_type", ""), "?")
        status_note = f" [{r['status']}]" if r.get("status") else ""
        date_part = (r.get("created_at") or "")[:10]
        title = r.get("title") or r.get("snippet", "")[:80]
        print(f"  {icon} [{r['id']}] {title[:80]}{status_note}")
        scope_or_ms = r.get("scope") or r.get("ms_id") or r.get("op_id") or ""
        if scope_or_ms or date_part:
            print(f"       └─ {scope_or_ms}  {date_part}")
        if r.get("snippet") and r["snippet"] != title:
            snip = r["snippet"][:120]
            print(f"       └─ {snip}")


def _load_session_logs() -> list[dict]:
    """Return session_log entries for the active project (ms-79 / e-1835).

    Cloud mode: pulls ``/api/projects/{id}/session_logs`` (= the
    aggregated session summaries written by ``beacon session end`` and
    ``beacon session rescue``).

    Local mode: walks ``.beacon/session_logs/*.json`` if present and
    returns each as a dict. The local layout is the same shape the cloud
    endpoint serves, so retro_query needs no mode-aware logic.

    Returns ``[]`` on any failure — retro_query treats this as "no
    session log source available" and silently skips the merge. This
    keeps /beacon-retrospect usable on installs that never enabled the
    session_log subcollection.
    """
    try:
        # ms-84 Phase 2: Store.list_session_logs() で cloud / local 二経路を
        # 単一の呼び出しに寄せる。 StoreApi 側が transport 失敗を [] に丸める
        # best-effort 契約を持つので、 cloud auth glitch も同じ try/except で
        # 拾える。 LocalStore は no-op で [] を返す設計 (= Protocol docstring
        # 参照) のため、 local モード時は下の directory walk fallback が拾う。
        rows = get_store().list_session_logs() or []
        if isinstance(rows, dict):
            rows = rows.get("session_logs") or rows.get("items") or []
        if isinstance(rows, list) and rows:
            return rows
        # Local mode (or empty cloud): walk .beacon/session_logs/*.json
        project_dir = os.path.dirname(get_project_file())
        sl_dir = os.path.join(project_dir, "session_logs")
        if not os.path.isdir(sl_dir):
            return []
        out: list[dict] = []
        for fname in os.listdir(sl_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(sl_dir, fname), "r", encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec, dict):
                    out.append(rec)
            except (OSError, json.JSONDecodeError):
                continue
        return out
    except Exception:
        return []


def _load_bus_archive() -> list[dict]:
    """Return DM / bus event archive entries (ms-79 / e-1832 / UC10-F1).

    Reads ``.beacon/bus_archive/*.json`` if the install has captured
    DM history (= ms-54 envelope archive). Returns ``[]`` if nothing is
    persisted — retro_query then silently skips the merge.

    Per SPEC ms-79 §8 ‘やらないこと’: we do not change the archive
    schema, we only **read** it. Whatever shape ms-54 writes is what
    we surface, with the renderer in lib/retro_query tolerating missing
    fields.
    """
    try:
        project_dir = os.path.dirname(get_project_file())
        ba_dir = os.path.join(project_dir, "bus_archive")
        if not os.path.isdir(ba_dir):
            return []
        out: list[dict] = []
        for fname in os.listdir(ba_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(ba_dir, fname), "r", encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec, dict):
                    out.append(rec)
                elif isinstance(rec, list):
                    # support an archive-as-list layout if any install uses it
                    out.extend(r for r in rec if isinstance(r, dict))
            except (OSError, json.JSONDecodeError):
                continue
        return out
    except Exception:
        return []


def _load_trek_summaries() -> list[dict]:
    """Return Trek summary entries (ms-79 / e-1835 / UC10-F4).

    Cloud mode: walks each trek's summaries via the API client (= ms-69
    Trek records carry their own summary list).

    Local mode: walks ``.beacon/treks/*/summaries/*.json`` if present.

    Returns ``[]`` on any failure — retro_query handles the absence
    gracefully.
    """
    out: list[dict] = []
    try:
        # Local mode walk (also useful as a cache when cloud sync is on).
        project_dir = os.path.dirname(get_project_file())
        treks_dir = os.path.join(project_dir, "treks")
        if os.path.isdir(treks_dir):
            for trek_name in os.listdir(treks_dir):
                summaries_dir = os.path.join(treks_dir, trek_name, "summaries")
                if not os.path.isdir(summaries_dir):
                    continue
                for fname in os.listdir(summaries_dir):
                    if not fname.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(summaries_dir, fname),
                                  "r", encoding="utf-8") as f:
                            rec = json.load(f)
                        if isinstance(rec, dict):
                            rec.setdefault("trek_id", trek_name)
                            out.append(rec)
                    except (OSError, json.JSONDecodeError):
                        continue
    except Exception:
        pass
    return out


def _find_all_on_path(name: str) -> list[str]:
    """Return every executable named ``name`` on PATH (across all PATH dirs).

    Unlike ``shutil.which`` which returns only the first hit, this walks
    every PATH directory and collects all matches. On Windows it also
    consults ``PATHEXT`` to find ``.exe`` / ``.cmd`` / ``.bat`` variants.

    Used by ``cmd_doctor`` to surface install-location shadowing (ms-44
    e-1170): e.g. an old ``~/.local/bin/beacon`` 0.11.1 silently shadowing
    a newer ``AppData/.../Scripts/beacon.exe`` 0.19.0.
    """
    path_env = os.environ.get("PATH", "")
    if not path_env:
        return []
    dirs = path_env.split(os.pathsep)
    if os.name == "nt":
        pathext_raw = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        exts = [""] + [e.lower() for e in pathext_raw.split(os.pathsep) if e]
    else:
        exts = [""]
    found: list[str] = []
    seen: set[str] = set()
    for d in dirs:
        d = d.strip()
        if not d:
            continue
        for ext in exts:
            cand = os.path.join(d, name + ext)
            if cand in seen:
                continue
            seen.add(cand)
            try:
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    found.append(cand)
                    break  # one hit per directory is enough
            except OSError:
                continue
    return found


def _shell_dispatched_subcommands():
    """Parse ``bin/beacon`` for top-level and 1-level-nested case branches.

    Returns set of word-tuples of subcommand labels. Some subcommands
    (e.g., ``status`` / ``log`` / ``milestone add``) are dispatched
    shell-side in ``bin/beacon`` rather than via the Python ``cmd_*``
    table, so the doctor drift check has to read both surfaces.

    State machine: track the current 4-space subgroup label, then group
    12-space sub-cases under it. ``milestone|ms)`` adds both forms.
    """
    beacon_bin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin", "beacon"
    )
    if not os.path.isfile(beacon_bin):
        return set()
    import re as _re
    top_pat = _re.compile(r"^    ([a-z][a-zA-Z0-9|_-]*)\)")
    sub_pat = _re.compile(r"^            ([a-z][a-zA-Z0-9|_-]*)\)")
    cases = set()
    cur_top_alts = []
    try:
        with open(beacon_bin, "r", encoding="utf-8") as fh:
            for line in fh:
                m = top_pat.match(line)
                if m:
                    alts = m.group(1).split("|")
                    for alt in alts:
                        cases.add((alt,))
                    cur_top_alts = alts
                    continue
                m = sub_pat.match(line)
                if m and cur_top_alts:
                    label = m.group(1)
                    for sub_alt in label.split("|"):
                        for top_alt in cur_top_alts:
                            cases.add((top_alt, sub_alt))
    except OSError:
        return set()
    return cases


def _canonical_subcommand_set():
    """Word-tuples for every known `beacon <subcmd>` invocation.

    Combines three sources for full coverage:
    - ``cmd_*`` functions in this module (Python dispatch table)
    - Top-level and nested case branches in ``bin/beacon`` (shell dispatch
      — ``status`` / ``log`` etc. are not ``cmd_*`` but plain shell)
    - Hardcoded entries for lambda-mapped dispatch keys
      (``version`` / ``auth_login`` / ``auth_logout`` / ``auth_status``)

    Adding a new ``cmd_foo`` function or a new ``foo)`` case in
    ``bin/beacon`` automatically makes ``beacon foo`` valid for the Skill
    drift check — no manual list update required. This is the ms-61 /
    e-1570 single-source-of-truth principle.
    """
    keys = set()
    for name, obj in list(globals().items()):
        if name.startswith("cmd_") and callable(obj):
            keys.add(name[4:])  # strip "cmd_"
    keys.update(["version", "auth_login", "auth_logout", "auth_status"])
    result = {tuple(k.split("_")) for k in keys}
    result.update(_shell_dispatched_subcommands())
    return result


def _doctor_check_skill_cli_drift(home):
    """Detect Skill markdown that references unknown beacon subcommands.

    Walks ``~/.claude/skills/<name>/*.md`` and extracts every ``beacon X
    [Y [Z]]`` invocation (regex: leading lowercase words after the literal
    "beacon"). For each, longest-prefix-matches the word sequence against
    the canonical set. Unmatched leads -> drift.

    Catches the class of bug behind e-1361 / e-1362 / the retired
    ``beacon summary`` write path: CLI evolved but dependent Skill markdown
    still calls the old command name.

    Returns a list of warning strings (one per drifting Skill). Empty list
    means no drift or no skills directory installed.
    """
    import re as _re
    skills_dir = os.path.join(home, ".claude", "skills")
    if not os.path.isdir(skills_dir):
        return []  # CI / new user — silent skip per ms-61 e-1570 AC#5

    canonical = _canonical_subcommand_set()
    # Match `beacon` followed by 1+ consecutive lowercase command words.
    # Stops at flags (-foo), placeholders (<id>), pipes, uppercase, digits.
    pattern = _re.compile(
        r"\bbeacon\s+([a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*)*)"
    )

    drift_by_skill = {}  # skill_name -> list[(lineno, phrase)]
    for skill_name in sorted(os.listdir(skills_dir)):
        skill_dir = os.path.join(skills_dir, skill_name)
        if not os.path.isdir(skill_dir):
            continue
        for fn in sorted(os.listdir(skill_dir)):
            if not fn.endswith(".md"):
                continue
            fpath = os.path.join(skill_dir, fn)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            seen_in_file = set()  # dedupe per file
            in_fence = False  # fenced code block state across lines
            for lineno, line in enumerate(lines, start=1):
                # Toggle fenced state on ``` or ~~~ delimiter lines.
                stripped = line.lstrip()
                if stripped.startswith("```") or stripped.startswith("~~~"):
                    in_fence = not in_fence
                    continue
                for m in pattern.finditer(line):
                    # Require command context: in a fenced code block,
                    # or wrapped in inline backticks. Prose like
                    # "beacon repo root" is then ignored.
                    if not in_fence:
                        prefix = line[:m.start()]
                        if prefix.count("`") % 2 == 0:
                            continue
                    raw_words = m.group(1).split()
                    # Normalize: hyphenated commands act as underscored
                    # (e.g., "workspace-cleanup" -> ["workspace", "cleanup"]).
                    # cmd_milestone_workspace_cleanup -> ("milestone",
                    # "workspace", "cleanup") in canonical, so the Skill
                    # form "beacon milestone workspace-cleanup" must
                    # normalize to the same tuple.
                    words = []
                    for w in raw_words:
                        words.extend(w.split("-"))
                    matched = False
                    for i in range(len(words), 0, -1):
                        if tuple(words[:i]) in canonical:
                            matched = True
                            break
                    if matched:
                        continue
                    phrase = f"beacon {m.group(1)}"
                    key = (phrase,)
                    if key in seen_in_file:
                        continue
                    seen_in_file.add(key)
                    drift_by_skill.setdefault(skill_name, []).append(
                        (lineno, phrase)
                    )

    if not drift_by_skill:
        return []

    warnings = []
    for skill_name, drifts in sorted(drift_by_skill.items()):
        sample = drifts[:3]
        more = len(drifts) - len(sample)
        sample_lines = "\n".join(
            f"       L{ln}: {phrase}" for ln, phrase in sample
        )
        more_line = f"\n       (+{more} more unique reference(s))" if more > 0 else ""
        warnings.append(
            f"WARN [skill-cli-drift] Skill `{skill_name}` references unknown beacon command(s):\n"
            f"{sample_lines}{more_line}\n"
            "       The leading word(s) after `beacon` don't match any known\n"
            "       subcommand (cmd_* function or lambda dispatch entry).\n"
            "       Either the CLI dropped that command, or the Skill markdown\n"
            "       has a typo / out-of-date example. Opt-out: set\n"
            "       BEACON_DOCTOR_SKIP_SKILL_DRIFT=1."
        )
    return warnings


def _doctor_check_project_staleness():
    """No-op since ms-84 Phase 5 (e-2039): the project-stale check is gone.

    Background: this check existed because cloud mode kept a local
    ``.beacon/project.json`` cache that could drift from the cloud truth
    source. ms-84 Phase 3 cut that cache over (= cloud mode no longer has
    a local project.json; cloud.json is the only on-disk marker), so the
    "stale" concept stops applying — there is no local cache to compare
    against the cloud document anymore.

    Keeping the function as a no-op (rather than deleting it outright)
    avoids breaking any in-flight Skill / doctor caller that imports the
    name. The doctor entry that ran it (= ``warnings.extend(...)``) was
    removed from cmd_doctor; this stub stays as a tombstone.
    """
    return []


def _doctor_check_claude_md_principle_marker():
    """Detect when CLAUDE.md lacks the entry-writing-principle marker.

    ms-68 / e-1640: the principle (= `entry-writing-principle` CORE doc) must
    live in CLAUDE.md so every session / every tool call has it in context.
    If the marker `<!-- BEACON_ENTRY_WRITING_PRINCIPLE -->` is missing, the
    section was either never installed (legacy projects predating ms-68) or
    silently dropped by a manual edit. Warning-level only — never block.

    Returns a list of warning strings. Empty list = OK, or skip (no CLAUDE.md
    in cwd, which is not a beacon-managed project).
    """
    claude_md = "CLAUDE.md"
    marker = "<!-- BEACON_ENTRY_WRITING_PRINCIPLE -->"

    # No CLAUDE.md = not a beacon-managed project from this cwd, skip silently.
    if not os.path.exists(claude_md):
        return []

    try:
        with open(claude_md, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []

    if marker in content:
        return []

    return [
        "WARN [entry-writing-principle] CLAUDE.md lacks the entry writing principle section.\n"
        f"       Missing marker: {marker}\n"
        "       Beacon's task/spec/doc readers include non-developers — the principle\n"
        "       (reader-first 1-line / loanword tiers / ID context / no truncated sentences)\n"
        "       must live in CLAUDE.md so it is in context for every session.\n"
        "       Run: beacon common-setup   (re-installs CLAUDE.md beacon section)"
    ]


def _doctor_check_cloud_state_consistency():
    """ms-61 / e-1776: surface cloud.json ↔ config.json mode-field divergence.

    Background: cloud mode has two on-disk state files in ``.beacon/``:

      - ``cloud.json`` — present iff this project is bound to a cloud
        Firestore document. e-1861 (ms-61) made *cloud.json existence the
        sole source of truth* for ``get_store()`` routing, so any CLI call
        looks at this file and only this file when deciding cloud vs local.
      - ``config.json["mode"]`` — legacy hint that historically toggled
        cloud / local mode. Retired in e-1861 because a sub-agent could
        silently rewrite it to ``{"mode": "local"}`` and flip every
        subsequent CLI call back to LocalStore without touching cloud.json,
        manifesting as apparent user data loss (2026-06-15 incident).

    Even though the runtime ignores ``config.json["mode"]``, a divergence
    between the two surfaces is still a structural signal that something
    is being narrated wrong. Two failure modes specifically:

      (1) cloud.json exists + config.json mode != "cloud"
          → CLI is reading cloud (correct), but config.json claims local.
            Humans and AI reading the on-disk state by hand will be misled,
            and the symptom mirrors the original e-1861 silent failure
            class. Surface a louder warning than the simple
            ``legacy-mode-field`` heads-up so the Web UI ↔ CLI parity
            confusion is named explicitly.

      (2) cloud.json absent + config.json mode == "cloud"
          → config.json narrates cloud but ``get_store()`` will route to
            LocalStore (cloud.json is missing). The next ``beacon auth``
            / cloud API call will silently degrade or fail in non-obvious
            ways. Surface as a separate broken-cloud-binding warning.

    Both warnings include:
      - the ``config.json`` mtime (= last time the mode field was rewritten,
        the only proxy we have for "when did the divergence appear"), and
      - a one-line repair command suggestion.

    Returns a list of warning strings; empty list = consistent or n/a.

    AC mapping (e-1776):
      AC #1: case (1) above.
      AC #2: case (2) above.
      AC #3: config.json mtime is appended to each warning body.
      AC #4: each warning ends with a one-line repair command.
    """
    cloud_json_path = os.path.join(".beacon", "cloud.json")
    config_json_path = os.path.join(".beacon", "config.json")

    cloud_exists = os.path.exists(cloud_json_path)
    config_exists = os.path.exists(config_json_path)

    # Both absent (typical fresh checkout) or only cloud.json present
    # without any config.json mode hint: nothing to reconcile.
    if not config_exists:
        return []

    try:
        with open(config_json_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
    except Exception:
        # Unreadable config.json is reported elsewhere; we can't read mode.
        return []

    if not isinstance(config_data, dict):
        return []

    mode = config_data.get("mode")
    # No mode field present at all = nothing to diverge on.
    if mode is None:
        return []

    # AC #3: mtime breadcrumb — when did the mode field last get rewritten?
    # Best-effort; if stat fails, we just omit the timestamp line.
    try:
        import datetime as _dt
        _mtime = os.path.getmtime(config_json_path)
        mtime_str = _dt.datetime.fromtimestamp(_mtime).isoformat(timespec="seconds")
        mtime_line = f"       config.json last modified: {mtime_str} (= proxy for last mode switch)\n"
    except Exception:
        mtime_line = ""

    warnings: list[str] = []

    if cloud_exists and mode != "cloud":
        # AC #1: cloud.json present, but config.json says non-cloud mode.
        # CLI reads cloud correctly (e-1861), but humans / AI eyeballing the
        # on-disk state are misled — same shape as the original 2026-06-15
        # silent failure class.
        warnings.append(
            "WARN [cloud-state-divergence] cloud.json exists but config.json has `mode`=" + repr(mode) + ".\n"
            "       The runtime reads cloud (cloud.json existence is the sole source\n"
            "       of truth since e-1861), but the on-disk narration is split.\n"
            "       Humans / AI reading state by hand may believe this project is\n"
            "       local-only and report Web UI ↔ CLI output as a data-loss bug.\n"
            + mtime_line +
            "       Repair (drop the stale mode hint):\n"
            "         python3 -c 'import json,pathlib;p=pathlib.Path(\".beacon/config.json\");"
            "d=json.loads(p.read_text());d.pop(\"mode\",None);p.write_text(json.dumps(d,indent=2))'"
        )
    elif (not cloud_exists) and mode == "cloud":
        # AC #2: config.json claims cloud but cloud.json is gone — get_store()
        # will route to LocalStore and any cloud API path will silently
        # degrade.
        warnings.append(
            "WARN [cloud-state-divergence] config.json `mode`='cloud' but .beacon/cloud.json is missing.\n"
            "       The runtime routes to LocalStore (cloud.json absence wins since\n"
            "       e-1861), so cloud auth / cloud API paths will silently degrade\n"
            "       or fail with confusing errors.\n"
            + mtime_line +
            "       Repair options (pick one):\n"
            "         beacon cloud upload-initial         # if this should be cloud\n"
            "         python3 -c 'import json,pathlib;p=pathlib.Path(\".beacon/config.json\");"
            "d=json.loads(p.read_text());d.pop(\"mode\",None);p.write_text(json.dumps(d,indent=2))'"
            "   # if this should be local"
        )

    return warnings


def _doctor_check_ms81_state_machine():
    """ms-81 e-1921: surface the four state-machine warnings named in SPEC AC #9.

    Each is warning-level only; the state machine is a forcing function,
    not enforcement. Returns a list of warning strings.

    Checks:
      A. active or observing MS missing assignee — work is happening but
         nobody's name is on it; audit trail loses provenance.
      B. done MS still has a worktree directory at `.worktrees/<branch>/`
         — another session could check it out and accidentally commit
         against a closed branch.
      C. occupation field present but `session_id` empty — a half-written
         claim that nothing can release; usually a sign of a crash that
         left the field stale.
      D. waiting MS has commits/PRs being attached after it was paused —
         we approximate this by checking whether the most recent commit
         entry's `created_at` is *after* `meta.waiting_at`.
    """
    warnings: list[str] = []
    try:
        data = load_project()
    except Exception:
        return warnings

    try:
        import branch as _branch
    except Exception:
        _branch = None

    cwd_root = os.getcwd()
    a_hits: list[str] = []
    b_hits: list[str] = []
    c_hits: list[str] = []
    d_hits: list[str] = []

    for ms in data.get("milestones", []):
        ms_id = ms.get("id", "")
        title = ms.get("title", "")
        status = ms.get("status", "")
        # A: active / observing without assignee
        if status in ("in_progress", "active", "observing"):
            assignee = ms.get("assignee", "")
            if not assignee or (
                isinstance(assignee, list) and not any(a.strip() for a in assignee)
            ):
                a_hits.append(f"{ms_id} ({status}, {title[:60]})")
        # B: done MS with leftover worktree
        if status == "done" and _branch is not None:
            try:
                branch_name = _branch.ms_branch_name(ms_id, title)
                if os.path.exists(os.path.join(cwd_root, ".worktrees", branch_name)):
                    b_hits.append(f"{ms_id} (worktree: .worktrees/{branch_name})")
            except Exception:
                pass
        # C: occupation field present but stale shape (no session_id)
        occ = ms.get("occupation")
        if occ and not occ.get("session_id"):
            c_hits.append(f"{ms_id} (occupation present without session_id)")
        # D: waiting MS with commits attached after the wait transition
        if status == "waiting":
            waiting_at = ms.get("meta", {}).get("waiting_at", "")
            if waiting_at:
                for e in ms.get("entries", []):
                    if e.get("type") in ("commit", "pr"):
                        created = e.get("created_at", "") or e.get("date", "")
                        if created and created > waiting_at:
                            d_hits.append(
                                f"{ms_id} ({title[:50]}) — {e.get('type')} "
                                f"[{e.get('id')}] attached after waiting_at"
                            )
                            break

    def _fmt(label, code, hits, suggestion):
        if not hits:
            return None
        return (
            f"WARN [{code}] {label}:\n"
            + "\n".join(f"       - {h}" for h in hits[:8])
            + (f"\n       (+{len(hits) - 8} more)" if len(hits) > 8 else "")
            + f"\n       {suggestion}"
        )

    for w in (
        _fmt(
            "Active/observing milestones without an assignee",
            "ms81-assignee-missing",
            a_hits,
            "Run: beacon milestone update <ms-id> --assignee <name> "
            "(or `beacon milestone join <ms-id>` to self-add).",
        ),
        _fmt(
            "Done milestones with a leftover worktree directory",
            "ms81-leftover-worktree",
            b_hits,
            "Run: beacon milestone workspace-cleanup <ms-id> to remove the "
            "worktree; leftover dirs are a takeover-by-mistake hazard.",
        ),
        _fmt(
            "Occupation field stuck without a session_id",
            "ms81-stale-occupation",
            c_hits,
            "Run: beacon milestone release <ms-id> to clear the half-written "
            "occupation marker.",
        ),
        _fmt(
            "Waiting milestones with commits/PRs attached after wait_at",
            "ms81-waiting-write",
            d_hits,
            "Re-activate with `beacon milestone start <ms-id>` before adding "
            "more commit / PR entries — the write gate (e-1916) catches new "
            "writes but does not retroactively clean up past attachments.",
        ),
    ):
        if w:
            warnings.append(w)
    return warnings


def _doctor_probe_beacon_version(binary_path: str) -> str:
    """Return the raw `--version` stdout for a beacon binary, or '?' on failure.

    Used by the e-1170 multi-binary surface and the e-2276
    selected-version-too-old gate. Subprocess timeout is intentionally
    short (3s) — doctor must remain responsive even when a stale binary
    hangs on stdin.
    """
    import subprocess as _subprocess
    try:
        return _subprocess.run(
            [binary_path, "--version"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip() or "?"
    except Exception:
        return "?"


def _doctor_parse_version(raw: str):
    """Return ``(major, minor, patch)`` parsed from a `beacon <ver>` string.

    Accepts both `beacon 0.48.0` and `0.48.0` shapes. Returns ``None``
    when the string does not contain a parseable semver triple — the
    caller treats that as "version unknown" and skips the
    selected-version-too-old gate, never blocking on a probe failure.
    """
    import re
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _doctor_probe_missing_subcommands(binary_path: str, subs: tuple) -> tuple:
    """Return tuple of `subs` that the binary rejects as 'Unknown command'.

    Probes ``<binary> <sub> --help`` for each subcommand. The probe
    returns no missing subs (= empty tuple) when the subprocess fails for
    transport reasons (timeout, ENOENT) — we don't want a flaky subprocess
    to fire a false bus-sessions-unavailable signal. Only an authoritative
    'Unknown command' line (printed by cmd dispatcher) is treated as
    missing.
    """
    import subprocess as _subprocess
    missing = []
    for sub in subs:
        try:
            r = _subprocess.run(
                [binary_path, sub, "--help"],
                capture_output=True, text=True, timeout=3,
            )
            combined = (r.stdout or "") + (r.stderr or "")
            # The CLI prints `Unknown command: <name>` (lib/commands.py
            # main dispatch) — pin on that exact phrase to avoid false
            # positives from unrelated stderr like deprecation notices.
            if "Unknown command" in combined:
                missing.append(sub)
        except Exception:
            # Subprocess failure (timeout / ENOENT) → can't say, skip.
            continue
    return tuple(missing)


def cmd_doctor():
    """Lightweight environment health check for Beacon.

    Checks (in order):
      1. beacon binary is on PATH
      2. ~/.claude/settings.json has PostToolUse hook configured
      3. Required Skills are installed in ~/.claude/skills/
      4. Token expiry (JWT decode — no network required)
      5. .beacon/cloud.json exists and has api_url set (if project is cloud-mode)
      6. Repo skills/ content matches installed ~/.claude/skills/ content
         (ms-10 e-722; only when running from a beacon source checkout).
      7. Duplicate IDs (Issue #14).
      8. Skill markdown references known beacon subcommands (ms-61 / e-1570).
      9. .beacon/project.json staleness vs cloud (ms-61 / e-1571).
     10. CLAUDE.md entry-writing-principle marker (ms-68 / e-1640).
     11. ms-81 state-machine warnings (e-1921).
     12. cloud.json ↔ config.json state-file divergence (ms-61 / e-1776).

    Only prints warnings for problems found. Exits 0 if all checks pass,
    exits 1 if at least one warning was emitted.
    """
    import shutil as _shutil
    import time as _time

    # ms-93 / e-2276: --json flag emits structured signals (Codex wrapper /
    # Skill gate parses these to drive 'use BEACON_BIN=<abs path>' UX).
    json_mode = "--json" in sys.argv[2:] if len(sys.argv) > 2 else False

    home = _user_home()
    warnings: list[str] = []
    # path_signals carries rich metadata that the Codex wrapper / Skill
    # need to act on PATH problems decisively (= which binaries, which
    # version, which subcommands are missing). The 3 codes are pinned by
    # tests/test_doctor_path_signals.py.
    path_signals: list[dict] = []
    # ``warning_to_signal_idx`` maps a warning's index in ``warnings`` to
    # the index in ``path_signals`` that already encodes it with rich
    # metadata. Used by --json mode to emit each warning exactly once
    # (rich signal wins when a mapping exists).
    warning_to_signal_idx: dict = {}

    # ------------------------------------------------------------------ #
    # 1. beacon on PATH (+ e-1170 version shadowing detect +
    #    e-2276 selected-version-too-old + bus-sessions-unavailable)
    # ------------------------------------------------------------------ #
    beacon_paths = _find_all_on_path("beacon")
    if not beacon_paths:
        warnings.append(
            "WARN [PATH] `beacon` not found on PATH.\n"
            "       Add the beacon bin/ directory to your PATH, or use\n"
            "       the full path to the beacon script."
        )
        warning_to_signal_idx[len(warnings) - 1] = len(path_signals)
        path_signals.append({
            "code": "beacon-not-on-path",
            "category": "PATH",
            "severity": "WARN",
            "message": "beacon binary not found on PATH",
            "metadata": {},
        })
    else:
        # Always probe primary version (used by both the multiple-binaries
        # warning and the version-too-old gate).
        import subprocess as _subprocess
        primary = beacon_paths[0]
        primary_version_raw = _doctor_probe_beacon_version(primary)
        primary_version_parsed = _doctor_parse_version(primary_version_raw)

        if len(beacon_paths) > 1:
            # e-1170: multiple beacon binaries on PATH = potential version
            # shadowing. Today's Win user case: stale ~/.local/bin 0.11.1
            # shadowing AppData/.../Scripts 0.19.0, so `beacon --version`
            # returned the old value silently and post-install confusion was
            # massive. Surface the shadow chain with versions for each.
            version_lines = []
            binaries_meta = []
            for bp in beacon_paths:
                v_raw = _doctor_probe_beacon_version(bp)
                version_lines.append(f"         {bp}  →  {v_raw}")
                binaries_meta.append({"path": bp, "version": v_raw})
            warnings.append(
                f"WARN [PATH] Multiple `beacon` binaries on PATH ({len(beacon_paths)} found):\n"
                + "\n".join(version_lines)
                + f"\n       Primary (will be used): {primary}\n"
                "       This is usually fine, but if `beacon --version` looks stale\n"
                "       relative to your last upgrade, the entry earlier on PATH is\n"
                "       shadowing a newer install. Remove or reorder PATH to put the\n"
                "       intended install first."
            )
            warning_to_signal_idx[len(warnings) - 1] = len(path_signals)
            path_signals.append({
                "code": "multiple-binaries",
                "category": "PATH",
                "severity": "WARN",
                "message": f"Multiple `beacon` binaries on PATH ({len(beacon_paths)} found); primary={primary}",
                "metadata": {
                    "binaries": binaries_meta,
                    "primary": primary,
                },
            })

        # e-2276 (a): selected-version-too-old. The Codex DM dogfood
        # (2026-06-25) confirmed that homebrew's stale 0.2.1 shadowing a
        # current /Users/.../beacon/bin (v0.48.0) leaves the user with a
        # primary that lacks `bus` / `sessions` entirely. We fire even when
        # there is only a single binary on PATH — an old solo install is
        # equally broken.
        min_required = os.environ.get("BEACON_DOCTOR_MIN_VERSION", "0.30.0")
        min_required_parsed = _doctor_parse_version(f"beacon {min_required}")
        if (
            primary_version_parsed is not None
            and min_required_parsed is not None
            and primary_version_parsed < min_required_parsed
        ):
            warnings.append(
                f"WARN [PATH] Selected `beacon` (primary on PATH) is older than required:\n"
                f"         path:     {primary}\n"
                f"         version:  {primary_version_raw}\n"
                f"         required: >= {min_required}\n"
                "       This often means a stale install (homebrew, ~/.local/bin) is\n"
                "       shadowing a newer beacon further down PATH. Set BEACON_BIN to\n"
                "       the absolute path of the newer install, or reorder PATH."
            )
            warning_to_signal_idx[len(warnings) - 1] = len(path_signals)
            path_signals.append({
                "code": "selected-version-too-old",
                "category": "PATH",
                "severity": "WARN",
                "message": f"Primary beacon on PATH ({primary_version_raw}) is older than required >= {min_required}",
                "metadata": {
                    "primary": primary,
                    "primary_version": primary_version_raw,
                    "required_min": min_required,
                },
            })

        # e-2276 (c): bus-sessions-unavailable. Probe the primary binary
        # directly. If `beacon bus --help` or `beacon sessions --help`
        # returns "Unknown command", the primary cannot fulfil DM /
        # session discovery — even if its version string looks acceptable.
        # This is the authoritative subcommand-presence check; the
        # version-too-old gate above is a fast proxy.
        missing_subs = _doctor_probe_missing_subcommands(primary, ("bus", "sessions"))
        if missing_subs:
            warnings.append(
                "WARN [PATH] Selected `beacon` is missing required subcommands:\n"
                f"         path:           {primary}\n"
                f"         missing:        {', '.join(missing_subs)}\n"
                "       Codex / Claude Code Beacon integration relies on `bus` and\n"
                "       `sessions`. Set BEACON_BIN to an absolute path of a recent\n"
                "       beacon (v0.30.0+), or reorder PATH."
            )
            warning_to_signal_idx[len(warnings) - 1] = len(path_signals)
            path_signals.append({
                "code": "bus-sessions-unavailable",
                "category": "PATH",
                "severity": "WARN",
                "message": f"Primary beacon on PATH lacks subcommands: {', '.join(missing_subs)}",
                "metadata": {
                    "primary": primary,
                    "missing_subcommands": list(missing_subs),
                },
            })

    # ------------------------------------------------------------------ #
    # 2. PostToolUse hook in ~/.claude/settings.json
    # ------------------------------------------------------------------ #
    settings_path = os.path.join(home, ".claude", "settings.json")
    hook_ok = False
    hook_broken_cmd = ""  # ms-44 e-853: beacon hook present but not runnable here
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as _f:
                _settings = json.load(_f)
            _post_tool = _settings.get("hooks", {}).get("PostToolUse", [])
            for _entry in _post_tool:
                for _h in _entry.get("hooks", []):
                    _cmd = _h.get("command", "")
                    if "beacon" not in _cmd:
                        continue
                    # A beacon hook is configured — but is it runnable on this
                    # OS? A .sh on Windows (or a path that no longer exists)
                    # silently no-ops, so /beacon-log never fires. (e-853)
                    if _hook_unusable_on_windows(_cmd) or (
                        _is_path_command(_cmd) and not os.path.exists(_cmd)
                    ):
                        hook_broken_cmd = _cmd
                        continue
                    hook_ok = True
                    break
                if hook_ok:
                    break
        except Exception:
            pass
    if not hook_ok and hook_broken_cmd:
        warnings.append(
            "WARN [hooks] PostToolUse hook is configured but not executable on this OS:\n"
            f"       {hook_broken_cmd}\n"
            "       (a .sh hook does not run on Windows; commit -> /beacon-log will not fire.)\n"
            "       Run: beacon skill install   (re-points it to the cross-platform hook)"
        )
    elif not hook_ok:
        warnings.append(
            "WARN [hooks] PostToolUse hook not configured in ~/.claude/settings.json.\n"
            "       Run: beacon skill install"
        )

    # ------------------------------------------------------------------ #
    # 2b. HOOK_MANIFEST completeness (ms-160 e-5807)
    # ------------------------------------------------------------------ #
    # The check above only confirms SOME runnable beacon PostToolUse hook exists.
    # But beacon installs a whole set across several events (commit / save / halt
    # / postcompact / stop / session-start / bus-inbox); a partial install — e.g.
    # a `beacon skill install` from before e-5806 left the MCP save hook and the
    # bus-inbox receive hook unwired — leaves the PostToolUse check green while
    # DM receive / auto-save / STOP silently no-op. Check every manifest entry so
    # a partial install surfaces instead of hiding. (installer manifest × doctor
    # 照合 — the other half of the e-5806 forcing function.)
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as _f:
                _all_hooks = json.load(_f).get("hooks", {})
            missing_hooks = [spec["key"] for spec in HOOK_MANIFEST
                             if not _manifest_hook_present(_all_hooks, spec)]
        except Exception:
            missing_hooks = []
        if missing_hooks:
            warnings.append(
                "WARN [hooks] Beacon hooks missing from ~/.claude/settings.json: "
                + ", ".join(missing_hooks) + "\n"
                "       (your install path did not wire these; the matching "
                "receive / auto-save / STOP behaviour silently no-ops.)\n"
                "       Run: beacon skill install"
            )

    # ------------------------------------------------------------------ #
    # 3. Required Skills installed
    # ------------------------------------------------------------------ #
    required_skills = ["beacon-log", "beacon-session-start", "beacon-session-end"]
    claude_skills = os.path.join(home, ".claude", "skills")
    for _skill in required_skills:
        _skill_dir = os.path.join(claude_skills, _skill)
        if not os.path.isdir(_skill_dir):
            warnings.append(
                f"WARN [skills] Skill `{_skill}` not installed.\n"
                "       Run: beacon skill install"
            )

    # ------------------------------------------------------------------ #
    # 4. Token expiry (no network — JWT decode only)
    # ------------------------------------------------------------------ #
    from auth import _credentials_path, _decode_jwt_expiry
    _cred_path = _credentials_path()
    if _cred_path.exists():
        try:
            with open(_cred_path, "r", encoding="utf-8") as _f:
                _creds = json.load(_f)
            _token = _creds.get("token") or _creds.get("id_token") or ""
            if _token:
                _exp = _creds.get("token_expiry") or _decode_jwt_expiry(_token)
                if _exp:
                    _now = int(_time.time())
                    _remaining = _exp - _now
                    if _remaining < 0:
                        warnings.append(
                            "WARN [token] Credentials have expired.\n"
                            "       Run: beacon auth login"
                        )
                    elif _remaining < 300:  # less than 5 minutes
                        warnings.append(
                            f"WARN [token] Credentials expire in {_remaining}s (< 5 min).\n"
                            "       Run: beacon auth login"
                        )
        except Exception:
            pass
    else:
        warnings.append(
            "WARN [token] Not logged in (no credentials file found).\n"
            "       Run: beacon auth login"
        )

    # ------------------------------------------------------------------ #
    # 5. cloud.json present and valid (only when in a beacon project dir)
    # ------------------------------------------------------------------ #
    # e-1861 (ms-61): cloud.json existence is the sole source of truth for
    # cloud mode. config.json's ``mode`` field was retired because it could
    # be silently overwritten by a sub-agent to flip cloud → local without
    # touching cloud.json (2026-06-15 incident). doctor now:
    #   (a) validates cloud.json shape directly when present, and
    #   (b) surfaces any legacy ``mode`` field still in config.json as a
    #       gentle migration warning (non-fatal, ignored by the runtime).
    cloud_json_path = os.path.join(".beacon", "cloud.json")
    config_json_path = os.path.join(".beacon", "config.json")
    if os.path.exists(cloud_json_path):
        try:
            with open(cloud_json_path, "r", encoding="utf-8") as _f:
                _cloud = json.load(_f)
            if not _cloud.get("api_url"):
                warnings.append(
                    "WARN [cloud.json] api_url is not set in .beacon/cloud.json.\n"
                    "       Run: beacon cloud upload-initial"
                )
        except Exception:
            warnings.append(
                "WARN [cloud.json] .beacon/cloud.json is unreadable.\n"
                "       Run: beacon cloud upload-initial"
            )
    if os.path.exists(config_json_path):
        try:
            with open(config_json_path, "r", encoding="utf-8") as _f:
                _config = json.load(_f)
            if isinstance(_config, dict) and "mode" in _config:
                warnings.append(
                    "WARN [legacy-mode-field] .beacon/config.json still contains a `mode` field.\n"
                    "       This field is ignored as of e-1861 (ms-61) — cloud.json existence\n"
                    "       is now the sole source of truth. The field is harmless but can be\n"
                    "       removed manually for cleanliness."
                )
        except Exception:
            pass  # config.json unreadable — not a fatal error

    # ------------------------------------------------------------------ #
    # 6. Duplicate IDs (Issue #14)
    # ------------------------------------------------------------------ #
    # We use load_project_unsafe so doctor can still surface the very
    # problem strict load refuses to load. If we can't load at all (no
    # project, network error in cloud mode, etc.), we silently skip this
    # check — doctor is best-effort for environment health, not a
    # project linter.
    try:
        _data = load_project_unsafe()
        dup_report = core.find_duplicate_ids(_data)
        for category_label, key in (
            ("milestone", "milestones"),
            ("entry", "entries"),
            ("operation", "operations"),
        ):
            for did, n in dup_report.get(key, {}).items():
                if category_label == "milestone":
                    fix_cmd = f"beacon milestone purge {did} --reason \"...\" --index <n>"
                elif category_label == "entry":
                    fix_cmd = f"beacon entry purge {did} --reason \"...\" --index <n>"
                elif category_label == "operation":
                    fix_cmd = f"beacon operation purge {did} --reason \"...\" --index <n>"
                else:
                    fix_cmd = f"contact maintainers (corrupt {category_label})"
                warnings.append(
                    f"WARN [dup-id] Duplicate {category_label} ID '{did}' "
                    f"appears {n} times (Issue #14).\n"
                    f"       Recovery: {fix_cmd}"
                )
    except Exception:
        pass  # Project not loadable from this CWD — skip dup check.

    # ------------------------------------------------------------------ #
    # 7. Repo skills/ vs installed ~/.claude/skills/ drift (ms-10 e-722)
    # ------------------------------------------------------------------ #
    # This check only runs when we can find the drift script — i.e. running
    # from a beacon source checkout. brew / pipx users don't have scripts/
    # in their install tree and would get nothing meaningful from the check.
    _beacon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _drift_script = os.path.join(_beacon_root, "scripts", "check-skill-drift.py")
    if os.path.isfile(_drift_script):
        try:
            _r = subprocess.run(
                ["python3", _drift_script, "--json"],
                capture_output=True, text=True, timeout=10,
            )
            _report = json.loads(_r.stdout) if _r.stdout.strip() else {"ok": True, "drift": []}
            if not _report.get("ok", True):
                _drift = _report.get("drift", [])
                _names = [d.get("name", "?") for d in _drift]
                _summary = ", ".join(_names[:5])
                if len(_names) > 5:
                    _summary += f", +{len(_names) - 5} more"
                warnings.append(
                    "WARN [skills-drift] repo skills/ and ~/.claude/skills/ differ.\n"
                    f"       Affected: {_summary}\n"
                    "       Run: beacon skill install"
                )
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            # Drift check is best-effort; never let it block doctor.
            pass

    # ------------------------------------------------------------------ #
    # 8. Skill ↔ CLI command drift (ms-61 / e-1570)
    # ------------------------------------------------------------------ #
    # Walk each ~/.claude/skills/<name>/*.md and verify that any
    # `beacon X [Y ...]` invocation hits a known subcommand. Catches the
    # case where a CLI command was renamed / removed but dependent Skills
    # weren't touched (e.g., the retired `beacon summary` write path that
    # /beacon-session-end was still calling pre-e-1364).
    if os.environ.get("BEACON_DOCTOR_SKIP_SKILL_DRIFT") != "1":
        warnings.extend(_doctor_check_skill_cli_drift(home))

    # ------------------------------------------------------------------ #
    # 9. project-stale check retired in ms-84 Phase 5 (e-2039)
    # ------------------------------------------------------------------ #
    # Cloud mode no longer keeps a local .beacon/project.json cache after
    # Phase 3, so the concept of "stale local relative to cloud" no longer
    # applies. The helper above is now a no-op tombstone; the doctor call
    # is left commented for audit-trail clarity.
    # warnings.extend(_doctor_check_project_staleness())  # ms-84 Phase 5

    # ------------------------------------------------------------------ #
    # 10. CLAUDE.md entry-writing-principle marker (ms-68 / e-1640)
    # ------------------------------------------------------------------ #
    # Detect when CLAUDE.md is missing the marker that anchors the
    # entry-writing-principle section. Without it, the principle drops
    # out of every-session context and AI silently breaks reader-first /
    # loanword / ID-context / no-truncation rules. Warning-level only.
    if os.environ.get("BEACON_DOCTOR_SKIP_PRINCIPLE_MARKER") != "1":
        warnings.extend(_doctor_check_claude_md_principle_marker())

    # ------------------------------------------------------------------ #
    # 11. ms-81 state-machine warnings (e-1921)
    # ------------------------------------------------------------------ #
    # Surface the four SPEC §9 conditions where the state-machine and
    # occupation model is being violated — warning-level only because the
    # whole MS is designed as a forcing function, not a wall.
    if os.environ.get("BEACON_DOCTOR_SKIP_MS81") != "1":
        warnings.extend(_doctor_check_ms81_state_machine())

    # ------------------------------------------------------------------ #
    # 12. cloud.json ↔ config.json state-file divergence (ms-61 / e-1776)
    # ------------------------------------------------------------------ #
    # Two on-disk state files (`.beacon/cloud.json` and
    # `.beacon/config.json["mode"]`) can narrate cloud vs local
    # independently. e-1861 made cloud.json existence the sole runtime
    # truth, but a stale config.json mode field still produces silent
    # "Web UI shows the doc / CLI says Document not found" confusion when
    # humans inspect state by hand. Surface the divergence loudly, name
    # the symptom class, and suggest a one-line repair (AC #1–#4 of
    # e-1776). Warning-level only; never block.
    if os.environ.get("BEACON_DOCTOR_SKIP_CLOUD_STATE") != "1":
        warnings.extend(_doctor_check_cloud_state_consistency())

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    if json_mode:
        # ms-93 / e-2276: Codex wrapper / Skill gate parses this. Each
        # warning string is converted to a minimal structured entry so
        # callers can branch on `code`. Warnings that have a paired rich
        # path_signal (= tracked in warning_to_signal_idx) use that
        # signal instead of the generic parse — rich metadata wins.
        structured: list[dict] = []
        for i, w in enumerate(warnings):
            rich_idx = warning_to_signal_idx.get(i)
            if rich_idx is not None:
                structured.append(path_signals[rich_idx])
            else:
                structured.append(_doctor_warning_to_signal(w))
        print(json.dumps({"ok": not warnings, "signals": structured}, ensure_ascii=False))
        sys.exit(1 if warnings else 0)

    if warnings:
        for w in warnings:
            print(w)
        sys.exit(1)
    else:
        print("OK: all checks passed.")
        sys.exit(0)


def _doctor_warning_to_signal(warn_text: str) -> dict:
    """Parse a `WARN [tag] ...` string into a minimal structured signal.

    Used by `beacon doctor --json` to convert legacy `warnings: list[str]`
    entries into ``{code, category, severity, message}`` dicts for
    machine consumption (= ms-93 / e-2276 Codex wrapper). PATH signals
    have a richer parallel list (``path_signals``) that overrides this
    minimal form when the code matches.
    """
    import re
    m = re.match(r"WARN \[([^\]]+)\]\s*(.*)", warn_text, re.DOTALL)
    if not m:
        return {
            "code": "unknown",
            "category": "other",
            "severity": "WARN",
            "message": warn_text.strip(),
        }
    tag, body = m.group(1), m.group(2).strip()
    # First line is the title; subsequent lines are detail.
    first_line = body.split("\n", 1)[0].strip()
    return {
        "code": tag,
        "category": tag,
        "severity": "WARN",
        "message": first_line,
    }


def _help_registry():
    """Single source of truth for the CLI command reference (ms-120 e-3897).

    Every help surface — machine-readable `beacon help --json`, the top-level
    `beacon --help` text, and each subcommand's `--help` — renders from THIS
    one list. There is no second hand-maintained help text to drift against
    (AX 原則 1/3: ヘルプが信頼できないと AI は毎回ソースを読む羽目になる /
    原則 6: 乖離は検出でなく構造で不能にする)。
    """
    return [
        {"command": "beacon init", "flags": [], "description": "Initialize .beacon/ in current directory"},
        {"command": "beacon setup", "flags": [], "description": "First-time setup wizard (auth + hooks + project)"},
        {"command": "beacon status", "flags": ["--json", "--ms <id>"], "description": "Show current status"},
        {"command": "beacon milestone add", "flags": [], "description": "Add a new milestone (interactive)"},
        {"command": "beacon milestone list", "flags": ["--json"], "description": "List milestones"},
        {"command": "beacon milestone start <id>", "flags": ["--no-branch", "--no-assignee"], "description": "Activate milestone + auto-create ms-XX-<slug> branch + self-add as assignee"},
        {"command": "beacon milestone join <id>", "flags": ["--checkout"], "description": "Add self as assignee on a milestone (and optionally switch to its branch)"},
        {"command": "beacon milestone close <id>", "flags": [], "description": "Close milestone"},
        {"command": "beacon milestone observe <id>", "flags": [], "description": "Set milestone to observing"},
        {"command": "beacon milestone wait <id>", "flags": ["--reason"], "description": "Pause an active/observing milestone (ms-81)"},
        {"command": "beacon milestone release <id>", "flags": [], "description": "Release occupation claim without changing status (ms-81)"},
        {"command": "beacon milestone occupations", "flags": ["--ms <id>", "--json"], "description": "List occupation event log (ms-81)"},
        {"command": "beacon milestone rename <id> <title>", "flags": [], "description": "Rename a milestone"},
        {"command": "beacon milestone depends <id> --on <id>", "flags": [], "description": "Declare milestone dependency"},
        {"command": "beacon milestone purge <id> --reason <text>", "flags": ["--index <n: 1-based, which duplicate to remove>", "--json"], "description": "Hard-delete a milestone record (recovery for duplicate-ID corruption; Issue #14). --index is required only when the id appears more than once; it selects which copy (1-based)."},
        {"command": "beacon entry purge <e-id> --reason <text>", "flags": ["--index <n: 1-based, which duplicate to remove>", "--json"], "description": "Hard-delete an entry record (recovery for duplicate-ID corruption; e-863). --index is required only when the id appears more than once; it selects which copy (1-based)."},
        {"command": "beacon operation purge <op-id> --reason <text>", "flags": ["--index <n: 1-based, which duplicate to remove>", "--json"], "description": "Hard-delete an operation record (recovery for duplicate-ID corruption; e-863). --index is required only when the id appears more than once; it selects which copy (1-based)."},
        {"command": "beacon operation create <title>", "flags": ["--status <todo|open>", "--schedule <daily|weekdays|weekly>", "--log-source <name>", "--activation-hint <text>", "--objective <text>", "--acceptance-criteria <text>", "--priority <p>"], "description": "Create an Operation (--status open to start active; e-3907 — alias: operation open)"},
        {"command": "beacon incident add <title> -o <op-id>", "flags": ["--desc <text>", "--priority <p>"], "description": "Create an incident under an Operation (e-3907 — alias: incident open)"},
        {"command": "beacon operation approve <op-id> --spec <doc-id>", "flags": ["--expires-at YYYY-MM-DD", "--ttl-seconds N", "--json"], "description": "Mint T2 envelope from SPEC doc's approved_actions list (ms-60 / e-1339)"},
        {"command": "beacon operation revoke <op-id>", "flags": ["--envelope-id ENV", "--reason TEXT", "--json"], "description": "Invalidate the active T2 envelope (auto-picks active if --envelope-id omitted)"},
        {"command": "beacon operation envelope verify <op-id> <action>", "flags": ["--json"], "description": "AI self-check: is <action> permitted by the active envelope's approved_actions? (ms-60 / e-1340)"},
        {"command": "beacon milestone graph", "flags": ["--json"], "description": "Show dependency graph"},
        {"command": "beacon target review-request <target-id>", "flags": ["--new-state <s>", "--old-state <s>", "--intent <text>", "--evidence e-1,e-2"], "description": "Request human approval for a target transition (目的達成レビュー; ms-119)"},
        {"command": "beacon target approve <entry-id>", "flags": ["--rationale <text>", "--reason <text>"], "description": "Approve a pending target transition (= executes the transition; --reason alias of --rationale, e-3906)"},
        {"command": "beacon target reject <entry-id>", "flags": ["--rationale <text>", "--reason <text>"], "description": "Reject a pending target transition (= transition does NOT execute; --reason alias of --rationale, e-3906)"},
        {"command": "beacon target list", "flags": ["--target <id>", "--pending", "--json"], "description": "List target transition-approval requests"},
        {"command": "beacon review context --type <ax|philosophy>", "flags": ["--pr <n>", "--diff-ref <base...head>", "--origin-doc <doc-id>", "--mode diff"], "description": "Emit the review-kernel bundle (原典 + mechanical diff only) for an independent judge subagent (ms-119 e-3947; see /beacon-review-run)"},
        {"command": "beacon task add <desc>", "flags": ["-m <ms-id>"], "description": "Add a task to a milestone"},
        {"command": "beacon task done <entry-id>", "flags": [], "description": "Mark task as done"},
        {"command": "beacon task list", "flags": ["--json", "--ms <id>"], "description": "List tasks"},
        {"command": "beacon task update <entry-id>", "flags": ["--ms <ms-id>", "--description <text>", "--status <s>", "--detail <text>", "--motivation <text>", "--acceptance-criteria <text>", "--behavior <text>", "--priority <p>"], "description": "Update task fields (description / status / detail / motivation / acceptance_criteria / behavior / priority) or move to another milestone"},
        {"command": "beacon log [message]", "flags": ["--prepare", "--finalize", "-m <ms-id>", "--progress <n>", "--summary <text>"], "description": "Record HEAD commit to active milestone"},
        {"command": "beacon dm send", "flags": ["--to <sid>", "--to-user <uid>", "--payload <json>", "--in-reply-to <eid>", "--manual", "--recipient-confirmed", "--action <name>", "--project <id>", "--json"], "description": "Send a DM (canonical DM verb; delegates to bus send --channel dm; e-3899)"},
        {"command": "beacon dm respond <approve|deny> <event_id>", "flags": ["--project <id>", "--json"], "description": "Decide a pending cross-user DM action envelope (receiver-side; ms-70)"},
        {"command": "beacon dm audit", "flags": ["--limit <n>", "--project <id>", "--json"], "description": "Read the DM-approval audit log (e-3899 canonical; alias: dm log)"},
        {"command": "beacon dm sent", "flags": ["--limit <n>", "--project <id>", "--json"], "description": "List DMs THIS session sent with receipt (sent/delivered/opened) + ⚠dup marker (sender-side; ms-141 e-4966)"},
        # ms-106 ② — sales job-template entities (profession=sales projects)
        {"command": "beacon account add <name>", "flags": ["--health <text>", "--assignee <user>"], "description": "Add a sales account (顧客; 対象・継続)"},
        {"command": "beacon account list", "flags": ["--json", "--as-project <id>", "--linked"], "description": "List sales accounts (+contacts); --as-project shows only accounts disclosed to that project (fail-closed); --linked pulls accounts disclosed to THIS project from other org projects (cloud mode)"},
        {"command": "beacon account contact add <acc-id> <name>", "flags": ["--role <text>", "--email <text>", "--phone <text>"], "description": "Add a contact (担当者) nested under an account (e-3907; bare `contact` form is a deprecated alias)"},
        {"command": "beacon account phase <acc-id> <phase>", "flags": ["--note <text>"], "description": "Declare an account lifecycle phase transition (append-only)"},
        {"command": "beacon account rename <acc-id> <new-name>", "flags": [], "description": "Rename an account (顧客名の変更)"},
        {"command": "beacon account assign <acc-id> <user>", "flags": [], "description": "Set the 担当ユーザー (assignee) on an account"},
        {"command": "beacon account nurturing <acc-id> <desc>", "flags": ["--deadline <date>", "--ball self|counterpart"], "description": "Add a nurturing (継続関係の業務; 商談なし顧客向け)"},
        {"command": "beacon disclose <resource-id> --to-project <id>", "flags": ["--to-project <id>"], "description": "Disclose any Target (account/opportunity/milestone/…) to another project so its members can reference it (cross-project 開示)"},
        {"command": "beacon undisclose <resource-id> --from-project <id>", "flags": ["--from-project <id>"], "description": "Revoke a Target's disclosure to a project (剥奪即時、query 時評価)"},
        {"command": "beacon account delete <acc-id>", "flags": ["--force"], "description": "Delete an account (--force orphans referencing opportunities)"},
        {"command": "beacon opportunity add <title>", "flags": ["--account <acc-id>", "--phase <p>", "--goal <n>", "--probability <n>", "--deadline <date>", "--ball self|counterpart", "--assignee <user>"], "description": "Add a sales opportunity (商談; 対象・有限)"},
        {"command": "beacon opportunity assign <opp-id> <user>", "flags": [], "description": "Set the 担当ユーザー (assignee) on an opportunity"},
        {"command": "beacon opportunity amount <opp-id> <amount>", "flags": [], "description": "Set an opportunity's 金額 (goal_amount, 円)"},
        {"command": "beacon opportunity describe <opp-id> <text>", "flags": [], "description": "Set an opportunity's 背景/経緯/メモ (free-text; empty clears)"},
        {"command": "beacon opportunity rename <opp-id> <new-title>", "flags": [], "description": "Rename an opportunity's title (e-3909; parallels milestone rename)"},
        {"command": "beacon acquisition add <title>", "flags": ["--description <text>", "--assignee <user>"], "description": "Add a 顧客獲得ターゲット (取引先の無い有限の獲得・準備作業の器; 営業専用)"},
        {"command": "beacon acquisition list", "flags": ["--json"], "description": "List 顧客獲得ターゲット with their lifecycle status"},
        {"command": "beacon acquisition start <acq-id>", "flags": [], "description": "Move a 顧客獲得ターゲット to in_progress (着手). Named lifecycle verb (ms-120 e-3907); status stays read-only."},
        {"command": "beacon acquisition done <acq-id>", "flags": [], "description": "Mark a 顧客獲得ターゲット done (完了)."},
        {"command": "beacon acquisition delete <acq-id>", "flags": ["--reason <text>", "--acknowledge"], "description": "打ち切り: soft-cancel a 顧客獲得ターゲット (tombstone, audit kept). Discontinuation = deletion, not a status (ms-132 e-4507). Aliases: cancel, rm. Requires --reason or --acknowledge (account/opportunity delete と同じ監査ゲート)."},
        {"command": "beacon acquisition attack-list <acq-id> <title>", "flags": ["--phases <a,b,c>", "--json"], "description": "Attach a typed アタックリスト (table-doc: 対象顧客/打診フェーズ/最終接触日/メモ) to a 顧客獲得ターゲット (ms-132)"},
        {"command": "beacon acquisition attack-lists <acq-id>", "flags": ["--json"], "description": "List a 顧客獲得ターゲット's アタックリスト with per-phase counts (ms-132)"},
        {"command": "beacon acquisition attack-list-fill <doc-id>", "flags": ["--account-phase <name>", "--assignee <user>", "--name-contains <s>", "--limit <n>", "--dry-run", "--json"], "description": "Bulk-register 未接触 Accounts matching a query into an アタックリスト (dedup, --dry-run preview) (ms-132)"},
        {"command": "beacon acquisition attack-list-send <doc-id>", "flags": ["--subject <s>", "--message-file <f>", "--message <body>", "--from-phase <name>", "--limit <n>", "--confirm", "--json"], "description": "Plan (dry-run) a bulk outreach to prospects; --confirm = the single human authorization (bus-refused). Send itself is Skill-driven (ms-132 承認境界)"},
        {"command": "beacon acquisition attack-list-send-record <doc-id> <acc-id>", "flags": ["--message-id <id>", "--message-file <f>", "--message <body>", "--url <permalink>", "--subject <s>", "--json"], "description": "Record one sent email inside an authorized batch (message digest must match the confirmed 文面): 証跡 + row 未接触→連絡済 (ms-132)"},
        {"command": "beacon acquisition attack-list-awaiting-reply <doc-id>", "flags": ["--json"], "description": "List prospects awaiting a reply (rows at 連絡済 with their sent message-id) — the reply-watch worklist (ms-132)"},
        {"command": "beacon acquisition attack-list-reply-record <doc-id> <acc-id>", "flags": ["--message-id <id>", "--url <link>", "--summary <s>", "--json"], "description": "Record a detected prospect reply: inbound 証跡 + row 連絡済→返信あり + notify (detection-only) (ms-132)"},
        {"command": "beacon acquisition attack-list-promote <doc-id> <acc-id>", "flags": ["--title <商談名>", "--json"], "description": "Promote a reacted (返信あり/アポ) prospect to an Opportunity + drive Account phase 未接触→リード (lead conversion) (ms-132)"},
        {"command": "beacon opportunity phase-prob <phase> <n>", "flags": [], "description": "Set a phase's 成約率 (win probability 0-100; per-company funnel tuning)"},
        {"command": "beacon sales target <user> <amount>", "flags": [], "description": "Set a member's 目標売上 (sales quota; empty amount clears)"},
        {"command": "beacon sales target list", "flags": ["--json"], "description": "List members' 目標売上 with their 見込み売上 (weighted pipeline)"},
        {"command": "beacon opportunity list", "flags": ["--json"], "description": "List sales opportunities with phase / status / account"},
        {"command": "beacon opportunity phase <opp-id> <phase>", "flags": ["--note <text>"], "description": "Declare a phase transition (append-only phase_history; master=人間)"},
        {"command": "beacon opportunity transition-date <opp-id> <YYYY-MM-DD>", "flags": ["--note <text>", "--clear"], "description": "Set the 遷移日 (judgement date) for the current phase (append-only transition_date_history)"},
        {"command": "beacon opportunity anchor <opp-id> <work-item-id>", "flags": [], "description": "Bind a meeting or activity (mtg-/act-) as the 発火源 of the open 前進ゲート; its completion fires the phase judgement (idempotent, ownership-checked)"},
        {"command": "beacon opportunity judge <opp-id> advance|retry|terminal", "flags": ["--note <text>"], "description": "Judge a reached 遷移日 (3-way: 次へ/やり直し/決着; human-confirmed, master=人間)"},
        {"command": "beacon opportunity due", "flags": ["--json"], "description": "List due/overdue 商談の遷移日 and 準備活動の期日 (transition_date + activity.deadline)"},
        {"command": "beacon deadline due", "flags": ["--json"], "description": "職種横断で期日 到達/超過 の work item を surface (milestone target_date / task・activity deadline を occupation イテレータ経由で 1 経路化)"},
        {"command": "beacon opportunity activity <opp-id> <desc>", "flags": ["--deadline <date>", "--ball self|counterpart"], "description": "Add an activity (業務・事前計画型) under an opportunity"},
        {"command": "beacon opportunity delete <opp-id>", "flags": [], "description": "Delete an opportunity and its activities"},
        {"command": "beacon communication add <target-id>", "flags": ["--direction inbound|outbound", "--channel <free-text: email/slack/messenger/line/in-person/…>", "--source-ref <id>", "--source-url <link>", "--occurred <datetime>"], "description": "Record a communication (証跡・事後記録型 = 営業の Commit); target is opp-/acc- or act-/nrt- (act-/nrt- links the activity/nurturing it fulfilled); channel is free-text for off-pipeline media"},
        {"command": "beacon communication list <target-id>", "flags": ["--json"], "description": "List communications (証跡) oldest→newest + derived ball; act-/nrt- lists only that work item's"},
        {"command": "beacon communication cancel <comm-id>", "flags": ["--reason <text>"], "description": "Cancel (取消) a mis-recorded communication — soft (status=cancelled + reason, kept struck-through); excluded from ball/watch derivation but shown in the log"},
        {"command": "beacon communication retarget <comm-id> <new-target>", "flags": [], "description": "Re-file a communication onto the correct target/work item (opp-/acc-/act-/nrt-); moves it — the evidence itself (summary/source/direction) is unchanged"},
        {"command": "beacon meeting schedule <opp-id>", "flags": ["--at <datetime>", "--end <datetime>", "--location <text>", "--event-id <id>", "--calendar-ns <ns>", "--calendar-account <acct>", "--set-transition"], "description": "Book a meeting (面談) with a Beacon 識別 ID; --set-transition moves the 遷移日 to the meeting date"},
        {"command": "beacon meeting reschedule <mtg-id>", "flags": ["--at <datetime>", "--end <datetime>", "--event-id <id>", "--set-transition"], "description": "Move a meeting (予定変更); --set-transition follows the 遷移日"},
        {"command": "beacon meeting end <mtg-id>", "flags": [], "description": "Mark a meeting ended (idempotent; used by the end-detector Operation)"},
        {"command": "beacon meeting cancel <mtg-id>", "flags": [], "description": "Cancel a scheduled meeting"},
        {"command": "beacon meeting list [<opp-id>]", "flags": ["--json"], "description": "List meetings; <opp-id> optional — omit to list across all opportunities (e-3909)"},
        {"command": "beacon meeting list-ended", "flags": ["--now <datetime>", "--json"], "description": "List meetings whose scheduled end has passed but are still scheduled (終了検知 Operation C の候補; e-3909 canonical read verb, alias: meeting ended)"},
        {"command": "beacon phase list", "flags": ["--json"], "description": "Show the configured phase funnels (account / opportunity / prospect vocabulary)"},
        {"command": "beacon phase add <account|opportunity|prospect> <name>", "flags": ["--index <n>"], "description": "Add or insert a funnel stage"},
        {"command": "beacon phase rename <account|opportunity|prospect> <old> <new>", "flags": [], "description": "Rename a funnel stage (references follow)"},
        {"command": "beacon phase move <account|opportunity|prospect> <name> <index>", "flags": [], "description": "Reorder a funnel stage"},
        {"command": "beacon phase remove <account|opportunity|prospect> <name>", "flags": [], "description": "Delete a funnel stage (blocked if non-empty)"},
        {"command": "beacon save <desc>", "flags": ["-m <ms-id>", "--hash <hash>", "--source manual", "--json"], "description": "Save a freeform entry to a milestone"},
        {"command": "beacon sync", "flags": [], "description": "Auto-sync recent git commits to active milestone"},
        {"command": "beacon summary <text>", "flags": [], "description": "Update project summary"},
        {"command": "beacon doc add", "flags": ["--scope <core|spec|memo>", "--ms <id>", "--title <title>", "--content <text>", "--stdin"], "description": "Add a document"},
        {"command": "beacon doc list", "flags": ["--json", "--scope <scope>", "--ms <id>"], "description": "List documents"},
        {"command": "beacon doc show <doc-id>", "flags": [], "description": "Show document content"},
        {"command": "beacon doc update <doc-id>", "flags": ["--content <text>", "--stdin"], "description": "Update document content"},
        {"command": "beacon doc image-upload <local-file>", "flags": ["--json"], "description": "Upload image, get markdown img tag"},
        {"command": "beacon doc table create <title>", "flags": ["--columns <json>", "--scope <scope>", "--ms <id>", "--op <id>", "--target <id>", "--id <slug>", "--json"], "description": "Create a typed table-doc (行×列の構造化ドキュメント)"},
        {"command": "beacon doc table add-row <doc-id>", "flags": ["--cells <json>", "--json"], "description": "Append a row to a table-doc (type-checked, history-seeded)"},
        {"command": "beacon doc table set-cell <doc-id> <row-id> <col> <val>", "flags": ["--value <v>", "--json"], "description": "Update a cell; old value kept in append-only history"},
        {"command": "beacon doc table rm-row <doc-id> <row-id>", "flags": ["--json"], "description": "Soft-delete a row (tombstone; audit trail survives)"},
        {"command": "beacon doc table show <doc-id>", "flags": ["--json"], "description": "Render a table-doc as a markdown table"},
        {"command": "beacon pr add", "flags": ["-m <ms-id>", "--url <url>", "--intent <text>"], "description": "Record a PR entry"},
        {"command": "beacon pr approve <entry-id>", "flags": ["--rationale <text>", "--no-auto-done", "--json"], "description": "Approve a PR (auto-dones bound tasks at HIGH confidence; --no-auto-done to opt out)"},
        {"command": "beacon pr reject <entry-id>", "flags": [], "description": "Reject a PR"},
        {"command": "beacon pr merge <entry-id>", "flags": [], "description": "Mark PR as merged"},
        {"command": "beacon pr sync", "flags": ["--dry-run", "--json"], "description": "Align beacon PR entries with GitHub state (merged/closed) — ms-61 / e-2005"},
        {"command": "beacon retro", "flags": [], "description": "Start weekly retrospective (interactive)"},
        {"command": "beacon trigger check", "flags": [], "description": "Check pending triggers (JSON array)"},
        {"command": "beacon cloud list", "flags": [], "description": "List cloud projects"},
        {"command": "beacon cloud upload-initial", "flags": ["--force"], "description": "Initial bootstrap upload to a new cloud project (one-shot local→cloud migration; ms-84 Phase 4)"},
        {"command": "beacon cloud migrate-from-local", "flags": ["--confirm", "--force-after-review"], "description": "Retire a stale .beacon/project.json that survived a prior cloud cut-over (pre-flight verifies cloud has every local entry; ms-95 / e-2339)"},
        # ms-84 Phase 4 (e-2038): push / pull / force-pull entries removed.
        # The cloud → local round-trip is structurally impossible (= cloud
        # is the sole truth source). bin/beacon now routes these names to
        # the wildcard 'unknown subcommand' branch.
        {"command": "beacon cloud join <id>", "flags": [], "description": "Join an existing cloud project"},
        {"command": "beacon auth login", "flags": [], "description": "Sign in with Google"},
        {"command": "beacon auth logout", "flags": [], "description": "Remove cached credentials"},
        {"command": "beacon auth status", "flags": [], "description": "Show login status"},
        {"command": "beacon skill install", "flags": [], "description": "Install Claude Code Skills to ~/.claude/skills/"},
        {"command": "beacon monitor context", "flags": ["--dry-run"], "description": "Stop hook: context-usage threshold monitor (e-854); --dry-run skips note/state writes"},
        # ms-69 e-1652+: Trek = cross-project / cross-session collaboration area
        {"command": "beacon trek create <title>", "flags": ["--type temporary|persistent (default persistent)", "--description <text>", "--goal-state <criterion>", "--json"], "description": "Create a trek (cross-project協奏作業領域). Requires an identified live session (the caller's session/email becomes the trek leader, ms-69) — run it from a real bclaude session, not a bare script. --goal-state は ms-75/e-1865 完了マーカー"},
        {"command": "beacon trek list", "flags": ["--status <s>", "--include-archived", "--all-actors", "--joined", "--json"], "description": "List treks visible to the caller. --joined で自分が join 済の trek だけ"},
        {"command": "beacon trek show <trek-id>", "flags": ["--all", "--json"], "description": "Show trek detail + 集約ビュー (task / commit / doc, ms-75/e-1864). --all で cap 解除"},
        {"command": "beacon trek review-verdicts <trek-id> <task-id>", "flags": ["--json"], "description": "leader_review target の verdict 集合を返す (completion / halt-rescue を halt_reason で分岐、ms-128/e-4374)。/beacon-trek-review が呼ぶ単一 source"},
        {"command": "beacon trek timeline <trek-id>", "flags": ["--limit N", "--json"], "description": "Trek の lifecycle / commit / task done / doc を時系列で参照 (ms-75/e-1867)"},
        {"command": "beacon trek start <trek-id>", "flags": ["--json"], "description": "Transition trek planning → active"},
        {"command": "beacon trek archive <trek-id>", "flags": ["--json"], "description": "Archive trek (= terminal); restart by creating a new one"},
        {"command": "beacon trek invite <trek-id> --actor <email>", "flags": ["--notify", "--json"], "description": "Invite a user (by email) into the trek"},
        {"command": "beacon trek join <trek-id>", "flags": ["--json"], "description": "Accept own invitation"},
        {"command": "beacon trek leave <trek-id>", "flags": ["--json"], "description": "Remove self from the trek (leader must transfer first)"},
        {"command": "beacon trek plan <trek-id>", "flags": ["--add-scope <project:ref>", "--remove-scope <project:ref>", "--goal-state <criterion>", "--json"], "description": "Edit trek scope or goal_state (ms-75/e-1865)"},
        {"command": "beacon trek scope-add <trek-id>", "flags": ["--project <pid>", "--milestone <ms-id>", "--operation <op-id>", "--json"], "description": "Canonical scope-add verb (= flag-style alias of plan --add-scope, ms-97/AC23 e-2626). Target = target-entity; legacy task refs are auto-migrated to their parent milestone at read (ms-128 方針3)"},
        {"command": "beacon trek scope-approve <trek-id> <pending-id>", "flags": ["--json"], "description": "Commit a staged scope op (= apply add or remove, ms-97/AC25 e-2611)"},
        {"command": "beacon trek scope-reject <trek-id> <pending-id>", "flags": ["--json"], "description": "Drop a staged scope op (ms-97/AC25)"},
        {"command": "beacon trek blanket-approve <trek-id> --category <cat>", "flags": ["--json"], "description": "Pre-approve scope-add for a category (ms-97/AC24 e-2603). Category: operation | milestone | task | project:<pid> | milestone:<ms-id>"},
        {"command": "beacon trek blanket-revoke <trek-id> --category <cat>", "flags": ["--json"], "description": "Drop a blanket pre-approval (ms-97/AC24)"},
        {"command": "beacon trek slot add <trek-id>", "flags": ["--project <pid>", "--milestone <ms-id>", "--operation <op-id>", "--children e-A,e-B", "--json"], "description": "(ms-99/e-2829) Stage a slot-add with a fresh sl-<8hex> id. Target = target-entity (milestone/operation); --task is rejected (ms-128 方針3, narrow via --children)"},
        {"command": "beacon trek slot amend <trek-id> <slot-id>", "flags": ["--add-child <e-id>", "--remove-child <e-id>", "--json"], "description": "(ms-99/e-2829) Stage a child-list edit on an existing MS slot"},
        {"command": "beacon trek slot claim <trek-id> <slot-id>", "flags": ["--session <sid>", "--unclaim", "--json"], "description": "(ms-99/e-2829) Stage a claim stamp (state-free, SPEC 方針 4)"},
        {"command": "beacon trek slot list <trek-id>", "flags": ["--json"], "description": "(ms-99/e-2829) Materialise slot rows for the trek"},
        {"command": "beacon trek stop <trek-id>", "flags": ["--reason <text>", "--json"], "description": "Pull the Andon cord (= halt signal, sessions pause)"},
        {"command": "beacon trek resume <trek-id>", "flags": ["--json"], "description": "Clear the halt signal"},
        {"command": "beacon trek transfer-leader <trek-id> --to <session-id>", "flags": ["--json"], "description": "Hand off leader_session_id to another session"},
        {"command": "beacon trek take-over <trek-id>", "flags": ["--json"], "description": "Fresh session (= same user, leader role) を新 leader_session_id に bind し直す (= dead session 引き継ぎ、ms-88 e-2089)"},
        {"command": "beacon trek kickoff <trek-id>", "flags": ["--session-id <sid>", "--kickoff-dm-event-id <eid>", "--json"], "description": "Kickoff Ritual の完了 stamp (= /beacon-trek-pulse Step 0.4 が叩く、 ms-88 e-2138)。 server endpoint だけ先に land した PR #177 の CLI wrapper 補完 (e-2139 残作業 #1)"},
        {"command": "beacon trek reconcile <trek-id>", "flags": ["--apply", "--json"], "description": "task pool ↔ Trek stamp 同期の reconcile (= 「pool で done だが Trek stamp が waiting-review / leader_review / working で残ってる」 stuck 状態を一括修復、 default dry-run、 ms-88 e-2167)"},
        # ms-54 e-1266: DM channel lifecycle commands (install / uninstall / opt-out / opt-in / status)
        {"command": "beacon channel install", "flags": [], "description": "Install beacon-bus MCP entry into .mcp.json (DM channel for multi-session messaging)"},
        {"command": "beacon channel uninstall", "flags": ["--purge-files", "--keep-files"], "description": "Remove beacon-bus MCP entry; --purge-files also wipes channel/node_modules (moved to .trash/)"},
        {"command": "beacon channel opt-out", "flags": ["--project", "--global"], "description": "Block all install / auto-install attempts (persistent flag)"},
        {"command": "beacon channel opt-in", "flags": ["--project", "--global"], "description": "Lift the opt-out flag at project or global scope"},
        {"command": "beacon channel status", "flags": [], "description": "Show install / files / opt-out / next-action state in one screen"},
        # ms-55 e-1736: coordination signals (= 走る / 止まる両輪).
        # SPEC `bnzTXhu6KYIMfVE2Ivy2` for the design; landed in
        # e-1646 (stop) / e-1647 (rollback) / e-1648 (claim) /
        # e-1649 (stuck) / e-1650 (morning).
        {"command": "beacon stop scoped <target>", "flags": ["--kind ms|task|session", "--reason-kind <k>", "--reason <text>", "--json"], "description": "Broadcast a STOP signal at a single MS / task / session (Andon cord — anyone can halt)"},
        {"command": "beacon stop global", "flags": ["--reason-kind <k>", "--reason <text>", "--json"], "description": "Broadcast STOP across every active autonomous session (everything-stops fallback)"},
        {"command": "beacon stop status", "flags": ["--json"], "description": "Show the latest stop / resume state from the stop-signal channel"},
        {"command": "beacon resume scoped <target>", "flags": ["--kind ms|task|session", "--reason <text>", "--json"], "description": "Clear a scoped STOP, allowing the targeted session(s) to resume work"},
        {"command": "beacon resume global", "flags": ["--reason <text>", "--json"], "description": "Clear a global STOP across every autonomous session"},
        {"command": "beacon rollback", "flags": ["--commits N", "--reason <text>", "--dry-run", "--no-record", "--json"], "description": "Undo working tree (git stash) + N local commits (--soft reset); push past upstream → report-only with compensation proposals"},
        {"command": "beacon claim request <kind>:<id>", "flags": ["--intent <text>", "--json"], "description": "Announce intent to take a target (ms/task/operation/trek/free); other sessions can respond"},
        {"command": "beacon claim respond <claim-id>", "flags": ["--accept|--reject", "--reason <text>", "--json"], "description": "Respond to another session's claim request"},
        {"command": "beacon claim post <kind>:<id>", "flags": ["--intent <text>", "--json"], "description": "Post-hoc record that this session already started on the target (no request/response dance)"},
        {"command": "beacon claim handoff <claim-id> --to <session>", "flags": ["--reason <text>", "--json"], "description": "Transfer an active claim to a different session"},
        {"command": "beacon claim release <claim-id>", "flags": ["--outcome completed|abandoned", "--reason <text>", "--json"], "description": "Release a claim (outcome surfaces in `beacon morning` as 完了 or skip)"},
        {"command": "beacon claim list", "flags": ["--json"], "description": "List active claims from local `.beacon/active_claims.json` (= restart restore path)"},
        {"command": "beacon stuck check", "flags": ["--telemetry-file <path>", "--idle-min N", "--json"], "description": "Detect sessions idle past --idle-min; emit STUCK stop signals so morning briefing surfaces 介入要望"},
        {"command": "beacon morning", "flags": ["--since-hours N", "--events-file <path>", "--no-doc", "--json"], "description": "4-bucket digest of recent autonomous activity (完了 / 停止 / skip / 介入要望); auto-saves as scope=report doc"},
        {"command": "beacon help", "flags": ["--json"], "description": "Show help (--json for machine-readable output)"},
    ]


def cmd_help_json():
    """Output beacon CLI command reference as machine-readable JSON."""
    print(json.dumps({"version": __version__, "commands": _help_registry()},
                     ensure_ascii=False, indent=2))


def _help_command_noun(command: str) -> str:
    """Return the grouping noun for a registry command (2nd token after
    'beacon'), e.g. 'beacon milestone start <id>' -> 'milestone'. Falls back to
    the 1st token for single-verb commands like 'beacon status'."""
    parts = command.split()
    # parts[0] == 'beacon'
    return parts[1] if len(parts) > 1 else command


def _render_command_usage(entry: dict) -> str:
    """One command's usage block: the invocation + its flags + description."""
    cmd = entry["command"]
    flags = entry.get("flags") or []
    line = f"Usage: {cmd}"
    if flags:
        line += " " + " ".join(f"[{f}]" for f in flags)
    return f"{line}\n  {entry.get('description', '').strip()}"


def cmd_help_render():
    """Human-readable help, rendered from _help_registry() (ms-120 e-3897).

    BEACON_HELP_QUERY selects scope:
      - empty       -> the full top-level help, grouped by command noun.
      - "<noun>"    -> every subcommand under that noun (e.g. "milestone").
      - "<noun sub>"-> the single matching command's usage block.

    Matching is prefix-based against the registry's "beacon <command>" strings,
    so both `beacon milestone --help` (noun) and `beacon milestone start --help`
    (specific) resolve without a second lookup table. Unknown queries fall back
    to the full top help rather than erroring (原則 3: 回復経路を残す)。
    """
    registry = _help_registry()
    query = (os.environ.get("BEACON_HELP_QUERY") or "").strip()

    if query:
        needle = query if query.startswith("beacon ") else f"beacon {query}"
        needle_tokens = needle.split()
        matches = []
        for e in registry:
            cmd_tokens = e["command"].split()
            # prefix match: registry command starts with the query tokens
            if cmd_tokens[: len(needle_tokens)] == needle_tokens:
                matches.append(e)
        if len(matches) == 1:
            print(_render_command_usage(matches[0]))
            return
        if len(matches) > 1:
            print(f"beacon {query} — subcommands:\n")
            for e in matches:
                flags = e.get("flags") or []
                suffix = ("  " + " ".join(f"[{f}]" for f in flags)) if flags else ""
                print(f"  {e['command']}{suffix}")
                if e.get("description"):
                    print(f"      {e['description'].strip()}")
            return
        # No registry match for a specific query. Exit 3 (print nothing) so the
        # bash dispatcher can fall through to that command's own --help handling
        # (ms-120 e-3897: bash-only commands like `bus` keep their bespoke help
        # rather than being overridden with the full top-level help).
        sys.exit(3)

    # Full top-level help, grouped by noun in first-seen order.
    print("Beacon - Milestone-driven project management\n")
    print("Usage: beacon <command> [subcommand] [flags]\n")
    groups: dict = {}
    order: list = []
    for e in registry:
        noun = _help_command_noun(e["command"])
        if noun not in groups:
            groups[noun] = []
            order.append(noun)
        groups[noun].append(e)
    for noun in order:
        print(f"{noun}:")
        for e in groups[noun]:
            flags = e.get("flags") or []
            suffix = ("  " + " ".join(f"[{f}]" for f in flags)) if flags else ""
            print(f"  {e['command']}{suffix}")
        print()
    print("Run 'beacon <command> --help' for a single command's flags.")
    print("Run 'beacon help --json' for machine-readable output.")


# ---------------------------------------------------------------------------
# Operation / Run commands (incident → lib/cmd_incident.py, ms-127 e-4320)
# ---------------------------------------------------------------------------


def cmd_run_record():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    batch = os.environ.get("BEACON_RUN_BATCH", "")
    status = os.environ.get("BEACON_RUN_STATUS", "")
    description = os.environ.get("BEACON_RUN_DESC", "")
    if not op_id or not batch or not status:
        print("Error: -o <op-id>, --batch <name>, --status ok|warning|error required")
        sys.exit(1)
    data = load_project()
    op, entry = core.run_record_add(data, op_id, batch=batch, status=status, description=description)
    save_project(data, op={"type": "run_record", "op_id": op_id, "entry_id": entry["id"], "status": status})
    # Clear the operation_check trigger for this op
    trigger_path = os.path.join(_get_triggers_dir(), f"operation_check_{op_id}.json")
    if os.path.exists(trigger_path):
        os.remove(trigger_path)
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(entry, ensure_ascii=False))
    else:
        icon = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(status, "?")
        print(f"Run recorded: {op_id} / {batch} {icon} {status}")
        if description:
            print(f"  {description}")


def cmd_run_list():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: -o <op-id> required")
        sys.exit(1)
    data = load_project()
    for op in data.get("operations", []):
        if op.get("id") == op_id:
            runs = [e for e in op.get("entries", []) if e.get("type") == "run_record"]
            if os.environ.get("BEACON_JSON"):
                print(json.dumps(runs, ensure_ascii=False))
            else:
                for e in runs:
                    icon = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(e.get("status", ""), "?")
                    print(f"{icon} {_local_date(e['date'])} {e.get('batch', '')} — {e.get('description', '')}")
            return
    print(f"Operation not found: {op_id}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Machine API keys (ms-151 / e-5474) — headless machine 認証の鍵の発行 CLI。
# ---------------------------------------------------------------------------
# 鍵の管理 (発行 / 一覧 / 失効) は cloud の owner 限定 endpoint を叩く。発行時だけ
# raw token が返り、以後サーバーは hash しか持たない。人間 owner が端末から鍵を
# 発行し、外部 machine (PE detector Lambda 等) に安全な経路で手渡す運用を想定する。

def _machine_key_error(exc: Exception, project_id: str) -> None:
    """machine key 管理エラーを 1 行で表示して exit する (stacktrace を出さない)。"""
    msg = str(exc)
    if "403" in msg:
        print(
            f"Error: machine key の管理は project {project_id!r} の owner のみ "
            f"可能です。\n  ({msg})",
            file=sys.stderr,
        )
    else:
        print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def cmd_machine_key_issue():
    label = os.environ.get("BEACON_MACHINE_KEY_LABEL", "")
    client, config = _get_api_client()
    project_id = config.get("project_id", "")
    try:
        resp = client.issue_machine_key(project_id, label=label)
    except Exception as exc:  # noqa: BLE001
        _machine_key_error(exc, project_id)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(resp, ensure_ascii=False))
        return
    info = resp.get("machine_key", {})
    raw = resp.get("key", "")
    print(f"machine key を発行しました (project: {project_id})")
    print(f"  key_id: {info.get('key_id', '')}")
    if info.get("label"):
        print(f"  label:  {info.get('label')}")
    print()
    print("  ↓ この鍵は今だけ表示されます。安全な場所に保存してください "
          "(再取得は不可):")
    print(f"  {raw}")


def cmd_machine_key_list():
    client, config = _get_api_client()
    project_id = config.get("project_id", "")
    try:
        resp = client.list_machine_keys(project_id)
    except Exception as exc:  # noqa: BLE001
        _machine_key_error(exc, project_id)
    rows = resp.get("machine_keys", [])
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        print(f"machine key はありません (project: {project_id})")
        return
    for r in rows:
        revoked = r.get("revoked")
        mark = "✗" if revoked else "✓"
        state = "revoked" if revoked else "active"
        label = f" — {r.get('label')}" if r.get("label") else ""
        print(f"{mark} {r.get('key_id', '')} [{state}] "
              f"{r.get('created_at', '')}{label}")


def cmd_machine_key_revoke():
    key_id = os.environ.get("BEACON_MACHINE_KEY_ID", "")
    if not key_id:
        print("Error: <key_id> required.\n"
              "  Example: beacon machine-key revoke <key_id>",
              file=sys.stderr)
        sys.exit(2)
    client, config = _get_api_client()
    project_id = config.get("project_id", "")
    try:
        resp = client.revoke_machine_key(project_id, key_id)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "404" in msg:
            print(
                f"Error: machine key {key_id!r} が project {project_id!r} に "
                f"見つかりません (既に失効済 / key_id 誤り)。",
                file=sys.stderr,
            )
            sys.exit(1)
        _machine_key_error(exc, project_id)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(resp.get("machine_key", {}), ensure_ascii=False))
        return
    print(f"machine key を失効しました: {key_id} (project: {project_id})")


# ---------------------------------------------------------------------------
# Bus (ms-54 / e-999 rendezvous client + e-1135 delivery field)
# ---------------------------------------------------------------------------
# `beacon bus send / listen / receive / ack` give a CLI surface on top of the
# /bus + /bus/unread + /bus/cursors API. They are the rendezvous primitive a
# Claude Code session uses to pause until a peer posts (via harness Monitor or
# ScheduleWakeup). The CLI keeps the daemon-side simple: events are streamed
# as JSON lines to stdout, and Claude Code's Monitor tool turns each line into
# a notification.


# ---------------------------------------------------------------------------
# Bus budget (ms-54 / e-1000) — outbound send-rate gate.
# ---------------------------------------------------------------------------
# The autonomous-DM scenario is "agent gets woken by a Monitor, sees a DM,
# composes a reply, sends, and goes back to sleep — repeat indefinitely".
# Without a structural gate that loop runs until token exhaustion or a runaway
# back-and-forth between two agents. The budget is the gate: a granted
# allowance of N outbound sends, consumed one per `beacon bus send`. When the
# count hits 0, `bus send` refuses with a non-zero exit until a human reissues
# `bus budget grant`.
#
# Storage is intentionally a local file (.beacon/bus-budget.json) rather than
# a Firestore field. Reasons:
#   * the gate is per-machine: if Mac's budget runs out, Windows shouldn't be
#     dragged into the same halt.
#   * a local file lets the harness check/decrement atomically without a
#     cloud round-trip on the hot path (every send would otherwise pay 100ms+).
#   * tamper resistance is a feature of *humans* re-granting the budget, not
#     of the file's storage — see CORE doc data-immutability-principle.
#
# Schema:
#   {"total": N, "used": M, "granted_at": "<ISO>", "channels": ["<ch>", ...]}
# `channels` is reserved for a future per-channel gate; today it's stored as
# an empty list and the budget gates all outbound sends.


# ---------------------------------------------------------------------------
# Bus auto-execute allowlist (ms-54 / e-1145) — receiver-side opt-in for the
# delivery=auto-execute mode.
# ---------------------------------------------------------------------------
# Without this allowlist, any sender can post a bus event with
# delivery=auto-execute and the receiver-side daemon has no structural way to
# refuse. The safety fallback in beacon-bus-inbox-hook.py treats
# auto-execute as propose-to-ai today, but the long-term answer the user wants
# is "default OFF, channel-level opt-in":
#
#   * project.json carries `bus_auto_execute_channels: [str, ...]`. Missing or
#     empty list ⇒ NO channel may auto-execute. Explicit, ordered, audit-able.
#   * The inbox hook reads the list and downgrades any auto-execute event whose
#     channel is not in the allowlist to propose-to-ai, while annotating the
#     context inject so the human sees "this was downgraded for safety".
#   * Adding a channel to the allowlist is itself a deliberate CLI step. We do
#     NOT auto-create the field on every save — the absence of the field is
#     the safe default; presence is the opt-in.
#
# The field lives in project.json (NOT a separate file) so it's covered by the
# v2 meta document migration (e-1209) and synced cross-machine with the rest of
# the project state.


def cmd_dm_respond():
    """Receiver-side decision CLI for a pending DM-action envelope (e-1716).

    Usage shape (= what the user types in their terminal):
      beacon dm respond approve <event_id>
      beacon dm respond deny    <event_id>

    SPEC 設計方針 3 ("承認は terminal Claude Code 内での user 直接判断のみ"):
    this primitive is reached by a human typing the command, never by an
    autonomous AI loop. The server stamps ``decision_by`` from the Bearer
    token's ``sub`` claim — the CLI cannot forge a different actor.

    Args flow through env vars set by ``bin/beacon`` dispatch:
      * ``BEACON_DM_DECISION``  = "approve" | "deny"
      * ``BEACON_DM_EVENT_ID``  = sidecar event_id (= parent bus_event id)
      * ``BEACON_BUS_PROJECT_ID`` = optional --project override (same
        semantics as bus subcmd)
      * ``BEACON_JSON`` = "1" emits the resulting 7-field sidecar row as
        JSON instead of a human summary

    Exit codes:
      * 0 — decision accepted (or idempotent no-op for "same user resubmits
        same decision")
      * 1 — server reported a structural error (404 unknown event_id, 403
        not your envelope, 409 already-decided / auto)
      * 2 — bad CLI usage (missing args)
    """
    decision = os.environ.get("BEACON_DM_DECISION", "").strip().lower()
    event_id = os.environ.get("BEACON_DM_EVENT_ID", "").strip()

    if decision not in ("approve", "deny"):
        print(
            "Usage: beacon dm respond approve <event_id>\n"
            "       beacon dm respond deny    <event_id>\n"
            "  Decide a pending cross-user DM action envelope. Only the\n"
            "  intended receiver (= the addressee on the sidecar) can press\n"
            "  approve/deny; the server enforces this via the Bearer token.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not event_id:
        print(
            "Error: <event_id> required.\n"
            "  Example: beacon dm respond approve evt-abc12345",
            file=sys.stderr,
        )
        sys.exit(2)

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # respects --project override

    try:
        row = client.respond_dm_approval(
            project_id, event_id, decision=decision
        )
    except Exception as e:
        # api_client raises RuntimeError("API error <code>: <detail>") on
        # HTTP errors. Surface a single human line — not a stacktrace —
        # because the human is staring at this terminal.
        msg = str(e)
        if "404" in msg:
            print(
                f"Error: no pending approval for event_id={event_id!r} "
                f"in project {project_id!r}.\n"
                "  Either the envelope was already auto-allowed (legacy "
                "path), the event_id is wrong, or the sidecar has not\n"
                "  landed yet.",
                file=sys.stderr,
            )
        elif "403" in msg:
            print(
                f"Error: event_id={event_id!r} is addressed to a different "
                "user; only the intended receiver can decide it.\n"
                f"  ({msg})",
                file=sys.stderr,
            )
        elif "409" in msg:
            print(
                f"Error: event_id={event_id!r} is already in a terminal "
                "state (approved / denied / auto).\n"
                f"  ({msg})",
                file=sys.stderr,
            )
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(row, ensure_ascii=False))
        return

    # Human-readable summary. Verb + 7-field row in compact form.
    verb_past = "approved" if decision == "approve" else "denied"
    print(f"{verb_past}: event_id={row.get('event_id', event_id)}")
    print(f"  status:      {row.get('approval_status', '')}")
    print(f"  decision_by: {row.get('decision_by', '')}")
    print(f"  decision_at: {row.get('decision_at', '')}")
    print(f"  sender:      {row.get('sender_user_id', '')}")
    print(f"  receiver:    {row.get('receiver_user_id', '')}")


def cmd_dm_log():
    """Audit-history CLI for decided DM-action sidecars (ms-70 / e-1923).

    Usage shape (= what the user types in their terminal):
      beacon dm log [<project_id>] [--limit N] [--json] [--project <id>]

    This is the CLI mirror of the Web UI Settings > Audit table that
    landed in e-1718 (commit 4c5052f). Both surfaces read the same
    server endpoint ``GET /api/projects/{pid}/dm/approval/history`` and
    show the same 6 fields (status / sender / receiver / decision_at /
    decision_by / event_id) — the Web UI as a styled table, the CLI as
    a fixed-column ASCII view sized for a terminal.

    SPEC 設計方針 3 ("承認は terminal Claude Code 内での user 直接判断のみ"):
    this view is read-only by design. The server filters out ``pending``
    and ``auto`` rows so a future contributor cannot wire an
    approve / deny button onto this output.

    Args flow through env vars set by ``bin/beacon`` / ``dispatch.py``:
      * ``BEACON_DM_LOG_PROJECT_ID`` = optional positional project_id
        (= the cloud project to read history from; defaults to the
        cwd's project)
      * ``BEACON_DM_LOG_LIMIT`` = optional row limit (defaults to 50,
        server caps at 500)
      * ``BEACON_BUS_PROJECT_ID`` = ``--project <id>`` override (same
        semantics as the bus / dm respond subcmds)
      * ``BEACON_JSON`` = "1" emits the raw rows list as JSON instead
        of the 6-column ASCII table

    Exit codes:
      * 0 — rows printed (zero rows is success, surfaces an empty-state
        line so a tail of "no decisions yet" is distinguishable from a
        broken pipe)
      * 1 — server reported a structural error (membership 403, 404, etc.)
      * 2 — bad CLI usage (currently unreachable: all flags have defaults)
    """
    # Positional project_id (if any) wins over the cwd default but loses
    # to an explicit --project override (= same precedence as bus subcmds).
    positional_pid = os.environ.get("BEACON_DM_LOG_PROJECT_ID", "").strip()
    limit_raw = os.environ.get("BEACON_DM_LOG_LIMIT", "").strip()
    try:
        limit = int(limit_raw) if limit_raw else 50
    except ValueError:
        print(
            f"Error: --limit must be an integer, got {limit_raw!r}",
            file=sys.stderr,
        )
        sys.exit(2)
    if limit <= 0:
        limit = 50

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    if not project_id and positional_pid:
        project_id = positional_pid
    elif positional_pid and not os.environ.get("BEACON_BUS_PROJECT_ID", "").strip():
        # Positional arg supplied without --project override: positional wins
        # over cwd default. (= matches the help-banner shape.)
        project_id = positional_pid
    if not project_id:
        print(
            "Error: no project_id resolved.\n"
            "  Run from a beacon project cwd, pass `beacon dm log <project_id>`,\n"
            "  or set --project <id>.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        rows = client.list_dm_approval_history(project_id, limit=limit)
    except Exception as e:
        msg = str(e)
        if "403" in msg:
            print(
                f"Error: not a member of project {project_id!r} "
                "(audit history is membership-gated).\n"
                f"  ({msg})",
                file=sys.stderr,
            )
        elif "404" in msg:
            print(
                f"Error: project {project_id!r} not found "
                "(check the project_id).\n"
                f"  ({msg})",
                file=sys.stderr,
            )
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(rows or [], ensure_ascii=False))
        return

    if not rows:
        # Match the Web UI's empty-state copy (renderDmApprovalHistoryRows)
        # so the audit narrative reads the same across both surfaces.
        print("承認履歴はまだありません (= 過去に approve / deny された DM action がない)。")
        return

    # 6-column ASCII table. Widths chosen for typical 100-col terminals:
    # status is short (approved / denied), decision_at is fixed-ish ISO8601,
    # the event_id / user_id columns get truncated with an ellipsis so a
    # too-long Google-OAuth-style sub doesn't wreck the layout. Order
    # matches the AC: event_id / sender / receiver / status / decision_at /
    # decision_by.
    def _trunc(s, n):
        s = "" if s is None else str(s)
        if len(s) <= n:
            return s
        # 3-char ellipsis fits inside the column width; using a single
        # ASCII period × 3 keeps copy-paste safe (no multibyte width math).
        return s[: max(0, n - 3)] + "..."

    # Column widths (= total ~98 chars + 5 separator spaces = ~103). Tweak
    # here, not in renderer logic, so a future column-width A/B test stays
    # readable.
    W_EVT, W_SEN, W_REC, W_STA, W_DEC_AT, W_DEC_BY = 18, 22, 22, 9, 20, 22
    header = (
        f"{'event_id':<{W_EVT}}  "
        f"{'sender':<{W_SEN}}  "
        f"{'receiver':<{W_REC}}  "
        f"{'status':<{W_STA}}  "
        f"{'decision_at':<{W_DEC_AT}}  "
        f"{'decision_by':<{W_DEC_BY}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    for r in rows:
        evt = _trunc(r.get("event_id", ""), W_EVT)
        sen = _trunc(r.get("sender_user_id", ""), W_SEN)
        rec = _trunc(r.get("receiver_user_id", ""), W_REC)
        sta = _trunc(r.get("approval_status", ""), W_STA)
        dat = _trunc(r.get("decision_at", ""), W_DEC_AT)
        dby = _trunc(r.get("decision_by", ""), W_DEC_BY)
        print(
            f"{evt:<{W_EVT}}  "
            f"{sen:<{W_SEN}}  "
            f"{rec:<{W_REC}}  "
            f"{sta:<{W_STA}}  "
            f"{dat:<{W_DEC_AT}}  "
            f"{dby:<{W_DEC_BY}}"
        )
    print(sep)
    print(f"{len(rows)} row(s).")


def _check_recipient_live_health(recipient: str, channel: str) -> None:
    """Back-compat shim around ``_resolve_recipient_live`` (e-1402 contract).

    Pre-e-2280 callers / external tests invoked this for its stderr
    side-effect only. Forward to the resolver but drop the return value
    so the old void contract holds; the new ``cmd_bus_send`` path uses
    the resolver directly and consumes the swapped sid.
    """
    _resolve_recipient_live(recipient, channel)


def cmd_profile_list():
    """List Beacon profiles found under ~/.beacon/profiles/ (ms-64 / e-1461).

    Each profile is a directory containing at least a credentials.json (created
    by ``beacon auth login --profile <name>``) and optionally a profile.json
    with ``api_url`` + ``backend_type`` overrides. The active profile is
    starred so the user can confirm which one would be picked by the current
    --profile / BEACON_PROFILE / cwd .beacon/cloud.json combination.

    Output respects ``BEACON_JSON=1`` for scripting; otherwise prints a
    human-readable list.
    """
    import profile as _profile  # local import: heavy module pulled lazily

    try:
        names = _profile.list_profiles()
    except Exception as exc:
        print(f"Error listing profiles: {exc}")
        sys.exit(1)

    # Resolve active profile name (does not require credentials to exist).
    try:
        active = _profile.resolve_profile_name()
    except Exception:
        active = ""

    if os.environ.get("BEACON_JSON", "") == "1":
        rows = []
        for name in names:
            try:
                p = _profile.load_profile(name)
                rows.append({
                    "name": name,
                    "api_url": p.api_url,
                    "credentials_exists": p.credentials_exist(),
                    "active": name == active,
                })
            except Exception:
                rows.append({"name": name, "active": name == active})
        print(json.dumps(rows, ensure_ascii=False))
        return

    if not names:
        print("(no profiles found)")
        print("Tip: `beacon auth login` will create the 'default' profile.")
        return

    for name in names:
        marker = "*" if name == active else " "
        try:
            p = _profile.load_profile(name)
            creds = "✓" if p.credentials_exist() else " "
            print(f" {marker} {creds} {name:<20} {p.api_url}")
        except Exception:
            print(f" {marker}   {name:<20} (could not load)")


# ---------------------------------------------------------------------------
# Stop signal CLI (ms-55 e-1646)
# ---------------------------------------------------------------------------
#
# `beacon stop` is the user-facing entry point for the halt protocol
# defined in lib/stop_signal.py. It rides on the existing bus event
# transport — the stop event is just another bus event on the
# `stop-signal` channel. The CLI's job is to:
#
#   1. Resolve the issuer's session_id (so the receipt is auditable).
#   2. Build a validated payload via stop_signal.build_stop_payload.
#   3. Post the event via the same api_client used by bus_send.
#
# Authorization mirrors SPEC §2: anyone can broadcast. We do NOT gate
# on envelope tier here — the human typing `beacon stop` is the human
# approval, and AI sessions that auto-emit a stop pass through the
# normal autonomous-action accounting. The halt itself is enforced on
# the receive side (the inbox hook for AI sessions, future Tauri UI for
# human-attended sessions).

def _stop_post_event(*, payload: dict, channel: str = None) -> dict:
    """Common transport for stop/resume events.

    Wraps the same client.post_bus_event call used by cmd_bus_send,
    but without the envelope-issue path: stop signals are explicitly
    not gated by tier (SPEC §2), so a T0 / no-envelope post is the
    correct wire form. This also makes the CLI usable from sessions
    that can't reach the envelope-issue endpoint (= offline, legacy
    server, etc.) — stopping a runaway must not fail because the
    auxiliary endpoint is down.
    """
    from stop_signal import STOP_CHANNEL  # local import to avoid cycle
    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    sender = payload.get("issued_by_session_id", "")
    event = client.post_bus_event(
        project_id, channel or STOP_CHANNEL,
        sender_session_id=sender,
        payload=payload,
        delivery="propose-to-ai",
        envelope=None,
        requested_action=None,
    )
    return event


def cmd_stop_scoped():
    """Broadcast a scoped stop signal.

    Env:
      BEACON_STOP_TARGET_KIND   "ms" | "task" | "session" (required)
      BEACON_STOP_TARGET_ID     id of the target (required)
      BEACON_STOP_REASON        free text (optional)
      BEACON_STOP_REASON_KIND   one of stop_signal.REASON_KINDS (optional)
      BEACON_STOP_MACHINE_REASON  optional JSON-encoded dict
      BEACON_JSON               "1" → json output
    """
    import stop_signal as _stop

    target_kind = os.environ.get("BEACON_STOP_TARGET_KIND", "").strip()
    target_id = os.environ.get("BEACON_STOP_TARGET_ID", "").strip()
    reason = os.environ.get("BEACON_STOP_REASON", "")
    reason_kind = os.environ.get("BEACON_STOP_REASON_KIND", "").strip() or "manual"
    machine_reason_raw = os.environ.get("BEACON_STOP_MACHINE_REASON", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not target_kind or not target_id:
        print(
            "Error: scoped stop requires --target <kind>:<id> "
            "(kind = ms|task|session)",
            file=sys.stderr,
        )
        sys.exit(1)

    machine_reason = None
    if machine_reason_raw:
        try:
            parsed = json.loads(machine_reason_raw)
        except json.JSONDecodeError as e:
            print(f"Error: --machine-reason must be valid JSON ({e})",
                  file=sys.stderr)
            sys.exit(1)
        if not isinstance(parsed, dict):
            print("Error: --machine-reason must be a JSON object",
                  file=sys.stderr)
            sys.exit(1)
        machine_reason = parsed

    sender = _resolve_session_id()
    if not sender:
        print(
            "Error: cannot resolve current session_id (run `beacon session id` "
            "to mint one, or set BEACON_BUS_SENDER explicitly)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        payload = _stop.build_stop_payload(
            scope=_stop.SCOPE_SCOPED,
            issued_by_session_id=sender,
            target_kind=target_kind,
            target_id=target_id,
            reason=reason,
            reason_kind=reason_kind,
            machine_reason=machine_reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _stop_post_event(payload=payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    suffix = f" — {reason}" if reason else ""
    print(
        f"STOP signal (scoped) raised on {target_kind}:{target_id} "
        f"by {sender}{suffix}"
    )
    print(f"  event_id: {event.get('event_id', '?')}")
    print("  All matching sessions will halt after the current tool call.")
    print(f"  Resume with: beacon resume scoped --target {target_kind}:{target_id}")


def cmd_stop_global():
    """Broadcast a global stop signal (= halt every active autonomous
    session in the project).

    Env:
      BEACON_STOP_REASON        free text (recommended)
      BEACON_STOP_REASON_KIND   one of stop_signal.REASON_KINDS (optional)
      BEACON_STOP_MACHINE_REASON  optional JSON-encoded dict
      BEACON_JSON               "1" → json output
    """
    import stop_signal as _stop

    reason = os.environ.get("BEACON_STOP_REASON", "")
    reason_kind = os.environ.get("BEACON_STOP_REASON_KIND", "").strip() or "manual"
    machine_reason_raw = os.environ.get("BEACON_STOP_MACHINE_REASON", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    machine_reason = None
    if machine_reason_raw:
        try:
            parsed = json.loads(machine_reason_raw)
        except json.JSONDecodeError as e:
            print(f"Error: --machine-reason must be valid JSON ({e})",
                  file=sys.stderr)
            sys.exit(1)
        if not isinstance(parsed, dict):
            print("Error: --machine-reason must be a JSON object",
                  file=sys.stderr)
            sys.exit(1)
        machine_reason = parsed

    sender = _resolve_session_id()
    if not sender:
        print(
            "Error: cannot resolve current session_id "
            "(set BEACON_BUS_SENDER explicitly)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        payload = _stop.build_stop_payload(
            scope=_stop.SCOPE_GLOBAL,
            issued_by_session_id=sender,
            reason=reason,
            reason_kind=reason_kind,
            machine_reason=machine_reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _stop_post_event(payload=payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    suffix = f" — {reason}" if reason else ""
    print(f"STOP signal (GLOBAL) raised by {sender}{suffix}")
    print(f"  event_id: {event.get('event_id', '?')}")
    print("  All active autonomous sessions will halt after the current tool call.")
    print("  Resume with: beacon resume global")


def cmd_stop_status():
    """List currently active stop signals.

    Reads recent events on the stop-signal channel via list_unread_bus_events
    (with a synthetic empty recipient so the server returns the full
    channel history rather than a per-recipient cursor view). This avoids
    consuming any real recipient's cursor — `stop status` is observational
    and must not race with receivers.

    Env:
      BEACON_JSON               "1" → json output
      BEACON_STOP_SINCE_HOURS   limit window (default 24)
    """
    import stop_signal as _stop

    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    try:
        client, config = _get_api_client()
    except Exception as e:
        print(f"Error: cannot reach bus ({e})", file=sys.stderr)
        sys.exit(1)
    project_id = _resolve_bus_project_id(config)

    # Pull the recent stop-signal channel history. We use a synthetic
    # recipient so we don't advance any real session's cursor; the
    # server returns the channel slice (most backends honor that).
    try:
        events = client.list_unread_bus_events(
            project_id, "_stop_status_observer",
            channel=_stop.STOP_CHANNEL,
            limit=500,
        )
    except TypeError:
        # Older api_client signature without `limit`. The default page
        # size is typically generous enough for a stop-status snapshot.
        events = client.list_unread_bus_events(
            project_id, "_stop_status_observer",
            channel=_stop.STOP_CHANNEL,
        )
    except Exception as e:
        print(f"Error: cannot list stop-signal events ({e})", file=sys.stderr)
        sys.exit(1)

    actives = _stop.latest_active_stops(events or [])

    if json_mode:
        # Strip raw_event for compactness — callers who need the full event
        # can call `beacon bus receive --channel stop-signal --once`.
        out = []
        for rec in actives:
            r = {k: v for k, v in rec.items() if k != "raw_event"}
            out.append(r)
        print(json.dumps(out, ensure_ascii=False))
        return

    if not actives:
        print("No active stop signals.")
        return

    print(f"Active stop signals ({len(actives)}):")
    for rec in actives:
        scope = rec["scope"]
        target = rec.get("target") or {}
        if scope == "global":
            tag = "GLOBAL"
        else:
            tag = f"{target.get('kind', '?')}:{target.get('id', '?')}"
        line = (
            f"  ⚠ {tag}  reason_kind={rec.get('reason_kind', '?')}  "
            f"by={rec.get('issued_by_session_id', '?')}  "
            f"at={rec.get('issued_at', '?')}"
        )
        print(line)
        if rec.get("reason"):
            print(f"      reason: {rec['reason']}")


def cmd_resume_scoped():
    """Broadcast a resume (= clear stop) for a scoped target.

    Env mirrors cmd_stop_scoped (without machine_reason).
    """
    import stop_signal as _stop

    target_kind = os.environ.get("BEACON_STOP_TARGET_KIND", "").strip()
    target_id = os.environ.get("BEACON_STOP_TARGET_ID", "").strip()
    reason = os.environ.get("BEACON_STOP_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not target_kind or not target_id:
        print(
            "Error: scoped resume requires --target <kind>:<id>",
            file=sys.stderr,
        )
        sys.exit(1)

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _stop.build_resume_payload(
            scope=_stop.SCOPE_SCOPED,
            issued_by_session_id=sender,
            target_kind=target_kind,
            target_id=target_id,
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _stop_post_event(payload=payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    print(
        f"RESUME signal (scoped) raised on {target_kind}:{target_id} "
        f"by {sender}"
    )
    print(f"  event_id: {event.get('event_id', '?')}")


# ---------------------------------------------------------------------------
# Rollback CLI (ms-55 e-1647)
# ---------------------------------------------------------------------------
#
# `beacon rollback` is the SPEC §4 "safe boundary" surface: it undoes
# work that lives entirely inside the local repo (working tree edits,
# un-pushed commits) automatically, and refuses to touch anything past
# the upstream branch. Anything past upstream becomes a "compensation
# proposal" in the report — concrete next-step text like "open a
# revert PR" rather than silent destructive action.

def _rollback_record_history(plan, result, reason: str) -> dict:
    """Record a `beacon rollback` execution as a save entry on the active MS.

    ms-55 e-1727: after `beacon rollback` mutates the working tree (=
    non-dry-run, no fatal errors), persist a structured trail so
    `beacon search "rollback"` surfaces "when did we roll back, what
    did we touch, what compensation was proposed". Without this trail
    the runaway story is lost — stash refs survive but project history
    has no pointer to them.

    The entry uses source="rollback" so the search query is single-keyword,
    and the description packs reason + commit hashes + working-tree count +
    compensation hints. Returns the {status, entry_id, milestone} dict
    from save_entry, or {"status": "error", "error": ...} on failure.
    Failures are non-fatal — the rollback already succeeded.
    """
    try:
        import operations as _ops  # noqa: PLC0415
    except Exception as e:
        return {"status": "error", "error": f"import operations: {e}"}

    state = plan.state
    head_hash = state.head_hash or "?"
    branch = state.branch or "(detached HEAD)"
    upstream = state.upstream_branch or "(no upstream)"

    parts: list[str] = []
    parts.append(f"rollback on {branch} (HEAD~={head_hash}, upstream={upstream})")
    if reason:
        parts.append(f"reason: {reason}")
    if result.stashed:
        parts.append(
            f"stashed {len(state.working_tree_files)} working-tree path(s) → "
            f"{result.stash_ref or 'stash@{0}'}"
        )
    if result.reset_commits > 0:
        parts.append(f"reset {result.reset_commits} local commit(s) (--soft)")
    if not result.stashed and result.reset_commits == 0:
        parts.append("(no-op: clean tree, no local commits)")
    if plan.pushed_warning:
        parts.append("⚠ rollback request extended past upstream — report-only")
    for opt in plan.compensation_options:
        parts.append(f"compensation: {opt}")

    description = "; ".join(parts)

    def op(d):
        return d, core.save_entry(
            d,
            ms_id="",  # auto-target the active MS
            description=description,
            source="rollback",
            date="",
            hash=head_hash,
        )

    try:
        project_id = _project_id_for_ops()
        return _ops.apply_operation(
            project_id, op,
            op_name="rollback.record",
            reason=reason or "rollback record",
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}


def cmd_rollback():
    """Inspect local git state and roll back the safe portion.

    Env:
      BEACON_ROLLBACK_COMMITS    int (default 0 = auto)
      BEACON_ROLLBACK_REASON     free text, recorded into stash msg + report
      BEACON_ROLLBACK_DRY_RUN    "1" → show plan, don't mutate
      BEACON_ROLLBACK_CWD        override cwd (mainly for tests)
      BEACON_ROLLBACK_NO_RECORD  "1" → skip the history save entry (= e-1727
                                 escape hatch for tests / no-op rollbacks)
      BEACON_JSON                "1" → json output (plan + result)
    """
    import rollback as _rb

    commits_raw = os.environ.get("BEACON_ROLLBACK_COMMITS", "0").strip()
    try:
        commits = int(commits_raw)
    except ValueError:
        print(
            f"Error: --commits must be an integer (got {commits_raw!r})",
            file=sys.stderr,
        )
        sys.exit(1)
    reason = os.environ.get("BEACON_ROLLBACK_REASON", "")
    dry_run = os.environ.get("BEACON_ROLLBACK_DRY_RUN", "") == "1"
    cwd = os.environ.get("BEACON_ROLLBACK_CWD", "").strip() or None
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    plan, result, report = _rb.rollback(
        cwd=cwd,
        commits=commits,
        reason=reason,
        dry_run=dry_run,
    )

    # ms-55 e-1727: also record on the non-error JSON path. We compute
    # the record first so it appears in the JSON payload.
    record_info: Optional[dict] = None
    if (
        not dry_run
        and result is not None
        and os.environ.get("BEACON_ROLLBACK_NO_RECORD", "") != "1"
        and (result.stashed or result.reset_commits > 0
             or plan.compensation_options)
    ):
        record_info = _rollback_record_history(plan, result, reason)

    if json_mode:
        out = {
            "plan": {
                "stash_working_tree": plan.stash_working_tree,
                "reset_commits": plan.reset_commits,
                "pushed_warning": plan.pushed_warning,
                "compensation_options": list(plan.compensation_options),
                "reason": plan.reason,
                "requested_commits": plan.requested_commits,
                "state": {
                    "head_hash": plan.state.head_hash,
                    "branch": plan.state.branch,
                    "upstream_branch": plan.state.upstream_branch,
                    "local_commits_ahead": plan.state.local_commits_ahead,
                    "working_tree_dirty": plan.state.working_tree_dirty,
                    "working_tree_files": list(plan.state.working_tree_files),
                },
            },
            "dry_run": dry_run,
        }
        if result is not None:
            out["result"] = {
                "stashed": result.stashed,
                "stash_ref": result.stash_ref,
                "reset_commits": result.reset_commits,
                "errors": list(result.errors),
            }
        if record_info is not None:
            out["record"] = record_info
        print(json.dumps(out, ensure_ascii=False))
        if result is not None and result.errors:
            sys.exit(1)
        return

    print(report, end="")
    if dry_run:
        print("(dry run — nothing executed. Re-run without --dry-run to apply.)")
        return

    if result is None:
        # Defensive: rollback() should always return a result when
        # dry_run=False. Falling through means something inside the
        # helper changed shape; surface so it gets caught early.
        print("Error: rollback executor returned no result.", file=sys.stderr)
        sys.exit(1)

    summary_bits = []
    if result.stashed:
        summary_bits.append(f"stashed ({result.stash_ref or 'stash@{0}'})")
    if result.reset_commits > 0:
        summary_bits.append(f"reset {result.reset_commits} commit(s)")
    if not summary_bits:
        summary_bits.append("nothing to do")
    print(f"Executed: {', '.join(summary_bits)}")

    # ms-55 e-1727: surface the history record outcome. The save itself
    # was already attempted in the shared pre-print block above; here we
    # just report what happened. Non-fatal — the rollback already
    # succeeded; the trail being broken is annoying, not catastrophic.
    if record_info is not None:
        if record_info.get("status") == "saved":
            print(
                f"Recorded: {record_info.get('entry_id', '?')} → "
                f"{record_info.get('milestone', '?')}"
            )
        elif record_info.get("status") == "duplicate":
            print(f"Recorded: (duplicate) → {record_info.get('milestone', '?')}")
        elif record_info.get("status") == "error":
            print(
                f"Warning: could not record rollback history: "
                f"{record_info.get('error', 'unknown')}",
                file=sys.stderr,
            )

    if result.errors:
        print("", file=sys.stderr)
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)


# Claim CLI (ms-55 e-1648) — moved to lib/cmd_claim.py (ms-127 e-4321);
# re-imported at top. (`beacon claim` fronts the claim primitives in lib/claims.py.)


def _morning_save_briefing_doc(briefing_text: str, briefing) -> dict:
    """Save the morning briefing as a `scope=report` doc (ms-55 e-1733).

    Lets the user re-read past briefings via Web UI Documents tab (or
    `beacon doc show`) without having to scroll terminal history. The
    daily cadence makes each briefing a small artifact worth preserving.

    Title is ISO-prefixed for chronological listing. Body is the rendered
    text wrapped in a code fence + a one-line meta header so the doc
    stands alone as a search target.

    Returns {"status": "saved", "doc_id": ..., "title": ...} or
    {"status": "error", "error": ...}. Failures are non-fatal — the
    terminal briefing already printed.
    """
    from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
    import datetime as _datetime  # noqa: PLC0415

    now = _dt.now(_tz.utc)
    iso_min = now.strftime("%Y-%m-%dT%H:%M")
    title = f"morning briefing {iso_min}Z"

    counts = briefing.counts or {}
    summary_bits = []
    for b in ("completed", "halted", "skipped", "needs_attention"):
        summary_bits.append(f"{b}={counts.get(b, 0)}")
    meta_line = (
        f"window: {briefing.since or '(start)'} → {briefing.until or '(now)'}  "
        f"counts: {' / '.join(summary_bits)}"
    )

    body = (
        f"# morning briefing — {iso_min}Z\n\n"
        f"{meta_line}\n\n"
        f"```\n{briefing_text.rstrip()}\n```\n"
    )

    scope = "report"

    try:
        # Frontmatter aligns with cmd_doc_add so Web UI / list filters
        # recognise the scope = report tag.
        content = _add_frontmatter(body, scope, "", "", "")
    except Exception as e:
        return {"status": "error", "error": f"frontmatter: {e}"}

    today_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if _is_cloud_mode():
            client, config = _get_api_client()
            result = client.create_document(
                config["project_id"], title, content,
            )
            doc_id = result["doc_id"]
        else:
            docs_dir = _get_docs_dir()
            os.makedirs(docs_dir, exist_ok=True)
            # Slug + minute resolution → collision-free across the day
            # while still distinguishable. Adds an "-Z" suffix to
            # mirror the title's UTC marker.
            slug = _doc_slug(f"morning-briefing-{iso_min}-Z")
            doc_id = slug
            fpath = os.path.join(docs_dir, f"{doc_id}.md")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as e:
        return {"status": "error", "error": f"persist: {e}"}

    # Record an MS entry pointing at the saved doc — same shape as
    # cmd_doc_add (= source=auto, revision_id=doc_id) so the briefing
    # appears in the active MS timeline. Failures are non-fatal.
    try:
        import operations as _ops  # noqa: PLC0415

        def op(d):
            return d, core.save_entry(
                d, ms_id="",
                description=f"doc add: {title} ({scope})",
                source="auto", date=today_iso,
                revision_id=doc_id,
            )
        project_id = _project_id_for_ops()
        _ops.apply_operation(
            project_id, op,
            op_name="morning.briefing_doc",
            reason="morning briefing snapshot",
        )
    except Exception as e:
        # The doc itself was written — we just couldn't link it from
        # the MS timeline. Surface as warning only.
        return {
            "status": "saved",
            "doc_id": doc_id,
            "title": title,
            "warning": f"could not link to MS timeline: {e}",
        }

    return {"status": "saved", "doc_id": doc_id, "title": title}


def cmd_morning():
    """beacon morning (ms-55 e-1650 / e-1733): 4-category summary of recent
    autonomous activity, plus auto-save as a report doc.

    Reads events from the bus (channels: stop-signal + claim-signal)
    over a window (default = last 12 hours) and buckets them as:

      ✓ 完了 (Completed):   claim releases with outcome=completed
      ⚠ 停止 (Halted):      stop signals other than STUCK
      ✗ Skip:               claim releases with outcome=abandoned
      ⏱ 介入要望 (Needs attention): STUCK signals (= idle timeout)

    The briefing is also persisted as a `scope=report` doc so the user
    can re-read past briefings via Web UI Documents tab. Skip the save
    with BEACON_MORNING_NO_DOC=1 (= --no-doc).

    Env:
      BEACON_MORNING_SINCE_HOURS  default 12
      BEACON_MORNING_EVENTS_FILE  optional path to a JSON array of
                                  events (= testing / replay path)
      BEACON_MORNING_NO_DOC       "1" → skip the report doc save (e-1733)
      BEACON_JSON                 "1" → JSON output
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import morning as _morning

    since_hours_raw = os.environ.get("BEACON_MORNING_SINCE_HOURS", "12")
    events_file = os.environ.get("BEACON_MORNING_EVENTS_FILE", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    try:
        since_hours = float(since_hours_raw)
    except ValueError:
        print(
            f"Error: --since-hours must be a number (got {since_hours_raw!r})",
            file=sys.stderr,
        )
        sys.exit(1)
    if since_hours <= 0:
        print("Error: --since-hours must be > 0", file=sys.stderr)
        sys.exit(1)

    now = _dt.now(_tz.utc)
    since = now - _td(hours=since_hours)

    events: list[dict] = []
    if events_file:
        try:
            with open(events_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error: could not read events file: {e}",
                  file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, list):
            print("Error: events file must be a JSON array",
                  file=sys.stderr)
            sys.exit(1)
        events = data
    else:
        # Pull recent stop + claim channel events from the bus. Use a
        # synthetic observer recipient so we don't advance anyone's
        # cursor (= same trick as cmd_stop_status).
        try:
            client, config = _get_api_client()
            project_id = _resolve_bus_project_id(config)
        except Exception as e:
            print(
                f"Error: cannot reach bus to gather events ({e}).\n"
                "If running offline, pass --events-file <path> with a "
                "JSON dump of events.",
                file=sys.stderr,
            )
            sys.exit(1)
        for channel in ("stop-signal", "claim-signal"):
            try:
                batch = client.list_unread_bus_events(
                    project_id, "_morning_observer",
                    channel=channel, limit=500,
                )
            except TypeError:
                batch = client.list_unread_bus_events(
                    project_id, "_morning_observer", channel=channel,
                )
            except Exception as e:
                print(
                    f"Note: could not fetch {channel} events ({e}); "
                    "continuing with partial data.",
                    file=sys.stderr,
                )
                continue
            if batch:
                events.extend(batch)

    briefing = _morning.build_briefing(events, since=since, until=now)
    briefing_text = _morning.render_briefing(briefing)

    # ms-55 e-1733: save the briefing as a `scope=report` doc so the
    # user can re-read past briefings from the Web UI Documents tab
    # (or via `beacon doc show`). Opt out with --no-doc.
    record_info: Optional[dict] = None
    if os.environ.get("BEACON_MORNING_NO_DOC", "") != "1":
        record_info = _morning_save_briefing_doc(briefing_text, briefing)

    if json_mode:
        out = {
            "since": briefing.since,
            "until": briefing.until,
            "counts": briefing.counts,
            "entries": [
                {
                    "bucket": e.bucket,
                    "title": e.title,
                    "detail": e.detail,
                    "at": e.at,
                    "source": e.source,
                    "ref": e.ref,
                }
                for e in briefing.entries
            ],
        }
        if record_info is not None:
            out["doc"] = record_info
        print(json.dumps(out, ensure_ascii=False))
        return

    print(briefing_text, end="")
    if record_info is not None:
        if record_info.get("status") == "saved":
            warn = record_info.get("warning")
            line = (
                f"\nSaved briefing as doc: {record_info.get('doc_id', '?')} "
                f"({record_info.get('title', '?')})"
            )
            print(line)
            if warn:
                print(f"  warning: {warn}", file=sys.stderr)
        elif record_info.get("status") == "error":
            print(
                f"\nWarning: could not save briefing doc: "
                f"{record_info.get('error', 'unknown')}",
                file=sys.stderr,
            )


def cmd_stuck_check():
    """beacon stuck check (ms-55 e-1649): scan session telemetry, emit
    STUCK signals for sessions idle past the timeout.

    Input modes (exactly one):
      * BEACON_STUCK_TELEMETRY_FILE — path to a JSON file with a list
        of telemetry records. Each record needs at least session_id +
        last_active; ms_id / task_id / ignore are optional.
      * BEACON_STUCK_TELEMETRY_INLINE — JSON string with the same shape.

    The JSON shape:
      [
        {"session_id": "sv-A", "last_active": "2026-06-15T11:00:00Z",
         "ms_id": "ms-55", "task_id": "e-1646"},
        {"session_id": "sv-B", "last_active": "2026-06-15T11:55:00Z"},
        ...
      ]

    Other env:
      BEACON_STUCK_TIMEOUT_MINUTES   default 30
      BEACON_STUCK_DRY_RUN          "1" → identify stuck sessions but
                                    don't emit signals
      BEACON_JSON                   "1" → JSON output (list of records
                                    {session_id, last_active, payload?, event?})
    """
    from datetime import datetime as _dt, timezone as _tz
    import stuck_detect as _stuck

    inline = os.environ.get("BEACON_STUCK_TELEMETRY_INLINE", "").strip()
    file_path = os.environ.get("BEACON_STUCK_TELEMETRY_FILE", "").strip()
    timeout_raw = os.environ.get("BEACON_STUCK_TIMEOUT_MINUTES",
                                 str(_stuck.DEFAULT_TIMEOUT_MINUTES))
    dry_run = os.environ.get("BEACON_STUCK_DRY_RUN", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    try:
        timeout_minutes = int(timeout_raw)
    except ValueError:
        print(
            f"Error: --timeout-minutes must be an integer "
            f"(got {timeout_raw!r})",
            file=sys.stderr,
        )
        sys.exit(1)
    if timeout_minutes <= 0:
        print("Error: --timeout-minutes must be > 0", file=sys.stderr)
        sys.exit(1)

    raw = ""
    if inline and file_path:
        print(
            "Error: pass either --telemetry-inline or --telemetry-file, "
            "not both",
            file=sys.stderr,
        )
        sys.exit(1)
    if inline:
        raw = inline
    elif file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            print(f"Error: could not read telemetry file: {e}",
                  file=sys.stderr)
            sys.exit(1)
    else:
        print(
            "Error: provide --telemetry-inline '<json>' "
            "or --telemetry-file <path>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Error: telemetry payload is not valid JSON ({e})",
              file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, list):
        print("Error: telemetry payload must be a JSON array",
              file=sys.stderr)
        sys.exit(1)

    rows = []
    for row in data:
        if not isinstance(row, dict):
            continue
        rows.append(_stuck.SessionTelemetry(
            session_id=row.get("session_id", "") or "",
            last_active=row.get("last_active", "") or "",
            ms_id=row.get("ms_id", "") or "",
            task_id=row.get("task_id", "") or "",
            ignore=bool(row.get("ignore", False)),
        ))

    now = _dt.now(_tz.utc)
    try:
        stuck_rows = _stuck.find_stuck_sessions(
            rows, now=now, timeout_minutes=timeout_minutes,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    sender = _resolve_session_id() or "_stuck_detector"

    results = []
    for t in stuck_rows:
        payload = _stuck.build_stuck_signal(
            t, issued_by_session_id=sender, now=now,
            timeout_minutes=timeout_minutes,
        )
        entry = {
            "session_id": t.session_id,
            "last_active": t.last_active,
            "ms_id": t.ms_id,
            "task_id": t.task_id,
            "payload": payload,
        }
        if not dry_run:
            try:
                ev = _stop_post_event(payload=payload)
                entry["event"] = ev
            except Exception as e:
                # Don't let a single send failure prevent the rest of
                # the batch from being reported — partial coverage is
                # still useful for the morning briefing.
                entry["error"] = str(e)
        results.append(entry)

    if json_mode:
        print(json.dumps({
            "dry_run": dry_run,
            "timeout_minutes": timeout_minutes,
            "stuck": results,
        }, ensure_ascii=False))
        return

    if not results:
        print(
            f"No stuck sessions (timeout = {timeout_minutes} min, "
            f"scanned {len(rows)} session(s))."
        )
        return

    print(f"Stuck sessions detected ({len(results)}):")
    for r in results:
        suffix = "  (dry-run, no signal emitted)" if dry_run else ""
        print(
            f"  ⚠ {r['session_id']}  last_active={r['last_active']}  "
            f"ms={r['ms_id'] or '-'}  task={r['task_id'] or '-'}{suffix}"
        )
        if "event" in r:
            print(f"      → STUCK signal posted: {r['event'].get('event_id', '?')}")
        if "error" in r:
            print(f"      → error: {r['error']}", file=sys.stderr)


def cmd_resume_global():
    """Broadcast a global resume."""
    import stop_signal as _stop

    reason = os.environ.get("BEACON_STOP_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _stop.build_resume_payload(
            scope=_stop.SCOPE_GLOBAL,
            issued_by_session_id=sender,
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _stop_post_event(payload=payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    print(f"RESUME signal (GLOBAL) raised by {sender}")
    print(f"  event_id: {event.get('event_id', '?')}")


# ---------------------------------------------------------------------------
# Wall-clock TTL for CLI subcommand execution (ms-98 / e-2773)
# ---------------------------------------------------------------------------

_DEFAULT_CLI_MAX_SECONDS = 60


def _install_wall_clock_timeout(cmd_name: str) -> None:
    """Install a self-termination timer so a single CLI invocation cannot
    outlive its budget.

    Rationale (2026-07-02 incident): ``urllib`` timeouts on individual API
    calls (30-60 s) do not compose. ``cmd_trigger_check`` used to invoke
    four ``_auto_fire_*`` helpers plus ``_cleanup_stale_triggers``; if any
    of those hung on a per-call timeout, the process could sit for
    hundreds of seconds without producing output. Concurrent hook fires
    then piled up hundreds of these zombie processes.

    A wall-clock ``SIGALRM`` at the top of the CLI dispatch caps every
    subcommand at a bounded budget regardless of how many API round-trips
    it stacks. Timeouts are best-effort: on a signal handler platforms
    (POSIX only — ``signal.SIGALRM`` is not defined on Windows) we skip
    installation silently so the CLI still runs.

    Configuration:
      * ``BEACON_CLI_MAX_SECONDS`` env var (int, seconds). Default 60.
      * ``0`` disables the timer entirely (post-mortem / long-running debug).
      * Negative or malformed values fall through to the default.
    """
    import signal
    if not hasattr(signal, "SIGALRM"):
        return  # Windows / other non-POSIX — silent skip.

    raw = os.environ.get("BEACON_CLI_MAX_SECONDS", "").strip()
    if raw:
        try:
            budget = int(raw)
        except ValueError:
            budget = _DEFAULT_CLI_MAX_SECONDS
    else:
        budget = _DEFAULT_CLI_MAX_SECONDS

    if budget <= 0:
        return  # explicitly disabled — no timer

    def _timeout(signum, frame):  # noqa: ARG001
        sys.stderr.write(
            f"[beacon] wall-clock timeout after {budget}s in {cmd_name or '?'}\n"
        )
        # Exit 124 mirrors GNU ``timeout(1)`` — well-known convention that
        # calling shells can pattern-match on to skip cleanup that assumes
        # a normal exit.
        os._exit(124)

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(budget)


# ---------------------------------------------------------------------------
# Sales job-template commands (ms-106 ② — opportunity / account / activity)
# ---------------------------------------------------------------------------
# These operate on the sales collections (opportunities / accounts) rather than
# on milestones. They are new ② code (SPEC 設計方針 0): sharing the Store
# substrate but not the milestone/task functions. All args arrive via env vars
# set by bin/beacon / beacon_cli.dispatch, matching the rest of this module.

# ms-127 e-4839: a duplicate `_today_iso` def lived here. It was shadowed by a
# second, later def (now promoted to commands_shared), so the effective behavior
# was already `_now_iso()[:10]` (UTC date). All callers now resolve the single
# re-exported commands_shared._today_iso — no behavior change.

# _require_sales_project (warn-only) was removed in ms-115 e-3785 — the sales
# target-creating commands now go through the hard containment gate
# (_gate_target_class → occupation.assert_target_class_owned) instead of a soft
# warning, so a wrong-profession create fails structurally.




def _master_adapter():
    """CLI から見た master adapter の唯一の定義点 (ms-111 e-3621 chunk2b).

    CLI プロセスには server 側 backend に配線済みの master adapter が無いので、現状は
    常に None (= 投影 fallback で従来値を返す)。将来 CLI にも adapter を配線する時
    (e-3622 以降) は **この 1 関数だけ差し替えれば** 全 CLI read site が一斉に master
    経由になる。散在する None リテラルを 1 箇所に閉じ、「一部だけ master 経由」の
    部分 swap (= stale と master が混ざる不整合) を構造的に防ぐ。
    """
    return None


def cmd_account_add():
    import sales_entities
    name = os.environ.get("BEACON_ACCOUNT_NAME", "")
    health = os.environ.get("BEACON_ACCOUNT_HEALTH", "")
    assignee = os.environ.get("BEACON_ACCOUNT_ASSIGNEE", "")
    _sales_skill_nudge("顧客 (Account)", "/beacon-sales-card",
                       "名刺から会社と担当者(Contact)をまとめて起票できます")
    data = load_project()
    _gate_target_class(data, "account")
    try:
        acc_id = sales_entities.account_add(data, name, health=health,
                                            assignee=assignee,
                                            created_at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Added account {acc_id}: {name}")


def _post_master_sync_event(payload):
    """master-sync event を 1 件だけ実発行する (ms-111 e-4355 で emit を pure に切り出し).

    cloud mode + login 済のときだけ実発行する (local mode は master server が無い /
    _get_api_client の sys.exit を避けるため先に login/cloud を確認)。発行できたら True、
    未 login / local / 発行例外なら False を返す (呼び出し側が outbox 判断に使う)。

    従来はこの emit を握り潰していた (best-effort、silent) が、silent 失敗は
    「master 反映漏れ → 後続 fan-out で local rename が revert = lost edit」の入口
    (e-4355)。成否を返し値で surface し、呼び出し側が pending マーカー (outbox) に
    落とせるようにする。
    """
    if not payload:
        return False
    try:
        from auth import load_credentials
        if load_credentials() is None or not os.path.exists(_get_cloud_config_path()):
            return False  # local / 未 login → master server 無し (= 未配送)
        client, config = _get_api_client()
        project_id = _resolve_bus_project_id(config)
        client.post_bus_event(
            project_id, "master-sync",
            sender_session_id=os.environ.get("BEACON_SESSION_ID", ""),
            payload=payload, delivery="propose-to-ai")
        return True
    except Exception:
        return False  # bus 不通 / 認証エラー等 → 未配送 (outbox に残す)


def _emit_master_sync_account_rename(data, account_id, new_name):
    """rename を master へ write-through する master-sync event を発行する (ms-111 e-3622 / e-4355).

    投影 account が master に link 済のときだけ発行 (未 link は master 実体が無いので投影
    のみで完結 = 現状の常態)。**at-least-once (e-4355)**: 発行に失敗したら投影 account に
    未同期 (unsynced) の pending マーカーを立てて data を返す (呼び出し側が save して次操作で
    再発行する = outbox)。発行成功なら既存マーカーを消す。

    戻り値 ``(delivered, changed)``:
      - ``delivered`` : master-sync event を実配送できたか。
      - ``changed``   : 投影 account の pending マーカーを変更したか (True なら save 要)。
    """
    try:
        import sales_entities
        acc = sales_entities.find_account(data, account_id)
        if acc is None:
            return False, False
        payload = master_projection.master_sync_payload(acc, new_name)
        if not payload:
            return False, False  # 未 link → 発行不要 (同期対象外)
        if _post_master_sync_event(payload):
            # 配送成功 → 未同期マーカーがあれば回収 (この rename も過去の残置も済み)。
            had = master_projection.is_sync_pending(acc)
            master_projection.clear_sync_pending(acc)
            return True, had
        # 配送失敗 → outbox: 未同期マーカーを立てて retry 源にする (lost edit 防止)。
        before = master_projection.sync_pending_name(acc)
        master_projection.mark_sync_pending(acc, new_name)
        return False, master_projection.sync_pending_name(acc) != before
    except Exception:
        return False, False  # 防御的: emit 経路の想定外例外は rename を壊さない


def _drain_master_sync_outbox(data):
    """未配送で残った rename (pending マーカー付き投影) の master-sync を再発行する (e-4355 outbox).

    linking を live にすると emit は時々失敗する (bus 一時不通等)。失敗を放置すると
    master 反映漏れ → fan-out revert (lost edit)。この drain を CLI 操作の折に呼び、
    pending を抱える account の master-sync を再発行して at-least-once に寄せる。
    戻り値は data を変更したか (= save 要)。
    """
    changed = False
    try:
        for acc in master_projection.pending_sync_accounts(data):
            payload = master_projection.master_sync_payload(
                acc, master_projection.sync_pending_name(acc))
            if payload and _post_master_sync_event(payload):
                master_projection.clear_sync_pending(acc)
                changed = True
    except Exception:
        pass  # 防御的: drain は best-effort、失敗は次回に持ち越す
    return changed


def cmd_account_rename():
    import sales_entities
    account_id = os.environ.get("BEACON_ACCOUNT_ID", "")
    new_name = os.environ.get("BEACON_ACCOUNT_NAME", "")
    data = load_project()
    try:
        sales_entities.account_rename(data, account_id, new_name)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Renamed {account_id} → {new_name.strip()}")
    # ms-111 e-3622 chunk2a / e-4355: link 済なら rename を master へ write-through。
    # emit が失敗したら pending マーカー (outbox) を残し、過去分の未配送も drain する
    # (at-least-once)。マーカーが変わったら投影 doc を再保存する。
    _, changed = _emit_master_sync_account_rename(data, account_id, new_name)
    if _drain_master_sync_outbox(data):
        changed = True
    if changed:
        save_project(data)


def cmd_master_sync_drain():
    """未配送で残った rename の master-sync を、CLI 操作を待たず定期経路から再発行する (e-4399).

    背景: e-4355 の outbox (未同期 pending マーカー) は従来 cmd_account_rename の末尾で
    しか drain されず、「次の CLI 操作」駆動だった。rename 後に別操作が無いと未配送分が
    master に届かず遅延する。本サブコマンドは drain を account_rename から独立させ、操作
    非依存の定期経路 (session-start helper 等) から呼べる entrypoint にする。

    load_project → _drain_master_sync_outbox → 変化があれば save_project の順で回す。
    save_project は cloud mode の lost-update guard (concurrent 変更で ConflictError) を
    通すため、定期 drain でも同時編集を握り潰さない。drain 自体は既存 pending マーカーを
    clear するだけ (新規 identity 変更を起こさない) なので apply_operation の changelog は
    要らないが、書き込み経路は通常の save_project (guard 付き) を必ず経由する。

    出力契約: pending が無い / 未 login / local mode では静かに no-op (発行できない環境で
    エラーにしない)。1 件以上再配送して投影 doc を更新したら要旨を 1 行出す。常に exit 0。
    """
    data = load_project()
    if _drain_master_sync_outbox(data):
        save_project(data)
        print("master-sync outbox: 未配送分を再送しました")
    # pending 無し / 発行不可 (local / 未 login) は静かに no-op (定期経路のノイズを避ける)。


def cmd_account_assign():
    import sales_entities
    account_id = os.environ.get("BEACON_ACCOUNT_ID", "")
    assignee = os.environ.get("BEACON_ASSIGNEE", "")
    data = load_project()
    try:
        sales_entities.set_assignee(data, account_id, assignee)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Assigned {account_id} → {assignee or '(cleared)'}")


def cmd_account_nurturing():
    import sales_entities
    account_id = os.environ.get("BEACON_ACCOUNT_ID", "")
    description = os.environ.get("BEACON_NURTURING_DESC", "")
    deadline = os.environ.get("BEACON_NURTURING_DEADLINE", "")
    ball = os.environ.get("BEACON_NURTURING_BALL", "") or sales_entities.BALL_SELF
    data = load_project()
    try:
        nrt_id = sales_entities.nurturing_add(
            data, account_id, description, deadline=deadline,
            who_has_the_ball=ball, created_at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Added nurturing {nrt_id} to {account_id}: {description}")


def _cmd_account_list_linked(json_mode: bool):
    """ms-111 e-3872: 同じ組織の他 project に開示された Account を横断して一覧する。

    サーバの ``GET /api/projects/{P}/disclosed-accounts`` を叩く (cloud mode 専用)。
    P に link された Account だけが返る (fail-closed、剥奪即時はサーバ側 disclosure
    プリミティブが担保)。
    """
    project_id = _current_project_id()
    if not project_id:
        print("Error: cross-project の Account 一覧は cloud mode 専用です "
              "(.beacon/cloud.json が必要)。", file=sys.stderr)
        sys.exit(1)
    try:
        # _get_api_client() は (client, config) の tuple を返す (e-3872 で
        # tuple を client として扱い 'tuple' has no attribute 'get' で落ちた
        # 実地バグ)。unpack して client だけ使う。
        client, _config = _get_api_client()
        resp = client.get(f"/api/projects/{project_id}/disclosed-accounts")
    except Exception as e:
        print(f"Error: 開示 Account の取得に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)
    accounts = (resp or {}).get("disclosed_accounts", [])
    if json_mode:
        print(json.dumps(accounts, ensure_ascii=False, indent=2))
        return
    if not accounts:
        print("この project に開示された(他 project の) Account はありません。"
              "\n  他 project 側で: beacon disclose <acc-id> --to-project "
              f"{project_id}")
        return
    print(f"(他 project から この project '{project_id}' に開示された Account)")
    for a in accounts:
        home = a.get("home_project_name") or a.get("home_project_id", "?")
        phase = a.get("phase", "")
        phase_str = f"phase: {phase} / " if phase else ""
        # ms-111 e-3621 chunk2b: identity は master 経由の resolver で読む。adapter の
        # 出所は _master_adapter() に一本化 (CLI は現状 None → 投影 fallback = 従来値)。
        name = master_projection.resolve_account_identity(a, _master_adapter()) or "?"
        print(f"[{a.get('id')}] {name} — {phase_str}home: {home}")


def cmd_account_list():
    import sales_entities
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    # ms-113 e-3734: --as-project <P> は「別 project P から見たら何が見えるか」を
    # 実演する。指定時は P の視点で開示可能な Account だけを fail-closed で絞る
    # (= link されていない Account は返らない)。未指定は home read = 全件。
    as_project = os.environ.get("BEACON_AS_PROJECT", "")
    # ms-111 e-3872: --linked は同じ組織の他 project に置かれ、この project に開示
    # された Account を横断して取り込む (cross-project read)。cloud mode 専用
    # (= 他 project の read はサーバ経由。local mode には他 project が無い)。
    if os.environ.get("BEACON_LINKED", "") == "1":
        _cmd_account_list_linked(json_mode)
        return
    data = load_project()
    accounts = data.get("accounts", [])
    if as_project:
        accounts = sales_entities.accounts_disclosable_from(
            accounts, as_project, is_home=False)
    if json_mode:
        print(json.dumps(accounts, ensure_ascii=False, indent=2))
        return
    if not accounts:
        if as_project:
            print(f"No accounts disclosed to project '{as_project}'. "
                  f"Disclose one with: beacon disclose <acc-id> --to-project {as_project}")
        else:
            print("No accounts yet. Add one with: beacon account add \"<name>\"")
        return
    if as_project:
        print(f"(project '{as_project}' の視点で開示される Account のみ表示)")
    for a in accounts:
        contacts = a.get("contacts", [])
        health = a.get("health", "")
        phase = a.get("phase", "")
        phase_str = f"phase: {phase} / " if phase else ""
        suffix = f" [health: {health}]" if health else ""
        # ms-113 e-3734: 開示リンク先 project を可視化 (どこから見えるか)。
        links = a.get("project_links", []) or []
        links_str = f" / linked: {', '.join(links)}" if links else ""
        # ms-111 e-3621 chunk2b: account/contact の identity を master 経由 resolver で
        # 読む。adapter の出所は _master_adapter() に一本化 (CLI は現状 None → 投影値)。
        # e-3622: contact は親 Account の所有 org を org 照合基準として渡す (fail-closed)。
        adapter = _master_adapter()
        acc_org = master_projection.projection_account_org(a)
        acc_name = master_projection.resolve_account_identity(a, adapter)
        print(f"[{a['id']}] {acc_name}{suffix} — {phase_str}contacts: {len(contacts)}{links_str}")
        for c in contacts:
            ident = master_projection.resolve_contact_identity(c, adapter, expected_org=acc_org)
            role = f" ({ident['role']})" if ident.get("role") else ""
            email = f" <{ident['email']}>" if ident.get("email") else ""
            print(f"    - {ident.get('name') or '?'}{role}{email}")


def cmd_disclose():
    """ms-113 generalization: 任意の Target を別 project に開示リンクする。

    Account 専用だった `account link` を、id で任意の Target を引く汎用動詞に
    持ち上げたもの (`beacon disclose <resource-id> --to-project <P>`)。
    """
    import target_disclosure
    resource_id = os.environ.get("BEACON_DISCLOSE_ID", "")
    project = os.environ.get("BEACON_DISCLOSE_PROJECT", "")
    data = load_project()
    try:
        added = target_disclosure.disclose_target(data, resource_id, project)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if added:
        print(f"Disclosed {resource_id} → project {project} "
              f"(project {project} のメンバーが参照できるようになりました)")
    else:
        print(f"{resource_id} は既に project {project} に開示済みです (no-op)")


def cmd_undisclose():
    """ms-113 generalization: 任意の Target の開示リンクを外す (剥奪即時)。"""
    import target_disclosure
    resource_id = os.environ.get("BEACON_DISCLOSE_ID", "")
    project = os.environ.get("BEACON_DISCLOSE_PROJECT", "")
    data = load_project()
    try:
        removed = target_disclosure.undisclose_target(data, resource_id, project)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if removed:
        print(f"Undisclosed {resource_id} from project {project} "
              f"(project {project} からは参照できなくなりました)")
    else:
        print(f"{resource_id} は project {project} に開示されていません (no-op)")


def cmd_sales_identity_set():
    """Internal (Skill-invoked): pin the send identity for the sales project.
    Not a user-facing verb — the sales Skills call this; kept out of bin/beacon
    / README so it doesn't need CLI-drift wiring."""
    import sales_entities
    identity = os.environ.get("BEACON_SEND_IDENTITY", "")
    data = load_project()
    try:
        val = sales_entities.set_send_identity(data, identity)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"send identity pinned: {val}")


def cmd_sales_identity_show():
    """Internal (Skill-invoked): show the pinned send identity."""
    import sales_entities
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    val = sales_entities.get_send_identity(data)
    if json_mode:
        print(json.dumps({"send_identity": val}, ensure_ascii=False))
        return
    print(val if val else "(未設定)")


def cmd_sales_identity_check():
    """Internal (Skill-invoked): check a proposed send ``from`` against the
    resolved identity (台帳 label or, legacy, the bare pin). Optional
    BEACON_SEND_LABEL selects a ledger entry for a per-send account switch;
    omit it to use the default send label. Exit 0 + 'OK: <msg>' on match, exit 1
    + 'BLOCK: <msg>' otherwise — a send Skill gates on the exit code."""
    import sales_entities
    from_value = os.environ.get("BEACON_SEND_FROM", "")
    label = os.environ.get("BEACON_SEND_LABEL", "")
    data = load_project()
    ok, msg = sales_entities.check_send_from(data, from_value, label)
    if ok:
        print(f"OK: {msg}")
        sys.exit(0)
    print(f"BLOCK: {msg}", file=sys.stderr)
    sys.exit(1)


# --- 顧客獲得ターゲット (Acquisition, ms-115 e-3786) ------------------------
# 取引先の無い有限の獲得・準備作業の器。営業が own する target-class。






























def cmd_sales_account_add():
    """Internal (Skill-invoked): add/update a send account (label + email).
    Env: BEACON_SEND_LABEL, BEACON_SEND_EMAIL. Idempotent on label."""
    import sales_entities
    label = os.environ.get("BEACON_SEND_LABEL", "")
    email = os.environ.get("BEACON_SEND_EMAIL", "")
    data = load_project()
    try:
        entry = sales_entities.add_send_account(data, label, email)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"send account: {entry['label']} → {entry['email']}")


def cmd_sales_account_route():
    """Internal (Skill-invoked): set one service's MCP route on a send account.
    Env: BEACON_SEND_LABEL, BEACON_SEND_SERVICE (gmail|calendar|drive),
    BEACON_SEND_NAMESPACE, optional BEACON_SEND_ALIAS."""
    import sales_entities
    label = os.environ.get("BEACON_SEND_LABEL", "")
    service = os.environ.get("BEACON_SEND_SERVICE", "")
    namespace = os.environ.get("BEACON_SEND_NAMESPACE", "")
    alias = os.environ.get("BEACON_SEND_ALIAS", "")
    data = load_project()
    try:
        route = sales_entities.set_account_route(data, label, service, namespace, alias)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    tail = f" (account={route['alias']})" if route.get("alias") else ""
    print(f"route set: {label}/{service} → {route['namespace']}{tail}")


def cmd_sales_account_signature():
    """Internal (Skill-invoked): set/clear the mail signature on a send account (e-3529).

    Env: BEACON_SEND_LABEL, BEACON_SEND_SIGNATURE (署名テキスト).
    #496 review (AX high): **空文字での暗黙 clear を禁止** + **clear と設定値の共存を拒否**。
    消すには明示 **BEACON_SEND_SIGNATURE_CLEAR=1**、かつそのとき非空 BEACON_SEND_SIGNATURE を
    同時に渡してはならない (stale env の CLEAR が set を silent に delete へ化けさせる経路を塞ぐ)。
    - BEACON_SEND_SIGNATURE_CLEAR=1 (値なし) → 署名を消す。
    - BEACON_SEND_SIGNATURE 非空 (clear なし) → 設定。
    - どちらも無い / 空 → エラー (exit1、既存署名は保持)。CLEAR と値の同時指定も exit1。"""
    import sales_entities
    label = os.environ.get("BEACON_SEND_LABEL", "")
    clear = os.environ.get("BEACON_SEND_SIGNATURE_CLEAR", "").strip().lower() in (
        "1", "true", "yes")
    signature = os.environ.get("BEACON_SEND_SIGNATURE", "")
    data = load_project()
    if not clear and not signature.strip():
        # env 未設定 / 空 = 設定するつもりの渡し忘れ。silent clear せず止める。
        print("Error: BEACON_SEND_SIGNATURE が未設定/空です。署名を設定するなら値を、"
              "消すなら BEACON_SEND_SIGNATURE_CLEAR=1 を渡してください "
              "(空文字では既存署名を消しません)。", file=sys.stderr)
        sys.exit(1)
    try:
        # clear のとき非空 signature を渡すと lib が共存拒否で raise する (単一 enforcement 点)。
        a = sales_entities.set_account_signature(data, label, signature, clear=clear)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if not a.get("signature"):
        print(f"signature cleared: {a['label']}")
        return
    print(f"signature set: {a['label']} ({len(a['signature'])} chars)")


def cmd_sales_account_list():
    """Internal (Skill-invoked): list the send-account ledger.
    BEACON_JSON=1 for machine output."""
    import sales_entities
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    ledger = sales_entities.list_send_accounts(data)
    default = sales_entities.get_send_identity(data)
    if json_mode:
        print(json.dumps({"send_accounts": ledger, "default": default},
                         ensure_ascii=False))
        return
    if not ledger:
        print("(台帳は空です)")
        return
    for a in ledger:
        mark = " ★default" if sales_entities._norm(a.get("label")) == \
            sales_entities._norm(default) else ""
        routes = ", ".join(
            f"{svc}={r.get('namespace')}" + (f":{r.get('alias')}" if r.get('alias') else "")
            for svc, r in (a.get("routes") or {}).items()) or "(routes 未設定)"
        print(f"  {a.get('label')} <{a.get('email')}>{mark}  [{routes}]")


def cmd_sales_gmail_permalink():
    """Internal (Skill-invoked): build the canonical Gmail permalink for a sent
    mail (e-3542; failure-mode split e-4185).

    Env: BEACON_SEND_FROM (resolved identity email), BEACON_RFC822_MSGID (rfc822
    Message-ID — old BEACON_MSGID kept as a back-compat alias; the new name says
    which id type is expected so a Gmail thread-id / API id is not passed by mistake).

    The three "cannot build" causes are told apart instead of all collapsing to a
    silent empty exit (e-4185):
    - empty BEACON_SEND_FROM = **wiring bug** → stderr + **exit 1** (the skill stops
      and surfaces it; a mis-wired caller can't drop permalinks unnoticed).
    - empty Message-ID, or a non-rfc822 id (no '@' = thread-id / API id) =
      **legitimate no-link** → **exit 0**, reason on stderr, empty stdout (the skill
      records the ref only, never a dead link).
    - success → URL on stdout, exit 0.
    """
    import sales_entities
    from_addr = os.environ.get("BEACON_SEND_FROM", "")
    # e-4185: renamed to name the id type; old var still honoured for back-compat.
    message_id = (os.environ.get("BEACON_RFC822_MSGID")
                  or os.environ.get("BEACON_MSGID", ""))
    try:
        url = sales_entities.build_gmail_permalink(from_addr, message_id)
    except sales_entities.PermalinkIdentityMissing as e:
        print(f"Error: {e}。permalink は記録しません — Step 2 の台帳解決 ($FROM) を"
              "見直してください。", file=sys.stderr)
        sys.exit(1)
    if url:
        print(url)
        return
    # legitimate no-link: exit 0, tell the human *why* (which of the two), empty stdout.
    mid = (message_id or "").strip().strip("<>").strip()
    if not mid:
        print("no-link: rfc822 Message-ID が空です — permalink 無し、ref のみ記録します。",
              file=sys.stderr)
    else:
        print("no-link: 渡された値は rfc822 Message-ID ではありません ('@' が無く、Gmail の "
              "thread-id / API id の可能性)。rfc822 Message-ID (例 <abc@mail.gmail.com>) を "
              "BEACON_RFC822_MSGID に渡すと辿れます。今回は ref のみ記録します。",
              file=sys.stderr)
    print("")  # empty stdout = no --source-url


def cmd_sales_account_resolve():
    """Internal (Skill-invoked): resolve the concrete MCP route for a service.
    Env: BEACON_SEND_SERVICE (gmail|calendar|drive), optional BEACON_SEND_LABEL
    (default send label when omitted). Prints JSON route on success (exit 0);
    exit 1 when the account/route is not configured — the Skill must not
    free-hand a namespace, so an unresolved route stops the send."""
    import sales_entities
    service = os.environ.get("BEACON_SEND_SERVICE", "")
    label = os.environ.get("BEACON_SEND_LABEL", "")
    data = load_project()
    try:
        route = sales_entities.resolve_route(data, service, label)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if route is None:
        who = label or sales_entities.get_send_identity(data) or "(default 未設定)"
        print(f"BLOCK: '{who}' の {service} route が台帳にありません。"
              "先に台帳へ登録してください", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(route, ensure_ascii=False))


def cmd_sales_account_remove():
    """Internal (Skill-invoked): remove a send account by label.
    Env: BEACON_SEND_LABEL."""
    import sales_entities
    label = os.environ.get("BEACON_SEND_LABEL", "")
    data = load_project()
    try:
        sales_entities.remove_send_account(data, label)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"send account removed: {label}")


def cmd_sales_account_transcript_source_set():
    """Internal (Skill-invoked): declare an Account's 議事録取得元 (e-3552).
    Env: BEACON_ACCOUNT_ID, BEACON_TS_TYPE (meet_calendar|drive_folder|external|
    manual), optional BEACON_TS_FOLDER_ID / BEACON_TS_NAMING / BEACON_TS_TOOL.

    #498 review (AX high): **空 BEACON_TS_TYPE での暗黙 clear を禁止** + **clear と設定値の
    共存を拒否**。消すには明示 **BEACON_TS_CLEAR=1**、かつそのとき type/folder_id/naming/tool
    を同時指定してはならない (stale env の CLEAR が set を silent に delete へ化けさせる経路を
    塞ぐ)。空/未設定 type かつ clear でない → exit1 (既存宣言は保持)。"""
    import sales_entities
    account_id = os.environ.get("BEACON_ACCOUNT_ID", "")
    clear = os.environ.get("BEACON_TS_CLEAR", "").strip().lower() in ("1", "true", "yes")
    source_type = os.environ.get("BEACON_TS_TYPE", "")
    folder_id = os.environ.get("BEACON_TS_FOLDER_ID", "")
    naming = os.environ.get("BEACON_TS_NAMING", "")
    tool = os.environ.get("BEACON_TS_TOOL", "")
    data = load_project()
    if not clear and not source_type.strip():
        print("Error: BEACON_TS_TYPE が未設定/空です。宣言するなら type を、消すなら "
              "BEACON_TS_CLEAR=1 を渡してください (空 type では既存宣言を消しません)。",
              file=sys.stderr)
        sys.exit(1)
    try:
        # clear のとき type/folder/naming/tool を渡すと lib が共存拒否で raise する
        # (stale env で set が delete に化けるのを防ぐ、単一の enforcement 点)。
        acc = sales_entities.set_account_transcript_source(
            data, account_id, source_type,
            folder_id=folder_id, naming=naming, tool=tool, clear=clear)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    ts = acc.get("transcript_source")
    if ts:
        print(f"transcript source: {account_id} → {json.dumps(ts, ensure_ascii=False)}")
    else:
        print(f"transcript source cleared: {account_id}")


def cmd_sales_account_transcript_source_get():
    """Internal (Skill-invoked): read an Account's 議事録取得元 declaration (e-3552).
    Env: BEACON_ACCOUNT_ID. Prints the JSON declaration (or `null` when unset)
    so meeting-wrap can resolve deterministically."""
    import sales_entities
    account_id = os.environ.get("BEACON_ACCOUNT_ID", "")
    data = load_project()
    try:
        ts = sales_entities.get_account_transcript_source(data, account_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(ts, ensure_ascii=False))


def cmd_account_phase():
    import sales_entities
    account_id = os.environ.get("BEACON_ACCOUNT_ID", "")
    new_phase = os.environ.get("BEACON_PHASE", "")
    note = os.environ.get("BEACON_PHASE_NOTE", "")
    data = load_project()
    for w in sales_entities.account_phase_warnings(data, new_phase):
        print(f"  ⚠ {w}", file=sys.stderr)
    if not account_id.startswith("acc-"):
        print(f"Error: expected an account id (acc-…), got {account_id!r}", file=sys.stderr)
        sys.exit(1)
    try:
        rec = sales_entities.phase_set(data, account_id, new_phase, note=note,
                                       at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"{account_id} phase → {rec['phase']} (recorded in phase_history)")


def cmd_account_delete():
    # e-3586: 物理削除でなく soft-cancel (取消)。証跡は残す。
    import sales_entities
    account_id = os.environ.get("BEACON_ACCOUNT_ID", "")
    force = os.environ.get("BEACON_FORCE", "") == "1"
    reason = os.environ.get("BEACON_CANCEL_REASON", "")
    data = load_project()
    try:
        orphaned = sales_entities.account_cancel(
            data, account_id, reason=reason, force=force)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Cancelled account {account_id}" + (f": {reason}" if reason else ""))
    if orphaned:
        print(f"  orphaned opportunities (account_id cleared): {', '.join(orphaned)}")


def cmd_account_contact():
    import sales_entities
    account_id = os.environ.get("BEACON_ACCOUNT_ID", "")
    name = os.environ.get("BEACON_CONTACT_NAME", "")
    role = os.environ.get("BEACON_CONTACT_ROLE", "")
    email = os.environ.get("BEACON_CONTACT_EMAIL", "")
    phone = os.environ.get("BEACON_CONTACT_PHONE", "")
    data = load_project()
    try:
        sales_entities.contact_add(data, account_id, name, role=role, email=email, phone=phone)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Added contact '{name}' to {account_id}")


def _warn_gate_unanchored(data, opp_id):
    """Print the「空ゲート (発火源 未紐づけ)」warning to stderr when the opportunity's
    open前進ゲート has no anchor yet — the derived predicate ``gate_needs_anchor``
    (ms-144 e-5202). Non-blocking (permissive, master=人間).

    Called at EVERY verb exit where an empty gate can be born (advance / jump / add /
    transition-date), not just the date path, so the cairn症状 (判定のきっかけが
    未結合のまま先へ進む) is surfaced consistently on the predicate rather than on one
    route only. Silent when the gate is already anchored or none is open (the
    predicate returns False), so it is safe to call unconditionally after any
    gate-touching verb — including on an acc- id (``gate_needs_anchor`` returns False
    for non-opp targets), so call sites need no external opp- guard (e-5203 review)."""
    import sales_entities
    if sales_entities.gate_needs_anchor(data, opp_id):
        print(f"  ⚠ {sales_entities.GATE_UNANCHORED_LABEL}: この前進ゲートには判定の"
              f"きっかけ (面談/活動) が結ばれていません。"
              f"`beacon opportunity anchor {opp_id} <work-item-id>` で結ぶと、"
              f"その完了で自動的にフェーズ判定が走ります。", file=sys.stderr)


def cmd_opportunity_add():
    import sales_entities
    title = os.environ.get("BEACON_OPP_TITLE", "")
    account_id = os.environ.get("BEACON_OPP_ACCOUNT", "")
    # Empty → the model picks the configured funnel entry (default_opportunity_phase).
    phase = os.environ.get("BEACON_OPP_PHASE", "")
    deadline = os.environ.get("BEACON_OPP_DEADLINE", "")
    ball = os.environ.get("BEACON_OPP_BALL", "") or sales_entities.BALL_SELF
    goal_raw = os.environ.get("BEACON_OPP_GOAL", "")
    prob_raw = os.environ.get("BEACON_OPP_PROBABILITY", "")
    assignee = os.environ.get("BEACON_OPP_ASSIGNEE", "")
    description = os.environ.get("BEACON_OPP_DESCRIPTION", "")  # ms-106 e-3526
    goal_amount = _parse_number(goal_raw, "--goal")
    probability = _parse_number(prob_raw, "--probability")
    _sales_skill_nudge("商談 (Opportunity)", "/beacon-sales-opportunity",
                       "顧客紐付け・開始フェーズ・想定金額・背景が起票の瞬間に揃います")
    data = load_project()
    _gate_target_class(data, "opportunity")
    try:
        opp_id = sales_entities.opportunity_add(
            data, title, account_id=account_id, phase=phase,
            goal_amount=goal_amount, probability=probability,
            deadline=deadline, who_has_the_ball=ball, assignee=assignee,
            description=description, created_at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # ms-106 e-3502 — seed the entry phase's anchor activities so a new deal
    # opens with its next steps instead of a blank list the rep must re-invent.
    # Done at the CLI (= sales workflow entry), same engine as phase advance
    # (instantiate_phase_activities, e-3270); idempotent by description.
    seeded = sales_entities.instantiate_phase_activities(
        data, opp_id, at=core._now_iso())
    save_project(data)
    # ms-143 e-5150: read the created deal back through the profession-generic
    # occupation.find_target (the sales twin of the milestone resolver, imported at
    # module top), not the sales-concrete find_opportunity — so the shared
    # ``opportunity_add`` verb no longer reaches a profession recorder
    # (KNOWN_SYMBOL_REACH row dropped).
    opp = occupation.find_target(data, opp_id, kind="opportunity")
    print(f"Added opportunity {opp_id}: {title}")
    if account_id:
        print(f"  account: {account_id}")
    print(f"  phase: {opp.get('phase', '') if opp else phase}")
    if seeded:
        print(f"  seeded {len(seeded)} フェーズ活動 (このフェーズの標準ステップ)")
    # ms-144 e-5202: a new deal opens with an empty前進ゲート — prompt to bind its
    # 発火源 here too (predicate-based, same as the other gate-touching verbs).
    _warn_gate_unanchored(data, opp_id)


def cmd_opportunity_assign():
    import sales_entities
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    assignee = os.environ.get("BEACON_ASSIGNEE", "")
    data = load_project()
    try:
        sales_entities.set_assignee(data, opp_id, assignee)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Assigned {opp_id} → {assignee or '(cleared)'}")


def cmd_opportunity_amount():
    import sales_entities
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    raw = os.environ.get("BEACON_OPP_AMOUNT", "")
    amount = _parse_number(raw, "<amount>")
    data = load_project()
    try:
        sales_entities.set_opportunity_amount(data, opp_id, amount)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Set amount on {opp_id}: {amount if amount is not None else '(cleared)'}")


def cmd_opportunity_rename():
    """Rename an opportunity's title (ms-120 e-3909).

    Before this the only post-creation edits were describe (背景) / assign /
    amount / phase — there was no way to fix the *name*, so a typo'd title was
    permanent. Parallels `milestone rename`. Env: BEACON_OPP_ID, BEACON_TITLE.
    """
    import sales_entities
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    title = os.environ.get("BEACON_TITLE", "")
    data = load_project()
    try:
        opp = sales_entities.opportunity_set_title(data, opp_id, title)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Renamed {opp_id} → {opp['title']}")


def cmd_opportunity_describe():
    """Set an opportunity's free-text 背景 / 経緯 / メモ (ms-106 e-3526)."""
    import sales_entities
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    description = os.environ.get("BEACON_OPP_DESCRIPTION", "")
    data = load_project()
    try:
        sales_entities.opportunity_set_description(data, opp_id, description)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if description.strip():
        print(f"Set description on {opp_id}")
    else:
        print(f"Cleared description on {opp_id}")


def cmd_opportunity_phase_prob():
    import sales_entities
    phase = os.environ.get("BEACON_PHASE_NAME", "")
    raw = os.environ.get("BEACON_PHASE_PROB", "")
    prob = _parse_number(raw, "<probability>")
    data = load_project()
    try:
        sales_entities.set_phase_probability(data, phase, prob)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Set probability on phase {phase}: {prob}%")


def cmd_sales_target():
    import sales_entities
    member = os.environ.get("BEACON_TARGET_MEMBER", "")
    raw = os.environ.get("BEACON_TARGET_AMOUNT", "")
    amount = _parse_number(raw, "<amount>") if raw.strip() else None
    data = load_project()
    try:
        sales_entities.set_sales_target(data, member, amount)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Target for {member}: {amount if amount is not None else '(cleared)'}")


def cmd_sales_target_list():
    import sales_entities
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    targets = sales_entities.sales_targets(data)
    if json_mode:
        print(json.dumps(targets, ensure_ascii=False))
        return
    if not targets:
        print("No sales targets set. Set one with: beacon sales target <user> <amount>")
        return
    for member, amt in targets.items():
        pipe = sales_entities.weighted_pipeline(data, assignee=member)
        print(f"  {member}: 目標 {amt} / 見込み {pipe:.0f}")




def cmd_opportunity_list():
    import sales_entities
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    show_all = os.environ.get("BEACON_ALL", "") == "1"  # e-3586: --all で取消済も出す
    data = load_project()
    # e-3586: 既定は取消済 (cancelled) を除外。--all で全件。
    opps = (occupation.target_records(data, "opportunity") if show_all
            else sales_entities.live_opportunities(data))
    if json_mode:
        # ms-144 e-5178 / e-5203: surface the open-gate twins「no発火源」and「no
        # 遷移日」as derived flags so the AI (and the cockpit) can see an unanchored
        # or dateless gate without re-deriving it from raw gates[]. Both twins are
        # projected symmetrically (e-5203): the cockpit reads gate_needs_anchor AND
        # needs_transition_date as facts, not one翼 from a flag and the other from a
        # raw gates[] walk. Projection only — not persisted onto the record.
        enriched = [{**o,
                     "gate_needs_anchor": sales_entities.gate_needs_anchor(data, o["id"]),
                     "needs_transition_date": sales_entities.needs_transition_date(data, o["id"])}
                    for o in opps]
        print(json.dumps(enriched, ensure_ascii=False, indent=2))
        return
    if not opps:
        print("No opportunities yet. Add one with: beacon opportunity add \"<title>\"")
        return
    import work_model
    import datetime
    today = datetime.date.today().isoformat()
    for o in opps:
        # e-3586: --all 時のみ現れる取消済は取消線相当のマーカーで区別する。
        if work_model.is_cancelled(o):
            reason = (o.get("meta", {}) or {}).get("cancel_reason", "")
            print(f"[{o['id']}] ~~{o.get('title', '')}~~ (取消済"
                  + (f": {reason}" if reason else "") + ")")
            continue
        acc = o.get("account_id") or "-"
        deadline = f" due {o['deadline']}" if o.get("deadline") else ""
        ball = o.get("who_has_the_ball", "")
        # e-3580 fold: 遷移日は商談ではなく open な前進ゲートが持つ。
        td = sales_entities.get_transition_date(data, o["id"])
        # 遷移日 = 判定予定日 (SPEC §2/§3). 判定待ちの overdue/due は距離を強調して促す。
        st = sales_entities.transition_status(data, o["id"], today)
        if st == sales_entities.TRANSITION_OVERDUE:
            td_str = f" / ⚠ 遷移日 超過 {td} (判定待ち)"
        elif st == sales_entities.TRANSITION_DUE:
            td_str = f" / ⏰ 遷移日 本日 {td} (要判定)"
        elif st == sales_entities.TRANSITION_SCHEDULED:
            td_str = f" / 遷移日: {td}"
        elif st == sales_entities.TRANSITION_UNSET:
            td_str = " / ⚠ 遷移日 未設定"
        else:  # settled
            td_str = ""
        # status は phase からの派生ミラーで参照ゼロ (表示のみ) だったため list から除外。
        # 保存フィールドの整理はリファクタ時 (UI FB 2026-07-17)。
        print(f"[{o['id']}] {work_model.target_label(o)} — phase: {o.get('phase', '?')} "
              f"/ account: {acc}"
              f"{deadline} / ball: {ball}{td_str} "
              f"/ activities: {len(o.get('activities', []))}")
        # e-3584: 前進ゲート (advance gate) の状態を一貫した呼称で見せる。
        # 空 = 発火源を確保せよ / 確定 = 完了に向けて準備せよ (SPEC 方針5B)。
        gate = sales_entities.current_gate(data, o["id"])
        done_n = len(sales_entities.gate_history(data, o["id"]))
        if gate is None:
            gate_str = ("決着済み"
                        if sales_entities.opportunity_phase_is_terminal(data, o.get("phase", ""))
                        else "無し")
        elif sales_entities.gate_needs_anchor(data, o["id"]):
            # e-5178 maint-a: the「anchor 空」判定 comes from the one predicate
            # (gate_needs_anchor), not a second inline copy — commands only builds
            # the display string around it.
            gate_str = f"空 ({sales_entities.GATE_UNANCHORED_LABEL})"
        else:
            gate_str = f"確定 (発火源 {gate['anchor']})"
        print(f"    前進ゲート: {gate_str} / 通過フェーズ履歴: {done_n}")


def cmd_opportunity_phase():
    import sales_entities
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    new_phase = os.environ.get("BEACON_PHASE", "")
    note = os.environ.get("BEACON_PHASE_NOTE", "")
    data = load_project()
    # Surface vocabulary / terminal-rule violations BEFORE the write, but never
    # block: master = human (SPEC §5). Warnings compare against the current phase.
    warnings = []
    if opp_id.startswith("opp-"):
        # ms-142 e-5169: resolve the Target through the profession-generic
        # occupation.find_target (the sales twin of the milestone resolver greened
        # in ms-143), not the sales-concrete find_opportunity — so the shared
        # ``opportunity_phase`` verb no longer reaches a profession recorder. The
        # transition itself already rides set_target_state via jump_transition.
        import occupation
        opp = occupation.find_target(data, opp_id, kind="opportunity")
        cur = opp.get("phase", "") if opp else ""
        # e-3527: pass the deal's 想定金額 (goal or amount) so require_amount
        # phases can warn when neither is set.
        warnings = sales_entities.opportunity_phase_warnings(
            data, cur, new_phase,
            goal_amount=(opp.get("goal_amount") if opp else None),
            amount=(opp.get("amount") if opp else None))
    elif opp_id.startswith("acc-"):
        warnings = sales_entities.account_phase_warnings(data, new_phase)
    for w in warnings:
        print(f"  ⚠ {w}", file=sys.stderr)
    try:
        # ms-106 e-3688 (fable C-1) — an opportunity phase declaration must go
        # through the advance-gate lifecycle so the change + note land as
        # evidence on the gate列. jump_transition settles the current gate and
        # opens a fresh one; only accounts (not gated) use the raw phase_set.
        if opp_id.startswith("opp-"):
            rec = sales_entities.jump_transition(data, opp_id, new_phase,
                                                 note=note, at=core._now_iso(),
                                                 actor=_human_actor())
        else:
            rec = sales_entities.phase_set(data, opp_id, new_phase, note=note,
                                           at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # ms-106 e-3502 — a manual phase move should seed the new phase's anchor
    # activities too, so create / judge-advance / manual-phase all behave the
    # same (the rep never lands in a phase with a blank next-steps list). CLI
    # layer, opportunities only (accounts have no activity templates); idempotent
    # by description via instantiate_phase_activities.
    seeded = []
    if opp_id.startswith("opp-"):
        seeded = sales_entities.instantiate_phase_activities(
            data, opp_id, at=core._now_iso())
    save_project(data)
    # C-6: opportunities record the change on the advance-gate列 (前進ゲート),
    # not phase_history (that field left the opportunity in the e-3580 fold);
    # accounts still keep an append-only phase_history.
    where = "前進ゲート列に記録" if opp_id.startswith("opp-") else "recorded in phase_history"
    print(f"{opp_id} phase → {rec['phase']} ({where})")
    if seeded:
        print(f"  seeded {len(seeded)} フェーズ活動 (このフェーズの標準ステップ)")
    # ms-144 e-5202: a jump settles the old gate and opens a fresh (empty) one, so
    # prompt to bind its発火源. Called unconditionally (e-5203 maint review): the
    # predicate gate_needs_anchor already returns False for acc-/non-opp ids, so no
    # external opp- guard is needed — the helper is safe on any id.
    _warn_gate_unanchored(data, opp_id)


def cmd_opportunity_transition_date():
    """Set (or clear) an opportunity's 遷移日 (transition_date, ms-107 e-3371).

    Env: BEACON_OPP_ID, BEACON_TRANSITION_DATE (empty clears), BEACON_PHASE_NOTE.
    The date is the planned day this phase's goal is judged (SPEC §2); the
    change is logged append-only in transition_date_history."""
    import sales_entities
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    transition_date = os.environ.get("BEACON_TRANSITION_DATE", "")
    note = os.environ.get("BEACON_PHASE_NOTE", "")
    data = load_project()
    try:
        rec = sales_entities.set_transition_date(
            data, opp_id, transition_date, note=note, at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if rec["transition_date"]:
        print(f"{opp_id} transition_date → {rec['transition_date']} "
              f"(recorded in transition_date_history)")
        # ms-144 e-5178 / e-5202: placing a判定日 on a gate with no発火源 is the
        # exact cairn症状 (opp-3/opp-4) — the date is set but nothing will fire the
        # judgement. The warning now rides the shared ``_warn_gate_unanchored`` helper
        # (e-5202) so this route and advance / jump / add all prompt off the same
        # predicate. Warn to stderr but never block (permissive 原則, master=人間).
        _warn_gate_unanchored(data, opp_id)
    else:
        print(f"{opp_id} transition_date cleared (recorded in transition_date_history)")


def cmd_opportunity_anchor():
    """Bind a work item (面談 mtg- / 活動 act-) as the発火源 of an opportunity's open
    前進ゲート (ms-144 e-5177).

    A nurturing (nrt-) is NOT accepted here — it lives under an Account, not an
    Opportunity, so ``anchor_opportunity_gate`` rejects it with a recovery hint
    (ms-144 e-5223: the docstring lists only what the verb actually binds, so the
    surface never reads as if nrt- were allowed).

    Env: BEACON_OPP_ID, BEACON_WORK_ITEM_ID. Wraps
    sales_entities.anchor_opportunity_gate — idempotent, ownership-checked, and
    an explicit error (exit 1) when no gate is open / the work item is unknown
    or belongs to another商談 (so 「確定 (発火源 X)」 is never a lie)."""
    import sales_entities
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    work_item_id = os.environ.get("BEACON_WORK_ITEM_ID", "")
    data = load_project()
    try:
        gate, changed, synced_date = sales_entities.anchor_opportunity_gate(
            data, opp_id, work_item_id, at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # Report only what actually happened — the verb's point is not to claim
    # state it did not set (review A3). An idempotent re-anchor changed nothing,
    # so nothing is written and the message says so.
    if not changed:
        print(f"{opp_id} 前進ゲート {gate['id']} は既に発火源 {gate['anchor']} に"
              f"結合済み (変更なし)")
        return
    save_project(data)
    synced_str = f" (遷移日 {synced_date} に同期)" if synced_date else ""
    print(f"{opp_id} 前進ゲート {gate['id']} → 発火源 {gate['anchor']} に結合"
          f"{synced_str} (前進ゲート履歴に記録)")


def _human_actor() -> str:
    """The human master's identity for a human-confirmed state transition
    (phase judge / manual phase jump). ms-106 e-3691 (fable review C-2).

    Unlike ``work_base.current_actor()`` — which stamps ``"claude"`` inside
    Claude Code to mark AI-authored commits — a gate judgement is the human's
    master decision (SPEC §3/§6, master=人間). The AI only *applies* what the
    human confirmed in the Skill, so the gate's audit row must attribute the
    decision to the person, not to the AI running the CLI. Resolves to
    ``human:<git user.email>``, else ``human:<os user>``, else ``"human"``.
    """
    import subprocess
    try:
        email = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=2).stdout.strip()
        if email:
            return f"human:{email}"
    except Exception:
        pass
    try:
        import getpass
        return f"human:{getpass.getuser()}"
    except Exception:
        return "human"


def _print_phase_fold(fold) -> None:
    """Surface the e-3553 phase-fold result of a transition. Evidence-linked
    activities were auto-closed (記帳=自動) — reported so the human sees what was
    tidied; the evidence-less ones need a human call (done / cancel / carry) and
    are listed with the exact commands. No-op when nothing was folded."""
    if not isinstance(fold, dict):
        return
    auto = fold.get("auto_done") or []
    pending = fold.get("needs_decision") or []
    if auto:
        print(f"  ✓ 前フェーズの活動 {len(auto)} 件を証跡ありで自動クローズ: "
              f"{', '.join(auto)}")
    for d in pending:
        print(f"  ▸ 要判断 {d.get('id')}: {d.get('description')}")
        print(f"      {d.get('reason')}")
    if pending:
        print("      → done: beacon activity done <id> / "
              "cancel: beacon activity cancel <id> --reason <理由>")


def cmd_opportunity_judge():
    """Judge a transition (ms-107 e-3372, SPEC §3): advance / retry / terminal.

    Env: BEACON_OPP_ID, BEACON_JUDGE_DECISION (advance|retry|terminal),
    BEACON_JUDGE_ARG (retry→new date, terminal→terminal phase; advance→optional
    next date), BEACON_PHASE_NOTE. The human decides the branch; this applies it
    (AI never auto-changes state — master=人間)."""
    import sales_entities
    # ms-143 e-5150: resolve the deal for read-back through the profession-generic
    # occupation.find_target (imported at module top), not the sales-concrete
    # find_opportunity — so the shared ``opportunity_judge`` verb no longer reaches a
    # profession recorder (KNOWN_SYMBOL_REACH row dropped). The transitions
    # themselves ride the sales judge-gate verbs (advance/retry/terminal_transition),
    # unchanged.
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    decision = os.environ.get("BEACON_JUDGE_DECISION", "")
    arg = os.environ.get("BEACON_JUDGE_ARG", "")
    note = os.environ.get("BEACON_PHASE_NOTE", "")
    # e-3909: the trailing positional (BEACON_JUDGE_ARG) means a DIFFERENT thing
    # per verb, so validate it per verb instead of leaving the AI to guess the
    # shape from a single opaque positional:
    #   advance  → optional next transition date (YYYY-MM-DD)
    #   retry    → REQUIRED new transition date (retry = 未達だが継続, 遷移日を置き直す)
    #   terminal → REQUIRED terminal phase (決着先, from allowed_terminals)
    if decision == "retry" and not arg.strip():
        print("Error: `opportunity judge <opp> retry <YYYY-MM-DD>` requires a new "
              "transition date — retry keeps the same phase but re-sets the "
              "transition date.", file=sys.stderr)
        sys.exit(1)
    if decision == "terminal" and not arg.strip():
        print("Error: `opportunity judge <opp> terminal <terminal-phase>` requires a "
              "terminal phase (決着先). Run `beacon opportunity judge <opp>` with no "
              "decision to see the allowed terminals.", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    at = core._now_iso()
    actor = _human_actor()  # C-2: the judgement is the human's master decision
    try:
        if decision == "advance":
            res = sales_entities.advance_transition(
                data, opp_id, next_transition_date=arg, note=note, at=at, actor=actor)
            save_project(data)
            print(f"{opp_id} advance → phase {res['phase']}")
            # e-3270: フェーズ固有の固定アンカー活動を自動起票 (あれば)。
            # 商談レコードは一度だけ解決し (ループ内で毎回引き直さない)、None は
            # {} に畳んで terminal 分岐と同じ None ガード流儀に揃える (e-5150 保守性
            # レビュー finding)。
            adv_activities = (occupation.find_target(data, opp_id, kind="opportunity") or {}).get("activities", [])
            for aid in res.get("activities", []):
                act = next((a for a in adv_activities if a["id"] == aid), None)
                if act:
                    print(f"  + 活動 {aid}: {act['description']} (テンプレ)")
            if arg:
                print(f"  次の遷移日: {arg}")
            elif sales_entities.needs_transition_date(data, opp_id):
                # SPEC §2: 新フェーズ入場 → 遷移日設定が最優先。lead があれば候補提示。
                base = _today_iso()
                sug = sales_entities.suggest_transition_date(data, res["phase"], base)
                hint = f" (候補: {sug})" if sug else ""
                # e-5203 AX review: this advisory rides stderr to match its twin
                # ``_warn_gate_unanchored`` below — both non-blocking prompts on a
                # fresh gate go to the same stream (stdout stays machine-parseable).
                print(f"  ⚠ 次フェーズの遷移日が未設定です{hint} — "
                      f"beacon opportunity transition-date {opp_id} <YYYY-MM-DD>",
                      file=sys.stderr)
            # ms-144 e-5202: advancing opens a fresh gate for the new phase — prompt
            # to bind its発火源 too (the anchor twin of the 遷移日 warning above; both
            # are empty on a fresh gate). Predicate-based, same helper as add / jump.
            _warn_gate_unanchored(data, opp_id)
            _print_phase_fold(res.get("fold"))
        elif decision == "retry":
            sales_entities.retry_transition(data, opp_id, arg, note=note, at=at, actor=actor)
            save_project(data)
            print(f"{opp_id} retry → 同フェーズ継続、新しい遷移日: {arg}")
        elif decision == "terminal":
            # 決着候補の外を宣言した時は warning を出す (block しない、master=人間)。
            opp = occupation.find_target(data, opp_id, kind="opportunity")
            cur = opp.get("phase", "") if opp else ""
            for w in sales_entities.opportunity_phase_warnings(data, cur, arg):
                print(f"  ⚠ {w}", file=sys.stderr)
            trec = sales_entities.terminal_transition(data, opp_id, arg, note=note, at=at, actor=actor)
            # ms-163 e-5879/5880: 商談の決着 (終端遷移) は完遂 — 開発の milestone done と
            # 同じく generic な完遂 seam (deliverable 記録 + 完遂 decision) を発火する。
            # opp は上で find_target 済 (terminal_transition が同じ dict の phase を更新)。
            # on_target_completion は handler 本体から DIRECT に呼ぶ — helper へ抽出すると
            # checker (direct-call attribution) が被覆 credit を落とす (COMPLETION_PRODUCER_CALLS)。
            import target_completion
            target_completion.on_target_completion(data, opp, verdict=arg, reason=note)
            save_project(data)
            print(f"{opp_id} terminal → {arg} (決着、遷移日は用済みでクリア)")
            _print_phase_fold(trec.get("fold"))
        else:
            print(f"Error: decision must be advance|retry|terminal, got {decision!r}",
                  file=sys.stderr)
            sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        # terminal 候補を添える (失敗時の次アクションを示す)。
        if decision in ("terminal", "advance"):
            try:
                allowed = sales_entities.allowed_terminals_for(data, opp_id)
                if allowed:
                    print(f"  この段階から決着できるのは: {', '.join(allowed)}",
                          file=sys.stderr)
            except ValueError:
                pass
        sys.exit(1)


def cmd_opportunity_due():
    """締切精査 (deadline review, ms-107 e-3271 + ms-139 e-4951): due/overdue な
    2 種類を surface する — (1) 商談の遷移日 (transition_date)、(2) 準備活動の期日
    (activity.deadline)。以前は (1) だけで、活動の期日超過は WebUI しか出さず AI が
    『8/7の会食どうでした?』と気づけなかった。どちらも who-has-the-ball で行動が
    割れる。BEACON_JSON=1 で machine 出力 (``{"opportunities": [...],
    "activities": [...]}``)。AI の overdue catch surface。"""
    import sales_entities
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    today = _today_iso()
    rows = sales_entities.opportunities_awaiting_judgement(data, today)
    acts = sales_entities.overdue_activities(data, today)  # ms-139 e-4951
    if json_mode:
        print(json.dumps({"opportunities": rows, "activities": acts},
                         ensure_ascii=False))
        return
    if not rows and not acts:
        print("締切精査: 期日 到達/超過の商談・活動はありません")
        return

    def _fmt(r):
        mark = "⚠ 超過" if r["transition_status"] == sales_entities.TRANSITION_OVERDUE else "⏰ 本日"
        return (f"  [{r['id']}] {r['title']} — phase: {r['phase']} / "
                f"遷移日 {r['transition_date']} {mark}")

    mine = [r for r in rows if r.get("who_has_the_ball") == sales_entities.BALL_SELF]
    theirs = [r for r in rows if r.get("who_has_the_ball") == sales_entities.BALL_COUNTERPART]
    if mine:
        print("商談の遷移日 — 自分のボール (判定/対応が必要):")
        for r in mine:
            print(_fmt(r))
        print("  → beacon opportunity judge <opp-id> advance|retry|terminal で判定")
    if theirs:
        print("商談の遷移日 — 相手のボール (相手待ちが期限超過、催促が必要):")
        for r in theirs:
            print(_fmt(r))
        print("  → 相手に催促 (メール/日程調整) or beacon opportunity judge で決着判断")

    # ms-139 e-4951: 活動 (準備行動) の期日超過。実施済みなら done、やめたなら cancel
    # で盤面から外す。
    if acts:
        def _fmt_act(a):
            mark = "⚠ 超過" if a["activity_status"] == sales_entities.TRANSITION_OVERDUE else "⏰ 本日"
            ball = "自分" if a["who_has_the_ball"] == sales_entities.BALL_SELF else "相手"
            return (f"  [{a['act_id']}] {a['description']} — {a['opp_title']} "
                    f"({a['opp_id']}) / 期日 {a['deadline']} {mark} / ボール:{ball}")
        print("準備活動の期日 — 到達/超過:")
        for a in acts:
            print(_fmt_act(a))
        print("  → 実施済みなら beacon opportunity activity done <act-id> / "
              "やめたなら cancel / 期日を延ばすなら update --deadline")


def cmd_deadline_due():
    """締切精査 (ms-142 e-5010): 職種横断で期日 到達/超過 の work item を surface する
    単一経路。サーバの overdue リマインダ (server/app.py) と同じ
    ``occupation.iter_deadline_candidates`` を consume し、L2 締切規則
    (``deadline.work_item_temporal_status``) を適用する。だから開発 (milestone の
    target_date / task の deadline) も営業 (activity の deadline) も、職種で分岐せず 1
    経路で拾える。session-start の締切表示 (scripts/session-start-deadlines.py) が
    ``beacon deadline due --json`` としてこれを呼ぶ。BEACON_JSON=1 で machine 出力
    (``{"items": [{kind, label, deadline, temporal, context}]}``、古い期日順)。"""
    import occupation
    import deadline
    import work_model
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    today = _today_iso()
    _terminal = {work_model.DONE_STATUS, work_model.CANCELLED_STATUS}
    items = []
    for cand in occupation.iter_deadline_candidates(data):
        # A work item under a terminal (done/cancelled) Target is noise — skip it,
        # matching the session-start display's historical behavior (ms-139
        # finding#4). A terminal Target itself is excluded by the temporal rule
        # below (its own status is terminal), so this is uniform across levels.
        if cand.get("target_status") in _terminal:
            continue
        st = deadline.work_item_temporal_status(cand["item"], today)
        if st not in (deadline.TRANSITION_DUE, deadline.TRANSITION_OVERDUE):
            continue
        items.append({
            "kind": cand["kind"],
            # ms-143 e-5047: resolve the display label here (where project data +
            # descriptors are loaded) and carry it in the payload, so the JSON
            # consumer (scripts/session-start-deadlines.py) need not re-hardcode a
            # kind→label map. A new occupation's label rides from its descriptor.
            "kind_label": occupation.kind_display_label(data, cand["kind"]),
            "label": cand["label"],
            "deadline": deadline.deadline_of(cand["item"]),
            "temporal": st,
            "context": cand["context"],
        })
    items.sort(key=lambda r: r.get("deadline") or "")
    if json_mode:
        print(json.dumps({"items": items}, ensure_ascii=False))
        return
    if not items:
        print("締切精査: 期日 到達/超過の work item はありません")
        return
    print("⏰ 締切超過/本日 の work item:")
    for r in items:
        mark = "⚠ 超過" if r["temporal"] == deadline.TRANSITION_OVERDUE else "⏰ 本日"
        ctx = f" — {r['context']}" if r.get("context") else ""
        print(f"  [{r['kind_label']}] {r['label']} / "
              f"期日 {r['deadline']} {mark}{ctx}")
    print("  → 済んだら完了/期日を延ばす/やめたら取消 で盤面から外してください。")


def cmd_opportunity_delete():
    # e-3586: 物理削除でなく soft-cancel (取消)。中の証跡 (活動/証跡) を消さない。
    import sales_entities
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    reason = os.environ.get("BEACON_CANCEL_REASON", "")
    data = load_project()
    try:
        sales_entities.opportunity_cancel(data, opp_id, reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Cancelled opportunity {opp_id}" + (f": {reason}" if reason else ""))


def cmd_phase_list():
    """Show the configured phase funnels (per-company vocabulary), not a single
    entity's transitions. This is the sales-project's methodology, editable
    config living in project.json."""
    import sales_entities
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    frame = sales_entities.opportunity_phase_frame(data)  # e-3582: 前進の macro-frame
    # e-4405 (ms-129): a sales project with no custom funnel configured still runs
    # on the built-in DEFAULT_* seeds — entity creation uses `account_phases(data)
    # or DEFAULT_ACCOUNT_PHASES` (sales_entities:216/228/1103), so 商談準備 / リード
    # etc. ARE in effect. Read data-only, this command showed nothing and printed
    # "not a sales project", making a営業 user think profession detection broke and
    # the effective defaults look "未設定". `effective_phases` resolves the SAME
    # config-or-default funnel entities use, with a per-funnel source label, so we
    # show it, flag which funnels are built-in defaults, and reserve the
    # "not a sales project" line for genuinely non-sales projects.
    profession = (data.get("profession") or "").strip().lower()
    eff = sales_entities.effective_phases(data)
    acc_phases, opp_phases, prospect_phases = (
        eff["account"], eff["opportunity"], eff["prospect"])
    using_default = eff["using_builtin_default"]
    if json_mode:
        print(json.dumps({"account_phases": acc_phases,
                          "opportunity_phases": opp_phases,
                          "prospect_phases": prospect_phases,
                          "opportunity_phase_frame": frame,
                          "phases_source": eff["source"],
                          "using_builtin_default": using_default},
                         ensure_ascii=False, indent=2))
        return
    if not acc_phases and not opp_phases and not prospect_phases:
        # profession=sales fills the defaults above, so this is only reached by a
        # non-sales project (or a sales project explicitly stripped of all phases).
        if profession == "sales":
            print("フェーズ funnel が未設定です（beacon phase add で追加できます）。")
        else:
            print("No phase funnel configured (not a sales project, or no phases set).")
        return
    if using_default:
        # Name which funnels are on the built-in default so it's not a blanket
        # claim (a project may have custom account phases but a default商談 funnel).
        _defaulted = [jp for key, jp in
                      (("account", "顧客"), ("opportunity", "商談"), ("prospect", "打診"))
                      if eff["source"][key] == sales_entities.PHASE_SOURCE_DEFAULT]
        print(f"（{'・'.join(_defaulted)} は組み込みデフォルトのフェーズを使用中 — "
              f"beacon phase add / rename 等で会社ごとに編集できます）\n")
    # e-3582: フェーズを読む前に「フェーズ = 次へ抜けさせるもの」の枠組みを刷り込む。
    if opp_phases:
        print(f"■ 前進の枠組み: {frame}\n")
    print("顧客 (Account) phases:")
    for p in acc_phases:
        print(f"  - {p.get('name')}")
    print("商談 (Opportunity) phases:")
    for p in opp_phases:
        if p.get("terminal"):
            outcome = p.get("outcome", "")
            print(f"  - {p.get('name')} [terminal → {outcome}]")
            continue
        allowed = p.get("allowed_terminals")
        prob = p.get("probability")
        bits = []
        if prob is not None:
            bits.append(f"prob {prob}")
        if allowed:
            bits.append(f"→ {'/'.join(allowed)}")
        suffix = f"  ({', '.join(bits)})" if bits else ""
        print(f"  - {p.get('name')}{suffix}")
        # Methodology (ms-107 e-3371): shown only when configured (seed carries
        # none yet; the 営業アダプタ config is seeded in e-3375).
        m = sales_entities.phase_methodology(p)
        if m["goal"]:
            print(f"      ゴール: {m['goal']}")
        if m["activity_template"]:
            print(f"      活動テンプレ: {', '.join(str(a) for a in m['activity_template'])}")
        # e-3581: 遷移判定手段 (transition_signal) は撤去。判定は前進ゲートに
        # 紐づけた work-item の完了が促す (フェーズ固定の分岐ではない)。
        if m["default_lead"] is not None:
            print(f"      遷移日リード: {m['default_lead']}日")
    # ms-132 e-4502: 打診フェーズ funnel — attack-list の相手ごとの状態語彙。
    if prospect_phases:
        print("打診 (attack-list prospect) phases:")
        for p in prospect_phases:
            print(f"  - {p.get('name')}")


# --- phase funnel editing (ms-116) -----------------------------------------
# Edit a running project's saved phase funnel (段の追加/挿入/改名/並べ替え/削除).
# The <funnel> arg selects which target-class's funnel (account / opportunity).
# Every edit routes through save_project so the change reaches cloud (MySQL
# projects 行) and the Web UI payload just like any other sales mutation
# (ms-116 方針2). Editing is gated by the ms-115 containment rule: only the
# profession that owns the target-class may edit its funnel (方針5 / e-3822).

_FUNNEL_KIND_ALIASES = {
    "account": "account", "accounts": "account", "顧客": "account",
    "opportunity": "opportunity", "opportunities": "opportunity",
    "opp": "opportunity", "商談": "opportunity",
    # ms-132 e-4502: 打診フェーズ funnel (attack-list の相手ごとの状態)。
    "prospect": "prospect", "prospects": "prospect", "打診": "prospect",
}

# The target-class whose ownership gates editing each funnel. account / opportunity
# are themselves target-classes; the prospect funnel governs attack-lists that hang
# off Acquisitions, so it is gated by acquisition ownership (ms-132 e-4502).
_FUNNEL_OWNING_CLASS = {
    "account": "account", "opportunity": "opportunity", "prospect": "acquisition",
}


def _resolve_funnel_kind(raw: str) -> str:
    kind = _FUNNEL_KIND_ALIASES.get((raw or "").strip().lower(), "")
    if not kind:
        print(f"Error: unknown funnel '{raw}' "
              f"(expected: account | opportunity | prospect)", file=sys.stderr)
        sys.exit(1)
    return kind


def _guard_funnel_owned(data: dict, kind: str) -> None:
    """Refuse to edit a funnel the project's profession does not own, reusing
    the ms-115 containment gate. account / opportunity / prospect are all
    sales-owned (prospect maps to the acquisition target-class it governs), so a
    dev project is blocked with the same guidance-rich message target creation
    uses (ms-116 e-3822 — enforce 職種 > 対象 for funnel edits too)."""
    import occupation
    owning_class = _FUNNEL_OWNING_CLASS.get(kind, kind)
    try:
        occupation.assert_target_class_owned(data, owning_class)
    except occupation.TargetClassProfessionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_phase_add():
    """Add a stage to a funnel: append to the end, or insert at --index."""
    import sales_entities
    kind = _resolve_funnel_kind(os.environ.get("BEACON_FUNNEL_KIND", ""))
    name = os.environ.get("BEACON_PHASE_NAME", "")
    raw_index = os.environ.get("BEACON_PHASE_INDEX", "")
    index = int(_parse_number(raw_index, "<index>")) if raw_index.strip() else None
    data = load_project()
    _guard_funnel_owned(data, kind)
    try:
        sales_entities.insert_phase(data, kind, name, index=index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    where = f"index {index}" if index is not None else "end"
    print(f"Added {kind} phase '{name}' ({where})")


def cmd_phase_rename():
    import sales_entities
    kind = _resolve_funnel_kind(os.environ.get("BEACON_FUNNEL_KIND", ""))
    old = os.environ.get("BEACON_PHASE_OLD", "")
    new = os.environ.get("BEACON_PHASE_NEW", "")
    data = load_project()
    _guard_funnel_owned(data, kind)
    try:
        sales_entities.rename_phase(data, kind, old, new)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Renamed {kind} phase '{old}' → '{new}'")


def cmd_phase_move():
    import sales_entities
    kind = _resolve_funnel_kind(os.environ.get("BEACON_FUNNEL_KIND", ""))
    name = os.environ.get("BEACON_PHASE_NAME", "")
    index = int(_parse_number(os.environ.get("BEACON_PHASE_INDEX", ""), "<index>"))
    data = load_project()
    _guard_funnel_owned(data, kind)
    try:
        sales_entities.move_phase(data, kind, name, index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Moved {kind} phase '{name}' → index {index}")


def cmd_phase_remove():
    import sales_entities
    kind = _resolve_funnel_kind(os.environ.get("BEACON_FUNNEL_KIND", ""))
    name = os.environ.get("BEACON_PHASE_NAME", "")
    data = load_project()
    _guard_funnel_owned(data, kind)
    try:
        sales_entities.remove_phase(data, kind, name)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Removed {kind} phase '{name}'")


def cmd_opportunity_activity():
    import sales_entities
    opp_id = os.environ.get("BEACON_OPP_ID", "")
    desc = os.environ.get("BEACON_ACTIVITY_DESC", "")
    deadline = os.environ.get("BEACON_ACTIVITY_DEADLINE", "")
    ball = os.environ.get("BEACON_ACTIVITY_BALL", "") or sales_entities.BALL_SELF
    data = load_project()
    try:
        # ms-143: add the activity through the profession-generic
        # occupation.add_work_item (an opportunity's activities ARE its work-item
        # arm), NOT the dev... sales-concrete sales_entities.activity_add symbol, so
        # this L2 verb stops symbol-reaching a PROFESSION_CONCRETE_SYMBOL. The sales
        # validations (opportunity exists / ball vocabulary / description required)
        # and the created_in_phase default stay here as the sales frontend concern —
        # same shape sales_entities.activity_add produces (parity).
        opp = occupation.find_target(data, opp_id, kind="opportunity")
        if opp is None:
            raise ValueError(f"Opportunity not found: {opp_id}")
        if not desc or not desc.strip():
            raise ValueError("Activity description is required")
        if ball not in sales_entities.VALID_BALL:
            raise ValueError(
                f"who_has_the_ball must be one of "
                f"{sorted(sales_entities.VALID_BALL)}, got {ball!r}")
        act = occupation.add_work_item(
            data, opp_id, description=desc.strip(), deadline=deadline,
            who_has_the_ball=ball, source="", created_at=core._now_iso(),
            created_in_phase=opp.get("phase", ""))
        act_id = act["id"]
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Added activity {act_id} to {opp_id}: {desc}")


def cmd_sales_reply_watch_op_ensure():
    """Internal (Skill-invoked): ensure a reply-watch Operation exists so the
    server tick drives ``/beacon-sales-reply-watch`` hourly (ms-106 e-3504
    Phase 2). Idempotent — reuses an Operation whose ``meta.execute_skill`` is
    the reply-watch skill, and repairs its tick flags if missing. Prints the
    op-id and whether it still needs ``beacon operation approve`` to arm
    auto-execute (the human mints the standing authorization once)."""
    SKILL = "beacon-sales-reply-watch"
    data = load_project()
    op = None
    for cand in data.get("operations", []) or []:
        if (cand.get("meta") or {}).get("execute_skill") == SKILL:
            op = cand
            break
    created = False
    if op is None:
        data, op = core.operation_open(
            data, "返信ウォッチャー (自動)", schedule="weekdays", status="open",
            author=_resolve_current_author(data))
        created = True
    meta = op.setdefault("meta", {})
    changed = created
    if meta.get("execute_skill") != SKILL:
        meta["execute_skill"] = SKILL
        changed = True
    if not meta.get("server_tick"):
        meta["server_tick"] = True
        changed = True
    if not meta.get("cadence_minutes"):
        meta["cadence_minutes"] = 60
        changed = True
    if changed:
        save_project(data, op={"type": "operation_open" if created
                               else "operation_update", "op_id": op["id"]})
    print(f"reply-watch operation: {op['id']} "
          f"(execute_skill={SKILL}, cadence={meta.get('cadence_minutes')}m, "
          f"{'created' if created else 'exists'})")
    print(f"  → 自動発火を有効にするには承認が必要: "
          f"beacon operation approve {op['id']} --spec <doc-id>")


def cmd_activity_done():
    """Internal (Skill-invoked): mark a planned Activity done/todo.
    Env: BEACON_ACT_ID, BEACON_ACT_STATUS (default 'done'). ms-106 e-3505 — a
    send records the Communication (fact) and marks the plan it fulfilled done,
    instead of leaving a lingering todo beside the证跡."""
    import sales_entities
    act_id = os.environ.get("BEACON_ACT_ID", "")
    status = (os.environ.get("BEACON_ACT_STATUS", "") or "done").strip().lower()
    data = load_project()
    try:
        act = sales_entities.activity_set_status(data, act_id, status,
                                                  at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"activity {act_id} → {act['status']}")


def cmd_activity_cancel():
    """取消 (cancel) a planned Activity — ms-139 e-4950. 誤起票やらないと決めた
    活動を、削除せず監査印つきで cancelled にする。Env: BEACON_ACT_ID,
    BEACON_REASON."""
    import sales_entities
    act_id = os.environ.get("BEACON_ACT_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    data = load_project()
    try:
        act = sales_entities.activity_cancel(data, act_id, reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"activity {act_id} → {act['status']}")


def cmd_activity_update():
    """Activity の説明 / 締切 / ボールを後追い更新する — ms-139 e-4950。空の項目は
    変更なし。Env: BEACON_ACT_ID, BEACON_ACTIVITY_DESC, BEACON_ACTIVITY_DEADLINE,
    BEACON_ACTIVITY_BALL."""
    import sales_entities
    act_id = os.environ.get("BEACON_ACT_ID", "")
    desc = os.environ.get("BEACON_ACTIVITY_DESC", "")
    deadline = os.environ.get("BEACON_ACTIVITY_DEADLINE", "")
    ball = os.environ.get("BEACON_ACTIVITY_BALL", "")
    data = load_project()
    try:
        act = sales_entities.activity_update(
            data, act_id, description=desc, deadline=deadline,
            who_has_the_ball=ball)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"activity {act_id} updated "
          f"(deadline={act.get('deadline', '')}, ball={act.get('who_has_the_ball', '')})")


def cmd_communication_add():
    # ms-107 e-3432 — 営業の Commit: source を辿れる事後記録型の証跡を target
    # (opp-… 優先 / acc-…) の子として append-only で残す。
    import sales_entities
    target_id = os.environ.get("BEACON_COMM_TARGET", "")
    summary = os.environ.get("BEACON_COMM_SUMMARY", "")
    direction = os.environ.get("BEACON_COMM_DIRECTION", "")
    channel = os.environ.get("BEACON_COMM_CHANNEL", "") or "other"
    source_ref = os.environ.get("BEACON_COMM_SOURCE_REF", "")
    source_url = os.environ.get("BEACON_COMM_SOURCE_URL", "")
    occurred_at = os.environ.get("BEACON_COMM_OCCURRED", "")
    body = os.environ.get("BEACON_COMM_BODY", "")  # e-3544: 任意の内容本文/要約
    source = {}
    if source_ref:
        source["ref"] = source_ref
    if source_url:
        source["url"] = source_url
    data = load_project()
    try:
        # ms-143: record the 証跡 through the profession-generic
        # occupation.add_evidence (the evidence-grain sibling of add_work_item),
        # NOT the sales-concrete sales_entities.communication_add symbol, so this
        # L2 verb stops symbol-reaching a PROFESSION_CONCRETE_SYMBOL. add_evidence
        # produces byte-identical records (parity harness), incl. act-/nrt- nesting.
        comm_id = occupation.add_evidence(
            data, target_id, summary=summary, direction=direction, channel=channel,
            body=body, source=source or None, occurred_at=occurred_at,
            created_at=core._now_iso())["id"]
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Recorded communication {comm_id} on {target_id} "
          f"({direction}/{channel}): {summary}")


def cmd_communication_cancel():
    # e-3537 — 誤って記録した証跡を取消 (soft-cancel)。物理削除でなく
    # status=cancelled + 理由で残す (data-immutability)。
    import sales_entities
    comm_id = os.environ.get("BEACON_COMM_ID", "")
    reason = os.environ.get("BEACON_COMM_REASON", "")
    data = load_project()
    try:
        sales_entities.communication_cancel(data, comm_id, reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Cancelled communication {comm_id}"
          + (f": {reason}" if reason else ""))


def cmd_communication_retarget():
    # e-3537 — 誤った活動に付けた証跡を正しい target/work item へ付け替える。
    # 事実は不変、綴じ場所 (linked_id/nesting) のみ移動。
    import sales_entities
    comm_id = os.environ.get("BEACON_COMM_ID", "")
    new_target = os.environ.get("BEACON_COMM_TARGET", "")
    reason = os.environ.get("BEACON_COMM_REASON", "")  # e-3585: 付け替え理由 (監査1行)
    data = load_project()
    try:
        comm = sales_entities.communication_retarget(
            data, comm_id, new_target, reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    linked = comm.get("linked_id") or "(target grain)"
    print(f"Retargeted communication {comm_id} → {new_target} (linked_id={linked})"
          + (f" — {reason}" if reason else ""))


def cmd_communication_list():
    # List a target's communications (証跡) oldest→newest, or --json.
    import sales_entities
    target_id = os.environ.get("BEACON_COMM_TARGET", "")
    as_json = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    container, linked_id = sales_entities.resolve_communication_target(data, target_id)
    if container is None:
        print(f"Error: Communication target not found (opp-…/acc-… target or "
              f"act-…/nrt-… work item): {target_id}", file=sys.stderr)
        sys.exit(1)
    # act-/nrt- id → only that work item's communications; opp-/acc- → all.
    comms = sales_entities.communications_of(
        container, linked_id=linked_id or None)
    if as_json:
        ball = sales_entities.derive_ball(container)
        print(json.dumps({"target": target_id, "ball": ball,
                          "communications": comms}, ensure_ascii=False))
        return
    if not comms:
        print(f"No communications on {target_id}.")
        return
    for c in comms:
        arrow = "←" if c.get("direction") == sales_entities.COMM_INBOUND else "→"
        when = c.get("occurred_at") or c.get("created_at") or ""
        src = c.get("source") or {}
        trace = src.get("url") or src.get("ref") or ""
        # e-3537: cancelled (取消済) 証跡は隠さず、印付きで忠実表示する。
        mark = "[取消] " if c.get("status") == sales_entities.CANCELLED_STATUS else ""
        line = f"  {c.get('id')} {arrow} [{c.get('channel')}] {mark}{c.get('summary')}"
        if when:
            line += f"  ({when})"
        print(line)
        if trace:
            print(f"      source: {trace}")
    ball = sales_entities.derive_ball(container)
    if ball:
        who = "自分" if ball == sales_entities.BALL_SELF else "相手"
        print(f"  ball: {who} ({ball})")


def cmd_meeting_schedule():
    # ms-107 e-3433 (B) — 予定確定: 遷移日 + カレンダー予定 + 識別 ID を束ねる。
    import sales_entities
    opp_id = os.environ.get("BEACON_MTG_OPP", "")
    at = os.environ.get("BEACON_MTG_AT", "")
    end = os.environ.get("BEACON_MTG_END", "")
    location = os.environ.get("BEACON_MTG_LOCATION", "")
    event_id = os.environ.get("BEACON_MTG_EVENT_ID", "")
    cal_ns = os.environ.get("BEACON_MTG_CAL_NS", "")
    cal_acct = os.environ.get("BEACON_MTG_CAL_ACCT", "")
    set_transition = os.environ.get("BEACON_MTG_SET_TRANSITION", "") == "1"
    data = load_project()
    try:
        mtg_id = sales_entities.meeting_schedule(
            data, opp_id, at, end_at=end, location=location,
            calendar_event_id=event_id, calendar_namespace=cal_ns,
            calendar_account=cal_acct, set_transition=set_transition,
            at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    tag = sales_entities.meeting_calendar_tag(mtg_id)
    print(f"Scheduled meeting {mtg_id} on {opp_id} at {at}")
    if set_transition:
        print(f"  遷移日 → {at[:10]}")
    print(f"  calendar tag (説明文に埋め込む): {tag}")


def cmd_meeting_reschedule():
    import sales_entities
    mtg_id = os.environ.get("BEACON_MTG_ID", "")
    at = os.environ.get("BEACON_MTG_AT", "")
    end = os.environ.get("BEACON_MTG_END", None) or None
    event_id = os.environ.get("BEACON_MTG_EVENT_ID", None) or None
    cal_ns = os.environ.get("BEACON_MTG_CAL_NS", None) or None
    cal_acct = os.environ.get("BEACON_MTG_CAL_ACCT", None) or None
    set_transition = os.environ.get("BEACON_MTG_SET_TRANSITION", "") == "1"
    data = load_project()
    try:
        sales_entities.meeting_reschedule(
            data, mtg_id, at, end_at=end, calendar_event_id=event_id,
            calendar_namespace=cal_ns, calendar_account=cal_acct,
            set_transition=set_transition, at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Rescheduled meeting {mtg_id} → {at}")
    if set_transition:
        print(f"  遷移日 → {at[:10]}")


def cmd_meeting_end():
    import sales_entities
    mtg_id = os.environ.get("BEACON_MTG_ID", "")
    data = load_project()
    try:
        sales_entities.meeting_mark_ended(data, mtg_id, at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Meeting {mtg_id} marked ended")


def cmd_meeting_cancel():
    import sales_entities
    mtg_id = os.environ.get("BEACON_MTG_ID", "")
    reason = os.environ.get("BEACON_MTG_CANCEL_REASON", "")
    data = load_project()
    try:
        sales_entities.meeting_cancel(data, mtg_id, at=core._now_iso(),
                                      reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Meeting {mtg_id} cancelled")


def cmd_meeting_list():
    import sales_entities
    opp_id = os.environ.get("BEACON_MTG_OPP", "")
    as_json = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    # e-3909: <opp-id> is now OPTIONAL — omitting it lists meetings across ALL
    # opportunities, symmetric with `opportunity list` / `account list` (which
    # are global). Previously the arg was required, so `meeting list` alone
    # errored — asymmetric with every other `list` verb and with the global
    # `meeting list-ended` sweep.
    if opp_id:
        opp = sales_entities.find_opportunity(data, opp_id)
        if opp is None:
            print(f"Error: Opportunity not found: {opp_id}", file=sys.stderr)
            sys.exit(1)
        scoped = [(opp, m) for m in sales_entities.opportunity_meetings(opp)]
    else:
        scoped = []
        for o in data.get("opportunities", []):
            for m in sales_entities.opportunity_meetings(o):
                scoped.append((o, m))
    if as_json:
        out = [{"opportunity_id": o.get("id"),
                "opportunity_title": o.get("title", ""), "meeting": m}
               for o, m in scoped]
        print(json.dumps({"opportunity": opp_id or None, "meetings": out},
                         ensure_ascii=False))
        return
    if not scoped:
        print(f"No meetings on {opp_id}." if opp_id else "No meetings.")
        return
    for o, m in scoped:
        line = f"  {m.get('id')} [{m.get('status')}] {m.get('scheduled_at')}"
        if not opp_id:
            line += f"  ({o.get('id')} {o.get('title', '')})"
        if m.get("location"):
            line += f" @ {m.get('location')}"
        if m.get("calendar_event_id"):
            line += f"  (event={m.get('calendar_event_id')})"
        print(line)


def cmd_meeting_ended():
    # ms-107 e-3434 (C) — 終了検知エンジン: 終了予定を過ぎた scheduled 面談を洗い出す。
    # 検知 Skill がこの候補をカレンダーで突合してから meeting end する。
    import sales_entities
    now = os.environ.get("BEACON_MTG_NOW", "") or core._now_iso()
    as_json = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    ended = sales_entities.scan_ended_meetings(data, now)
    rows = []
    for opp, m in ended:
        rows.append({
            "opportunity_id": opp.get("id"),
            "opportunity_title": opp.get("title", ""),
            "meeting_id": m.get("id"),
            "scheduled_at": m.get("scheduled_at", ""),
            "end_at": m.get("end_at", ""),
            "calendar_event_id": m.get("calendar_event_id", ""),
            "calendar_namespace": m.get("calendar_namespace", ""),
            "calendar_account": m.get("calendar_account", ""),
            "tag": sales_entities.meeting_calendar_tag(m.get("id", "")),
            # e-3583: 紐づけ面談だけがフェーズ判定を促す (紐づけ外は議事録のみ)。
            "is_gate_anchor": sales_entities.meeting_is_gate_anchor(data, m.get("id", "")),
        })
    if as_json:
        print(json.dumps({"now": now, "ended": rows}, ensure_ascii=False))
        return
    if not rows:
        print("No ended meetings awaiting detection.")
        return
    for r in rows:
        print(f"  {r['meeting_id']} ({r['opportunity_id']} {r['opportunity_title']}) "
              f"ended {r['end_at'] or r['scheduled_at']} "
              f"event={r['calendar_event_id'] or '—'}")


# ms-107 e-3437 — watch (返信待ち見張り) は内部コマンド。送信 Skill が arm し、
# 返信ウォッチャー (E) が list/clear する。user 向け CLI 動詞ではない
# (sales_identity_* と同じ内部専用、bin/beacon/README/dispatch には出さない)。

def cmd_watch_set():
    import sales_entities
    wi_id = os.environ.get("BEACON_WATCH_TARGET", "")
    channel = os.environ.get("BEACON_WATCH_CHANNEL", "")
    thread_ref = os.environ.get("BEACON_WATCH_THREAD", "")
    cadence = os.environ.get("BEACON_WATCH_CADENCE", "") or "60"
    data = load_project()
    try:
        w = sales_entities.set_watch(data, wi_id, channel=channel,
                                     thread_ref=thread_ref,
                                     cadence_minutes=int(cadence),
                                     at=core._now_iso())
    except (ValueError, TypeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Armed watch on {wi_id} ({w['channel']}, cadence {w['cadence_minutes']}m)")


def cmd_watch_clear():
    import sales_entities
    wi_id = os.environ.get("BEACON_WATCH_TARGET", "")
    data = load_project()
    try:
        sales_entities.clear_watch(data, wi_id, at=core._now_iso())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Cleared watch on {wi_id}")


def cmd_watch_list():
    import sales_entities
    awaiting = os.environ.get("BEACON_WATCH_AWAITING", "") == "1"
    as_json = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    items = sales_entities.watched_work_items(data, awaiting_reply_only=awaiting)
    rows = []
    for target, wi in items:
        w = wi.get("watch", {})
        rows.append({
            "target_id": target.get("id"),
            "target_title": work_model.target_label(target),
            "work_item_id": wi.get("id"),
            "work_item": wi.get("description", ""),
            "channel": w.get("channel", ""),
            "thread_ref": w.get("thread_ref", ""),
            "cadence_minutes": w.get("cadence_minutes"),
            "last_checked_at": w.get("last_checked_at", ""),
            "ball": sales_entities.derive_ball(target),
        })
    if as_json:
        print(json.dumps({"awaiting_reply_only": awaiting, "watches": rows},
                         ensure_ascii=False))
        return
    if not rows:
        print("No armed watches." if not awaiting else "No threads awaiting a reply.")
        return
    for r in rows:
        print(f"  {r['work_item_id']} ({r['target_id']}) [{r['channel']}] "
              f"{r['work_item']}  ball={r['ball']} thread={r['thread_ref'] or '—'}")


# ---------------------------------------------------------------------------
# ms-136 e-4699 — scenario asset operations (自動デバッグ基盤の実行可能シナリオ)
# The generation half is the /beacon-scenario-gen Skill (Claude reads a SPEC);
# these deterministic verbs run / save / list the resulting diffable assets.
# `scenario_run` is headless-invokable so CI (e-4702) can drive it without the
# interactive Skill.
# ---------------------------------------------------------------------------

def cmd_scenario_run():
    import scenario_store
    import scenario_runner
    import scenario_bisect
    path = os.environ.get("BEACON_SCENARIO_PATH", "")
    as_json = os.environ.get("BEACON_JSON", "") == "1"
    if not path:
        print("Error: scenario file path required (beacon scenario run <file>)",
              file=sys.stderr)
        sys.exit(2)
    try:
        scenario = scenario_store.load_scenario(path)
        report = scenario_runner.run_scenario(scenario)
    except (scenario_store.ScenarioError, FileNotFoundError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    # On failure, auto-append the dataflow-layer bisect (e-4700): turn the
    # failure into a one-shot layer localization instead of a手探り.
    diag = (scenario_bisect.diagnose_failure(scenario, report)
            if not report["passed"] else None)
    if as_json:
        out = dict(report)
        if diag:
            out["diagnosis"] = diag
        print(json.dumps(out, ensure_ascii=False))
    else:
        mark = "PASS" if report["passed"] else "FAIL"
        print(f"[{mark}] {report['name']} ({report['spec_ref']}) — "
              f"{len(report['steps'])} steps")
        if not report["passed"] and report["failure"]:
            f = report["failure"]
            print(f"  x step {f['index']} ({f['kind']}): {f['reason']}")
            if f.get("spec_source"):
                print(f"    spec_source: {f['spec_source']}")
            if f.get("observation_basis"):
                print(f"    observation_basis: {f['observation_basis']}")
        if diag and diag.get("diagnosable"):
            print(f"  → 障害層 bisect (e-4700): {diag['summary']}")
            if diag.get("why"):
                print(f"    根拠: {diag['why']}")
            # recovery hint (AX review finding #4): tell the agent what to do next.
            print(f"    → 次の一手: 上記の層を修正し `beacon scenario run {path}` "
                  "で再実行 (層が L_cli なら persona 操作の CLI 面、L_engine なら "
                  "commands.py、L_store なら永続を確認)")
    # exit nonzero on a failed journey so CI (e-4702) can gate on it.
    sys.exit(0 if report["passed"] else 1)


def cmd_scenario_save():
    import scenario_store
    src = os.environ.get("BEACON_SCENARIO_PATH", "")
    as_json = os.environ.get("BEACON_JSON", "") == "1"
    if not src:
        print("Error: input scenario JSON path required "
              "(beacon scenario save <file.json>)", file=sys.stderr)
        sys.exit(2)
    try:
        with open(src, "r", encoding="utf-8") as f:
            scenario = json.load(f)
        dest = scenario_store.save_scenario(scenario)
    except (scenario_store.ScenarioError, FileNotFoundError,
            json.JSONDecodeError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if as_json:
        print(json.dumps({"saved": str(dest)}, ensure_ascii=False))
    else:
        print(f"Saved scenario -> {dest}")


def cmd_scenario_list():
    import scenario_store
    ms = os.environ.get("BEACON_SCENARIO_MS", "") or None
    as_json = os.environ.get("BEACON_JSON", "") == "1"
    rows = scenario_store.list_scenarios(milestone=ms)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        print("No saved scenarios." if not ms else f"No scenarios for {ms}.")
        return
    for r in rows:
        print(f"  [{r['milestone']}] {r['name']} - {r['step_count']} steps, "
              f"{r['quality_signal_count']} quality-signal(s)  ({r['spec_ref']})")


def cmd_scenario_replay():
    # ms-136 e-4702 — dual use: CI regression (default) + attainment evidence
    # (--attainment). Default blocks on any failure (exit 1) and labels each as
    # infra (harness/net broke) vs product (real regression). --attainment emits
    # journey-pass EVIDENCE (not a verdict, no gating) for the MS's review.
    import scenario_regression
    ms = os.environ.get("BEACON_SCENARIO_MS", "") or None
    as_json = os.environ.get("BEACON_JSON", "") == "1"
    attainment = os.environ.get("BEACON_SCENARIO_ATTAINMENT", "") == "1"

    if attainment:
        if not ms:
            print("Error: --attainment requires --ms <ms-id>", file=sys.stderr)
            sys.exit(2)
        ev = scenario_regression.attainment_evidence(ms)
        if as_json:
            print(json.dumps(ev, ensure_ascii=False))
        else:
            print(f"journey-pass evidence for {ms} "
                  f"({'all green' if ev['all_journeys_green'] else 'some red'}, "
                  f"{ev['journey_count']} journeys):")
            for j in ev["journeys"]:
                mark = "✓" if j["passed"] else "✗"
                extra = "" if j["passed"] else f" [{j['origin']}/{j.get('responsible_layer') or '?'}]"
                print(f"  {mark} {j['name']} ({j['spec_ref']}){extra}")
            print(f"  ⚠ {ev['label']}")
        return  # evidence, not a gate — never exit-1 blocks the reviewer

    run = scenario_regression.run_saved_scenarios(milestone=ms)
    if as_json:
        print(json.dumps(run, ensure_ascii=False))
    else:
        total = len(run["results"])
        print(f"scenario regression: {total} scenario(s), "
              f"{'ALL GREEN' if run['all_passed'] else 'FAILURES'}")
        for r in run["product_regressions"]:
            print(f"  ✗ [product regression] {r['name']} — "
                  f"層 {r.get('responsible_layer') or '?'}: {r['reason']}")
        for r in run["infra_failures"]:
            print(f"  ⚠ [infra failure — net flaky/broken, not a product regression] "
                  f"{r['name']} — {r['reason']}")
    # CI block on ANY failure; the labels above tell infra vs product apart.
    sys.exit(0 if run["all_passed"] else 1)


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    _install_wall_clock_timeout(cmd)
    commands = {
        "init": cmd_init,
        "scenario_run": cmd_scenario_run,
        "scenario_save": cmd_scenario_save,
        "scenario_list": cmd_scenario_list,
        "scenario_replay": cmd_scenario_replay,
        "milestone_add": cmd_milestone_add,
        "milestone_list": cmd_milestone_list,
        "milestone_start": cmd_milestone_start,
        "milestone_done": cmd_milestone_done,
        "target_review_request": cmd_target_review_request,
        "target_approve": cmd_target_approve,
        "target_attach_evidence": cmd_target_attach_evidence,
        "target_attach_disposition": cmd_target_attach_disposition,  # ms-119 e-4579
        "target_reject": cmd_target_reject,
        "target_list": cmd_target_list,
        "target_create": cmd_target_create,      # ms-122 e-3956
        "target_advance": cmd_target_advance,     # ms-122 e-3956
        "target_close": cmd_target_close,         # ms-122 e-3956
        "target_instances": cmd_target_instances,  # ms-122 e-3956
        "target_work_item": cmd_target_work_item,  # ms-124 e-4089
        "target_evidence": cmd_target_evidence,    # ms-124 e-4089
        "target_ball": cmd_target_ball,            # ms-124 e-4089
        "target_class_add": cmd_target_class_add,  # ms-124 e-4091
        "target_class_update": cmd_target_class_update,  # ms-146 e-5346
        "target_purge": cmd_target_purge,  # ms-146 e-5351
        "target_split": cmd_target_split,  # ms-146 e-5340
        "target_class_list": cmd_target_class_list,  # ms-124 e-4091
        "target_class_adopt": cmd_target_class_adopt,  # ms-150 全 target 一律 adoptable
        "review_context": cmd_review_context,
        "review_batch_context": cmd_review_batch_context,  # ms-119 e-4125
        "review_done": cmd_review_done,      # ms-119 e-4060
        "review_skip": cmd_review_skip,      # ms-119 e-4124

        "milestone_observe": cmd_milestone_observe,
        "milestone_wait": cmd_milestone_wait,
        "milestone_release": cmd_milestone_release,
        "milestone_occupations": cmd_milestone_occupations,
        "milestone_join": cmd_milestone_join,
        "milestone_show": cmd_milestone_show,
        "milestone_update": cmd_milestone_update,
        "milestone_delete": cmd_milestone_delete,
        "milestone_purge": cmd_milestone_purge,
        "entry_purge": cmd_entry_purge,
        "operation_purge": cmd_operation_purge,
        "log": cmd_log,
        "log_prepare": cmd_log_prepare,
        "log_finalize": cmd_log_finalize,
        "sync": cmd_sync,
        # ms-106 ② sales job-template entities
        "account_add": cmd_account_add,
        "account_list": cmd_account_list,
        "acquisition_add": cmd_acquisition_add,
        "acquisition_list": cmd_acquisition_list,
        "acquisition_status": cmd_acquisition_status,
        "acquisition_delete": cmd_acquisition_delete,
        "acquisition_attach_list": cmd_acquisition_attach_list,
        "acquisition_lists": cmd_acquisition_lists,
        "acquisition_attack_list_fill": cmd_acquisition_attack_list_fill,
        "acquisition_attack_list_send": cmd_acquisition_attack_list_send,
        "acquisition_attack_list_send_record": cmd_acquisition_attack_list_send_record,
        "acquisition_attack_list_awaiting_reply": cmd_acquisition_attack_list_awaiting_reply,
        "acquisition_attack_list_reply_record": cmd_acquisition_attack_list_reply_record,
        "acquisition_attack_list_promote": cmd_acquisition_attack_list_promote,
        "account_contact": cmd_account_contact,
        "account_phase": cmd_account_phase,
        "account_rename": cmd_account_rename,
        "master_sync_drain": cmd_master_sync_drain,  # e-4399: 操作非依存の定期 drain
        "account_assign": cmd_account_assign,
        "account_nurturing": cmd_account_nurturing,
        "account_delete": cmd_account_delete,
        "disclose": cmd_disclose,      # ms-113 generalization (旧 account_link)
        "undisclose": cmd_undisclose,  # ms-113 generalization (旧 account_unlink)
        "opportunity_add": cmd_opportunity_add,
        "opportunity_list": cmd_opportunity_list,
        "opportunity_phase": cmd_opportunity_phase,
        "opportunity_assign": cmd_opportunity_assign,
        "opportunity_amount": cmd_opportunity_amount,
        "opportunity_describe": cmd_opportunity_describe,
        "opportunity_rename": cmd_opportunity_rename,
        "opportunity_phase_prob": cmd_opportunity_phase_prob,
        "sales_target": cmd_sales_target,
        "sales_target_list": cmd_sales_target_list,
        "opportunity_transition_date": cmd_opportunity_transition_date,
        "opportunity_anchor": cmd_opportunity_anchor,
        "opportunity_judge": cmd_opportunity_judge,
        "opportunity_due": cmd_opportunity_due,
        "deadline_due": cmd_deadline_due,             # ms-142 e-5010
        "opportunity_activity": cmd_opportunity_activity,
        "activity_done": cmd_activity_done,
        "activity_cancel": cmd_activity_cancel,      # ms-139 e-4950
        "activity_update": cmd_activity_update,       # ms-139 e-4950
        "sales_reply_watch_op_ensure": cmd_sales_reply_watch_op_ensure,
        "opportunity_delete": cmd_opportunity_delete,
        # ms-107 e-3432 — Communication (証跡・事後記録型 = 営業の Commit)
        "communication_add": cmd_communication_add,
        "communication_list": cmd_communication_list,
        "communication_cancel": cmd_communication_cancel,
        "communication_retarget": cmd_communication_retarget,
        # ms-107 e-3433 — Meeting (面談・運用状態型: 遷移日+カレンダー+識別ID の束)
        "meeting_schedule": cmd_meeting_schedule,
        "meeting_reschedule": cmd_meeting_reschedule,
        "meeting_end": cmd_meeting_end,
        "meeting_cancel": cmd_meeting_cancel,
        "meeting_list": cmd_meeting_list,
        "meeting_ended": cmd_meeting_ended,
        # ms-107 e-3437 — watch (返信待ち見張り, 内部専用: 送信 Skill arm / E read-clear)
        "watch_set": cmd_watch_set,
        "watch_clear": cmd_watch_clear,
        "watch_list": cmd_watch_list,
        "phase_list": cmd_phase_list,
        # ms-133 e-4648 — onboarding plan emitter (reached via `init --plan`,
        # internal: no standalone verb / README / help_json entry)
        "onboarding_plan": cmd_onboarding_plan,
        # ms-116 — edit a running project's saved phase funnel
        "phase_add": cmd_phase_add,
        "phase_rename": cmd_phase_rename,
        "phase_move": cmd_phase_move,
        "phase_remove": cmd_phase_remove,
        # ms-107 e-3353 — send identity pin (internal; called by sales Skills,
        # not exposed as a user CLI verb → no bin/beacon/README/dispatch.py entry)
        "sales_identity_set": cmd_sales_identity_set,
        "sales_identity_show": cmd_sales_identity_show,
        "sales_identity_check": cmd_sales_identity_check,
        # ms-107 e-3365 — send-account ledger (label→email+per-service MCP route)
        "sales_account_add": cmd_sales_account_add,
        "sales_account_route": cmd_sales_account_route,
        "sales_account_signature": cmd_sales_account_signature,
        "sales_account_list": cmd_sales_account_list,
        "sales_account_resolve": cmd_sales_account_resolve,
        "sales_account_remove": cmd_sales_account_remove,
        "sales_gmail_permalink": cmd_sales_gmail_permalink,
        "sales_account_transcript_source_set": cmd_sales_account_transcript_source_set,
        "sales_account_transcript_source_get": cmd_sales_account_transcript_source_get,
        "task_add": cmd_task_add,
        "task_done": cmd_task_done,
        "task_list": cmd_task_list,
        "task_show": cmd_task_show,
        "task_detail": cmd_task_detail,
        "task_update": cmd_task_update,
        "task_delete": cmd_task_delete,
        "task_cancel": cmd_task_cancel,
        "entry_move": cmd_entry_move,
        "summary": cmd_summary,
        "save": cmd_save,
        "milestone_depends": cmd_milestone_depends,
        "milestone_workspace": cmd_milestone_workspace,
        "milestone_workspace_cleanup": cmd_milestone_workspace_cleanup,
        "milestone_graph": cmd_milestone_graph,
        "retro_prepare": cmd_retro_prepare,
        "retro_save": cmd_retro_save,
        "retro_done": cmd_retro_done,
        "retro_default_since": cmd_retro_default_since,
        "deliverable_list": cmd_deliverable_list,   # ms-155 e-5602
        # ms-161 e-5902/e-5903: changelog curation + derived-map render.
        "deliverable_add": cmd_deliverable_add,
        "deliverable_retire": cmd_deliverable_retire,
        "deliverable_supersede": cmd_deliverable_supersede,
        "deliverable_map": cmd_deliverable_map,
        "trigger_fire": cmd_trigger_fire,
        "trigger_check": cmd_trigger_check,
        "trigger_tick": cmd_trigger_tick,
        "trigger_clear": cmd_trigger_clear,
        "doc_list": cmd_doc_list,
        "doc_show": cmd_doc_show,
        "doc_add": cmd_doc_add,
        "doc_update": cmd_doc_update,
        "doc_delete": cmd_doc_delete,
        "doc_history": cmd_doc_history,
        "doc_restore": cmd_doc_restore,
        "doc_image_upload": cmd_doc_image_upload,
        # ms-131 e-4496 — table-doc row operations.
        "doc_table_create": cmd_doc_table_create,
        "doc_table_add_row": cmd_doc_table_add_row,
        "doc_table_set_cell": cmd_doc_table_set_cell,
        "doc_table_rm_row": cmd_doc_table_rm_row,
        "doc_table_show": cmd_doc_table_show,
        "cloud_list": cmd_cloud_list,
        "cloud_push": cmd_cloud_push,
        # ms-84 Phase 4 (e-2038): cloud_pull dispatch entry removed.
        # cmd_cloud_pull was deleted; the bin/beacon `pull` / `force-pull`
        # subcommands now hit the wildcard 'unknown subcommand' branch.
        "cloud_status": cmd_cloud_status,
        "cloud_check_project": cmd_cloud_check_project,
        "cloud_join": cmd_cloud_join,
        "cloud_migrate_from_local": cmd_cloud_migrate_from_local,
        "migrate_target_labels": cmd_migrate_target_labels,
        "common_setup": cmd_common_setup,
        "auth_check": cmd_auth_check,
        "auth_login": lambda: __import__("auth").login(),
        "auth_logout": lambda: __import__("auth").logout(),
        "auth_status": lambda: __import__("auth").status(),
        "skill_install": cmd_skill_install,
        "update": cmd_update,
        "search": cmd_search,
        "cycle_status": cmd_cycle_status,
        "project_rename": cmd_project_rename,      # ms-122 e-4033
        "project_archive": cmd_project_archive,
        "deploy_record": cmd_deploy_record,
        "deploy_list": cmd_deploy_list,
        "deploy_delete": cmd_deploy_delete,
        "deploy_void": cmd_deploy_void,
        "deploy_rollback": cmd_deploy_rollback,
        "push_record": cmd_push_record,
        "push_list": cmd_push_list,
        "project_unarchive": cmd_project_unarchive,
        "project_orphans": cmd_project_orphans,
        "project_cleanup": cmd_project_cleanup,
        "project_dump": cmd_project_dump,
        "project_export": cmd_project_export,
        "project_import": cmd_project_import,
        "pr_add": cmd_pr_add,
        "pr_close": cmd_pr_close,
        "pr_approve": cmd_pr_approve,
        "pr_reject": cmd_pr_reject,
        "pr_create": cmd_pr_create,
        "pr_show": cmd_pr_show,
        "pr_request_review": cmd_pr_request_review,
        "pr_request_changes": cmd_pr_request_changes,
        "pr_merge": cmd_pr_merge,
        "pr_sync": cmd_pr_sync,
        "issue_import": cmd_issue_import,
        "issue_list": cmd_issue_list,
        "issue_sync": cmd_issue_sync,
        "operation_open": cmd_operation_open,
        "operation_close": cmd_operation_close,
        # ms-160 e-5814 — operator-facing pause/resume (fire-suppression verb)
        "operation_pause": cmd_operation_pause,
        "operation_resume": cmd_operation_resume,
        # ms-107 e-3461 — server tick opt-in (内部専用、有効化 setup で使う)
        "operation_server_tick": cmd_operation_server_tick,
        "operation_set_status": cmd_operation_set_status,
        "operation_update": cmd_operation_update,
        "operation_task_add": cmd_operation_task_add,
        "operation_task_done": cmd_operation_task_done,
        "operation_task_list": cmd_operation_task_list,
        "operation_list": cmd_operation_list,
        "operation_show": cmd_operation_show,
        "operation_approve": cmd_operation_approve,
        "operation_revoke": cmd_operation_revoke,
        "operation_envelope_verify": cmd_operation_envelope_verify,
        "run_record": cmd_run_record,
        "run_list": cmd_run_list,
        # ms-151 / e-5474: machine API key 発行 CLI (owner 限定、cloud endpoint)。
        "machine_key_issue": cmd_machine_key_issue,
        "machine_key_list": cmd_machine_key_list,
        "machine_key_revoke": cmd_machine_key_revoke,
        "incident_open": cmd_incident_open,
        "incident_close": cmd_incident_close,
        "incident_escalate": cmd_incident_escalate,
        "incident_list": cmd_incident_list,
        "note_add": cmd_note_add,
        "note_list": cmd_note_list,
        "note_clear": cmd_note_clear,
        "decision_record": cmd_decision_record,
        "decision_list": cmd_decision_list,
        "decision_derive": cmd_decision_derive,

        "bus_send": cmd_bus_send,
        "bus_listen": cmd_bus_listen,
        "bus_receive": cmd_bus_receive,
        "bus_ack": cmd_bus_ack,
        "bus_status": cmd_bus_status,
        "bus_directory": cmd_bus_directory,
        # ms-70 / e-1716: receiver-side decision primitive for pending DM
        # actions. Reached by `beacon dm respond approve|deny <event_id>`.
        "dm_respond": cmd_dm_respond,
        # ms-70 / e-1923 (e-1718 AC 4): audit history view in the terminal.
        # Mirror of the Web UI Settings > Audit table; reaches the same
        # server endpoint and renders 6 columns of decided sidecar rows.
        "dm_log": cmd_dm_log,
        # ms-141 / e-4966: sender-side "DMs I sent" audit (complement of the
        # receive-side dm_log/dm_audit). Reached by `beacon dm sent`.
        "dm_sent": cmd_dm_sent,
        # ms-55 e-1646: stop / resume signal CLI. Anyone can broadcast
        # (Andon cord principle, SPEC §2). The events ride on the
        # existing bus on channel `stop-signal`.
        "stop_scoped": cmd_stop_scoped,
        "stop_global": cmd_stop_global,
        "stop_status": cmd_stop_status,
        "resume_scoped": cmd_resume_scoped,
        "resume_global": cmd_resume_global,
        # ms-55 e-1647: rollback boundary CLI. Auto-undoes working tree +
        # un-pushed commits; refuses to touch pushed/merged/deployed
        # state (those produce report + compensation proposals).
        "rollback": cmd_rollback,
        # ms-55 e-1648: claim primitives. 3 kinds (request/handoff/claim);
        # request + handoff need recipient consent, claim is first-publisher
        # -wins broadcast. Local persistence for session restart recovery.
        "claim_request": cmd_claim_request,
        "claim_handoff": cmd_claim_handoff,
        "claim_post": cmd_claim_post,
        "claim_respond": cmd_claim_respond,
        "claim_release": cmd_claim_release,
        "claim_list": cmd_claim_list,
        # ms-112 e-3674: 2-layer claim view. Reads occupation (LIVE session) +
        # assignee (persistent) into one claim-aware view, target-class 横断.
        # The consuming layer session-start / dispatch / cockpit read this.
        "claim_view": cmd_claim_view,
        # ms-55 e-1649: STUCK detector. Idle-timeout based emission of
        # stop signals with reason_kind="stuck", same protocol as e-1646.
        "stuck_check": cmd_stuck_check,
        # ms-55 e-1650: morning briefing. 4-bucket digest of recent
        # autonomous activity for the human to read with coffee.
        "morning": cmd_morning,
        "sessions_list": cmd_sessions_list,
        "profile_list": cmd_profile_list,
        "bus_budget_grant": cmd_bus_budget_grant,
        "bus_budget_show": cmd_bus_budget_show,
        "bus_budget_clear": cmd_bus_budget_clear,
        "bus_auto_execute_list": cmd_bus_auto_execute_list,
        "bus_auto_execute_add": cmd_bus_auto_execute_add,
        "bus_auto_execute_remove": cmd_bus_auto_execute_remove,
        "session_end": cmd_session_end,
        "session_rescue": cmd_session_rescue,
        "session_log_list": cmd_session_log_list,
        "session_log_show": cmd_session_log_show,
        "session_id": cmd_session_id,
        "session_focus": cmd_session_focus,
        "session_attention": cmd_session_attention,
        "session_fork": cmd_session_fork,
        "session_fork_list": cmd_session_fork_list,
        "channel_install": cmd_channel_install,
        "channel_uninstall": cmd_channel_uninstall,
        "channel_opt_out": cmd_channel_opt_out,
        "channel_opt_in": cmd_channel_opt_in,
        "channel_status": cmd_channel_status,
        "member_add": cmd_member_add,
        "member_list": cmd_member_list,
        "member_remove": cmd_member_remove,
        "member_role": cmd_member_role,
        # ms-78 e-1805: token-based invite flow + self-introspection.
        "member_invite": cmd_member_invite,
        "member_invitation_list": cmd_member_invitation_list,
        "member_invitation_cancel": cmd_member_invitation_cancel,
        "member_join": cmd_member_join,
        "member_whoami": cmd_member_whoami,
        # ms-118 e-4231: organizations (top-level tenancy) — create/list/show.
        "org_create": cmd_org_create,
        "org_list": cmd_org_list,
        "org_show": cmd_org_show,
        # ms-118 e-4232: org membership — add-member (所属のみ) / remove-member.
        "org_add_member": cmd_org_add_member,
        "org_remove_member": cmd_org_remove_member,
        "org_delete": cmd_org_delete,
        "org_rehome": cmd_org_rehome,
        "trek_create": cmd_trek_create,
        "trek_list": cmd_trek_list,
        "trek_show": cmd_trek_show,
        "trek_review_verdicts": cmd_trek_review_verdicts,
        "trek_start": cmd_trek_start,
        "trek_archive": cmd_trek_archive,
        "trek_invite": cmd_trek_invite,
        "trek_join": cmd_trek_join,
        "trek_leave": cmd_trek_leave,
        "trek_plan": cmd_trek_plan,
        # ms-97 / e-2611 AC25 — scope mutation approval flow.
        "trek_scope_approve": cmd_trek_scope_approve,
        "trek_scope_reject": cmd_trek_scope_reject,
        # ms-99 / e-2829 — slot verbs (schema v2).
        "trek_slot_add": cmd_trek_slot_add,
        "trek_slot_amend": cmd_trek_slot_amend,
        "trek_slot_claim": cmd_trek_slot_claim,
        "trek_slot_list": cmd_trek_slot_list,
        # ms-97 / Phase 7-C / AC24 — blanket pre-approval (e-2603).
        "trek_blanket_approve": cmd_trek_blanket_approve,
        "trek_blanket_revoke": cmd_trek_blanket_revoke,
        "trek_task_state": cmd_trek_task_state,
        "trek_block": cmd_trek_block,
        "trek_unblock": cmd_trek_unblock,
        "trek_blockers": cmd_trek_blockers,
        "trek_extend_ttl": cmd_trek_extend_ttl,
        # ms-97 / Phase 7-A / AC21 — leader が user summary DM 送信後
        # に meta.summary_sent_at を stamp する CLI wrapper。
        "trek_summary_sent": cmd_trek_summary_sent,
        # ms-92 / e-2141 — cross-project task add via Trek scope
        "trek_task_add": cmd_trek_task_add,
        "trek_stop": cmd_trek_stop,
        "trek_resume": cmd_trek_resume,
        "trek_pulse_ack": cmd_trek_pulse_ack,
        "trek_take_over": cmd_trek_take_over,
        "trek_kickoff": cmd_trek_kickoff,
        "trek_reconcile": cmd_trek_reconcile,
        "trek_transfer_leader": cmd_trek_transfer_leader,
        "trek_timeline": cmd_trek_timeline,
        "version": lambda: print(f"beacon {__version__}"),
        "help_json": cmd_help_json,
        "help_render": cmd_help_render,
        "doctor": cmd_doctor,
    }
    fn = commands.get(cmd)
    # ms-54 e-1319: the CLI-side heartbeat (formerly bumped here, ms-57 e-1035)
    # has been retired. Post Option C (PR #111 / commit 78048b6) the bridge
    # poll loop is the truth source for both ``last_active`` (proof of life)
    # and ``last_poll_at`` (proof of receive-capability). A CLI-side write
    # created ambiguity — a session could "heartbeat" while the bridge poll
    # was dead, leaving DMs accumulating server-side unread. Resolve stays
    # in the CLI (`beacon session id` pure getter); mint+heartbeat = bridge;
    # lifecycle close = `beacon session end`.
    if fn:
        try:
            fn()
        except ValueError as e:
            # ms-120 / e-3896: domain / user-input errors are raised as
            # ValueError throughout (e.g. "Milestone not found: ms-999" from an
            # unresolved -m/--ms reference). Before this they bubbled up as a raw
            # Python traceback — non-zero, but the "cause" was buried in a
            # stacktrace, exactly the silent-ish failure e-3896 targets. Command
            # handlers that already catch ValueError and print a tailored message
            # exit before reaching here; this is the uniform safety net for the
            # ones that don't, so an unresolved reference can never dump a
            # stacktrace at the operator. Programming bugs raise other exception
            # types and still surface with a full traceback.
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
