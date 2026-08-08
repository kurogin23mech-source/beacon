# shellcheck shell=bash
# beacon CLI — meeting family (6 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_positional _require_audit_reason
# requires-var: COMMANDS_PY BEACON_ACK_SENTINEL
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time. Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_meeting_schedule() {
    ensure_project
    local opp_id="" at="" end="" location="" event_id="" cal_ns="" cal_acct="" set_transition=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --at)              at="${2:-}"; shift 2 ;;
            --end)             end="${2:-}"; shift 2 ;;
            --location)        location="${2:-}"; shift 2 ;;
            --event-id)        event_id="${2:-}"; shift 2 ;;
            --calendar-ns)     cal_ns="${2:-}"; shift 2 ;;
            --calendar-account) cal_acct="${2:-}"; shift 2 ;;
            --set-transition)  set_transition="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon meeting schedule <opp-id> --at <datetime> [--end <datetime>] [--location <text>] [--event-id <id>] [--calendar-ns <ns>] [--calendar-account <acct>] [--set-transition]" ;;
            *)   opp_id="$1"; shift ;;
        esac
    done
    if [ -z "$opp_id" ] || [ -z "$at" ]; then
        echo "Usage: beacon meeting schedule <opp-id> --at <datetime> [--end <datetime>] [--location <text>] [--event-id <id>] [--calendar-ns <ns>] [--calendar-account <acct>] [--set-transition]"
        exit 1
    fi
    BEACON_MTG_OPP="$opp_id" BEACON_MTG_AT="$at" BEACON_MTG_END="$end" \
        BEACON_MTG_LOCATION="$location" BEACON_MTG_EVENT_ID="$event_id" \
        BEACON_MTG_CAL_NS="$cal_ns" BEACON_MTG_CAL_ACCT="$cal_acct" \
        BEACON_MTG_SET_TRANSITION="$set_transition" \
        python3 "$COMMANDS_PY" meeting_schedule
}

cmd_meeting_reschedule() {
    ensure_project
    local mtg_id="" at="" end="" event_id="" set_transition=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --at)             at="${2:-}"; shift 2 ;;
            --end)            end="${2:-}"; shift 2 ;;
            --event-id)       event_id="${2:-}"; shift 2 ;;
            --set-transition) set_transition="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon meeting reschedule <mtg-id> --at <datetime> [--end <datetime>] [--event-id <id>] [--set-transition]" ;;
            *)   mtg_id="$1"; shift ;;
        esac
    done
    if [ -z "$mtg_id" ] || [ -z "$at" ]; then
        echo "Usage: beacon meeting reschedule <mtg-id> --at <datetime> [--end <datetime>] [--event-id <id>] [--set-transition]"
        exit 1
    fi
    BEACON_MTG_ID="$mtg_id" BEACON_MTG_AT="$at" BEACON_MTG_END="$end" \
        BEACON_MTG_EVENT_ID="$event_id" BEACON_MTG_SET_TRANSITION="$set_transition" \
        python3 "$COMMANDS_PY" meeting_reschedule
}

cmd_meeting_end() {
    ensure_project
    local mtg_id=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon meeting end <mtg-id>" ;;
            *)   mtg_id="$1"; shift ;;
        esac
    done
    if [ -z "$mtg_id" ]; then echo "Usage: beacon meeting end <mtg-id>"; exit 1; fi
    BEACON_MTG_ID="$mtg_id" python3 "$COMMANDS_PY" meeting_end
}

cmd_meeting_cancel() {
    ensure_project
    local mtg_id="" reason="" acknowledge=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reason) reason="$2"; shift 2 ;;
            --acknowledge) acknowledge="1"; shift ;;
            -?*) _guard_positional "$1" "Usage: beacon meeting cancel <mtg-id> (--reason <text> | --acknowledge)" ;;
            *)   mtg_id="$1"; shift ;;
        esac
    done
    if [ -z "$mtg_id" ]; then echo "Usage: beacon meeting cancel <mtg-id> (--reason <text> | --acknowledge)"; exit 1; fi
    _require_audit_reason "meeting cancel" "$reason" "$acknowledge"
    [ -z "$reason" ] && reason="$BEACON_ACK_SENTINEL"
    BEACON_MTG_ID="$mtg_id" BEACON_MTG_CANCEL_REASON="$reason" \
        python3 "$COMMANDS_PY" meeting_cancel
}

cmd_meeting_list() {
    ensure_project
    # e-3909: <opp-id> is OPTIONAL — omit it to list meetings across ALL
    # opportunities (symmetric with `opportunity list` / the global list-ended).
    local opp_id="" json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*)    _guard_positional "$1" "Usage: beacon meeting list [<opp-id>] [--json]" ;;
            *)      opp_id="$1"; shift ;;
        esac
    done
    BEACON_MTG_OPP="$opp_id" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" meeting_list
}

cmd_meeting_ended() {
    ensure_project
    local now="" json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --now)  now="${2:-}"; shift 2 ;;
            --json) json_flag="1"; shift ;;
            -?*)    _guard_positional "$1" "Usage: beacon meeting ended [--now <datetime>] [--json]" ;;
            *)      shift ;;
        esac
    done
    BEACON_MTG_NOW="$now" BEACON_JSON="$json_flag" \
        python3 "$COMMANDS_PY" meeting_ended
}
