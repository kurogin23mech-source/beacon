# shellcheck shell=bash
# beacon CLI — scenario family (ms-136 e-4699: 実ユースケース自動デバッグ基盤)
# sourced by bin/beacon (noun-family module split, ms-127 e-4867).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
#
# requires-fn: ensure_project _guard_positional
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced.

cmd_scenario_run() {
    ensure_project
    local path="" json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*)    _guard_positional "$1" "Usage: beacon scenario run <file> [--json]" ;;
            *)      path="$1"; shift ;;
        esac
    done
    if [ -z "$path" ]; then
        echo "Usage: beacon scenario run <file> [--json]"
        exit 1
    fi
    BEACON_SCENARIO_PATH="$path" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" scenario_run
}

cmd_scenario_save() {
    ensure_project
    local path="" json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*)    _guard_positional "$1" "Usage: beacon scenario save <file.json> [--json]" ;;
            *)      path="$1"; shift ;;
        esac
    done
    if [ -z "$path" ]; then
        echo "Usage: beacon scenario save <file.json> [--json]"
        exit 1
    fi
    BEACON_SCENARIO_PATH="$path" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" scenario_save
}

cmd_scenario_list() {
    ensure_project
    # --ms / -m: -m matches the repo-wide milestone-filter convention (AX review
    # finding #3). Unexpected positionals ERROR (not silently discarded) so a
    # misuse like `scenario list ms-136` (expecting positional filter) is caught
    # instead of returning an unfiltered list + exit 0 (AX review finding #6).
    local ms="" json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ms|-m) ms="${2:-}"; shift 2 ;;
            --json)  json_flag="1"; shift ;;
            *)       echo "Error: unexpected argument '$1' (did you mean --ms <ms-id>?)"; \
                     echo "Usage: beacon scenario list [--ms <ms-id>] [--json]"; exit 1 ;;
        esac
    done
    BEACON_SCENARIO_MS="$ms" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" scenario_list
}

cmd_scenario_replay() {
    ensure_project
    local ms="" json_flag="" attainment=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ms|-m)      ms="${2:-}"; shift 2 ;;
            --attainment) attainment="1"; shift ;;
            --json)       json_flag="1"; shift ;;
            *)            echo "Error: unexpected argument '$1' (did you mean --ms <ms-id>?)"; \
                          echo "Usage: beacon scenario replay [--ms <ms-id>] [--attainment (=evidence only, exit 0)] [--json]"; exit 1 ;;
        esac
    done
    BEACON_SCENARIO_MS="$ms" BEACON_SCENARIO_ATTAINMENT="$attainment" \
        BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" scenario_replay
}
