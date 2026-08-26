# shellcheck shell=bash
# beacon CLI — pr family (1 functions)
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

cmd_pr() {
    ensure_project
    case "${1:-}" in
        show)
            shift
            local ident=""
            local json_flag=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json) json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)      ident="$1"; shift ;;
                esac
            done
            if [ -z "$ident" ]; then
                echo "Usage: beacon pr show <entry-id|pr-number|url> [--json]"
                exit 1
            fi
            BEACON_PR_IDENT="$ident" BEACON_JSON="$json_flag" \
                python3 "$COMMANDS_PY" pr_show
            ;;
        add)
            shift
            local url=""
            local ms_id=""
            local intent=""
            local author=""
            local json_flag=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -m|--ms)       ms_id="${2:-}"; shift 2 ;;
                    --intent)      intent="${2:-}"; shift 2 ;;
                    --author)      author="${2:-}"; shift 2 ;;
                    --json)        json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)             url="$1"; shift ;;
                esac
            done

            if [ -z "$url" ]; then
                echo "Usage: beacon pr add <github-url> [-m <ms-id>] [--intent \"text\"] [--author user]"
                exit 1
            fi

            local date_str
            date_str=$(date +%Y-%m-%d)

            BEACON_URL="$url" BEACON_MS_ID="$ms_id" BEACON_INTENT="$intent" \
                BEACON_AUTHOR="$author" BEACON_DATE="$date_str" BEACON_JSON="$json_flag" \
                python3 "$COMMANDS_PY" pr_add
            ;;
        close)
            shift
            local entry_id=""
            local json_flag=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json) json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)      entry_id="$1"; shift ;;
                esac
            done

            if [ -z "$entry_id" ]; then
                echo "Usage: beacon pr close <entry-id> [--json]"
                exit 1
            fi
            BEACON_ENTRY_ID="$entry_id" BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" pr_close
            ;;
        approve)
            shift
            local entry_id=""
            local rationale=""
            local json_flag=""
            local no_auto_done_flag=""
            local evidence=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --rationale|--reason)    rationale="${2:-}"; shift 2 ;;
                    --evidence)     evidence="${2:-}"; shift 2 ;;
                    --json)         json_flag="1"; shift ;;
                    --no-auto-done) no_auto_done_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)              entry_id="$1"; shift ;;
                esac
            done

            if [ -z "$entry_id" ]; then
                echo "Usage: beacon pr approve <entry-id> [--rationale \"text\"] [--evidence \"link\"] [--no-auto-done] [--json]"
                exit 1
            fi
            BEACON_ENTRY_ID="$entry_id" BEACON_RATIONALE="$rationale" BEACON_EVIDENCE="$evidence" \
                BEACON_JSON="$json_flag" BEACON_NO_AUTO_DONE="$no_auto_done_flag" \
                python3 "$COMMANDS_PY" pr_approve
            ;;
        reject)
            shift
            local entry_id=""
            local rationale=""
            local json_flag=""
            local evidence=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --rationale|--reason) rationale="${2:-}"; shift 2 ;;
                    --evidence)  evidence="${2:-}"; shift 2 ;;
                    --json)      json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)           entry_id="$1"; shift ;;
                esac
            done

            if [ -z "$entry_id" ]; then
                echo "Usage: beacon pr reject <entry-id> [--rationale \"text\"] [--evidence \"link\"] [--json]"
                exit 1
            fi
            BEACON_ENTRY_ID="$entry_id" BEACON_RATIONALE="$rationale" BEACON_EVIDENCE="$evidence" \
                BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" pr_reject
            ;;
        create)
            shift
            local ms_id=""
            local intent=""
            local gh_args_json="[]"

            # Collect known beacon flags; pass remaining args to gh pr create
            local remaining=()
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -m|--ms)     ms_id="${2:-}"; shift 2 ;;
                    --intent)    intent="${2:-}"; shift 2 ;;
                    *)           remaining+=("$1"); shift ;;
                esac
            done

            # Pass argv as JSON, not a shell-quoted string. `printf %q` emits
            # bash-specific $'...' quoting for non-ASCII, and Python shlex
            # cannot round-trip that format (it corrupts Japanese PR titles
            # and prepends a stray `$`). A JSON array round-trips cleanly.
            if [ ${#remaining[@]} -gt 0 ]; then
                gh_args_json=$(
                    PYTHONUTF8=1 python3 -c 'import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' \
                        "${remaining[@]}"
                )
            fi

            BEACON_MS_ID="$ms_id" BEACON_INTENT="$intent" BEACON_GH_ARGS_JSON="$gh_args_json" \
                python3 "$COMMANDS_PY" pr_create
            ;;
        request-review)
            shift
            local entry_id=""
            local json_flag=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json) json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)      entry_id="$1"; shift ;;
                esac
            done

            if [ -z "$entry_id" ]; then
                echo "Usage: beacon pr request-review <entry-id> [--json]"
                exit 1
            fi
            BEACON_ENTRY_ID="$entry_id" BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" pr_request_review
            ;;
        request-changes)
            shift
            local entry_id=""
            local rationale=""
            local json_flag=""
            local evidence=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --rationale|--reason) rationale="${2:-}"; shift 2 ;;
                    --evidence)  evidence="${2:-}"; shift 2 ;;
                    --json)      json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)           entry_id="$1"; shift ;;
                esac
            done

            if [ -z "$entry_id" ]; then
                echo "Usage: beacon pr request-changes <entry-id> [--rationale \"text\"] [--evidence \"link\"] [--json]"
                exit 1
            fi
            BEACON_ENTRY_ID="$entry_id" BEACON_RATIONALE="$rationale" BEACON_EVIDENCE="$evidence" \
                BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" pr_request_changes
            ;;
        review)
            echo "beacon pr review is now handled by the /review Claude Code Skill."
            echo "Use: /review <PR-number>"
            ;;
        merge)
            shift
            local entry_id=""
            local json_flag=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json) json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)      entry_id="$1"; shift ;;
                esac
            done

            if [ -z "$entry_id" ]; then
                echo "Usage: beacon pr merge <entry-id> [--json]"
                exit 1
            fi
            BEACON_ENTRY_ID="$entry_id" BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" pr_merge
            ;;
        sync)
            # ms-61 / e-2005: align beacon PR entries with GitHub state.
            # GitHub MERGED → beacon pr merge; GitHub CLOSED → beacon pr close.
            shift
            local json_flag=""
            local dry_run_flag=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json)    json_flag="1"; shift ;;
                    --dry-run) dry_run_flag="1"; shift ;;
                    -?*) _guard_flag "$1" ;;
                    *)         shift ;;
                esac
            done

            BEACON_JSON="$json_flag" BEACON_DRY_RUN="$dry_run_flag" \
                python3 "$COMMANDS_PY" pr_sync
            ;;
        *)
            echo "Usage: beacon pr [create|add|show|review|approve|request-changes|reject|merge|close|sync]"
            echo "  create          [-m <ms-id>] [--intent \"text\"] [gh pr create flags...]"
            echo "                  Run gh pr create and auto-record the PR in beacon"
            echo "  add             <github-url> [-m <ms-id>] [--intent \"text\"]  Register a PR"
            echo "  show            <entry-id|pr-number|url> [--json]  Show PR detail (intent, commits, review history)"
            echo "  review          → use /review Claude Code Skill instead"
            echo "  approve         <entry-id> [--rationale \"text\"]  Approve (rationale required)"
            echo "  request-changes <entry-id> [--rationale \"text\"]  Request changes"
            echo "  reject          <entry-id> [--rationale \"text\"]  Reject a PR"
            echo "  merge           <entry-id>   Mark as merged"
            echo "  close           <entry-id>   Close without merging"
            echo "  sync            [--dry-run]  Align beacon PR entries with GitHub state (merged/closed)"
            ;;
    esac
}
