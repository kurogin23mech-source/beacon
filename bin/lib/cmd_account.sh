# shellcheck shell=bash
# beacon CLI — account family (8 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_flag _guard_positional _require_audit_reason
# requires-var: COMMANDS_PY BEACON_ACK_SENTINEL
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time. Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

# ms-106: Sales entities (profession=sales). Account / Opportunity CRUD.
# Thin argv→env translators mirroring cmd_save's shape; business logic
# lives in lib/commands.py (cmd_account_* / cmd_opportunity_*).
cmd_account_add() {
    ensure_project
    local name="" health="" assignee=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --health)   health="${2:-}"; shift 2 ;;
            --assignee) assignee="${2:-}"; shift 2 ;;
            -?*)      _guard_positional "$1" "Usage: beacon account add \"<name>\" [--health <text>] [--assignee <user>]" ;;
            *)        name="$1"; shift ;;
        esac
    done
    if [ -z "$name" ]; then
        echo "Usage: beacon account add \"<name>\" [--health <text>] [--assignee <user>]"
        exit 1
    fi
    BEACON_ACCOUNT_NAME="$name" BEACON_ACCOUNT_HEALTH="$health" \
        BEACON_ACCOUNT_ASSIGNEE="$assignee" \
        python3 "$COMMANDS_PY" account_add
}

cmd_account_rename() {
    ensure_project
    local acc_id="" name=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon account rename <acc-id> <new-name>" ;;
            *)   if [ -z "$acc_id" ]; then acc_id="$1"; else name="$1"; fi; shift ;;
        esac
    done
    if [ -z "$acc_id" ] || [ -z "$name" ]; then
        echo "Usage: beacon account rename <acc-id> <new-name>"
        exit 1
    fi
    BEACON_ACCOUNT_ID="$acc_id" BEACON_ACCOUNT_NAME="$name" \
        python3 "$COMMANDS_PY" account_rename
}

cmd_account_assign() {
    ensure_project
    local acc_id="" assignee=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon account assign <acc-id> <user>" ;;
            *)   if [ -z "$acc_id" ]; then acc_id="$1"; else assignee="$1"; fi; shift ;;
        esac
    done
    if [ -z "$acc_id" ]; then
        echo "Usage: beacon account assign <acc-id> <user>"
        exit 1
    fi
    BEACON_ACCOUNT_ID="$acc_id" BEACON_ASSIGNEE="$assignee" \
        python3 "$COMMANDS_PY" account_assign
}

cmd_account_nurturing() {
    ensure_project
    local acc_id="" desc="" deadline="" ball=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --deadline) deadline="${2:-}"; shift 2 ;;
            --ball)     ball="${2:-}"; shift 2 ;;
            -?*) _guard_positional "$1" "Usage: beacon account nurturing <acc-id> <desc> [--deadline <date>] [--ball self|counterpart]" ;;
            *)   if [ -z "$acc_id" ]; then acc_id="$1"; else desc="$1"; fi; shift ;;
        esac
    done
    if [ -z "$acc_id" ] || [ -z "$desc" ]; then
        echo "Usage: beacon account nurturing <acc-id> <desc> [--deadline <date>] [--ball self|counterpart]"
        exit 1
    fi
    BEACON_ACCOUNT_ID="$acc_id" BEACON_NURTURING_DESC="$desc" \
        BEACON_NURTURING_DEADLINE="$deadline" BEACON_NURTURING_BALL="$ball" \
        python3 "$COMMANDS_PY" account_nurturing
}

cmd_account_list() {
    ensure_project
    local json_flag="" as_project="" linked=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            # ms-113 e-3734: 「別 project から見たら何が見えるか」を実演する。
            # 指定 project の視点で開示可能な Account だけを fail-closed で絞る。
            --as-project) as_project="${2:-}"; shift 2 ;;
            # ms-111 e-3872: 同じ組織の他 project に置かれ、この project に開示
            # された Account を横断して取り込む (cross-project read, cloud mode)。
            --linked) linked="1"; shift ;;
            -?*) _guard_flag "$1" ;;
            *)      shift ;;
        esac
    done
    BEACON_JSON="$json_flag" BEACON_AS_PROJECT="$as_project" BEACON_LINKED="$linked" \
        python3 "$COMMANDS_PY" account_list
}

cmd_account_contact() {
    ensure_project
    # ms-120 / e-3907: canonical form is `account contact add <acc-id> <name>`
    # (verb). `contact` is a noun; the bare `account contact <acc> <name>` form
    # mutates (creates a contact) without naming the action. The bare form stays
    # as a deprecated alias with a nudge.
    if [ "${1:-}" = "add" ]; then
        shift
    elif [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
        echo "Note: 'beacon account contact <acc> <name>' は 'beacon account contact add <acc> <name>' に統一されました (e-3907: contact は名詞、add が作成動詞)。旧形は当面 alias として動きます。" >&2
    fi
    local acc_id="" name="" role="" email="" phone=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --role)  role="${2:-}"; shift 2 ;;
            --email) email="${2:-}"; shift 2 ;;
            --phone) phone="${2:-}"; shift 2 ;;
            -?*)     _guard_positional "$1" "Usage: beacon account contact <acc-id> <name> [--role <text>] [--email <text>] [--phone <text>]" ;;
            *)
                if [ -z "$acc_id" ]; then acc_id="$1"; else name="$1"; fi
                shift ;;
        esac
    done
    if [ -z "$acc_id" ] || [ -z "$name" ]; then
        echo "Usage: beacon account contact <acc-id> <name> [--role <text>] [--email <text>] [--phone <text>]"
        exit 1
    fi
    BEACON_ACCOUNT_ID="$acc_id" BEACON_CONTACT_NAME="$name" \
        BEACON_CONTACT_ROLE="$role" BEACON_CONTACT_EMAIL="$email" \
        BEACON_CONTACT_PHONE="$phone" \
        python3 "$COMMANDS_PY" account_contact
}

cmd_account_phase() {
    ensure_project
    local acc_id="" phase="" note=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --note) note="${2:-}"; shift 2 ;;
            -?*)    _guard_positional "$1" "Usage: beacon account phase <acc-id> <phase> [--note <text>]" ;;
            *)
                if [ -z "$acc_id" ]; then acc_id="$1"; else phase="$1"; fi
                shift ;;
        esac
    done
    if [ -z "$acc_id" ] || [ -z "$phase" ]; then
        echo "Usage: beacon account phase <acc-id> <phase> [--note <text>]"
        exit 1
    fi
    BEACON_ACCOUNT_ID="$acc_id" BEACON_PHASE="$phase" BEACON_PHASE_NOTE="$note" \
        python3 "$COMMANDS_PY" account_phase
}

cmd_account_delete() {
    ensure_project
    local acc_id="" force="" reason="" acknowledge=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force)  force="1"; shift ;;
            --reason) reason="$2"; shift 2 ;;
            --acknowledge) acknowledge="1"; shift ;;
            -?*)      _guard_positional "$1" "Usage: beacon account delete <acc-id> [--force] (--reason <text> | --acknowledge)" ;;
            *)        acc_id="$1"; shift ;;
        esac
    done
    if [ -z "$acc_id" ]; then
        echo "Usage: beacon account delete <acc-id> [--force] (--reason <text> | --acknowledge)"
        exit 1
    fi
    _require_audit_reason "account delete" "$reason" "$acknowledge"
    [ -z "$reason" ] && reason="$BEACON_ACK_SENTINEL"
    BEACON_ACCOUNT_ID="$acc_id" BEACON_FORCE="$force" BEACON_CANCEL_REASON="$reason" \
        python3 "$COMMANDS_PY" account_delete
}
