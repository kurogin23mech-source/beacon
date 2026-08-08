# shellcheck shell=bash
# beacon CLI — milestone family (17 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_flag _guard_positional
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_milestone_add() {
    ensure_project
    local title=""
    local target_date=""
    local description=""
    local priority=""
    local objective=""
    local acceptance_criteria=""
    local owner=""
    local assignee=""
    local allow_untriaged=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -d)                         target_date="${2:-}";          shift 2 ;;
            --description|--desc)       description="${2:-}";          shift 2 ;;
            --priority)                 priority="${2:-}";             shift 2 ;;
            --untriaged)                allow_untriaged="1";           shift   ;;
            --objective)                objective="${2:-}";            shift 2 ;;
            --acceptance-criteria|--ac) acceptance_criteria="${2:-}";  shift 2 ;;
            --owner)                    owner="${2:-}";                shift 2 ;;
            --assignee)                 assignee="${2:-}";             shift 2 ;;
            -?*)                        _guard_positional "$1" "Usage: beacon milestone add \"<title>\" [-d <date>] --priority P [--untriaged] [--objective O] [--ac A] [--owner U] [--assignee U]" ;;
            *)                          title="$1";                    shift   ;;
        esac
    done

    if [ -z "$title" ]; then
        echo "Add Milestone"
        echo "============="
        read -rp "Title: " title
        read -rp "Target date (YYYY-MM-DD): " target_date
        read -rp "Description: " description
        # ms-126: priority is mandatory. Loop until the user picks one of the 5
        # severities so the interactive path can never fall through empty.
        while [ -z "$priority" ]; do
            read -rp "Priority (highest/high/medium/low/lowest): " priority
        done
        read -rp "Objective (what becomes possible): " objective
        read -rp "Acceptance criteria (when is it done): " acceptance_criteria
    fi

    BEACON_TITLE="$title" BEACON_TARGET_DATE="$target_date" \
        BEACON_DESCRIPTION="$description" BEACON_PRIORITY="$priority" \
        BEACON_ALLOW_UNTRIAGED="$allow_untriaged" \
        BEACON_OBJECTIVE="$objective" BEACON_ACCEPTANCE_CRITERIA="$acceptance_criteria" \
        BEACON_OWNER="$owner" BEACON_ASSIGNEE="$assignee" \
        python3 "$COMMANDS_PY" milestone_add
}

cmd_milestone_list() {
    ensure_project
    python3 "$COMMANDS_PY" milestone_list
}

cmd_milestone_start() {
    ensure_project
    local ms_id=""
    local no_branch=""
    local no_assignee=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-branch)   no_branch="1"; shift ;;
            --no-assignee) no_assignee="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)             ms_id="$1"; shift ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone start <ms-id> [--no-branch] [--no-assignee]"
        exit 1
    fi
    BEACON_MS_ID="$ms_id" \
        BEACON_NO_BRANCH="$no_branch" \
        BEACON_NO_ASSIGNEE="$no_assignee" \
        python3 "$COMMANDS_PY" milestone_start
}

cmd_milestone_done() {
    ensure_project
    local ms_id=""
    local reason=""
    local reason_set=0
    local acknowledge=0
    local review=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -r|--reason) reason="${2:-}"; reason_set=1; shift 2 ;;
            --acknowledge) acknowledge="1"; shift ;;
            --review)    review=1;        shift ;;
            -?*)           _guard_flag "$1" ;;
            *)           ms_id="$1";     shift   ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone done <ms-id> --reason <text> [--review]"
        exit 1
    fi
    # e-976: forward BEACON_REASON only when --reason was passed. The python
    # gate (_require_reason_or_skip) refuses missing env, so the dispatcher
    # must not silently set an empty default.
    # ms-119 e-3912: --review routes the completion through the 目的達成レビュー
    # gate (human approval) instead of applying it directly.
    if [ "$reason_set" = "1" ]; then
        BEACON_MS_ID="$ms_id" BEACON_REASON="$reason" BEACON_ACKNOWLEDGE="$acknowledge" BEACON_REVIEW="$review" python3 "$COMMANDS_PY" milestone_done
    else
        BEACON_MS_ID="$ms_id" BEACON_ACKNOWLEDGE="$acknowledge" BEACON_REVIEW="$review" python3 "$COMMANDS_PY" milestone_done
    fi
}

cmd_milestone_join() {
    ensure_project
    local ms_id=""
    local checkout=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --checkout) checkout="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)          ms_id="$1";   shift ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon ms join <ms-id> [--checkout]"
        exit 1
    fi
    BEACON_MS_ID="$ms_id" BEACON_CHECKOUT="$checkout" \
        python3 "$COMMANDS_PY" milestone_join
}

cmd_milestone_observe() {
    ensure_project
    local ms_id=""
    local reason=""
    local reason_set=0
    local acknowledge=0
    local review=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -r|--reason) reason="${2:-}"; reason_set=1; shift 2 ;;
            --acknowledge) acknowledge="1"; shift ;;
            --review)    review=1;        shift ;;
            -?*)           _guard_flag "$1" ;;
            *)           ms_id="$1";     shift   ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone observe <ms-id> --reason <text> [--review]"
        exit 1
    fi
    # e-976: route observe to the dedicated milestone_observe handler so the
    # --reason gate applies. The handler still forwards to core.milestone_update
    # internally, so behavior other than the gate is identical.
    # ms-119: observing is a completion claim, so --review must be parsed and
    # forwarded here exactly like `milestone done` — otherwise the Python-side
    # BEACON_REVIEW gate is unreachable from the CLI and the error/SKILL docs
    # that advertise `observe --review` point at a dead flag.
    if [ "$reason_set" = "1" ]; then
        BEACON_MS_ID="$ms_id" BEACON_REASON="$reason" \
            BEACON_ACKNOWLEDGE="$acknowledge" BEACON_REVIEW="$review" \
            python3 "$COMMANDS_PY" milestone_observe
    else
        BEACON_MS_ID="$ms_id" \
            BEACON_ACKNOWLEDGE="$acknowledge" BEACON_REVIEW="$review" \
            python3 "$COMMANDS_PY" milestone_observe
    fi
}

# ms-81 e-1921: list the occupation event log (worktree_sessions).
cmd_milestone_occupations() {
    ensure_project
    local ms_id=""
    local json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -m|--ms)   ms_id="${2:-}"; shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)      ms_id="$1"; shift ;;
        esac
    done
    BEACON_MS_ID="$ms_id" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" milestone_occupations
}

# ms-81 e-1918: release the occupation claim on an MS without flipping
# status. Used when a session finishes working on an active MS so another
# session can pick it up immediately.
cmd_milestone_release() {
    ensure_project
    local ms_id="${1:-}"
    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone release <ms-id>"
        exit 1
    fi
    BEACON_MS_ID="$ms_id" python3 "$COMMANDS_PY" milestone_release
}

# ms-81 e-1915: wait transitions an active/observing MS to waiting.
# Mirror of cmd_milestone_observe — same --reason gate, same dispatch shape;
# core.milestone_wait enforces source-status validity.
cmd_milestone_wait() {
    ensure_project
    local ms_id=""
    local reason=""
    local reason_set=0
    local acknowledge=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -r|--reason) reason="${2:-}"; reason_set=1; shift 2 ;;
            --acknowledge) acknowledge="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)           ms_id="$1";     shift   ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone wait <ms-id> --reason <text>"
        exit 1
    fi
    if [ "$reason_set" = "1" ]; then
        BEACON_MS_ID="$ms_id" BEACON_REASON="$reason" \
            BEACON_ACKNOWLEDGE="$acknowledge" python3 "$COMMANDS_PY" milestone_wait
    else
        BEACON_MS_ID="$ms_id" \
            BEACON_ACKNOWLEDGE="$acknowledge" python3 "$COMMANDS_PY" milestone_wait
    fi
}

cmd_milestone_show() {
    ensure_project
    local ms_id=""
    local json_flag=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)      ms_id="$1"; shift ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone show <ms-id> [--json]"
        exit 1
    fi
    BEACON_MS_ID="$ms_id" BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" milestone_show
}

cmd_milestone_update() {
    ensure_project
    local ms_id=""
    local json_flag=""
    local title=""
    local progress=""
    local target_date=""
    local status=""
    local description=""
    local priority=""
    local objective=""
    local acceptance_criteria=""
    local owner=""
    local assignee=""
    local reason=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)         json_flag="1"; shift ;;
            --title)        title="${2:-}"; shift 2 ;;
            -p|--progress)  progress="${2:-}"; shift 2 ;;
            --target-date)  target_date="${2:-}"; shift 2 ;;
            --status)       status="${2:-}"; shift 2 ;;
            --description|--desc) description="${2:-}"; shift 2 ;;
            --priority)     priority="${2:-}"; shift 2 ;;
            --objective)    objective="${2:-}"; shift 2 ;;
            --acceptance-criteria|--ac) acceptance_criteria="${2:-}"; shift 2 ;;
            --owner)        owner="${2:-}"; shift 2 ;;
            --assignee)     assignee="${2:-}"; shift 2 ;;
            -r|--reason)    reason="${2:-}"; shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)              ms_id="$1"; shift ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone update <ms-id> [--title T] [--progress N] [--target-date D] [--status S] [--description D] [--priority P] [--objective O] [--ac A] [--owner OWNER] [--assignee ASSIGNEE] [-r REASON] [--json]"
        echo "  Tip: pass --owner '-' or --assignee '-' to clear the field."
        exit 1
    fi
    BEACON_MS_ID="$ms_id" BEACON_JSON="$json_flag" BEACON_TITLE="$title" \
        BEACON_PROGRESS="$progress" BEACON_TARGET_DATE="$target_date" \
        BEACON_STATUS="$status" BEACON_DESCRIPTION="$description" \
        BEACON_PRIORITY="$priority" BEACON_OBJECTIVE="$objective" \
        BEACON_ACCEPTANCE_CRITERIA="$acceptance_criteria" \
        BEACON_OWNER="$owner" BEACON_ASSIGNEE="$assignee" \
        BEACON_REASON="$reason" \
        python3 "$COMMANDS_PY" milestone_update
}

cmd_milestone_delete() {
    ensure_project
    local ms_id=""
    local reason=""
    local json_flag=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)      json_flag="1";    shift   ;;
            -r|--reason) reason="${2:-}";  shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)           ms_id="$1";       shift   ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone delete <ms-id> --reason <text> [--json]"
        exit 1
    fi
    BEACON_MS_ID="$ms_id" BEACON_REASON="$reason" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" milestone_delete
}

cmd_milestone_purge() {
    # Issue #14: hard-delete recovery path for duplicate-ID corruption.
    # Intentionally does NOT call ensure_project so it can run even when
    # the project fails validation (which is the whole point).
    local ms_id=""
    local reason=""
    local index=""
    local json_flag=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)      json_flag="1";    shift   ;;
            -r|--reason) reason="${2:-}";  shift 2 ;;
            --index)     index="${2:-}";   shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)           ms_id="$1";       shift   ;;
        esac
    done

    if [ -z "$ms_id" ] || [ -z "$reason" ]; then
        echo "Usage: beacon milestone purge <ms-id> --reason <text> [--index <n>] [--json]"
        echo
        echo "Hard-deletes a milestone record (recovery path for Issue #14)."
        echo "Soft delete (preserves history) is 'beacon milestone delete'."
        echo "Use --index <n> when the same ms-id appears more than once."
        exit 1
    fi
    BEACON_MS_ID="$ms_id" BEACON_REASON="$reason" \
        BEACON_INDEX="$index" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" milestone_purge
}

cmd_milestone_depends() {
    ensure_project
    local ms_id=""
    local depends_on=""
    local clear=""
    local json_flag=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --on)
                if [ -n "$depends_on" ]; then
                    depends_on="$depends_on,${2:-}"
                else
                    depends_on="${2:-}"
                fi
                shift 2 ;;
            --clear) clear="1"; shift ;;
            --json)  json_flag="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)       ms_id="$1"; shift ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone depends <ms-id> --on <dep-id> [--on <dep-id>] | --clear"
        exit 1
    fi
    BEACON_MS_ID="$ms_id" BEACON_DEPENDS_ON="$depends_on" \
        BEACON_CLEAR="$clear" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" milestone_depends
}

cmd_milestone_workspace() {
    # OSS: git worktree lifecycle core
    # Usage:
    #   beacon milestone workspace <ms-id> [--executor ai|human|human:<user>] [--json]
    #   beacon milestone workspace <ms-id> --dir <path> [--no-git] [--json]   (legacy)
    #   beacon milestone workspace <ms-id> --clear                             (legacy)
    ensure_project
    local ms_id=""
    local workspace=""
    local clear=""
    local json_flag=""
    local executor=""
    local no_git=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dir)      workspace="${2:-}"; shift 2 ;;
            --clear)    clear="1"; shift ;;
            --json)     json_flag="1"; shift ;;
            --executor) executor="${2:-}"; shift 2 ;;
            --no-git)   no_git="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)          ms_id="$1"; shift ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone workspace <ms-id> [--executor ai|human|human:<user>]"
        echo "       beacon milestone workspace <ms-id> --dir <path> [--no-git]"
        echo "       beacon milestone workspace <ms-id> --clear"
        exit 1
    fi
    BEACON_MS_ID="$ms_id" BEACON_WORKSPACE="$workspace" \
        BEACON_CLEAR="$clear" BEACON_JSON="$json_flag" \
        BEACON_EXECUTOR="$executor" BEACON_NO_GIT="$no_git" \
        python3 "$COMMANDS_PY" milestone_workspace
}

cmd_milestone_workspace_cleanup() {
    # OSS: git merge + git worktree remove lifecycle cleanup
    # Usage: beacon milestone workspace-cleanup <ms-id> [--merge-to <branch>] [--json]
    ensure_project
    local ms_id=""
    local merge_to=""
    local json_flag=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --merge-to) merge_to="${2:-}"; shift 2 ;;
            --json)     json_flag="1"; shift ;;
            -?*)           _guard_flag "$1" ;;
            *)          ms_id="$1"; shift ;;
        esac
    done

    if [ -z "$ms_id" ]; then
        echo "Usage: beacon milestone workspace-cleanup <ms-id> [--merge-to <branch>]"
        exit 1
    fi
    BEACON_MS_ID="$ms_id" BEACON_MERGE_TO="$merge_to" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" milestone_workspace_cleanup
}

cmd_milestone_graph() {
    ensure_project
    local json_flag=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*) _guard_flag "$1" ;;
            *)      shift ;;
        esac
    done

    BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" milestone_graph
}
