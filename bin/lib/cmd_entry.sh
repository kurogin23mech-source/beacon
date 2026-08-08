# shellcheck shell=bash
# beacon CLI — entry family (cmd_entry_purge / cmd_entry_move)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_flag
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_entry_purge() {
    # e-863: hard-delete recovery for duplicate entry-ID corruption.
    # Like cmd_milestone_purge, intentionally does NOT call ensure_project.
    local entry_id="" reason="" index="" json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)      json_flag="1";    shift   ;;
            -r|--reason) reason="${2:-}";  shift 2 ;;
            --index)     index="${2:-}";   shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)           entry_id="$1";    shift   ;;
        esac
    done
    if [ -z "$entry_id" ] || [ -z "$reason" ]; then
        echo "Usage: beacon entry purge <e-id> --reason <text> [--index <n>] [--json]"
        echo
        echo "Hard-deletes an entry record (recovery for duplicate-ID corruption; e-863)."
        echo "Use --index <n> when the same e-id appears more than once."
        exit 1
    fi
    BEACON_ENTRY_ID="$entry_id" BEACON_REASON="$reason" \
        BEACON_INDEX="$index" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" entry_purge
}

cmd_entry_move() {
    ensure_project
    local entry_id=""
    local task_id=""
    local ms_id=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -t|--task)
                task_id="${2:-}"
                shift 2
                ;;
            -m|--ms)
                ms_id="${2:-}"
                shift 2
                ;;
            *)
                entry_id="$1"
                shift
                ;;
        esac
    done

    BEACON_ENTRY_ID="$entry_id" BEACON_TASK_ID="$task_id" BEACON_MS_ID="$ms_id" \
        python3 "$COMMANDS_PY" entry_move
}
