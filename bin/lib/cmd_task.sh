# shellcheck shell=bash
# beacon CLI — task family (cmd_task_*)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly (`bash bin/lib/cmd_task.sh` does
# nothing useful; it only defines functions). bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
#
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_flag _guard_positional
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_task_add() {
    ensure_project
    local ms_id=""
    local entry_type="task"
    local description=""
    local detail=""
    local requested_by=""
    local priority=""
    local motivation=""
    local acceptance_criteria=""
    local allow_untriaged=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -m|--ms)                    ms_id="${2:-}";                shift 2 ;;
            -t|--type)                  entry_type="${2:-}";           shift 2 ;;
            -d|--detail)                detail="${2:-}";               shift 2 ;;
            --from)                     requested_by="${2:-}";         shift 2 ;;
            --priority)                 priority="${2:-}";             shift 2 ;;
            --untriaged)                allow_untriaged="1";           shift   ;;
            --motivation|--why)         motivation="${2:-}";           shift 2 ;;
            --acceptance-criteria|--ac) acceptance_criteria="${2:-}";  shift 2 ;;
            -?*)                        _guard_positional "$1" "Usage: beacon task add \"<description>\" [-m <ms-id>] [-t <type>] [-d <detail>] --priority P [--untriaged] [--motivation W] [--ac A]" ;;
            *)                          description="$1";              shift   ;;
        esac
    done

    local date_str
    date_str=$(date +%Y-%m-%d)

    BEACON_DESCRIPTION="$description" BEACON_MS_ID="$ms_id" \
        BEACON_TYPE="$entry_type" BEACON_DATE="$date_str" \
        BEACON_DETAIL="$detail" BEACON_REQUESTED_BY="$requested_by" \
        BEACON_PRIORITY="$priority" BEACON_MOTIVATION="$motivation" \
        BEACON_ALLOW_UNTRIAGED="$allow_untriaged" \
        BEACON_ACCEPTANCE_CRITERIA="$acceptance_criteria" \
        python3 "$COMMANDS_PY" task_add
}

cmd_task_show() {
    ensure_project
    local entry_id=""
    local json_flag=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)      entry_id="$1"; shift ;;
        esac
    done

    if [ -z "$entry_id" ]; then
        echo "Usage: beacon task show <entry-id> [--json]"
        exit 1
    fi
    BEACON_ENTRY_ID="$entry_id" BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" task_show
}

cmd_task_update() {
    ensure_project
    local entry_id=""
    local json_flag=""
    local description=""
    local status=""
    local detail=""
    local ms_id=""
    local motivation=""
    local acceptance_criteria=""
    local behavior=""
    local priority=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)                     json_flag="1"; shift ;;
            --description)              description="${2:-}"; shift 2 ;;
            --status)                   status="${2:-}"; shift 2 ;;
            -d|--detail)                detail="${2:-}"; shift 2 ;;
            -m|--ms)                    ms_id="${2:-}"; shift 2 ;;
            --motivation|--why)         motivation="${2:-}"; shift 2 ;;
            --acceptance-criteria|--ac) acceptance_criteria="${2:-}"; shift 2 ;;
            --behavior)                 behavior="${2:-}"; shift 2 ;;
            --priority|-p)              priority="${2:-}"; shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)                          entry_id="$1"; shift ;;
        esac
    done

    if [ -z "$entry_id" ]; then
        echo "Usage: beacon task update <entry-id> [--description D] [--status S] [--detail D] [--ms MS-ID]"
        echo "                                     [--motivation TEXT] [--acceptance-criteria TEXT]"
        echo "                                     [--behavior TEXT] [--priority highest|high|medium|low|lowest]"
        echo "                                     [--json]"
        exit 1
    fi
    BEACON_ENTRY_ID="$entry_id" BEACON_JSON="$json_flag" \
        BEACON_DESCRIPTION="$description" BEACON_STATUS="$status" \
        BEACON_DETAIL="$detail" BEACON_MS_ID="$ms_id" \
        BEACON_MOTIVATION="$motivation" \
        BEACON_ACCEPTANCE_CRITERIA="$acceptance_criteria" \
        BEACON_BEHAVIOR="$behavior" \
        BEACON_PRIORITY="$priority" \
        python3 "$COMMANDS_PY" task_update
}

cmd_task_delete() {
    ensure_project
    local entry_id=""
    local json_flag=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)      entry_id="$1"; shift ;;
        esac
    done

    if [ -z "$entry_id" ]; then
        echo "Usage: beacon task delete <entry-id> [--json]"
        exit 1
    fi
    BEACON_ENTRY_ID="$entry_id" BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" task_delete
}

cmd_task_detail() {
    ensure_project
    local entry_id="${1:-}"
    local detail="${2:-}"
    if [ -z "$entry_id" ]; then
        echo "Usage: beacon task detail <entry-id> [detail-text]"
        exit 1
    fi
    BEACON_ENTRY_ID="$entry_id" BEACON_DETAIL="$detail" \
        python3 "$COMMANDS_PY" task_detail
}

cmd_task_done() {
    ensure_project
    local entry_id=""
    local progress=""
    local reason=""
    local reason_set=0
    local acknowledge=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p|--progress) progress="${2:-}"; shift 2 ;;
            -r|--reason)   reason="${2:-}";   reason_set=1; shift 2 ;;
            --acknowledge) acknowledge="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)             entry_id="$1";     shift   ;;
        esac
    done

    if [ -z "$entry_id" ]; then
        echo "Usage: beacon task done <entry-id> --reason <text> [-p <progress>]"
        exit 1
    fi
    # e-976: forward BEACON_REASON only when --reason was passed so the python
    # gate can distinguish "operator omitted the flag" (refuse) from
    # "operator passed --reason ''" (explicit waiver, accepted but discouraged).
    if [ "$reason_set" = "1" ]; then
        BEACON_ENTRY_ID="$entry_id" BEACON_PROGRESS="$progress" BEACON_REASON="$reason" \
            BEACON_ACKNOWLEDGE="$acknowledge" python3 "$COMMANDS_PY" task_done
    else
        BEACON_ENTRY_ID="$entry_id" BEACON_PROGRESS="$progress" \
            BEACON_ACKNOWLEDGE="$acknowledge" python3 "$COMMANDS_PY" task_done
    fi
}

cmd_task_cancel() {
    ensure_project
    local entry_id=""
    local reason=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -r|--reason) reason="${2:-}"; shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)           entry_id="$1";  shift   ;;
        esac
    done

    if [ -z "$entry_id" ]; then
        echo "Usage: beacon task cancel <entry-id> [--reason <text>]"
        exit 1
    fi
    BEACON_ENTRY_ID="$entry_id" BEACON_REASON="$reason" \
        python3 "$COMMANDS_PY" task_cancel
}

cmd_task_list() {
    ensure_project
    local ms_id=""
    local json_flag=""
    local all_flag=""
    local type_filter=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -m|--ms)
                ms_id="${2:-}"
                shift 2
                ;;
            --json)
                json_flag="1"
                shift
                ;;
            --all|-a)
                all_flag="1"
                shift
                ;;
            --type|-t)
                type_filter="${2:-}"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    BEACON_MS_ID="$ms_id" BEACON_JSON="$json_flag" BEACON_ALL="$all_flag" \
        BEACON_TYPE_FILTER="$type_filter" python3 "$COMMANDS_PY" task_list
}
