"""Verb classification DATA for ms-114 e-3740 — the Q/R/B/C label + fusion seams
of every live CLI dispatch verb. Split from ``verb_ledger.py`` (which holds the
accessors / reconcile logic) so the large data table reads on its own.

Source of truth for the labels: memo ``42YTZkmtzfFhaarzCSv6`` (the 2026-07-19
design discussion's ~239-verb prose classification). Verbs added since that memo
carry a ``[post-memo]`` note in ``note`` so a human can confirm the class letter
that was applied here (the classification test is the memo's: Q=read-only,
R=ledger-append, B=machinery, C=human/outward/local).

284 entries = 282 live dispatch keys + 2 shell top-level keys (``status`` /
``reset``); ``verb_ledger.reconcile()`` pins this against the live surface.

Judgment calls flagged for human review (all already ``[post-memo]``-noted; these
are the ones where the class letter was a genuine toss-up):

- ``org_create`` / ``org_add_member`` / ``org_remove_member``: labelled C(+R) to
  mirror the memo's ``member add=C(+R)``. They are immediate store writes with NO
  accept/token flow, so B(+R) is defensible if the intended seam is "store /
  authorization machinery => B".
- ``org_rehome``: B (authorization/projection relink, like ``disclose``); could be
  R if "records a re-home decision" is the salient act.
- ``phase_add/move/remove/rename``: B (edits the funnel *config*/methodology in
  project.json, not an entity-state transition — that's ``opportunity_phase=R``).
- ``sales_account_signature`` / ``sales_account_transcript_source_set``: B to match
  ``account-route=B`` (ledger-config setters); arguably R.
- ``review_skip``: R(+B) — the durable audited waiver is primary (R), trigger-clear
  is the machinery half (B). ``review_done``: B (pure gate/trigger control).
- ``target_approve``: R(+C+B) mirroring milestone/pr approve (verdict-record / human
  decision / execute-transition); confirm R is the right PRIMARY vs C.
- ``target_work_item`` / ``target_evidence``: one dispatch key multiplexes
  add/done/list; classified by the PRIMARY (write) subaction = R.
- ``target_class_add``: B (schema/descriptor authoring = machinery); could be R.
"""

# key = dispatch key (noun_subcommand); value = {cls, fused, note}.
VERB_LEDGER = {
    # --- account ---
    "account_add": {"cls": "R", "fused": ["B"], "note": "生報告(R)/非構造→構造の解釈(B)"},
    "account_assign": {"cls": "R", "fused": [], "note": ""},
    "account_contact": {"cls": "R", "fused": [], "note": ""},
    "account_delete": {"cls": "B", "fused": ["C"], "note": ""},
    "account_list": {"cls": "Q", "fused": [], "note": ""},
    "account_nurturing": {"cls": "R", "fused": [], "note": ""},
    "account_phase": {"cls": "R", "fused": [], "note": ""},
    "account_rename": {"cls": "R", "fused": [], "note": ""},

    # --- acquisition (post-memo, ms-115 target-class) ---
    "acquisition_add": {"cls": "R", "fused": [], "note": "[post-memo] 獲得ターゲット起票=台帳追記(R)、milestone add と同型"},
    "acquisition_list": {"cls": "Q", "fused": [], "note": "[post-memo] read-only 一覧"},
    "acquisition_status": {"cls": "R", "fused": [], "note": "[post-memo] 状態遷移の記録(R)"},

    # --- activity ---
    "activity_done": {"cls": "R", "fused": [], "note": ""},

    # --- auth ---
    "auth_login": {"cls": "C", "fused": [], "note": ""},
    "auth_logout": {"cls": "C", "fused": [], "note": ""},
    "auth_status": {"cls": "Q", "fused": [], "note": ""},

    # --- bus ---
    "bus_ack": {"cls": "C", "fused": [], "note": ""},
    "bus_auto_execute_add": {"cls": "B", "fused": [], "note": ""},
    "bus_auto_execute_list": {"cls": "Q", "fused": [], "note": ""},
    "bus_auto_execute_remove": {"cls": "B", "fused": [], "note": ""},
    "bus_budget_clear": {"cls": "B", "fused": [], "note": ""},
    "bus_budget_grant": {"cls": "B", "fused": [], "note": ""},
    "bus_budget_show": {"cls": "Q", "fused": [], "note": ""},
    "bus_directory": {"cls": "Q", "fused": [], "note": ""},
    "bus_listen": {"cls": "C", "fused": [], "note": ""},
    "bus_receive": {"cls": "C", "fused": [], "note": ""},
    "bus_send": {"cls": "C", "fused": [], "note": ""},
    "bus_status": {"cls": "Q", "fused": [], "note": ""},

    # --- channel ---
    "channel_install": {"cls": "C", "fused": [], "note": ""},
    "channel_opt_in": {"cls": "C", "fused": [], "note": ""},
    "channel_opt_out": {"cls": "C", "fused": [], "note": ""},
    "channel_status": {"cls": "Q", "fused": [], "note": ""},
    "channel_uninstall": {"cls": "C", "fused": [], "note": ""},

    # --- claim ---
    "claim_handoff": {"cls": "R", "fused": ["C"], "note": ""},
    "claim_list": {"cls": "Q", "fused": [], "note": ""},
    "claim_post": {"cls": "R", "fused": [], "note": ""},
    "claim_release": {"cls": "R", "fused": [], "note": ""},
    "claim_request": {"cls": "R", "fused": ["C"], "note": ""},
    "claim_respond": {"cls": "R", "fused": ["C"], "note": ""},
    "claim_view": {"cls": "Q", "fused": [], "note": "[post-memo] 2層 claim state を read-only 表示、非排他 (ms-112 設計方針3)"},

    # --- cloud ---
    "cloud_join": {"cls": "C", "fused": [], "note": ""},
    "cloud_list": {"cls": "Q", "fused": [], "note": ""},
    "cloud_migrate_from_local": {"cls": "B", "fused": [], "note": ""},
    "cloud_push": {"cls": "B", "fused": [], "note": ""},
    "cloud_status": {"cls": "Q", "fused": [], "note": ""},

    # --- communication ---
    "communication_add": {"cls": "R", "fused": ["B"], "note": "生報告(R)/非構造→構造の解釈(B)"},
    "communication_cancel": {"cls": "R", "fused": [], "note": ""},
    "communication_list": {"cls": "Q", "fused": [], "note": ""},
    "communication_retarget": {"cls": "R", "fused": [], "note": ""},

    # --- cycle ---
    "cycle_status": {"cls": "Q", "fused": [], "note": ""},

    # --- deploy ---
    "deploy_delete": {"cls": "B", "fused": [], "note": ""},
    "deploy_list": {"cls": "Q", "fused": [], "note": ""},
    "deploy_record": {"cls": "R", "fused": [], "note": ""},
    "deploy_rollback": {"cls": "C", "fused": ["B"], "note": ""},
    "deploy_void": {"cls": "B", "fused": ["R"], "note": ""},

    # --- disclose / undisclose (post-memo, ms-113) ---
    "disclose": {"cls": "B", "fused": [], "note": "[post-memo] 任意 Target を別 project へ開示=認可/投影の機械操作(B)"},
    "undisclose": {"cls": "B", "fused": [], "note": "[post-memo] 開示リンク剥奪 (即時)=認可/投影の機械操作(B)"},

    # --- dm ---
    "dm_log": {"cls": "Q", "fused": [], "note": ""},
    "dm_respond": {"cls": "C", "fused": ["B"], "note": "人間承認(C)/gate 反映(B)"},

    # --- doc ---
    "doc_add": {"cls": "R", "fused": ["C"], "note": "台帳追記(R)/対話(C)"},
    "doc_delete": {"cls": "B", "fused": ["C"], "note": ""},
    "doc_history": {"cls": "Q", "fused": [], "note": ""},
    "doc_image_upload": {"cls": "R", "fused": ["C"], "note": ""},
    "doc_list": {"cls": "Q", "fused": [], "note": ""},
    "doc_restore": {"cls": "B", "fused": ["R"], "note": ""},
    "doc_show": {"cls": "Q", "fused": [], "note": ""},
    "doc_update": {"cls": "R", "fused": ["C"], "note": "台帳追記(R)/対話(C)"},

    # --- doctor ---
    "doctor": {"cls": "B", "fused": [], "note": ""},

    # --- entry ---
    "entry_move": {"cls": "B", "fused": ["R"], "note": ""},
    "entry_purge": {"cls": "B", "fused": [], "note": ""},

    # --- help ---
    "help_render": {"cls": "Q", "fused": [], "note": "[post-memo] レジストリからの help 描画=read-only 参照(Q)"},

    # --- incident ---
    "incident_close": {"cls": "R", "fused": [], "note": "close報告+決定(R)"},
    "incident_escalate": {"cls": "R", "fused": ["C"], "note": ""},
    "incident_list": {"cls": "Q", "fused": [], "note": ""},
    "incident_open": {"cls": "R", "fused": [], "note": ""},

    # --- init ---
    "init": {"cls": "B", "fused": ["C"], "note": ""},

    # --- issue ---
    "issue_import": {"cls": "B", "fused": ["R"], "note": ""},
    "issue_list": {"cls": "Q", "fused": [], "note": ""},
    "issue_sync": {"cls": "B", "fused": [], "note": ""},

    # --- log (R系統の雛形) ---
    "log": {"cls": "R", "fused": ["B"], "note": "commit報告(R)/progress導出・AC判定(B、自己採点をサーバへ)"},
    "log_finalize": {"cls": "R", "fused": ["B"], "note": ""},
    "log_prepare": {"cls": "Q", "fused": [], "note": ""},

    # --- meeting ---
    "meeting_cancel": {"cls": "R", "fused": ["C"], "note": "cancel報告(R)/外向き通知(C)"},
    "meeting_end": {"cls": "R", "fused": [], "note": ""},
    "meeting_ended": {"cls": "B", "fused": [], "note": "検知"},
    "meeting_list": {"cls": "Q", "fused": [], "note": ""},
    "meeting_reschedule": {"cls": "C", "fused": ["R"], "note": "カレンダー作成(C外向き)/活動記録(R)"},
    "meeting_schedule": {"cls": "C", "fused": ["R"], "note": "カレンダー作成(C外向き)/活動記録(R)"},

    # --- member ---
    "member_add": {"cls": "C", "fused": ["R"], "note": ""},
    "member_invitation_cancel": {"cls": "C", "fused": [], "note": ""},
    "member_invitation_list": {"cls": "Q", "fused": [], "note": ""},
    "member_invite": {"cls": "C", "fused": [], "note": ""},
    "member_join": {"cls": "C", "fused": [], "note": ""},
    "member_list": {"cls": "Q", "fused": [], "note": ""},
    "member_remove": {"cls": "C", "fused": ["R"], "note": ""},
    "member_role": {"cls": "C", "fused": ["R"], "note": ""},
    "member_whoami": {"cls": "Q", "fused": [], "note": ""},

    # --- migrate ---
    "migrate_target_labels": {"cls": "B", "fused": [], "note": ""},

    # --- milestone ---
    "milestone_add": {"cls": "R", "fused": [], "note": ""},
    "milestone_delete": {"cls": "B", "fused": ["C"], "note": ""},
    "milestone_depends": {"cls": "R", "fused": [], "note": ""},
    "milestone_done": {"cls": "R", "fused": [], "note": ""},
    "milestone_graph": {"cls": "Q", "fused": [], "note": ""},
    "milestone_join": {"cls": "R", "fused": [], "note": ""},
    "milestone_list": {"cls": "Q", "fused": [], "note": ""},
    "milestone_observe": {"cls": "R", "fused": [], "note": ""},
    "milestone_occupations": {"cls": "Q", "fused": [], "note": ""},
    "milestone_purge": {"cls": "B", "fused": [], "note": ""},
    "milestone_release": {"cls": "R", "fused": [], "note": ""},
    "milestone_show": {"cls": "Q", "fused": [], "note": ""},
    "milestone_start": {"cls": "R", "fused": ["C", "B"], "note": "活性化決定(R)/worktree作成(C)/branch(B)"},
    "milestone_update": {"cls": "R", "fused": [], "note": ""},
    "milestone_wait": {"cls": "R", "fused": [], "note": ""},
    "milestone_workspace": {"cls": "C", "fused": [], "note": ""},
    "milestone_workspace_cleanup": {"cls": "C", "fused": [], "note": ""},

    # --- morning ---
    "morning": {"cls": "Q", "fused": [], "note": ""},

    # --- note ---
    "note_add": {"cls": "R", "fused": [], "note": ""},
    "note_clear": {"cls": "B", "fused": [], "note": ""},
    "note_list": {"cls": "Q", "fused": [], "note": ""},

    # --- operation ---
    "operation_approve": {"cls": "C", "fused": ["B"], "note": "人間承認(C)/gate反映(B)"},
    "operation_close": {"cls": "R", "fused": ["B"], "note": ""},
    "operation_envelope_verify": {"cls": "B", "fused": [], "note": ""},
    "operation_list": {"cls": "Q", "fused": [], "note": ""},
    "operation_open": {"cls": "R", "fused": ["C"], "note": "起票(R)/設定対話(C)"},
    "operation_purge": {"cls": "B", "fused": [], "note": ""},
    "operation_revoke": {"cls": "C", "fused": ["B"], "note": ""},
    "operation_server_tick": {"cls": "B", "fused": [], "note": ""},
    "operation_set_status": {"cls": "R", "fused": ["B"], "note": ""},
    "operation_show": {"cls": "Q", "fused": [], "note": ""},
    "operation_task_add": {"cls": "R", "fused": [], "note": ""},
    "operation_task_done": {"cls": "R", "fused": [], "note": ""},
    "operation_task_list": {"cls": "Q", "fused": [], "note": ""},
    "operation_update": {"cls": "R", "fused": [], "note": ""},

    # --- opportunity ---
    "opportunity_activity": {"cls": "R", "fused": [], "note": ""},
    "opportunity_add": {"cls": "R", "fused": [], "note": ""},
    "opportunity_amount": {"cls": "R", "fused": [], "note": ""},
    "opportunity_assign": {"cls": "R", "fused": [], "note": ""},
    "opportunity_delete": {"cls": "B", "fused": ["C"], "note": ""},
    "opportunity_describe": {"cls": "R", "fused": [], "note": "[post-memo] 背景/経緯メモの追記(R)、opportunity add と同族"},
    "opportunity_due": {"cls": "R", "fused": [], "note": ""},
    "opportunity_judge": {"cls": "R", "fused": ["B"], "note": "判定主体を監査重みで振る(人間=R運搬/サーバ=B)"},
    "opportunity_list": {"cls": "Q", "fused": [], "note": ""},
    "opportunity_phase": {"cls": "R", "fused": [], "note": ""},
    "opportunity_phase_prob": {"cls": "R", "fused": [], "note": ""},
    "opportunity_rename": {"cls": "R", "fused": [], "note": "[post-memo] title 修正の記録(R)、milestone rename と同型"},
    "opportunity_transition_date": {"cls": "R", "fused": [], "note": ""},

    # --- org (post-memo, ms-118) ---
    "org_create": {"cls": "C", "fused": ["R"], "note": "[post-memo] org 立ち上げ=owner 割当を伴う identity 作成(C)/store 追記(R)、member 系と同族"},
    "org_list": {"cls": "Q", "fused": [], "note": "[post-memo] 自分が member の org 一覧=read-only(Q)"},
    "org_show": {"cls": "Q", "fused": [], "note": "[post-memo] 単一 org 表示=read-only(Q)"},
    "org_add_member": {"cls": "C", "fused": ["R"], "note": "[post-memo] 所属付与=人間窓口の招待(C)/store 追記(R)、member add と同型"},
    "org_remove_member": {"cls": "C", "fused": ["R"], "note": "[post-memo] 所属剥奪=人間窓口(C)/store 更新(R)、member remove と同型"},
    "org_delete": {"cls": "B", "fused": ["C"], "note": "[post-memo] org 破壊的削除=store 保守/認可の機械(B)/owner-only 窓口(C)、milestone delete と同型"},
    "org_rehome": {"cls": "B", "fused": [], "note": "[post-memo] project の org 所属リンク張替=認可/投影の機械操作(B)、disclose と同族"},

    # --- phase funnel config (post-memo, ms-116) ---
    "phase_add": {"cls": "B", "fused": [], "note": "[post-memo] funnel(config)段追加=project.json 保守/store 編集(B)、entity 遷移でなく methodology"},
    "phase_list": {"cls": "Q", "fused": [], "note": ""},
    "phase_move": {"cls": "B", "fused": [], "note": "[post-memo] funnel 段の並べ替え=config 保守(B)"},
    "phase_remove": {"cls": "B", "fused": [], "note": "[post-memo] funnel 段削除=config 保守(B)"},
    "phase_rename": {"cls": "B", "fused": [], "note": "[post-memo] funnel 段改名=config 保守(B)"},

    # --- pr ---
    "pr_add": {"cls": "R", "fused": [], "note": ""},
    "pr_approve": {"cls": "R", "fused": ["C"], "note": "承認記録(R)/決定(C)"},
    "pr_close": {"cls": "R", "fused": [], "note": ""},
    "pr_create": {"cls": "C", "fused": ["R"], "note": "GitHub効果(C)/PR状態記録(R)"},
    "pr_merge": {"cls": "C", "fused": ["R"], "note": "GitHub効果(C)/PR状態記録(R)"},
    "pr_reject": {"cls": "R", "fused": ["C"], "note": ""},
    "pr_request_changes": {"cls": "R", "fused": ["C"], "note": ""},
    "pr_request_review": {"cls": "R", "fused": ["C"], "note": ""},
    "pr_show": {"cls": "Q", "fused": [], "note": ""},
    "pr_sync": {"cls": "B", "fused": ["R"], "note": ""},

    # --- profile ---
    "profile_list": {"cls": "Q", "fused": [], "note": ""},

    # --- project ---
    "project_archive": {"cls": "B", "fused": ["C"], "note": ""},
    "project_cleanup": {"cls": "B", "fused": ["C"], "note": "[post-memo] orphan project 一括 archive=store 保守の機械(B)/人間確認 checkpoint(C)、ms-123 二段確認"},
    "project_export": {"cls": "B", "fused": [], "note": ""},
    "project_import": {"cls": "B", "fused": [], "note": ""},
    "project_orphans": {"cls": "Q", "fused": [], "note": "[post-memo] orphan 候補の read-only スキャン(Q)、変更なし (archive は別 verb)"},
    "project_rename": {"cls": "R", "fused": [], "note": "[post-memo] project 名の更新記録(R)、milestone rename と同型"},
    "project_unarchive": {"cls": "B", "fused": ["C"], "note": ""},

    # --- push ---
    "push_list": {"cls": "Q", "fused": [], "note": ""},
    "push_record": {"cls": "R", "fused": [], "note": ""},

    # --- resume ---
    "resume_global": {"cls": "C", "fused": ["B"], "note": ""},
    "resume_scoped": {"cls": "C", "fused": ["B"], "note": ""},

    # --- retro ---
    "retro_done": {"cls": "R", "fused": [], "note": ""},
    "retro_prepare": {"cls": "Q", "fused": [], "note": ""},
    "retro_save": {"cls": "R", "fused": ["C"], "note": "台帳追記(R)/対話(C)"},

    # --- review (post-memo, ms-119) ---
    "review_batch_context": {"cls": "Q", "fused": [], "note": "[post-memo] 節目の全レビュー bundle を JSON 出力=read-only 供給(Q)、状態変更なし"},
    "review_context": {"cls": "Q", "fused": [], "note": "[post-memo] review-kernel(原典+diff)を JSON 出力=read-only 供給(Q)、状態変更なし"},
    "review_done": {"cls": "B", "fused": [], "note": "[post-memo] review-due トリガ解消=gate 制御の機械操作(B)、review 実施の記録"},
    "review_skip": {"cls": "R", "fused": ["B"], "note": "[post-memo] 人間の waiver 決定を durable audit に記録(R)/review-due トリガ解消(B)"},

    # --- rollback ---
    "rollback": {"cls": "C", "fused": ["B"], "note": ""},

    # --- run ---
    "run_list": {"cls": "Q", "fused": [], "note": ""},
    "run_record": {"cls": "R", "fused": ["B"], "note": "結果記録(R)/Operation自律実行(B)"},

    # --- sales ---
    "sales_account_add": {"cls": "R", "fused": [], "note": ""},
    "sales_account_list": {"cls": "Q", "fused": [], "note": ""},
    "sales_account_remove": {"cls": "B", "fused": ["C"], "note": ""},
    "sales_account_resolve": {"cls": "B", "fused": [], "note": "解釈"},
    "sales_account_route": {"cls": "B", "fused": [], "note": ""},
    "sales_account_signature": {"cls": "B", "fused": [], "note": "[post-memo] 送信 account 署名の台帳 config 設定=store 編集(B)、account-route と同族"},
    "sales_account_transcript_source_get": {"cls": "Q", "fused": [], "note": "[post-memo] Account 議事録取得元宣言の read-only 読み出し(Q)"},
    "sales_account_transcript_source_set": {"cls": "B", "fused": [], "note": "[post-memo] Account 議事録取得元の台帳宣言=store config 設定(B)、account-route と同族"},
    "sales_gmail_permalink": {"cls": "Q", "fused": [], "note": "[post-memo] rfc822 id から Gmail permalink URL を算出=純関数/read-only(Q)、state 変更なし"},
    "sales_identity_check": {"cls": "B", "fused": [], "note": ""},
    "sales_identity_set": {"cls": "C", "fused": [], "note": ""},
    "sales_identity_show": {"cls": "Q", "fused": [], "note": ""},
    "sales_reply_watch_op_ensure": {"cls": "B", "fused": [], "note": ""},
    "sales_target": {"cls": "B", "fused": ["R"], "note": ""},
    "sales_target_list": {"cls": "Q", "fused": [], "note": ""},

    # --- save ---
    "save": {"cls": "R", "fused": [], "note": ""},

    # --- search ---
    "search": {"cls": "Q", "fused": [], "note": ""},

    # --- session ---
    "session_attention": {"cls": "R", "fused": [], "note": "attention_required フラグの記録(R)"},
    "session_end": {"cls": "R", "fused": ["C"], "note": "session-end 記録(R)/対話(C)"},
    "session_focus": {"cls": "R", "fused": [], "note": "intent の記録(R)"},
    "session_fork": {"cls": "C", "fused": [], "note": "local"},
    "session_fork_list": {"cls": "Q", "fused": [], "note": ""},
    "session_id": {"cls": "Q", "fused": [], "note": ""},
    "session_log_list": {"cls": "Q", "fused": [], "note": ""},
    "session_log_show": {"cls": "Q", "fused": [], "note": ""},
    "session_rescue": {"cls": "C", "fused": ["B"], "note": ""},

    # --- sessions ---
    "sessions_list": {"cls": "Q", "fused": [], "note": ""},

    # --- skill ---
    "skill_install": {"cls": "B", "fused": ["C"], "note": ""},

    # --- stop ---
    "stop_global": {"cls": "C", "fused": ["B"], "note": ""},
    "stop_scoped": {"cls": "C", "fused": ["B"], "note": ""},
    "stop_status": {"cls": "Q", "fused": [], "note": ""},

    # --- stuck ---
    "stuck_check": {"cls": "B", "fused": [], "note": "検知"},

    # --- summary ---
    "summary": {"cls": "R", "fused": [], "note": ""},

    # --- sync ---
    "sync": {"cls": "R", "fused": ["B"], "note": "報告(R)/bulk 投影(B)"},

    # --- target (post-memo, ms-119/122/124) ---
    "target_advance": {"cls": "R", "fused": [], "note": "[post-memo] target のフェーズ進行を記録(R)、opportunity phase と同型"},
    "target_approve": {"cls": "R", "fused": ["C", "B"], "note": "[post-memo] 目的達成 verdict 記録(R)/人間決定(C)/遷移実行(B)、target/pr approve と同型"},
    "target_attach_evidence": {"cls": "R", "fused": [], "note": "[post-memo] 独立レビュー verdict/根拠を pending 承認に添付=台帳追記(R)"},
    "target_ball": {"cls": "R", "fused": [], "note": "[post-memo] 次手のコート(self/counterpart)を記録(R)"},
    "target_class_add": {"cls": "B", "fused": [], "note": "[post-memo] 新 target-class を project.json へ宣言=descriptor/schema 追加の機械操作(B)、no-code onboarding 経路"},
    "target_class_list": {"cls": "Q", "fused": [], "note": "[post-memo] 宣言済 target-class の read-only 一覧(Q)"},
    "target_close": {"cls": "R", "fused": ["B"], "note": "[post-memo] target を done に=完了決定記録(R)/review-due 発火(B)、milestone done と同型"},
    "target_create": {"cls": "R", "fused": [], "note": "[post-memo] data-defined target 起票=台帳追記(R)、milestone add と同型"},
    "target_evidence": {"cls": "R", "fused": [], "note": "[post-memo] target の Evidence 追加/一覧=追記(R、list は read だが primary は add)"},
    "target_instances": {"cls": "Q", "fused": [], "note": "[post-memo] target-class の instance 一覧=read-only(Q)"},
    "target_list": {"cls": "Q", "fused": [], "note": "[post-memo] target 一覧=read-only(Q)"},
    "target_reject": {"cls": "R", "fused": ["C"], "note": "[post-memo] 遷移却下 verdict 記録(R)/人間決定(C)、遷移は実行しない、pr reject と同型"},
    "target_review_request": {"cls": "R", "fused": [], "note": "[post-memo] 目的達成レビュー(pending 承認)を起票=台帳追記(R)、pr request-review と同族"},
    "target_work_item": {"cls": "R", "fused": [], "note": "[post-memo] target の WorkItem add/done/list=追記(R、list は read だが primary は add/done)"},

    # --- task ---
    "task_add": {"cls": "R", "fused": [], "note": ""},
    "task_cancel": {"cls": "R", "fused": [], "note": ""},
    "task_delete": {"cls": "B", "fused": ["C"], "note": ""},
    "task_detail": {"cls": "Q", "fused": [], "note": ""},
    "task_done": {"cls": "R", "fused": ["B"], "note": "done報告(R)/AC物理照合(B)"},
    "task_list": {"cls": "Q", "fused": [], "note": ""},
    "task_show": {"cls": "Q", "fused": [], "note": ""},
    "task_update": {"cls": "R", "fused": [], "note": ""},

    # --- trek ---
    "trek_archive": {"cls": "B", "fused": ["C"], "note": ""},
    "trek_blanket_approve": {"cls": "C", "fused": ["B"], "note": ""},
    "trek_blanket_revoke": {"cls": "C", "fused": ["B"], "note": ""},
    "trek_create": {"cls": "C", "fused": ["R"], "note": ""},
    "trek_extend_ttl": {"cls": "C", "fused": ["R"], "note": ""},
    "trek_invite": {"cls": "C", "fused": [], "note": ""},
    "trek_join": {"cls": "C", "fused": [], "note": ""},
    "trek_kickoff": {"cls": "C", "fused": ["R"], "note": ""},
    "trek_leave": {"cls": "C", "fused": [], "note": ""},
    "trek_list": {"cls": "Q", "fused": [], "note": ""},
    "trek_plan": {"cls": "C", "fused": ["R"], "note": ""},
    "trek_pulse_ack": {"cls": "R", "fused": [], "note": ""},
    "trek_reconcile": {"cls": "B", "fused": [], "note": ""},
    "trek_resume": {"cls": "C", "fused": ["B"], "note": ""},
    "trek_scope_approve": {"cls": "R", "fused": ["C"], "note": "scope承認記録(R)/決定(C)"},
    "trek_scope_reject": {"cls": "R", "fused": ["C"], "note": ""},
    "trek_show": {"cls": "Q", "fused": [], "note": ""},
    "trek_slot_add": {"cls": "R", "fused": ["B"], "note": ""},
    "trek_slot_amend": {"cls": "R", "fused": ["B"], "note": ""},
    "trek_slot_claim": {"cls": "R", "fused": ["B"], "note": ""},
    "trek_slot_list": {"cls": "Q", "fused": [], "note": ""},
    "trek_start": {"cls": "C", "fused": ["R"], "note": ""},
    "trek_stop": {"cls": "C", "fused": ["B"], "note": ""},
    "trek_summary_sent": {"cls": "R", "fused": [], "note": ""},
    "trek_take_over": {"cls": "C", "fused": ["R"], "note": ""},
    "trek_task_add": {"cls": "R", "fused": [], "note": ""},
    "trek_task_state": {"cls": "R", "fused": [], "note": ""},
    "trek_timeline": {"cls": "Q", "fused": [], "note": ""},
    "trek_transfer_leader": {"cls": "C", "fused": ["R"], "note": ""},

    # --- trigger ---
    "trigger_check": {"cls": "Q", "fused": [], "note": ""},
    "trigger_clear": {"cls": "B", "fused": [], "note": ""},
    "trigger_fire": {"cls": "B", "fused": [], "note": ""},
    "trigger_tick": {"cls": "B", "fused": [], "note": ""},

    # --- update ---
    "update": {"cls": "B", "fused": [], "note": ""},

    # --- watch ---
    "watch_clear": {"cls": "B", "fused": [], "note": ""},
    "watch_list": {"cls": "Q", "fused": [], "note": ""},
    "watch_set": {"cls": "B", "fused": [], "note": ""},

    # --- shell top-level (not in the dispatch map; memo: status=Q, reset=B(+C)) ---
    "status": {"cls": "Q", "fused": [], "note": ""},
    "reset": {"cls": "B", "fused": ["C"], "note": ""},
}
