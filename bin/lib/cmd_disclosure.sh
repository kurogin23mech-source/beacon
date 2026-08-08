# shellcheck shell=bash
# beacon CLI — disclosure family (2 functions)
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

# ms-113 e-3734: Account の cross-project 開示リンク管理。
# ms-113 generalization: 任意の Target を cross-project 開示する汎用動詞。
# 旧 `beacon account link/unlink` を id で任意 Target を引く形に持ち上げた。
cmd_disclose() {
    ensure_project
    local resource_id="" project=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --to-project|--project) project="${2:-}"; shift 2 ;;
            -?*) _guard_positional "$1" "Usage: beacon disclose <resource-id> --to-project <project-id>" ;;
            *)   if [ -z "$resource_id" ]; then resource_id="$1"; fi; shift ;;
        esac
    done
    if [ -z "$resource_id" ] || [ -z "$project" ]; then
        echo "Usage: beacon disclose <resource-id> --to-project <project-id>"
        echo "  その project のメンバーが、この Target (顧客/商談/MS 等) を参照できるようにする (cross-project 開示)。"
        exit 1
    fi
    BEACON_DISCLOSE_ID="$resource_id" BEACON_DISCLOSE_PROJECT="$project" \
        python3 "$COMMANDS_PY" disclose
}

cmd_undisclose() {
    ensure_project
    local resource_id="" project=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --from-project|--project) project="${2:-}"; shift 2 ;;
            -?*) _guard_positional "$1" "Usage: beacon undisclose <resource-id> --from-project <project-id>" ;;
            *)   if [ -z "$resource_id" ]; then resource_id="$1"; fi; shift ;;
        esac
    done
    if [ -z "$resource_id" ] || [ -z "$project" ]; then
        echo "Usage: beacon undisclose <resource-id> --from-project <project-id>"
        exit 1
    fi
    BEACON_DISCLOSE_ID="$resource_id" BEACON_DISCLOSE_PROJECT="$project" \
        python3 "$COMMANDS_PY" undisclose
}
