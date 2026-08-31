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
            # Reject unknown flags AND stray positionals (ms-155 e-5666 AX): a
            # silent `*) shift` would drop a bad token on bash but argparse
            # errors on it — a cross-frontend parity break. Fail the same way.
            *) _guard_flag "$1" ;;
        esac
    done

    BEACON_RESOLVE="$resolve" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" deliverable_list
}

# ms-161 e-5902/e-5903: changelog curation surface + derived-map render. These
# forward their flags verbatim to the Python cmd (argparse over sys.argv[2:]),
# unlike cmd_deliverable_list's env-var layout — the write verbs take repeatable
# --tag and positional ids that argparse parses directly.
cmd_deliverable_add() {
    ensure_project
    python3 "$COMMANDS_PY" deliverable_add "$@"
}

cmd_deliverable_retire() {
    ensure_project
    python3 "$COMMANDS_PY" deliverable_retire "$@"
}

cmd_deliverable_supersede() {
    ensure_project
    python3 "$COMMANDS_PY" deliverable_supersede "$@"
}

cmd_deliverable_map() {
    ensure_project
    python3 "$COMMANDS_PY" deliverable_map "$@"
}
