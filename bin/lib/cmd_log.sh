# shellcheck shell=bash
# beacon CLI — log family (cmd_log)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_flag
# requires-var: COMMANDS_PY BEACON_INVOCATION_CWD
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

# The single definition of "is this path Beacon's own runtime state?" (ms-160
# e-7406). Two decisions in this file need it and must not drift apart:
#
#   * the dirty-tree gate  — may `beacon log` run at all?
#   * the beacon-only skip — is this commit worth recording?
#
# They used to carry separate copies spelled differently (`^\.beacon/` here,
# `^\.(beacon)/` there). The spellings were equivalent as regexes but not as
# text, so `grep -n '\.beacon/' cmd_log.sh` did not find the second one — which
# is exactly how a later change updates one site and silently leaves the other.
# ``beacon_cli/dispatch.py`` holds the third copy for the Python frontend;
# tests/test_log_gate_beacon_churn_e7406.py runs both against one corpus.
_BEACON_CHURN_PATH_RE='^\.beacon/'

# Read paths on stdin, emit only the ones the USER is responsible for.
# Blank lines are dropped so an empty input does not read as one dirty path.
_non_beacon_paths() {
    grep -v '^$' | grep -vE "$_BEACON_CHURN_PATH_RE" || true
}

# `git diff` quotes and octal-escapes non-ASCII paths by default
# (core.quotePath=true), which turns `.beacon/documents/日本語.md` into
# `".beacon/documents/\343\203..."` — a string starting with a quote character,
# so the prefix test above misses it and Beacon's own file is blamed on the
# user. profile-extractor (the repo that reported this bug) tracks 15 such paths
# under `.beacon/`, so the symptom would have come straight back through that
# door. Every git call whose output feeds the predicate goes through here.
_git_paths() {
    git -C "$_git_cwd" -c core.quotepath=false "$@" 2>/dev/null
}

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

    # Block if the USER left uncommitted changes (staged or unstaged).
    # --prepare is read-only, so skip this check.
    #
    # `.beacon/` is excluded on purpose. Beacon itself rewrites files there while
    # it runs (the PreToolUse hook writes .beacon/tool-unregistered.json on every
    # tool call; the local mirror .beacon/project.json is rewritten after each
    # record), so in a repo that TRACKS those files the tree is dirty in the gap
    # between two tool calls and `beacon log` can never run. Reported from a
    # profile-extractor session on 2026-11-10: it had to fold the ledger intake
    # and finalize into one command to get past this.
    #
    # The rule is not new here. Twenty lines below, the beacon-only commit skip
    # already decides "a change under .beacon/ is Beacon's own, not the user's"
    # (same `^\.beacon/` test). That idea simply had not been applied on the
    # dirty-tree side, so one of the two sides was closed and the other open.
    #
    # Whether a repo is affected depends on whether it tracks those files, which
    # is why this looked repo-specific rather than structural. Per-repo .gitignore
    # entries cannot converge on it either: the next state file Beacon starts
    # writing breaks the gate again in whichever repo tracked it. The survey of
    # which repos were affected is evidence that does not live in this repo, so it
    # is recorded in task e-7406 rather than asserted here where no reader can
    # check it.
    #
    # A git failure must still block. The old `! git diff --quiet HEAD` form
    # treated a non-zero git exit as dirty, so a broken/absent repo stopped the
    # command. Listing names instead makes "no output" indistinguishable from a
    # clean tree, so the exit status is captured separately and the gate stays
    # fail-closed. The capture and the filtering are two statements on purpose:
    # piping would replace the status with grep's.
    if [ "$mode" != "prepare" ]; then
        local changed_all rc user_dirty
        changed_all=$(_git_paths diff --name-only HEAD)
        rc=$?
        if [ "$rc" -ne 0 ]; then
            echo "Error: could not inspect the working tree (git diff failed)."
            echo "Run beacon log from inside a git repository."
            exit 1
        fi
        user_dirty=$(printf '%s\n' "$changed_all" | _non_beacon_paths)
        if [ -n "$user_dirty" ]; then
            echo "Error: uncommitted changes detected."
            echo "Commit your changes first, then run beacon log."
            echo "  (To record a note without a commit, use: beacon task add \"description\")"
            echo "  Uncommitted:"
            echo "$user_dirty" | sed 's/^/    /'
            exit 1
        fi
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
        changed_files=$(_git_paths diff-tree --no-commit-id --name-only -r "$target_ref")
        local non_beacon
        non_beacon=$(printf '%s\n' "$changed_files" | _non_beacon_paths)
        if [ -n "$changed_files" ] && [ -z "$non_beacon" ]; then
            echo "Skipped: commit $hash only changes .beacon/ (avoiding infinite loop)"
            exit 0
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
