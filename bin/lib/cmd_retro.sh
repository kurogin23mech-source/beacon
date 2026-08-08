# shellcheck shell=bash
# beacon CLI — retro family (cmd_retro)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_flag
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). This machine-readable
#   seam names cross-file deps so a context-zero reader need not read all
#   of bin/beacon.

cmd_retro() {
    ensure_project
    local since="" until="" catch_up=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --since) since="${2:-}"; shift 2 ;;
            --until) until="${2:-}"; shift 2 ;;
            --prepare) shift ;;
            --catch-up) catch_up="1"; shift ;;
            -?*) _guard_flag "$1" ;;
            *) shift ;;
        esac
    done

    # ms-43 e-570: when --since is omitted, prefer "day after last reviewed week"
    # over the naive "this Monday". If the user delayed retro from Friday to
    # Tuesday next week, "this Monday" would cover only 2 days and miss the
    # entire previous week. The Python helper looks at `.beacon/retro/.reviewed`
    # and falls back to ISO-week boundaries when no marker exists.
    if [ -z "$since" ]; then
        since=$(BEACON_DEFAULT_SINCE_QUERY=1 python3 "$COMMANDS_PY" retro_default_since 2>/dev/null)
    fi
    if [ -z "$until" ]; then
        local dow
        dow=$(date +%u)
        if [[ "$dow" -eq 1 ]]; then
            until=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d "yesterday" +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d)
        else
            until=$(date +%Y-%m-%d)
        fi
    fi

    # Fallback if the helper didn't yield a value (older install, py error, etc.)
    if [ -z "$since" ]; then
        local dow
        dow=$(date +%u)
        if [[ "$(uname)" == "Darwin" ]]; then
            if [[ "$dow" -eq 1 ]]; then
                since=$(date -v-7d +%Y-%m-%d)
            else
                since=$(date -v-"$((dow - 1))"d +%Y-%m-%d)
            fi
        else
            if [[ "$dow" -eq 1 ]]; then
                since=$(date -d "7 days ago" +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d)
            else
                since=$(date -d "last monday" +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d)
            fi
        fi
    fi

    BEACON_SINCE="$since" BEACON_UNTIL="$until" BEACON_RETRO_CATCH_UP="$catch_up" \
        python3 "$COMMANDS_PY" retro_prepare
}
