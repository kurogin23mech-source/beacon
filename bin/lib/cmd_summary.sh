# shellcheck shell=bash
# beacon CLI — summary family (1 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_summary() {
    ensure_project
    local text=""
    local json_flag=""

    # ms-120 / e-3907 note: `summary` was flagged as a noun-mutate to verbify,
    # but it is already a LOUD-deprecated no-op (ms-57 / e-1040 retired the
    # write; the command prints "IGNORED" and changes nothing). Since it no
    # longer mutates, there is nothing to verbify — no `summary set` alias is
    # added. The deprecation message below is the AX-correct surface (it names
    # the replacement paths). Left as-is intentionally.

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)
                json_flag="1"
                shift
                ;;
            *)
                if [ -z "$text" ]; then
                    text="$1"
                else
                    text="$text $1"
                fi
                shift
                ;;
        esac
    done

    BEACON_SUMMARY_TEXT="$text" BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" summary
}
