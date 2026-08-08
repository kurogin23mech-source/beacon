# shellcheck shell=bash
# beacon CLI — status family (1 functions)
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

cmd_status() {
    ensure_project
    local json_flag=""
    local all_flag=""
    local ms_filter=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            --all|-a) all_flag="1"; shift ;;
            -m|--ms)
                if [ -n "$ms_filter" ]; then
                    ms_filter="$ms_filter,${2:-}"
                else
                    ms_filter="${2:-}"
                fi
                shift 2 ;;
            -?*) _guard_flag "$1" ;;
            *) shift ;;
        esac
    done
    BEACON_JSON="$json_flag" BEACON_ALL="$all_flag" BEACON_MS_FILTER="$ms_filter" \
        python3 "$COMMANDS_PY" milestone_list
}
