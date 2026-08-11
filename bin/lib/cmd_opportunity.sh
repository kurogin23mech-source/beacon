# shellcheck shell=bash
# beacon CLI — opportunity family (13 functions)
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

cmd_opportunity_add() {
    ensure_project
    local title="" account="" phase="" goal="" probability="" deadline="" ball="" assignee="" description=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --account)     account="${2:-}"; shift 2 ;;
            --phase)       phase="${2:-}"; shift 2 ;;
            --goal)        goal="${2:-}"; shift 2 ;;
            --probability) probability="${2:-}"; shift 2 ;;
            --deadline)    deadline="${2:-}"; shift 2 ;;
            --ball)        ball="${2:-}"; shift 2 ;;
            --assignee)    assignee="${2:-}"; shift 2 ;;
            --description|--desc) description="${2:-}"; shift 2 ;;
            -?*)           _guard_positional "$1" "Usage: beacon opportunity add \"<title>\" [--account <acc-id>] [--phase <p>] [--goal <n>] [--probability <n>] [--deadline <date>] [--ball self|counterpart] [--assignee <user>] [--description <text>]" ;;
            *)             title="$1"; shift ;;
        esac
    done
    if [ -z "$title" ]; then
        echo "Usage: beacon opportunity add \"<title>\" [--account <acc-id>] [--phase <p>] [--goal <n>] [--probability <n>] [--deadline <date>] [--ball self|counterpart] [--assignee <user>] [--description <text>]"
        exit 1
    fi
    BEACON_OPP_TITLE="$title" BEACON_OPP_ACCOUNT="$account" \
        BEACON_OPP_PHASE="$phase" BEACON_OPP_GOAL="$goal" \
        BEACON_OPP_PROBABILITY="$probability" BEACON_OPP_DEADLINE="$deadline" \
        BEACON_OPP_BALL="$ball" BEACON_OPP_ASSIGNEE="$assignee" \
        BEACON_OPP_DESCRIPTION="$description" \
        python3 "$COMMANDS_PY" opportunity_add
}

cmd_opportunity_describe() {
    ensure_project
    local opp_id="" description=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon opportunity describe <opp-id> <text>" ;;
            *)   if [ -z "$opp_id" ]; then opp_id="$1"; else description="$1"; fi; shift ;;
        esac
    done
    if [ -z "$opp_id" ]; then
        echo "Usage: beacon opportunity describe <opp-id> <text>   (背景/経緯/メモ; 空文字でクリア)"
        exit 1
    fi
    BEACON_OPP_ID="$opp_id" BEACON_OPP_DESCRIPTION="$description" \
        python3 "$COMMANDS_PY" opportunity_describe
}

cmd_opportunity_rename() {
    # e-3909: post-creation title edit. Before this an opportunity's name was
    # permanent (describe sets 背景, not the title). Parallels `milestone rename`.
    ensure_project
    local opp_id="" title=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon opportunity rename <opp-id> <new-title>" ;;
            *)   if [ -z "$opp_id" ]; then opp_id="$1"; else title="$1"; fi; shift ;;
        esac
    done
    if [ -z "$opp_id" ] || [ -z "$title" ]; then
        echo "Usage: beacon opportunity rename <opp-id> <new-title>"
        exit 1
    fi
    BEACON_OPP_ID="$opp_id" BEACON_TITLE="$title" \
        python3 "$COMMANDS_PY" opportunity_rename
}

cmd_opportunity_assign() {
    ensure_project
    local opp_id="" assignee=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon opportunity assign <opp-id> <user>" ;;
            *)   if [ -z "$opp_id" ]; then opp_id="$1"; else assignee="$1"; fi; shift ;;
        esac
    done
    if [ -z "$opp_id" ]; then
        echo "Usage: beacon opportunity assign <opp-id> <user>"
        exit 1
    fi
    BEACON_OPP_ID="$opp_id" BEACON_ASSIGNEE="$assignee" \
        python3 "$COMMANDS_PY" opportunity_assign
}

cmd_opportunity_amount() {
    ensure_project
    local opp_id="" amount=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon opportunity amount <opp-id> <amount>" ;;
            *)   if [ -z "$opp_id" ]; then opp_id="$1"; else amount="$1"; fi; shift ;;
        esac
    done
    if [ -z "$opp_id" ] || [ -z "$amount" ]; then
        echo "Usage: beacon opportunity amount <opp-id> <amount>"
        exit 1
    fi
    BEACON_OPP_ID="$opp_id" BEACON_OPP_AMOUNT="$amount" \
        python3 "$COMMANDS_PY" opportunity_amount
}

cmd_opportunity_phase_prob() {
    ensure_project
    local phase="" prob=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon opportunity phase-prob <phase> <n>" ;;
            *)   if [ -z "$phase" ]; then phase="$1"; else prob="$1"; fi; shift ;;
        esac
    done
    if [ -z "$phase" ] || [ -z "$prob" ]; then
        echo "Usage: beacon opportunity phase-prob <phase> <n>"
        exit 1
    fi
    BEACON_PHASE_NAME="$phase" BEACON_PHASE_PROB="$prob" \
        python3 "$COMMANDS_PY" opportunity_phase_prob
}

cmd_opportunity_list() {
    ensure_project
    local json_flag="" all_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            --all)  all_flag="1"; shift ;;   # e-3586: 取消済も含めて全件出す
            -?*) _guard_flag "$1" ;;
            *)      shift ;;
        esac
    done
    BEACON_JSON="$json_flag" BEACON_ALL="$all_flag" \
        python3 "$COMMANDS_PY" opportunity_list
}

cmd_opportunity_phase() {
    ensure_project
    local opp_id="" phase="" note=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --note) note="${2:-}"; shift 2 ;;
            -?*)    _guard_positional "$1" "Usage: beacon opportunity phase <opp-id> <phase> [--note <text>]" ;;
            *)
                if [ -z "$opp_id" ]; then opp_id="$1"; else phase="$1"; fi
                shift ;;
        esac
    done
    if [ -z "$opp_id" ] || [ -z "$phase" ]; then
        echo "Usage: beacon opportunity phase <opp-id> <phase> [--note <text>]"
        exit 1
    fi
    BEACON_OPP_ID="$opp_id" BEACON_PHASE="$phase" BEACON_PHASE_NOTE="$note" \
        python3 "$COMMANDS_PY" opportunity_phase
}

cmd_opportunity_transition_date() {
    ensure_project
    # e-3909: <YYYY-MM-DD> と --clear は「遷移日を設定する / クリアする」で意味が
    # 排他。旧実装は両者を同じ変数に流し込んでいたため、両方渡すと順序依存で
    # 静かにどちらかが勝つ曖昧さがあった。別変数で受けて明示的に排他判定する。
    local opp_id="" pos_date="" note="" clear_flag=""
    local _usage="Usage: beacon opportunity transition-date <opp-id> (<YYYY-MM-DD> | --clear) [--note <text>]"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --note) note="${2:-}"; shift 2 ;;
            --clear) clear_flag="1"; shift ;;
            -?*)    _guard_positional "$1" "$_usage" ;;
            *)
                if [ -z "$opp_id" ]; then opp_id="$1"; else pos_date="$1"; fi
                shift ;;
        esac
    done
    if [ -n "$clear_flag" ] && [ -n "$pos_date" ]; then
        echo "Error: <YYYY-MM-DD> と --clear は排他です (遷移日を設定する か クリアする かのどちらか一方)。" >&2
        echo "       $_usage" >&2
        exit 2
    fi
    if [ -z "$opp_id" ] || { [ -z "$clear_flag" ] && [ -z "$pos_date" ]; }; then
        echo "$_usage"
        exit 1
    fi
    # --clear passes an empty date through (set_transition_date treats it as clear).
    BEACON_OPP_ID="$opp_id" BEACON_TRANSITION_DATE="$pos_date" BEACON_PHASE_NOTE="$note" \
        python3 "$COMMANDS_PY" opportunity_transition_date
}

cmd_opportunity_anchor() {
    ensure_project
    # e-5177: 前進ゲートの発火源 (anchor) に work-item (面談/活動/ナーチャリング)
    # を結ぶ入口。既定フェーズ (先方検討中/合意済み) は面談 template を持たず
    # auto-anchor が発火しないため、AI が非面談 work-item を明示的に結べる verb。
    local opp_id="" work_item=""
    local _usage="Usage: beacon opportunity anchor <opp-id> <work-item-id>  (work-item = mtg-/act-)"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*)    _guard_positional "$1" "$_usage" ;;
            *)
                # Reject a 3rd+ positional explicitly (last-wins would silently
                # drop it) so bash matches the Python argparse front (review A1).
                if [ -z "$opp_id" ]; then opp_id="$1"
                elif [ -z "$work_item" ]; then work_item="$1"
                else
                    echo "Error: 余分な引数 '$1' — anchor は <opp-id> <work-item-id> の 2 つだけを取ります。" >&2
                    echo "       $_usage" >&2
                    exit 1
                fi
                shift ;;
        esac
    done
    if [ -z "$opp_id" ] || [ -z "$work_item" ]; then
        echo "$_usage"
        exit 1
    fi
    BEACON_OPP_ID="$opp_id" BEACON_WORK_ITEM_ID="$work_item" \
        python3 "$COMMANDS_PY" opportunity_anchor
}

cmd_opportunity_judge() {
    ensure_project
    local opp_id="" decision="" arg="" note=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --note) note="${2:-}"; shift 2 ;;
            -?*)    _guard_positional "$1" "Usage: beacon opportunity judge <opp-id> advance|retry|terminal [<date|terminal-phase>] [--note <text>]" ;;
            *)
                if [ -z "$opp_id" ]; then opp_id="$1"
                elif [ -z "$decision" ]; then decision="$1"
                else arg="$1"; fi
                shift ;;
        esac
    done
    if [ -z "$opp_id" ] || [ -z "$decision" ]; then
        echo "Usage: beacon opportunity judge <opp-id> advance|retry|terminal [<date|terminal-phase>] [--note <text>]"
        echo "  advance  [<next YYYY-MM-DD>]   ゴール達成 → 次フェーズへ"
        echo "  retry    <new YYYY-MM-DD>      未達だが継続 → 遷移日を置き直す"
        echo "  terminal <terminal-phase>      決着 (allowed_terminals から選ぶ)"
        exit 1
    fi
    BEACON_OPP_ID="$opp_id" BEACON_JUDGE_DECISION="$decision" \
        BEACON_JUDGE_ARG="$arg" BEACON_PHASE_NOTE="$note" \
        python3 "$COMMANDS_PY" opportunity_judge
}

cmd_opportunity_due() {
    ensure_project
    local json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*) _guard_flag "$1" ;;
            *)      shift ;;
        esac
    done
    BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" opportunity_due
}

cmd_opportunity_activity() {
    ensure_project
    # ms-139 e-4950: 活動のライフサイクル動詞。先頭が done/cancel/update なら sub-verb、
    # そうでなければ従来の add 形 (<opp-id> <desc>)。act-id は全 opp 横断で一意なので
    # done/cancel/update は <act-id> だけで引ける。
    case "${1:-}" in
        done)    shift; cmd_opportunity_activity_done "$@"; return ;;
        cancel)  shift; cmd_opportunity_activity_cancel "$@"; return ;;
        update)  shift; cmd_opportunity_activity_update "$@"; return ;;
    esac
    local opp_id="" desc="" deadline="" ball=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --deadline) deadline="${2:-}"; shift 2 ;;
            --ball)     ball="${2:-}"; shift 2 ;;
            -?*)        _guard_positional "$1" "Usage: beacon opportunity activity <opp-id> <desc> [--deadline <date>] [--ball self|counterpart]  (also: done|cancel|update <act-id>)" ;;
            *)
                if [ -z "$opp_id" ]; then opp_id="$1"; else desc="$1"; fi
                shift ;;
        esac
    done
    if [ -z "$opp_id" ] || [ -z "$desc" ]; then
        echo "Usage: beacon opportunity activity <opp-id> <desc> [--deadline <date>] [--ball self|counterpart]"
        echo "       beacon opportunity activity done   <act-id>"
        echo "       beacon opportunity activity cancel <act-id> [--reason <text>]"
        echo "       beacon opportunity activity update <act-id> [--deadline <date>] [--ball self|counterpart] [--description <text>]"
        exit 1
    fi
    BEACON_OPP_ID="$opp_id" BEACON_ACTIVITY_DESC="$desc" \
        BEACON_ACTIVITY_DEADLINE="$deadline" BEACON_ACTIVITY_BALL="$ball" \
        python3 "$COMMANDS_PY" opportunity_activity
}

cmd_opportunity_activity_done() {
    local act_id="${1:-}"
    if [ -z "$act_id" ]; then
        echo "Usage: beacon opportunity activity done <act-id>"
        exit 1
    fi
    BEACON_ACT_ID="$act_id" BEACON_ACT_STATUS="done" \
        python3 "$COMMANDS_PY" activity_done
}

cmd_opportunity_activity_cancel() {
    local act_id="" reason=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reason) reason="${2:-}"; shift 2 ;;
            -?*)      _guard_positional "$1" "Usage: beacon opportunity activity cancel <act-id> [--reason <text>]" ;;
            *)        act_id="$1"; shift ;;
        esac
    done
    if [ -z "$act_id" ]; then
        echo "Usage: beacon opportunity activity cancel <act-id> [--reason <text>]"
        exit 1
    fi
    BEACON_ACT_ID="$act_id" BEACON_REASON="$reason" \
        python3 "$COMMANDS_PY" activity_cancel
}

cmd_opportunity_activity_update() {
    local act_id="" deadline="" ball="" desc=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --deadline)           deadline="${2:-}"; shift 2 ;;
            --ball)               ball="${2:-}"; shift 2 ;;
            --description|--desc) desc="${2:-}"; shift 2 ;;
            -?*)                  _guard_positional "$1" "Usage: beacon opportunity activity update <act-id> [--deadline <date>] [--ball self|counterpart] [--description <text>]" ;;
            *)                    act_id="$1"; shift ;;
        esac
    done
    if [ -z "$act_id" ]; then
        echo "Usage: beacon opportunity activity update <act-id> [--deadline <date>] [--ball self|counterpart] [--description <text>]"
        exit 1
    fi
    BEACON_ACT_ID="$act_id" BEACON_ACTIVITY_DEADLINE="$deadline" \
        BEACON_ACTIVITY_BALL="$ball" BEACON_ACTIVITY_DESC="$desc" \
        python3 "$COMMANDS_PY" activity_update
}

cmd_opportunity_delete() {
    ensure_project
    local opp_id="" reason="" acknowledge=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reason) reason="$2"; shift 2 ;;
            --acknowledge) acknowledge="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon opportunity delete <opp-id> (--reason <text> | --acknowledge)" ;;
            *)   opp_id="$1"; shift ;;
        esac
    done
    if [ -z "$opp_id" ]; then
        echo "Usage: beacon opportunity delete <opp-id> (--reason <text> | --acknowledge)"
        exit 1
    fi
    _require_audit_reason "opportunity delete" "$reason" "$acknowledge"
    [ -z "$reason" ] && reason="$BEACON_ACK_SENTINEL"
    BEACON_OPP_ID="$opp_id" BEACON_CANCEL_REASON="$reason" \
        python3 "$COMMANDS_PY" opportunity_delete
}
