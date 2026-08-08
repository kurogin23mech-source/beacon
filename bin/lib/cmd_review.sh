# shellcheck shell=bash
# beacon CLI — review family (1 functions)
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

cmd_review_context() {
    # ms-119 e-3947: emit the review-kernel bundle (原典 + mechanical diff only)
    # for an independent judge subagent. See /beacon-review-run.
    ensure_project
    local review_type="" diff_ref="" pr="" origin_doc="" mode="diff" target="" judge_model="" batch=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --batch)       batch="1";            shift ;;
            --type)        review_type="${2:-}"; shift 2 ;;
            --diff-ref)    diff_ref="${2:-}";    shift 2 ;;
            --pr)          pr="${2:-}";          shift 2 ;;
            --origin-doc)  origin_doc="${2:-}";  shift 2 ;;
            --target)      target="${2:-}";      shift 2 ;;
            --judge-model) judge_model="${2:-}"; shift 2 ;;
            --mode)        mode="${2:-}";        shift 2 ;;
            -?*)           _guard_flag "$1" ;;
            *)             _guard_positional "$1" "Usage: beacon review context [--batch] --type <ax|philosophy|attainment> [--pr <n> | --diff-ref <base...head>] [--origin-doc <doc-id>] [--target <ms-XX|op-X>] [--judge-model <m>] [--mode diff]  (このコマンドは flag のみを取ります。--type <値> の形で渡してください)" ;;
        esac
    done
    if [[ -n "$batch" ]]; then
        # ms-119 e-4125: emit every review bundle that fires at this 節目 in one
        # call (PR-open → AX + 保守性) for parallel judge fan-out + aggregation.
        # --batch honors only --pr; reject the flags it would silently ignore
        # (e-4125 AX review: accepted-but-ignored input is a silent no-op).
        if [[ -n "$review_type" || -n "$diff_ref" || -n "$origin_doc" || -n "$target" || -n "$judge_model" || "$mode" != "diff" ]]; then
            echo "Error: --batch は --pr のみを取ります。--type / --mode / --origin-doc / --target / --judge-model は無視されるため拒否します (batch は節目の全 review 種別を自動で出します)。" >&2
            exit 1
        fi
        BEACON_PR="$pr" BEACON_IMPLEMENTER_MODEL="${BEACON_IMPLEMENTER_MODEL:-}" \
            python3 "$COMMANDS_PY" review_batch_context
        return
    fi
    BEACON_REVIEW_TYPE="$review_type" BEACON_DIFF_REF="$diff_ref" BEACON_PR="$pr" \
        BEACON_ORIGIN_DOC="$origin_doc" BEACON_TARGET_ID="$target" BEACON_MODE="$mode" \
        BEACON_JUDGE_MODEL="$judge_model" \
        python3 "$COMMANDS_PY" review_context
}
