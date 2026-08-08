# shellcheck shell=bash
# beacon CLI — communication family (4 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_positional _require_audit_reason
# requires-var: COMMANDS_PY BEACON_ACK_SENTINEL
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_communication_add() {
    ensure_project
    local target_id="" summary="" direction="" channel="" ref="" url="" body="" occurred=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --direction) direction="${2:-}"; shift 2 ;;
            --channel)   channel="${2:-}"; shift 2 ;;
            --source-ref) ref="${2:-}"; shift 2 ;;
            --source-url) url="${2:-}"; shift 2 ;;
            --body)      body="${2:-}"; shift 2 ;;
            --occurred)  occurred="${2:-}"; shift 2 ;;
            -?*) _guard_positional "$1" "Usage: beacon communication add <target-id> <summary> --direction inbound|outbound [--channel email|slack|meeting|calendar|phone|other] [--source-ref <id>] [--source-url <link>] [--body <multi-line-digest>] [--occurred <datetime>]" ;;
            *)
                if [ -z "$target_id" ]; then target_id="$1"; else summary="$1"; fi
                shift ;;
        esac
    done
    if [ -z "$target_id" ] || [ -z "$summary" ] || [ -z "$direction" ]; then
        echo "Usage: beacon communication add <target-id> <summary> --direction inbound|outbound [--channel email|slack|meeting|calendar|phone|other] [--source-ref <id>] [--source-url <link>] [--body <multi-line-digest>] [--occurred <datetime>]"
        exit 1
    fi
    BEACON_COMM_TARGET="$target_id" BEACON_COMM_SUMMARY="$summary" \
        BEACON_COMM_DIRECTION="$direction" BEACON_COMM_CHANNEL="$channel" \
        BEACON_COMM_SOURCE_REF="$ref" BEACON_COMM_SOURCE_URL="$url" \
        BEACON_COMM_BODY="$body" \
        BEACON_COMM_OCCURRED="$occurred" \
        python3 "$COMMANDS_PY" communication_add
}

cmd_communication_list() {
    ensure_project
    local target_id="" json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*)    _guard_positional "$1" "Usage: beacon communication list <target-id> [--json]" ;;
            *)      target_id="$1"; shift ;;
        esac
    done
    if [ -z "$target_id" ]; then
        echo "Usage: beacon communication list <target-id> [--json]"
        exit 1
    fi
    BEACON_COMM_TARGET="$target_id" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" communication_list
}

cmd_communication_cancel() {
    ensure_project
    local comm_id="" reason="" acknowledge=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reason) reason="$2"; shift 2 ;;
            --acknowledge) acknowledge="1"; shift ;;
            -?*)      _guard_positional "$1" "Usage: beacon communication cancel <comm-id> --reason <text> | --acknowledge" ;;
            *)        comm_id="$1"; shift ;;
        esac
    done
    if [ -z "$comm_id" ]; then
        echo "Usage: beacon communication cancel <comm-id> --reason <text> | --acknowledge"
        exit 1
    fi
    _require_audit_reason "communication cancel" "$reason" "$acknowledge"
    [ -z "$reason" ] && reason="$BEACON_ACK_SENTINEL"
    BEACON_COMM_ID="$comm_id" BEACON_COMM_REASON="$reason" \
        python3 "$COMMANDS_PY" communication_cancel
}

cmd_communication_retarget() {
    ensure_project
    local comm_id="" new_target="" reason=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reason) reason="$2"; shift 2 ;;
            -?*) _guard_positional "$1" "Usage: beacon communication retarget <comm-id> <new-target: opp-|acc-|act-|nrt-> [--reason <text>]" ;;
            *)   if [ -z "$comm_id" ]; then comm_id="$1"; elif [ -z "$new_target" ]; then new_target="$1"; fi; shift ;;
        esac
    done
    if [ -z "$comm_id" ] || [ -z "$new_target" ]; then
        echo "Usage: beacon communication retarget <comm-id> <new-target: opp-|acc-|act-|nrt-> [--reason <text>]"
        exit 1
    fi
    BEACON_COMM_ID="$comm_id" BEACON_COMM_TARGET="$new_target" BEACON_COMM_REASON="$reason" \
        python3 "$COMMANDS_PY" communication_retarget
}
