# shellcheck shell=bash
# beacon CLI — save family (1 functions)
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

cmd_save() {
    ensure_project
    local ms_id="" description="" source="" url="" revision_id="" progress="" json_flag="" hash=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -m|--ms)       ms_id="${2:-}"; shift 2 ;;
            --source)      source="${2:-}"; shift 2 ;;
            --url)         url="${2:-}"; shift 2 ;;
            --revision-id) revision_id="${2:-}"; shift 2 ;;
            --hash)        hash="${2:-}"; shift 2 ;;
            -p|--progress) progress="${2:-}"; shift 2 ;;
            --json)        json_flag="1"; shift ;;
            -?*)           _guard_positional "$1" "Usage: beacon save \"<description>\" [-m <ms-id>]" ;;
            *)             description="$1"; shift ;;
        esac
    done

    if [ -z "$source" ]; then
        source="manual"
    fi

    local date_str
    date_str=$(date +%Y-%m-%d)

    BEACON_DESCRIPTION="$description" BEACON_MS_ID="$ms_id" \
        BEACON_SOURCE="$source" BEACON_URL="$url" \
        BEACON_REVISION_ID="$revision_id" BEACON_PROGRESS="$progress" \
        BEACON_HASH="$hash" \
        BEACON_DATE="$date_str" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" save
}
