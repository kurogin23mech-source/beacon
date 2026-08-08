# shellcheck shell=bash
# beacon CLI — sync family (1 functions)
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

cmd_sync() {
    ensure_project
    if ! git rev-parse --is-inside-work-tree &>/dev/null; then
        echo "Error: Not a git repository"
        exit 1
    fi
    python3 "$COMMANDS_PY" sync
}
