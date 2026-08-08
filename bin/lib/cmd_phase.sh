# shellcheck shell=bash
# beacon CLI — phase family (5 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_flag _guard_positional
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_phase_list() {
    ensure_project
    local json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*) _guard_flag "$1" ;;
            *)      shift ;;
        esac
    done
    BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" phase_list
}

# ms-116 — edit a running project's saved phase funnel.
# <funnel> = account | opportunity. The stage-name arg may contain spaces, so
# positionals are consumed in order after the funnel selector.
cmd_phase_add() {
    ensure_project
    local funnel="" name="" index=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --index|--at) index="${2:-}"; shift 2 ;;
            -?*) _guard_positional "$1" "Usage: beacon phase add <account|opportunity|prospect> <name> [--index N]" ;;
            *)   if [ -z "$funnel" ]; then funnel="$1"; else name="$1"; fi; shift ;;
        esac
    done
    if [ -z "$funnel" ] || [ -z "$name" ]; then
        echo "Usage: beacon phase add <account|opportunity|prospect> <name> [--index N]"
        exit 1
    fi
    BEACON_FUNNEL_KIND="$funnel" BEACON_PHASE_NAME="$name" BEACON_PHASE_INDEX="$index" \
        python3 "$COMMANDS_PY" phase_add
}

cmd_phase_rename() {
    ensure_project
    local funnel="" old="" new=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon phase rename <account|opportunity|prospect> <old> <new>" ;;
            *)   if [ -z "$funnel" ]; then funnel="$1";
                 elif [ -z "$old" ]; then old="$1"; else new="$1"; fi; shift ;;
        esac
    done
    if [ -z "$funnel" ] || [ -z "$old" ] || [ -z "$new" ]; then
        echo "Usage: beacon phase rename <account|opportunity|prospect> <old> <new>"
        exit 1
    fi
    BEACON_FUNNEL_KIND="$funnel" BEACON_PHASE_OLD="$old" BEACON_PHASE_NEW="$new" \
        python3 "$COMMANDS_PY" phase_rename
}

cmd_phase_move() {
    ensure_project
    local funnel="" name="" index=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon phase move <account|opportunity|prospect> <name> <index>" ;;
            *)   if [ -z "$funnel" ]; then funnel="$1";
                 elif [ -z "$name" ]; then name="$1"; else index="$1"; fi; shift ;;
        esac
    done
    if [ -z "$funnel" ] || [ -z "$name" ] || [ -z "$index" ]; then
        echo "Usage: beacon phase move <account|opportunity|prospect> <name> <index>"
        exit 1
    fi
    BEACON_FUNNEL_KIND="$funnel" BEACON_PHASE_NAME="$name" BEACON_PHASE_INDEX="$index" \
        python3 "$COMMANDS_PY" phase_move
}

cmd_phase_remove() {
    ensure_project
    local funnel="" name=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon phase remove <account|opportunity|prospect> <name>" ;;
            *)   if [ -z "$funnel" ]; then funnel="$1"; else name="$1"; fi; shift ;;
        esac
    done
    if [ -z "$funnel" ] || [ -z "$name" ]; then
        echo "Usage: beacon phase remove <account|opportunity|prospect> <name>"
        exit 1
    fi
    BEACON_FUNNEL_KIND="$funnel" BEACON_PHASE_NAME="$name" \
        python3 "$COMMANDS_PY" phase_remove
}
