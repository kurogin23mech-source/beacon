# shellcheck shell=bash
# beacon CLI — sales family (1 function)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_positional
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_sales_target() {
    ensure_project
    # `beacon sales target list [--json]` vs `beacon sales target <user> <amount>`
    if [ "${1:-}" = "list" ]; then
        shift
        local json_flag=""
        while [[ $# -gt 0 ]]; do
            case "$1" in --json) json_flag="1"; shift ;; *) shift ;; esac
        done
        BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" sales_target_list
        return
    fi
    local member="" amount=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon sales target <user> <amount> | list" ;;
            *)   if [ -z "$member" ]; then member="$1"; else amount="$1"; fi; shift ;;
        esac
    done
    if [ -z "$member" ]; then
        echo "Usage: beacon sales target <user> <amount> | list"
        exit 1
    fi
    BEACON_TARGET_MEMBER="$member" BEACON_TARGET_AMOUNT="$amount" \
        python3 "$COMMANDS_PY" sales_target
}
