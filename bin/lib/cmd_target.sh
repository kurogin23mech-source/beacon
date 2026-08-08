# shellcheck shell=bash
# beacon CLI — target family (15 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_positional
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time. Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_target_review_request() {
    ensure_project
    local target_id="" new_state="" old_state="" intent="" evidence=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --new-state) new_state="${2:-}"; shift 2 ;;
            --old-state) old_state="${2:-}"; shift 2 ;;
            --intent)    intent="${2:-}";    shift 2 ;;
            --evidence)  evidence="${2:-}";  shift 2 ;;
            -?*)         _guard_positional "$1" "Usage: beacon target review-request <target-id> --new-state <state> [--old-state <state>] [--intent <text>] [--evidence e-1,e-2]" ;;
            *)           target_id="$1";     shift ;;
        esac
    done
    BEACON_TARGET_ID="$target_id" BEACON_NEW_STATE="$new_state" \
        BEACON_OLD_STATE="$old_state" BEACON_INTENT="$intent" \
        BEACON_EVIDENCE="$evidence" python3 "$COMMANDS_PY" target_review_request
}

cmd_target_approve() {
    ensure_project
    local entry_id="" rationale="" ack_no_ev="" ack_backlog=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --rationale|--reason) rationale="${2:-}"; shift 2 ;;
            --acknowledge-no-evidence) ack_no_ev=1; shift ;;
            --acknowledge-undisposed-backlog) ack_backlog=1; shift ;;
            -?*)         _guard_positional "$1" "Usage: beacon target approve <entry-id> [--rationale <text>] [--acknowledge-no-evidence] [--acknowledge-undisposed-backlog]" ;;
            *)           entry_id="$1";      shift ;;
        esac
    done
    BEACON_ENTRY_ID="$entry_id" BEACON_RATIONALE="$rationale" \
        BEACON_ACK_NO_EVIDENCE="$ack_no_ev" \
        BEACON_ACK_UNDISPOSED_BACKLOG="$ack_backlog" \
        python3 "$COMMANDS_PY" target_approve
}

cmd_target_attach_evidence() {
    ensure_project
    local entry_id="" verdict="" summary="" source=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --verdict)  verdict="${2:-}"; shift 2 ;;
            --summary)  summary="${2:-}"; shift 2 ;;
            --source)   source="${2:-}";  shift 2 ;;
            -?*)        _guard_positional "$1" "Usage: beacon target attach-review-evidence <entry-id> --verdict <attained|partial|not-attained> --summary <text> [--source <text>]" ;;
            *)          entry_id="$1";    shift ;;
        esac
    done
    BEACON_ENTRY_ID="$entry_id" BEACON_EV_VERDICT="$verdict" \
        BEACON_EV_SUMMARY="$summary" BEACON_EV_SOURCE="$source" \
        python3 "$COMMANDS_PY" target_attach_evidence
}

cmd_target_attach_disposition() {
    ensure_project
    # #551 SHOULD-1: the flag is --disposition (its value domain is
    # done|superseded|blocks-attainment). --verdict is accepted as a migration alias
    # so old scripts don't break, but its value domain differs from
    # attach-review-evidence's --verdict (attained|partial|not-attained) — the two
    # collided. commands.py steers a caller who mixes them up.
    local entry_id="" task="" disposition="" verdict_alias="" reason="" source=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --task)        task="${2:-}";        shift 2 ;;
            --disposition) disposition="${2:-}"; shift 2 ;;
            --verdict)     verdict_alias="${2:-}"; shift 2 ;;
            --reason)      reason="${2:-}";       shift 2 ;;
            --source)      source="${2:-}";       shift 2 ;;
            -?*)           _guard_positional "$1" "Usage: beacon target attach-disposition <entry-id> --task <task-id> --disposition <done|superseded|blocks-attainment> [--reason <text> (superseded 時必須)] [--source <text>]" ;;
            *)             entry_id="$1";         shift ;;
        esac
    done
    BEACON_ENTRY_ID="$entry_id" BEACON_DISP_TASK="$task" \
        BEACON_DISP_DISPOSITION="$disposition" BEACON_DISP_VERDICT="$verdict_alias" \
        BEACON_DISP_REASON="$reason" BEACON_DISP_SOURCE="$source" \
        python3 "$COMMANDS_PY" target_attach_disposition
}

cmd_target_reject() {
    ensure_project
    local entry_id="" rationale=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --rationale|--reason) rationale="${2:-}"; shift 2 ;;
            -?*)         _guard_positional "$1" "Usage: beacon target reject <entry-id> [--rationale <text>]" ;;
            *)           entry_id="$1";      shift ;;
        esac
    done
    BEACON_ENTRY_ID="$entry_id" BEACON_RATIONALE="$rationale" \
        python3 "$COMMANDS_PY" target_reject
}

cmd_target_list() {
    ensure_project
    local target_id="" pending=0 json=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target)  target_id="${2:-}"; shift 2 ;;
            --pending) pending=1;          shift ;;
            --json)    json=1;             shift ;;
            *)         shift ;;
        esac
    done
    BEACON_TARGET_ID="$target_id" BEACON_PENDING="$pending" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" target_list
}

cmd_target_create() {
    ensure_project
    local kind="" label="" fields=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --class)  kind="${2:-}";  shift 2 ;;
            --label)  label="${2:-}"; shift 2 ;;
            --field)  fields="${fields}${2:-}"$'\n'; shift 2 ;;
            -?*)      _guard_positional "$1" "Usage: beacon target create --class <kind> --label <text> [--field key=value ...]" ;;
            *)        shift ;;
        esac
    done
    BEACON_TARGET_CLASS="$kind" BEACON_LABEL="$label" BEACON_FIELDS="$fields" \
        python3 "$COMMANDS_PY" target_create
}

cmd_target_advance() {
    ensure_project
    local kind="" target_id="" to_phase="" reason="" fields=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --class)  kind="${2:-}";     shift 2 ;;
            --to)     to_phase="${2:-}"; shift 2 ;;
            --field)  fields="${fields}${2:-}"$'\n'; shift 2 ;;
            --reason) reason="${2:-}";   shift 2 ;;
            -?*)      _guard_positional "$1" "Usage: beacon target advance --class <kind> <target-id> [--to <phase>] [--field key=value ...] [--reason <text>]" ;;
            *)        target_id="$1";    shift ;;
        esac
    done
    BEACON_TARGET_CLASS="$kind" BEACON_TARGET_ID="$target_id" \
        BEACON_TO_PHASE="$to_phase" BEACON_REASON="$reason" BEACON_FIELDS="$fields" \
        python3 "$COMMANDS_PY" target_advance
}

cmd_target_close() {
    ensure_project
    local kind="" target_id="" reason=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --class)  kind="${2:-}";   shift 2 ;;
            --reason) reason="${2:-}"; shift 2 ;;
            -?*)      _guard_positional "$1" "Usage: beacon target close --class <kind> <target-id> [--reason <text>]" ;;
            *)        target_id="$1";  shift ;;
        esac
    done
    BEACON_TARGET_CLASS="$kind" BEACON_TARGET_ID="$target_id" \
        BEACON_REASON="$reason" python3 "$COMMANDS_PY" target_close
}

cmd_target_instances() {
    ensure_project
    local kind="" json=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --class)  kind="${2:-}"; shift 2 ;;
            --json)   json=1;        shift ;;
            -?*)      _guard_positional "$1" "Usage: beacon target instances --class <kind> [--json]" ;;
            *)        shift ;;
        esac
    done
    BEACON_TARGET_CLASS="$kind" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" target_instances
}

cmd_target_work_item() {
    ensure_project
    local action="${1:-}"; [[ $# -gt 0 ]] && shift
    local kind="" target_id="" item_id="" desc="" reason="" json=0
    local _pos=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --class)  kind="${2:-}";   shift 2 ;;
            --desc)   desc="${2:-}";   shift 2 ;;
            --reason) reason="${2:-}"; shift 2 ;;
            --json)   json=1;          shift ;;
            -?*)      _guard_positional "$1" "Usage: beacon target work-item <add|done|list> --class <kind> <target-id> ..." ;;
            *)        # ms-124 AX review: reject excess positional instead of
                      # silently overwriting target-id / item-id.
                      if [[ $_pos -eq 0 ]]; then target_id="$1";
                      elif [[ $_pos -eq 1 ]]; then item_id="$1";
                      else echo "Error: 余分な引数 '$1' (Usage: beacon target work-item <add|done|list> --class <kind> <target-id> [<item-id>])" >&2; exit 1; fi
                      _pos=$((_pos+1)); shift ;;
        esac
    done
    BEACON_WI_ACTION="$action" BEACON_TARGET_CLASS="$kind" \
        BEACON_TARGET_ID="$target_id" BEACON_WI_ITEM_ID="$item_id" \
        BEACON_WI_DESC="$desc" BEACON_REASON="$reason" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" target_work_item
}

cmd_target_evidence() {
    ensure_project
    # First token may be an action (add|list); default to add when the first
    # token is a flag or a target-id (back-compat with the bare-add form).
    local action=""
    case "${1:-}" in add|list) action="$1"; shift ;; esac
    local kind="" target_id="" summary="" ev_for="" json=0 _pos=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --class)   kind="${2:-}";    shift 2 ;;
            --summary) summary="${2:-}"; shift 2 ;;
            --for)     ev_for="${2:-}";  shift 2 ;;
            --json)    json=1;           shift ;;
            -?*)       _guard_positional "$1" "Usage: beacon target evidence <add|list> --class <kind> <target-id> [--summary <text>] [--for <item-id>] [--json]" ;;
            *)         # ms-124 AX review: reject excess positional instead of
                       # last-wins silently mis-recording to another target.
                       if [[ $_pos -eq 0 ]]; then target_id="$1";
                       else echo "Error: 余分な引数 '$1' (Usage: beacon target evidence <add|list> --class <kind> <target-id> ...)" >&2; exit 1; fi
                       _pos=$((_pos+1)); shift ;;
        esac
    done
    BEACON_EV_ACTION="$action" BEACON_TARGET_CLASS="$kind" \
        BEACON_TARGET_ID="$target_id" BEACON_EV_SUMMARY="$summary" \
        BEACON_EV_FOR="$ev_for" BEACON_JSON="$json" \
        python3 "$COMMANDS_PY" target_evidence
}

cmd_target_ball() {
    ensure_project
    local kind="" target_id="" ball="" reason="" _pos=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --class)  kind="${2:-}";   shift 2 ;;
            --reason) reason="${2:-}"; shift 2 ;;
            -?*)      _guard_positional "$1" "Usage: beacon target ball --class <kind> <target-id> <self|counterpart|none> [--reason <text>]" ;;
            *)        # ms-124 AX review: reject excess positional.
                      if [[ $_pos -eq 0 ]]; then target_id="$1";
                      elif [[ $_pos -eq 1 ]]; then ball="$1";
                      else echo "Error: 余分な引数 '$1' (Usage: beacon target ball --class <kind> <target-id> <self|counterpart|none>)" >&2; exit 1; fi
                      _pos=$((_pos+1)); shift ;;
        esac
    done
    BEACON_TARGET_CLASS="$kind" BEACON_TARGET_ID="$target_id" \
        BEACON_BALL="$ball" BEACON_REASON="$reason" \
        python3 "$COMMANDS_PY" target_ball
}

cmd_target_class_add() {
    ensure_project
    local kind="" label="" profession="" dtype="" id_prefix="" collection=""
    local fields="" req_fields="" phases="" term_phases="" stdin=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --kind|--class)   kind="${2:-}";       shift 2 ;;  # --class alias (target 側と語彙統一)
            --label)          label="${2:-}";      shift 2 ;;
            --profession)     profession="${2:-}"; shift 2 ;;
            --type)           dtype="${2:-}";      shift 2 ;;
            --id-prefix)      id_prefix="${2:-}";  shift 2 ;;
            --collection)     collection="${2:-}"; shift 2 ;;
            --field)          fields="${fields}${2:-}"$'\n'; shift 2 ;;
            --required-field) req_fields="${req_fields}${2:-}"$'\n'; shift 2 ;;
            --phase)          phases="${phases}${2:-}"$'\n'; shift 2 ;;
            --terminal-phase) term_phases="${term_phases}${2:-}"$'\n'; shift 2 ;;
            --stdin)          stdin=1;             shift ;;
            -?*)              _guard_positional "$1" "Usage: beacon target-class add --kind <k> --label <l> --profession <p> --type <single-shot|persistent> --id-prefix <pfx-> --collection <coll> [...]" ;;
            *)                # ms-124 AX review: reject stray positional instead
                              # of silently discarding it (a bare kind is a
                              # common mistake for --kind).
                              echo "Error: 余分な引数 '$1' — target-class add はフラグで指定します (--kind/--class <k> --label <l> ...)" >&2; exit 1 ;;
        esac
    done
    BEACON_TC_KIND="$kind" BEACON_TC_LABEL="$label" \
        BEACON_TC_PROFESSION="$profession" BEACON_TC_TYPE="$dtype" \
        BEACON_TC_ID_PREFIX="$id_prefix" BEACON_TC_COLLECTION="$collection" \
        BEACON_TC_FIELDS="$fields" BEACON_TC_REQUIRED_FIELDS="$req_fields" \
        BEACON_TC_PHASES="$phases" BEACON_TC_TERMINAL_PHASES="$term_phases" \
        BEACON_TC_STDIN="$stdin" \
        python3 "$COMMANDS_PY" target_class_add
}

cmd_target_class_list() {
    ensure_project
    local json=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json=1; shift ;;
            -?*)    _guard_positional "$1" "Usage: beacon target-class list [--json]" ;;
            *)      echo "Error: 余分な引数 '$1' (Usage: beacon target-class list [--json])" >&2; exit 1 ;;
        esac
    done
    BEACON_JSON="$json" python3 "$COMMANDS_PY" target_class_list
}
