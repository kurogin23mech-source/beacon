# shellcheck shell=bash
# beacon CLI — acquisition family (12 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_flag _guard_positional _require_audit_reason
# requires-var: COMMANDS_PY BEACON_ACK_SENTINEL
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_acquisition_add() {
    ensure_project
    local title="" description="" assignee=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --description|--desc) description="${2:-}"; shift 2 ;;
            --assignee)           assignee="${2:-}"; shift 2 ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition add \"<title>\" [--description <text>] [--assignee <user>]" ;;
            *)   title="$1"; shift ;;
        esac
    done
    if [ -z "$title" ]; then
        echo "Usage: beacon acquisition add \"<title>\" [--description <text>] [--assignee <user>]"
        exit 1
    fi
    BEACON_ACQ_TITLE="$title" BEACON_ACQ_DESCRIPTION="$description" \
        BEACON_ACQ_ASSIGNEE="$assignee" \
        python3 "$COMMANDS_PY" acquisition_add
}

cmd_acquisition_list() {
    ensure_project
    local json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json="1"; shift ;;
            -?*) _guard_flag "$1" ;;
            *)      shift ;;
        esac
    done
    BEACON_JSON="$json" python3 "$COMMANDS_PY" acquisition_list
}

cmd_acquisition_status() {
    ensure_project
    local acq_id="" status=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon acquisition status <acq-id> <todo|in_progress|done>" ;;
            *)   if [ -z "$acq_id" ]; then acq_id="$1"; else status="$1"; fi; shift ;;
        esac
    done
    if [ -z "$acq_id" ] || [ -z "$status" ]; then
        echo "Usage: beacon acquisition status <acq-id> <todo|in_progress|done>"
        exit 1
    fi
    BEACON_ACQ_ID="$acq_id" BEACON_ACQ_STATUS="$status" \
        python3 "$COMMANDS_PY" acquisition_status
}

cmd_acquisition_attach_list() {
    # ms-132 e-4501 — 施策(acq-)にアタックリスト(ms-131 table-doc)を紐づけ作成。
    # 列は正準スキーマ(対象顧客=acc参照 / 打診フェーズ / 最終接触日 / メモ)。
    ensure_project
    local acq_id="" title="" phases="" json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --phases) phases="${2:-}"; shift 2 ;;
            --json)   json="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition attack-list <acq-id> \"<title>\" [--phases a,b,c] [--json]" ;;
            *)   if [ -z "$acq_id" ]; then acq_id="$1"; else title="$1"; fi; shift ;;
        esac
    done
    if [ -z "$acq_id" ] || [ -z "$title" ]; then
        echo "Usage: beacon acquisition attack-list <acq-id> \"<title>\" [--phases 未接触,連絡済,返信あり,アポ] [--json]"
        echo "  施策(acq-)にアタックリスト(table-doc)を紐づけ作成。列=対象顧客(acc参照)/打診フェーズ/最終接触日/メモ。"
        exit 1
    fi
    BEACON_ACQ_ID="$acq_id" BEACON_ACQ_LIST_TITLE="$title" \
        BEACON_ACQ_LIST_PHASES="$phases" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" acquisition_attach_list
}

cmd_acquisition_lists() {
    # ms-132 e-4501 — 施策配下のアタックリスト一覧 (read-only)。
    ensure_project
    local acq_id="" json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition attack-lists <acq-id> [--json]" ;;
            *)   acq_id="$1"; shift ;;
        esac
    done
    if [ -z "$acq_id" ]; then
        echo "Usage: beacon acquisition attack-lists <acq-id> [--json]"
        exit 1
    fi
    BEACON_ACQ_ID="$acq_id" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" acquisition_lists
}

cmd_acquisition_attack_list_fill() {
    # ms-132 e-4503 — 条件一致の未接触 Account をアタックリストへ一括行追記 (dedup + dry-run)。
    ensure_project
    local doc_id="" phase="" assignee="" name="" limit="" dry="" json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --account-phase) phase="${2:-}"; shift 2 ;;
            --assignee)      assignee="${2:-}"; shift 2 ;;
            --name-contains) name="${2:-}"; shift 2 ;;
            --limit)         limit="${2:-}"; shift 2 ;;
            --dry-run)       dry="1"; shift ;;
            --json)          json="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition attack-list-fill <attack-list-doc-id> [--account-phase <name>] [--assignee <user>] [--name-contains <s>] [--limit N] [--dry-run] [--json]" ;;
            *)   doc_id="$1"; shift ;;
        esac
    done
    if [ -z "$doc_id" ]; then
        echo "Usage: beacon acquisition attack-list-fill <attack-list-doc-id> [--account-phase <name>] [--assignee <user>] [--name-contains <s>] [--limit N (登録順で先頭N件)] [--dry-run] [--json]"
        echo "  条件(既定=プロジェクトの第1顧客フェーズ=未接触)に一致する Account を対象アタックリストへ一括で行追加。重複は自動 skip。--dry-run で事前確認。"
        exit 1
    fi
    BEACON_DOC_ID="$doc_id" BEACON_FILL_PHASE="$phase" BEACON_FILL_ASSIGNEE="$assignee" \
        BEACON_FILL_NAME="$name" BEACON_FILL_LIMIT="$limit" BEACON_DRY_RUN="$dry" \
        BEACON_JSON="$json" python3 "$COMMANDS_PY" acquisition_attack_list_fill
}

cmd_acquisition_attack_list_send() {
    # ms-132 e-4504 — 一括連絡の計画(dry-run 既定) / 人間の1confirm=承認(--confirm)。
    # 承認境界 方針4: confirm 前は 1 通も送信/記録されない。承認は bus-refused で人間限定。
    ensure_project
    local doc_id="" subject="" message="" msgfile="" fromphase="" limit="" confirm="" json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --subject)      subject="${2:-}"; shift 2 ;;
            --message)      message="${2:-}"; shift 2 ;;
            --message-file) msgfile="${2:-}"; shift 2 ;;
            --from-phase)   fromphase="${2:-}"; shift 2 ;;
            --limit)        limit="${2:-}"; shift 2 ;;
            --confirm)      confirm="1"; shift ;;
            --json)         json="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition attack-list-send <attack-list-doc-id> [--subject <s>] [--message-file <f> | --message <body>] [--from-phase <name>] [--limit N] [--confirm] [--json]" ;;
            *)   doc_id="$1"; shift ;;
        esac
    done
    if [ -z "$doc_id" ]; then
        echo "Usage: beacon acquisition attack-list-send <attack-list-doc-id> [--subject <s>] [--message-file <f> | --message <body>] [--from-phase <name>] [--limit N] [--confirm] [--json]"
        echo "  既定=dry-run(計画のみ、送信も記録もしない)。--confirm で人間が1回承認(bus からは不可)。送信自体は Skill が行う。"
        exit 1
    fi
    if [ -n "$msgfile" ] && [ -z "$message" ]; then
        if ! message="$(cat "$msgfile")"; then
            echo "Error: --message-file 読み込み失敗: $msgfile" >&2
            exit 1
        fi
    fi
    BEACON_DOC_ID="$doc_id" BEACON_SEND_SUBJECT="$subject" BEACON_SEND_MESSAGE="$message" \
        BEACON_SEND_FROM_PHASE="$fromphase" BEACON_SEND_LIMIT="$limit" \
        BEACON_CONFIRM="$confirm" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" acquisition_attack_list_send
}

cmd_acquisition_attack_list_send_record() {
    # ms-132 e-4504 — 承認済バッチ内の1送信を記録(証跡+行 未接触→連絡済)。authorized 必須。
    # --message/--message-file は送信した文面で、承認時の文面と digest 照合する。
    ensure_project
    local doc_id="" acc_id="" msgid="" url="" subject="" message="" msgfile="" json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --message-id)   msgid="${2:-}"; shift 2 ;;
            --url)          url="${2:-}"; shift 2 ;;
            --subject)      subject="${2:-}"; shift 2 ;;
            --message)      message="${2:-}"; shift 2 ;;
            --message-file) msgfile="${2:-}"; shift 2 ;;
            --json)         json="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition attack-list-send-record <attack-list-doc-id> <acc-id> --message-id <id> [--message-file <f>|--message <body>] [--subject <s>] [--url <permalink>] [--json]" ;;
            *)   if [ -z "$doc_id" ]; then doc_id="$1"; else acc_id="$1"; fi; shift ;;
        esac
    done
    if [ -z "$doc_id" ] || [ -z "$acc_id" ]; then
        echo "Usage: beacon acquisition attack-list-send-record <attack-list-doc-id> <acc-id> --message-id <id> [--message-file <f>|--message <body>] [--subject <s>] [--url <permalink>] [--json]"
        exit 1
    fi
    if [ -n "$msgfile" ] && [ -z "$message" ]; then
        if ! message="$(cat "$msgfile")"; then
            echo "Error: --message-file 読み込み失敗: $msgfile" >&2
            exit 1
        fi
    fi
    BEACON_DOC_ID="$doc_id" BEACON_SEND_ACC_ID="$acc_id" BEACON_SEND_MESSAGE_ID="$msgid" \
        BEACON_SEND_URL="$url" BEACON_SEND_SUBJECT="$subject" BEACON_SEND_MESSAGE="$message" \
        BEACON_JSON="$json" python3 "$COMMANDS_PY" acquisition_attack_list_send_record
}

cmd_acquisition_attack_list_awaiting_reply() {
    # ms-132 e-4505 — 返信待ち(連絡済)の宛先一覧 (reply-watch の worklist、read-only)。
    ensure_project
    local doc_id="" json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition attack-list-awaiting-reply <attack-list-doc-id> [--json]" ;;
            *)   doc_id="$1"; shift ;;
        esac
    done
    if [ -z "$doc_id" ]; then
        echo "Usage: beacon acquisition attack-list-awaiting-reply <attack-list-doc-id> [--json]"
        exit 1
    fi
    BEACON_DOC_ID="$doc_id" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" acquisition_attack_list_awaiting_reply
}

cmd_acquisition_attack_list_reply_record() {
    # ms-132 e-4505 — 検知した返信を記録(inbound証跡 + 行 連絡済→返信あり + 通知)。検知のみ。
    ensure_project
    local doc_id="" acc_id="" msgid="" url="" summary="" json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --message-id) msgid="${2:-}"; shift 2 ;;
            --url)        url="${2:-}"; shift 2 ;;
            --summary)    summary="${2:-}"; shift 2 ;;
            --json)       json="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition attack-list-reply-record <attack-list-doc-id> <acc-id> [--message-id <id>] [--url <link>] [--summary <s>] [--json]" ;;
            *)   if [ -z "$doc_id" ]; then doc_id="$1"; else acc_id="$1"; fi; shift ;;
        esac
    done
    if [ -z "$doc_id" ] || [ -z "$acc_id" ]; then
        echo "Usage: beacon acquisition attack-list-reply-record <attack-list-doc-id> <acc-id> [--message-id <id>] [--url <link>] [--summary <s>] [--json]"
        exit 1
    fi
    BEACON_DOC_ID="$doc_id" BEACON_SEND_ACC_ID="$acc_id" BEACON_SEND_MESSAGE_ID="$msgid" \
        BEACON_SEND_URL="$url" BEACON_COMM_SUMMARY="$summary" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" acquisition_attack_list_reply_record
}

cmd_acquisition_delete() {
    # ms-132 e-4507 — 打ち切り (soft-cancel)。status でなく削除で中止を表す。
    # account/opportunity delete と同じ監査ゲート: --reason か --acknowledge を要求
    # (destructive verb はどれも同じ形で reason を強制する — AX 原則1 の一貫性)。
    ensure_project
    local acq_id="" reason="" acknowledge=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reason) reason="${2:-}"; shift 2 ;;
            --acknowledge) acknowledge="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition delete <acq-id> (--reason <text> | --acknowledge)" ;;
            *)   acq_id="$1"; shift ;;
        esac
    done
    if [ -z "$acq_id" ]; then
        echo "Usage: beacon acquisition delete <acq-id> (--reason <text> | --acknowledge)"
        exit 1
    fi
    _require_audit_reason "acquisition delete" "$reason" "$acknowledge"
    # Pre-substitute the acknowledged sentinel here, exactly like the sibling
    # destructive verbs (account/opportunity delete, communication cancel) — one
    # audit-gate pattern across all of bin/beacon (保守性レビュー #558 M4). The
    # sentinel value is pinned equal to commands._ACKNOWLEDGED_REASON by a test.
    [ -z "$reason" ] && reason="$BEACON_ACK_SENTINEL"
    BEACON_ACQ_ID="$acq_id" BEACON_CANCEL_REASON="$reason" \
        python3 "$COMMANDS_PY" acquisition_delete
}

cmd_acquisition_attack_list_promote() {
    # ms-132 e-4506 — 返信あり/アポの行を商談へ引き上げ + Account phase 未接触→リード連動。
    ensure_project
    local doc_id="" acc_id="" title="" json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --title) title="${2:-}"; shift 2 ;;
            --json)  json="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon acquisition attack-list-promote <attack-list-doc-id> <acc-id> [--title <商談名>] [--json]" ;;
            *)   if [ -z "$doc_id" ]; then doc_id="$1"; else acc_id="$1"; fi; shift ;;
        esac
    done
    if [ -z "$doc_id" ] || [ -z "$acc_id" ]; then
        echo "Usage: beacon acquisition attack-list-promote <attack-list-doc-id> <acc-id> [--title <商談名>] [--json]"
        exit 1
    fi
    BEACON_DOC_ID="$doc_id" BEACON_SEND_ACC_ID="$acc_id" BEACON_OPP_TITLE="$title" \
        BEACON_JSON="$json" python3 "$COMMANDS_PY" acquisition_attack_list_promote
}
