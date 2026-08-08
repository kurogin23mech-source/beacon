# shellcheck shell=bash
# beacon CLI — log family (cmd_log)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires: ensure_project _guard_flag COMMANDS_PY BEACON_INVOCATION_CWD
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). This machine-readable
#   seam names cross-file deps so a context-zero reader need not read all
#   of bin/beacon.

cmd_log() {
    ensure_project
    # e-1227 (ms-17): resolve git operations from the *original* invocation
    # cwd, not the post-relocate cwd. When invoked from a worktree where
    # `.beacon/` lives in the parent repo, the top-level relocate already
    # cd'd us into the parent. Reading HEAD without `-C` would then capture
    # the parent's HEAD instead of the worktree's, silently attaching the
    # wrong commit hash to every beacon entry.
    local _git_cwd="${BEACON_INVOCATION_CWD:-$PWD}"
    if ! git -C "$_git_cwd" rev-parse --is-inside-work-tree &>/dev/null; then
        echo "Error: Not a git repository"
        exit 1
    fi

    local ms_id=""
    local custom_summary=""
    local progress=""
    local json_flag=""
    local mode="normal"
    local explicit_hash=""
    local behavior=""
    local resolves=""
    # Distinguish "--resolves was passed" (even with an empty value = "bind to
    # nothing") from "--resolves omitted" (= fall back to fuzzy message match).
    # Without this the core matcher cannot tell an explicit opt-out from the
    # auto-fire path and always fuzzy-binds. See lib/core._find_matching_task.
    local resolves_set=""

    # Parse options
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -m|--ms)       ms_id="${2:-}";          shift 2 ;;
            -p|--progress) progress="${2:-}";        shift 2 ;;
            --json)        json_flag="1";            shift   ;;
            --prepare)     mode="prepare";           shift   ;;
            --finalize)    mode="finalize";          shift   ;;
            --summary)     custom_summary="${2:-}";  shift 2 ;;
            --hash)        explicit_hash="${2:-}";   shift 2 ;;
            --behavior)    behavior="${2:-}";        shift 2 ;;
            --resolves)    resolves="${2:-}"; resolves_set="1"; shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)             custom_summary="$1";      shift   ;;
        esac
    done

    # Block if there are uncommitted changes (staged or unstaged)
    # --prepare is read-only, so skip this check
    if [ "$mode" != "prepare" ] && ! git -C "$_git_cwd" diff --quiet HEAD 2>/dev/null; then
        echo "Error: uncommitted changes detected."
        echo "Commit your changes first, then run beacon log."
        echo "  (To record a note without a commit, use: beacon task add \"description\")"
        exit 1
    fi

    local hash message date_str target_ref
    if [ -n "$explicit_hash" ]; then
        target_ref="$explicit_hash"
    else
        target_ref="HEAD"
    fi
    hash=$(git -C "$_git_cwd" rev-parse --short "$target_ref")
    message=$(git -C "$_git_cwd" log -1 --pretty=%s "$target_ref")
    date_str=$(git -C "$_git_cwd" log -1 --pretty=%ci "$target_ref" | cut -d' ' -f1)

    # Skip beacon-only commits to prevent infinite loop:
    # beacon log → project.json change → commit → hook → beacon log → ...
    # Only skip in auto modes (normal/prepare). --finalize is explicitly called
    # by Skill, so it should always proceed.
    if [ "$mode" != "finalize" ]; then
        local changed_files
        changed_files=$(git -C "$_git_cwd" diff-tree --no-commit-id --name-only -r "$target_ref" 2>/dev/null)
        if echo "$changed_files" | grep -qE '^\.(beacon)/'; then
            local non_beacon
            non_beacon=$(echo "$changed_files" | grep -vE '^\.(beacon)/' || true)
            if [ -z "$non_beacon" ]; then
                echo "Skipped: commit $hash only changes .beacon/ (avoiding infinite loop)"
                exit 0
            fi
        fi
    fi

    case "$mode" in
        prepare)
            BEACON_HASH="$hash" BEACON_MESSAGE="$message" BEACON_DATE="$date_str" \
                BEACON_SUMMARY="$custom_summary" BEACON_MS_ID="$ms_id" \
                python3 "$COMMANDS_PY" log_prepare
            ;;
        finalize)
            BEACON_HASH="$hash" BEACON_MESSAGE="$message" BEACON_DATE="$date_str" \
                BEACON_SUMMARY="$custom_summary" BEACON_MS_ID="$ms_id" \
                BEACON_PROGRESS="$progress" \
                BEACON_NEW_SUMMARY="${BEACON_NEW_SUMMARY:-$custom_summary}" \
                BEACON_JSON="$json_flag" \
                BEACON_BEHAVIOR="$behavior" BEACON_RESOLVES="$resolves" \
                BEACON_RESOLVES_SET="$resolves_set" \
                python3 "$COMMANDS_PY" log_finalize
            ;;
        normal)
            BEACON_HASH="$hash" BEACON_MESSAGE="$message" BEACON_DATE="$date_str" \
                BEACON_SUMMARY="$custom_summary" BEACON_MS_ID="$ms_id" \
                BEACON_PROGRESS="$progress" BEACON_JSON="$json_flag" \
                BEACON_BEHAVIOR="$behavior" BEACON_RESOLVES="$resolves" \
                BEACON_RESOLVES_SET="$resolves_set" \
                python3 "$COMMANDS_PY" log
            ;;
    esac
}
