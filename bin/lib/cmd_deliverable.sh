# shellcheck shell=bash
# beacon CLI — deliverable family (cmd_deliverable)
# ms-155 e-5602: sourced by bin/beacon (noun-family god-module split).
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

cmd_deliverable_list() {
    ensure_project
    local resolve="" json=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --resolve) resolve="1"; shift ;;
            --json)    json="1"; shift ;;
            -?*) _guard_flag "$1" ;;
            *) shift ;;
        esac
    done

    BEACON_RESOLVE="$resolve" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" deliverable_list
}
