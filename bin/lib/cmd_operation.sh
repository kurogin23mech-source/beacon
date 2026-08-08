# shellcheck shell=bash
# beacon CLI — operation family (1 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: _guard_flag
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_operation_purge() {
    # e-863: hard-delete recovery for duplicate operation-ID corruption.
    local op_id="" reason="" index="" json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)      json_flag="1";    shift   ;;
            -r|--reason) reason="${2:-}";  shift 2 ;;
            --index)     index="${2:-}";   shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)           op_id="$1";       shift   ;;
        esac
    done
    if [ -z "$op_id" ] || [ -z "$reason" ]; then
        echo "Usage: beacon operation purge <op-id> --reason <text> [--index <n>] [--json]"
        echo
        echo "Hard-deletes an operation record (recovery for duplicate-ID corruption; e-863)."
        echo "Use --index <n> when the same op-id appears more than once."
        exit 1
    fi
    BEACON_OP_ID="$op_id" BEACON_REASON="$reason" \
        BEACON_INDEX="$index" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" operation_purge
}
