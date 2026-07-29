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

# key = dispatch key (noun_subcommand); value = {cls, secondary, note}.
VERB_LEDGER = {
    # --- account ---
    "account_add": {"cls": "R", "secondary": ["B"], "note": "生報告(R)/非構造→構造の解釈(B)"},
    "account_assign": {"cls": "R", "secondary": [], "note": ""},
    "account_contact": {"cls": "R", "secondary": [], "note": ""},
    "account_delete": {"cls": "B", "secondary": ["C"], "note": ""},
    "account_list": {"cls": "Q", "secondary": [], "note": ""},
    "account_nurturing": {"cls": "R", "secondary": [], "note": ""},
    "account_phase": {"cls": "R", "secondary": [], "note": ""},
    "account_rename": {"cls": "R", "secondary": [], "note": ""},

    # --- acquisition (post-memo, ms-115 target-class) ---
    "acquisition_add": {"cls": "R", "secondary": [], "note": "[post-memo] 獲得ターゲット起票=台帳追記(R)、milestone add と同型"},
    "acquisition_list": {"cls": "Q", "secondary": [], "note": "[post-memo] read-only 一覧"},
    "acquisition_status": {"cls": "R", "secondary": [], "note": "[post-memo] 状態遷移の記録(R)"},

    # --- activity ---
    "activity_done": {"cls": "R", "secondary": [], "note": ""},

    # --- auth ---
    "auth_login": {"cls": "C", "secondary": [], "note": ""},
    "auth_logout": {"cls": "C", "secondary": [], "note": ""},
    "auth_status": {"cls": "Q", "secondary": [], "note": ""},

    # --- bus ---
    "bus_ack": {"cls": "C", "secondary": [], "note": ""},
    "bus_auto_execute_add": {"cls": "B", "secondary": [], "note": ""},
    "bus_auto_execute_list": {"cls": "Q", "secondary": [], "note": ""},
    "bus_auto_execute_remove": {"cls": "B", "secondary": [], "note": ""},
    "bus_budget_clear": {"cls": "B", "secondary": [], "note": ""},
    "bus_budget_grant": {"cls": "B", "secondary": [], "note": ""},
    "bus_budget_show": {"cls": "Q", "secondary": [], "note": ""},
    "bus_directory": {"cls": "Q", "secondary": [], "note": ""},
    "bus_listen": {"cls": "C", "secondary": [], "note": ""},
    "bus_receive": {"cls": "C", "secondary": [], "note": ""},
    "bus_send": {"cls": "C", "secondary": [], "note": ""},
    "bus_status": {"cls": "Q", "secondary": [], "note": ""},

    # --- channel ---
    "channel_install": {"cls": "C", "secondary": [], "note": ""},
    "channel_opt_in": {"cls": "C", "secondary": [], "note": ""},
    "channel_opt_out": {"cls": "C", "secondary": [], "note": ""},
    "channel_status": {"cls": "Q", "secondary": [], "note": ""},
    "channel_uninstall": {"cls": "C", "secondary": [], "note": ""},

    # --- claim ---
    "claim_handoff": {"cls": "R", "secondary": ["C"], "note": ""},
    "claim_list": {"cls": "Q", "secondary": [], "note": ""},
    "claim_post": {"cls": "R", "secondary": [], "note": ""},
    "claim_release": {"cls": "R", "secondary": [], "note": ""},
    "claim_request": {"cls": "R", "secondary": ["C"], "note": ""},
    "claim_respond": {"cls": "R", "secondary": ["C"], "note": ""},
    "claim_view": {"cls": "Q", "secondary": [], "note": "[post-memo] 2層 claim state を read-only 表示、非排他 (ms-112 設計方針3)"},

    # --- cloud ---
    "cloud_join": {"cls": "C", "secondary": [], "note": ""},
    "cloud_list": {"cls": "Q", "secondary": [], "note": ""},
    "cloud_migrate_from_local": {"cls": "B", "secondary": [], "note": ""},
    "cloud_push": {"cls": "B", "secondary": [], "note": ""},
    "cloud_status": {"cls": "Q", "secondary": [], "note": ""},

    # --- communication ---
    "communication_add": {"cls": "R", "secondary": ["B"], "note": "生報告(R)/非構造→構造の解釈(B)"},
    "communication_cancel": {"cls": "R", "secondary": [], "note": ""},
    "communication_list": {"cls": "Q", "secondary": [], "note": ""},
    "communication_retarget": {"cls": "R", "secondary": [], "note": ""},

    # --- cycle ---
    "cycle_status": {"cls": "Q", "secondary": [], "note": ""},

    # --- deploy ---
    "deploy_delete": {"cls": "B", "secondary": [], "note": ""},
    "deploy_list": {"cls": "Q", "secondary": [], "note": ""},
    "deploy_record": {"cls": "R", "secondary": [], "note": ""},
    "deploy_rollback": {"cls": "C", "secondary": ["B"], "note": ""},
    "deploy_void": {"cls": "B", "secondary": ["R"], "note": ""},

    # --- disclose / undisclose (post-memo, ms-113) ---
    "disclose": {"cls": "B", "secondary": [], "note": "[post-memo] 任意 Target を別 project へ開示=認可/投影の機械操作(B)"},
    "undisclose": {"cls": "B", "secondary": [], "note": "[post-memo] 開示リンク剥奪 (即時)=認可/投影の機械操作(B)"},

    # --- dm ---
    "dm_log": {"cls": "Q", "secondary": [], "note": ""},
    "dm_respond": {"cls": "C", "secondary": ["B"], "note": "人間承認(C)/gate 反映(B)"},

    # --- doc ---
    "doc_add": {"cls": "R", "secondary": ["C"], "note": "台帳追記(R)/対話(C)"},
    "doc_delete": {"cls": "B", "secondary": ["C"], "note": ""},
    "doc_history": {"cls": "Q", "secondary": [], "note": ""},
    "doc_image_upload": {"cls": "R", "secondary": ["C"], "note": ""},
    "doc_list": {"cls": "Q", "secondary": [], "note": ""},
    "doc_restore": {"cls": "B", "secondary": ["R"], "note": ""},
    "doc_show": {"cls": "Q", "secondary": [], "note": ""},
    # ms-131 e-4496 — table-doc row operations. All writes route through the
    # append-only model (set-cell/rm-row never overwrite past state), so they are
    # record verbs (R); show is a read-only render (Q).
    "doc_table_add_row": {"cls": "R", "secondary": [], "note": "table-doc に行を追記(R、型検査+履歴)"},
    "doc_table_create": {"cls": "R", "secondary": [], "note": "format:table の doc を作成(R)"},
    "doc_table_rm_row": {"cls": "R", "secondary": [], "note": "行を soft-delete=tombstone 追記(R、監査痕跡は残る)"},
    "doc_table_set_cell": {"cls": "R", "secondary": [], "note": "セル更新=旧値を上書きせず履歴に追記(R)"},
    "doc_table_show": {"cls": "Q", "secondary": [], "note": "table-doc を表として read-only 描画(Q)"},
    "doc_update": {"cls": "R", "secondary": ["C"], "note": "台帳追記(R)/対話(C)"},

    # --- doctor ---
    "doctor": {"cls": "B", "secondary": [], "note": ""},

    # --- entry ---
    "entry_move": {"cls": "B", "secondary": ["R"], "note": ""},
    "entry_purge": {"cls": "B", "secondary": [], "note": ""},

    # --- help ---
    "help_render": {"cls": "Q", "secondary": [], "note": "[post-memo] レジストリからの help 描画=read-only 参照(Q)"},

    # --- incident ---
    "incident_close": {"cls": "R", "secondary": [], "note": "close報告+決定(R)"},
    "incident_escalate": {"cls": "R", "secondary": ["C"], "note": ""},
    "incident_list": {"cls": "Q", "secondary": [], "note": ""},
    "incident_open": {"cls": "R", "secondary": [], "note": ""},

    # --- init ---
    "init": {"cls": "B", "secondary": ["C"], "note": ""},

    # --- issue ---
    "issue_import": {"cls": "B", "secondary": ["R"], "note": ""},
    "issue_list": {"cls": "Q", "secondary": [], "note": ""},
    "issue_sync": {"cls": "B", "secondary": [], "note": ""},

    # --- log (R系統の雛形) ---
    "log": {"cls": "R", "secondary": ["B"], "note": "commit報告(R)/progress導出・AC判定(B、自己採点をサーバへ)"},
    "log_finalize": {"cls": "R", "secondary": ["B"], "note": ""},
    "log_prepare": {"cls": "Q", "secondary": [], "note": ""},

    # --- master (post-memo, ms-111 identity master-sync) ---
    "master_sync_drain": {"cls": "B", "secondary": [], "note": "[post-memo] identity master-sync の未配送 outbox を操作非依存の定期経路から再送する machinery (e-4399)。plumbing なので B"},

    # --- meeting ---
    "meeting_cancel": {"cls": "R", "secondary": ["C"], "note": "cancel報告(R)/外向き通知(C)"},
    "meeting_end": {"cls": "R", "secondary": [], "note": ""},
    "meeting_ended": {"cls": "B", "secondary": [], "note": "検知"},
    "meeting_list": {"cls": "Q", "secondary": [], "note": ""},
    "meeting_reschedule": {"cls": "C", "secondary": ["R"], "note": "カレンダー作成(C外向き)/活動記録(R)"},
    "meeting_schedule": {"cls": "C", "secondary": ["R"], "note": "カレンダー作成(C外向き)/活動記録(R)"},

    # --- member ---
    "member_add": {"cls": "C", "secondary": ["R"], "note": ""},
    "member_invitation_cancel": {"cls": "C", "secondary": [], "note": ""},
    "member_invitation_list": {"cls": "Q", "secondary": [], "note": ""},
    "member_invite": {"cls": "C", "secondary": [], "note": ""},
    "member_join": {"cls": "C", "secondary": [], "note": ""},
    "member_list": {"cls": "Q", "secondary": [], "note": ""},
    "member_remove": {"cls": "C", "secondary": ["R"], "note": ""},
    "member_role": {"cls": "C", "secondary": ["R"], "note": ""},
    "member_whoami": {"cls": "Q", "secondary": [], "note": ""},

    # --- migrate ---
    "migrate_target_labels": {"cls": "B", "secondary": [], "note": ""},

    # --- milestone ---
    "milestone_add": {"cls": "R", "secondary": [], "note": ""},
    "milestone_delete": {"cls": "B", "secondary": ["C"], "note": ""},
    "milestone_depends": {"cls": "R", "secondary": [], "note": ""},
    "milestone_done": {"cls": "R", "secondary": [], "note": ""},
    "milestone_graph": {"cls": "Q", "secondary": [], "note": ""},
    "milestone_join": {"cls": "R", "secondary": [], "note": ""},
    "milestone_list": {"cls": "Q", "secondary": [], "note": ""},
    "milestone_observe": {"cls": "R", "secondary": [], "note": ""},
    "milestone_occupations": {"cls": "Q", "secondary": [], "note": ""},
    "milestone_purge": {"cls": "B", "secondary": [], "note": ""},
    "milestone_release": {"cls": "R", "secondary": [], "note": ""},
    "milestone_show": {"cls": "Q", "secondary": [], "note": ""},
    "milestone_start": {"cls": "R", "secondary": ["C", "B"], "note": "活性化決定(R)/worktree作成(C)/branch(B)"},
    "milestone_update": {"cls": "R", "secondary": [], "note": ""},
    "milestone_wait": {"cls": "R", "secondary": [], "note": ""},
    "milestone_workspace": {"cls": "C", "secondary": [], "note": ""},
    "milestone_workspace_cleanup": {"cls": "C", "secondary": [], "note": ""},

    # --- morning ---
    "morning": {"cls": "Q", "secondary": [], "note": ""},

    # --- note ---
    "note_add": {"cls": "R", "secondary": [], "note": ""},
    "note_clear": {"cls": "B", "secondary": [], "note": ""},
    "note_list": {"cls": "Q", "secondary": [], "note": ""},

    # --- operation ---
    "operation_approve": {"cls": "C", "secondary": ["B"], "note": "人間承認(C)/gate反映(B)"},
    "operation_close": {"cls": "R", "secondary": ["B"], "note": ""},
    "operation_envelope_verify": {"cls": "B", "secondary": [], "note": ""},
    "operation_list": {"cls": "Q", "secondary": [], "note": ""},
    "operation_open": {"cls": "R", "secondary": ["C"], "note": "起票(R)/設定対話(C)"},
    "operation_purge": {"cls": "B", "secondary": [], "note": ""},
    "operation_revoke": {"cls": "C", "secondary": ["B"], "note": ""},
    "operation_server_tick": {"cls": "B", "secondary": [], "note": ""},
    "operation_set_status": {"cls": "R", "secondary": ["B"], "note": ""},
    "operation_show": {"cls": "Q", "secondary": [], "note": ""},
    "operation_task_add": {"cls": "R", "secondary": [], "note": ""},
    "operation_task_done": {"cls": "R", "secondary": [], "note": ""},
    "operation_task_list": {"cls": "Q", "secondary": [], "note": ""},
    "operation_update": {"cls": "R", "secondary": [], "note": ""},

    # --- opportunity ---
    "opportunity_activity": {"cls": "R", "secondary": [], "note": ""},
    "opportunity_add": {"cls": "R", "secondary": [], "note": ""},
    "opportunity_amount": {"cls": "R", "secondary": [], "note": ""},
    "opportunity_assign": {"cls": "R", "secondary": [], "note": ""},
    "opportunity_delete": {"cls": "B", "secondary": ["C"], "note": ""},
    "opportunity_describe": {"cls": "R", "secondary": [], "note": "[post-memo] 背景/経緯メモの追記(R)、opportunity add と同族"},
    "opportunity_due": {"cls": "R", "secondary": [], "note": ""},
    "opportunity_judge": {"cls": "R", "secondary": ["B"], "note": "判定主体を監査重みで振る(人間=R運搬/サーバ=B)"},
    "opportunity_list": {"cls": "Q", "secondary": [], "note": ""},
    "opportunity_phase": {"cls": "R", "secondary": [], "note": ""},
    "opportunity_phase_prob": {"cls": "R", "secondary": [], "note": ""},
    "opportunity_rename": {"cls": "R", "secondary": [], "note": "[post-memo] title 修正の記録(R)、milestone rename と同型"},
    "opportunity_transition_date": {"cls": "R", "secondary": [], "note": ""},

    # --- org (post-memo, ms-118) ---
    "org_create": {"cls": "C", "secondary": ["R"], "note": "[post-memo] org 立ち上げ=owner 割当を伴う identity 作成(C)/store 追記(R)、member 系と同族"},
    "org_list": {"cls": "Q", "secondary": [], "note": "[post-memo] 自分が member の org 一覧=read-only(Q)"},
    "org_show": {"cls": "Q", "secondary": [], "note": "[post-memo] 単一 org 表示=read-only(Q)"},
    "org_add_member": {"cls": "C", "secondary": ["R"], "note": "[post-memo] 所属付与=人間窓口の招待(C)/store 追記(R)、member add と同型"},
    "org_remove_member": {"cls": "C", "secondary": ["R"], "note": "[post-memo] 所属剥奪=人間窓口(C)/store 更新(R)、member remove と同型"},
    "org_delete": {"cls": "B", "secondary": ["C"], "note": "[post-memo] org 破壊的削除=store 保守/認可の機械(B)/owner-only 窓口(C)、milestone delete と同型"},
    "org_rehome": {"cls": "B", "secondary": [], "note": "[post-memo] project の org 所属リンク張替=認可/投影の機械操作(B)、disclose と同族"},

    # --- phase funnel config (post-memo, ms-116) ---
    "phase_add": {"cls": "B", "secondary": [], "note": "[post-memo] funnel(config)段追加=project.json 保守/store 編集(B)、entity 遷移でなく methodology"},
    "phase_list": {"cls": "Q", "secondary": [], "note": ""},
    "phase_move": {"cls": "B", "secondary": [], "note": "[post-memo] funnel 段の並べ替え=config 保守(B)"},
    "phase_remove": {"cls": "B", "secondary": [], "note": "[post-memo] funnel 段削除=config 保守(B)"},
    "phase_rename": {"cls": "B", "secondary": [], "note": "[post-memo] funnel 段改名=config 保守(B)"},

    # --- pr ---
    "pr_add": {"cls": "R", "secondary": [], "note": ""},
    "pr_approve": {"cls": "R", "secondary": ["C"], "note": "承認記録(R)/決定(C)"},
    "pr_close": {"cls": "R", "secondary": [], "note": ""},
    "pr_create": {"cls": "C", "secondary": ["R"], "note": "GitHub効果(C)/PR状態記録(R)"},
    "pr_merge": {"cls": "C", "secondary": ["R"], "note": "GitHub効果(C)/PR状態記録(R)"},
    "pr_reject": {"cls": "R", "secondary": ["C"], "note": ""},
    "pr_request_changes": {"cls": "R", "secondary": ["C"], "note": ""},
    "pr_request_review": {"cls": "R", "secondary": ["C"], "note": ""},
    "pr_show": {"cls": "Q", "secondary": [], "note": ""},
    "pr_sync": {"cls": "B", "secondary": ["R"], "note": ""},

    # --- profile ---
    "profile_list": {"cls": "Q", "secondary": [], "note": ""},

    # --- project ---
    "project_archive": {"cls": "B", "secondary": ["C"], "note": ""},
    "project_cleanup": {"cls": "B", "secondary": ["C"], "note": "[post-memo] orphan project 一括 archive=store 保守の機械(B)/人間確認 checkpoint(C)、ms-123 二段確認"},
    "project_export": {"cls": "B", "secondary": [], "note": ""},
    "project_import": {"cls": "B", "secondary": [], "note": ""},
    "project_orphans": {"cls": "Q", "secondary": [], "note": "[post-memo] orphan 候補の read-only スキャン(Q)、変更なし (archive は別 verb)"},
    "project_rename": {"cls": "R", "secondary": [], "note": "[post-memo] project 名の更新記録(R)、milestone rename と同型"},
    "project_unarchive": {"cls": "B", "secondary": ["C"], "note": ""},

    # --- push ---
    "push_list": {"cls": "Q", "secondary": [], "note": ""},
    "push_record": {"cls": "R", "secondary": [], "note": ""},

    # --- resume ---
    "resume_global": {"cls": "C", "secondary": ["B"], "note": ""},
    "resume_scoped": {"cls": "C", "secondary": ["B"], "note": ""},

    # --- retro ---
    "retro_done": {"cls": "R", "secondary": [], "note": ""},
    "retro_prepare": {"cls": "Q", "secondary": [], "note": ""},
    "retro_save": {"cls": "R", "secondary": ["C"], "note": "台帳追記(R)/対話(C)"},

    # --- review (post-memo, ms-119) ---
    "review_batch_context": {"cls": "Q", "secondary": [], "note": "[post-memo] 節目の全レビュー bundle を JSON 出力=read-only 供給(Q)、状態変更なし"},
    "review_context": {"cls": "Q", "secondary": [], "note": "[post-memo] review-kernel(原典+diff)を JSON 出力=read-only 供給(Q)、状態変更なし"},
    "review_done": {"cls": "B", "secondary": [], "note": "[post-memo] review-due トリガ解消=gate 制御の機械操作(B)、review 実施の記録"},
    "review_skip": {"cls": "R", "secondary": ["B"], "note": "[post-memo] 人間の waiver 決定を durable audit に記録(R)/review-due トリガ解消(B)"},

    # --- rollback ---
    "rollback": {"cls": "C", "secondary": ["B"], "note": ""},

    # --- run ---
    "run_list": {"cls": "Q", "secondary": [], "note": ""},
    "run_record": {"cls": "R", "secondary": ["B"], "note": "結果記録(R)/Operation自律実行(B)"},

    # --- sales ---
    "sales_account_add": {"cls": "R", "secondary": [], "note": ""},
    "sales_account_list": {"cls": "Q", "secondary": [], "note": ""},
    "sales_account_remove": {"cls": "B", "secondary": ["C"], "note": ""},
    "sales_account_resolve": {"cls": "B", "secondary": [], "note": "解釈"},
    "sales_account_route": {"cls": "B", "secondary": [], "note": ""},
    "sales_account_signature": {"cls": "B", "secondary": [], "note": "[post-memo] 送信 account 署名の台帳 config 設定=store 編集(B)、account-route と同族"},
    "sales_account_transcript_source_get": {"cls": "Q", "secondary": [], "note": "[post-memo] Account 議事録取得元宣言の read-only 読み出し(Q)"},
    "sales_account_transcript_source_set": {"cls": "B", "secondary": [], "note": "[post-memo] Account 議事録取得元の台帳宣言=store config 設定(B)、account-route と同族"},
    "sales_gmail_permalink": {"cls": "Q", "secondary": [], "note": "[post-memo] rfc822 id から Gmail permalink URL を算出=純関数/read-only(Q)、state 変更なし"},
    "sales_identity_check": {"cls": "B", "secondary": [], "note": ""},
    "sales_identity_set": {"cls": "C", "secondary": [], "note": ""},
    "sales_identity_show": {"cls": "Q", "secondary": [], "note": ""},
    "sales_reply_watch_op_ensure": {"cls": "B", "secondary": [], "note": ""},
    "sales_target": {"cls": "B", "secondary": ["R"], "note": ""},
    "sales_target_list": {"cls": "Q", "secondary": [], "note": ""},

    # --- save ---
    "save": {"cls": "R", "secondary": [], "note": ""},

    # --- search ---
    "search": {"cls": "Q", "secondary": [], "note": ""},

    # --- session ---
    "session_attention": {"cls": "R", "secondary": [], "note": "attention_required フラグの記録(R)"},
    "session_end": {"cls": "R", "secondary": ["C"], "note": "session-end 記録(R)/対話(C)"},
    "session_focus": {"cls": "R", "secondary": [], "note": "intent の記録(R)"},
    "session_fork": {"cls": "C", "secondary": [], "note": "local"},
    "session_fork_list": {"cls": "Q", "secondary": [], "note": ""},
    "session_id": {"cls": "Q", "secondary": [], "note": ""},
    "session_log_list": {"cls": "Q", "secondary": [], "note": ""},
    "session_log_show": {"cls": "Q", "secondary": [], "note": ""},
    "session_rescue": {"cls": "C", "secondary": ["B"], "note": ""},

    # --- sessions ---
    "sessions_list": {"cls": "Q", "secondary": [], "note": ""},

    # --- skill ---
    "skill_install": {"cls": "B", "secondary": ["C"], "note": ""},

    # --- stop ---
    "stop_global": {"cls": "C", "secondary": ["B"], "note": ""},
    "stop_scoped": {"cls": "C", "secondary": ["B"], "note": ""},
    "stop_status": {"cls": "Q", "secondary": [], "note": ""},

    # --- stuck ---
    "stuck_check": {"cls": "B", "secondary": [], "note": "検知"},

    # --- summary ---
    "summary": {"cls": "R", "secondary": [], "note": ""},

    # --- sync ---
    "sync": {"cls": "R", "secondary": ["B"], "note": "報告(R)/bulk 投影(B)"},

    # --- target (post-memo, ms-119/122/124) ---
    "target_advance": {"cls": "R", "secondary": [], "note": "[post-memo] target のフェーズ進行を記録(R)、opportunity phase と同型"},
    "target_approve": {"cls": "R", "secondary": ["C", "B"], "note": "[post-memo] 目的達成 verdict 記録(R)/人間決定(C)/遷移実行(B)、target/pr approve と同型"},
    "target_attach_evidence": {"cls": "R", "secondary": [], "note": "[post-memo] 独立レビュー verdict/根拠を pending 承認に添付=台帳追記(R)"},
    "target_ball": {"cls": "R", "secondary": [], "note": "[post-memo] 次手のコート(self/counterpart)を記録(R)"},
    "target_class_add": {"cls": "B", "secondary": [], "note": "[post-memo] 新 target-class を project.json へ宣言=descriptor/schema 追加の機械操作(B)、no-code onboarding 経路"},
    "target_class_list": {"cls": "Q", "secondary": [], "note": "[post-memo] 宣言済 target-class の read-only 一覧(Q)"},
    "target_close": {"cls": "R", "secondary": ["B"], "note": "[post-memo] target を done に=完了決定記録(R)/review-due 発火(B)、milestone done と同型"},
    "target_create": {"cls": "R", "secondary": [], "note": "[post-memo] data-defined target 起票=台帳追記(R)、milestone add と同型"},
    "target_evidence": {"cls": "R", "secondary": [], "note": "[post-memo] target の Evidence 追加/一覧=追記(R、list は read だが primary は add)"},
    "target_instances": {"cls": "Q", "secondary": [], "note": "[post-memo] target-class の instance 一覧=read-only(Q)"},
    "target_list": {"cls": "Q", "secondary": [], "note": "[post-memo] target 一覧=read-only(Q)"},
    "target_reject": {"cls": "R", "secondary": ["C"], "note": "[post-memo] 遷移却下 verdict 記録(R)/人間決定(C)、遷移は実行しない、pr reject と同型"},
    "target_review_request": {"cls": "R", "secondary": [], "note": "[post-memo] 目的達成レビュー(pending 承認)を起票=台帳追記(R)、pr request-review と同族"},
    "target_work_item": {"cls": "R", "secondary": [], "note": "[post-memo] target の WorkItem add/done/list=追記(R、list は read だが primary は add/done)"},

    # --- task ---
    "task_add": {"cls": "R", "secondary": [], "note": ""},
    "task_cancel": {"cls": "R", "secondary": [], "note": ""},
    "task_delete": {"cls": "B", "secondary": ["C"], "note": ""},
    "task_detail": {"cls": "Q", "secondary": [], "note": ""},
    "task_done": {"cls": "R", "secondary": ["B"], "note": "done報告(R)/AC物理照合(B)"},
    "task_list": {"cls": "Q", "secondary": [], "note": ""},
    "task_show": {"cls": "Q", "secondary": [], "note": ""},
    "task_update": {"cls": "R", "secondary": [], "note": ""},

    # --- trek ---
    "trek_archive": {"cls": "B", "secondary": ["C"], "note": ""},
    "trek_blanket_approve": {"cls": "C", "secondary": ["B"], "note": ""},
    "trek_blanket_revoke": {"cls": "C", "secondary": ["B"], "note": ""},
    "trek_block": {"cls": "R", "secondary": ["B"], "note": "依存(blocker)を張る記録(R)/state遷移はserver機構(B)"},
    "trek_blockers": {"cls": "Q", "secondary": [], "note": ""},
    "trek_create": {"cls": "C", "secondary": ["R"], "note": ""},
    "trek_extend_ttl": {"cls": "C", "secondary": ["R"], "note": ""},
    "trek_invite": {"cls": "C", "secondary": [], "note": ""},
    "trek_join": {"cls": "C", "secondary": [], "note": ""},
    "trek_kickoff": {"cls": "C", "secondary": ["R"], "note": ""},
    "trek_leave": {"cls": "C", "secondary": [], "note": ""},
    "trek_list": {"cls": "Q", "secondary": [], "note": ""},
    "trek_plan": {"cls": "C", "secondary": ["R"], "note": ""},
    "trek_pulse_ack": {"cls": "R", "secondary": [], "note": ""},
    "trek_reconcile": {"cls": "B", "secondary": [], "note": ""},
    "trek_resume": {"cls": "C", "secondary": ["B"], "note": ""},
    "trek_review_verdicts": {"cls": "Q", "secondary": [], "note": "leader_review verdict 集合を read-only で返す (e-4374)"},
    "trek_scope_approve": {"cls": "R", "secondary": ["C"], "note": "scope承認記録(R)/決定(C)"},
    "trek_scope_reject": {"cls": "R", "secondary": ["C"], "note": ""},
    "trek_show": {"cls": "Q", "secondary": [], "note": ""},
    "trek_slot_add": {"cls": "R", "secondary": ["B"], "note": ""},
    "trek_slot_amend": {"cls": "R", "secondary": ["B"], "note": ""},
    "trek_slot_claim": {"cls": "R", "secondary": ["B"], "note": ""},
    "trek_slot_list": {"cls": "Q", "secondary": [], "note": ""},
    "trek_start": {"cls": "C", "secondary": ["R"], "note": ""},
    "trek_stop": {"cls": "C", "secondary": ["B"], "note": ""},
    "trek_summary_sent": {"cls": "R", "secondary": [], "note": ""},
    "trek_take_over": {"cls": "C", "secondary": ["R"], "note": ""},
    "trek_task_add": {"cls": "R", "secondary": [], "note": ""},
    "trek_task_state": {"cls": "R", "secondary": [], "note": ""},
    "trek_timeline": {"cls": "Q", "secondary": [], "note": ""},
    "trek_transfer_leader": {"cls": "C", "secondary": ["R"], "note": ""},
    "trek_unblock": {"cls": "R", "secondary": ["B"], "note": "依存を外す記録(R)/reconcileはserver機構(B)"},

    # --- trigger ---
    "trigger_check": {"cls": "Q", "secondary": [], "note": ""},
    "trigger_clear": {"cls": "B", "secondary": [], "note": ""},
    "trigger_fire": {"cls": "B", "secondary": [], "note": ""},
    "trigger_tick": {"cls": "B", "secondary": [], "note": ""},

    # --- update ---
    "update": {"cls": "B", "secondary": [], "note": ""},

    # --- watch ---
    "watch_clear": {"cls": "B", "secondary": [], "note": ""},
    "watch_list": {"cls": "Q", "secondary": [], "note": ""},
    "watch_set": {"cls": "B", "secondary": [], "note": ""},

    # --- shell top-level (not in the dispatch map; memo: status=Q, reset=B(+C)) ---
    "status": {"cls": "Q", "secondary": [], "note": ""},
    "reset": {"cls": "B", "secondary": ["C"], "note": ""},
}
