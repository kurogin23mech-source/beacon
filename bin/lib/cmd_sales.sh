# shellcheck shell=bash
# beacon CLI — sales family (1 function)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_positional _guard_extra_positional
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_sales_target() {
    ensure_project
    # `beacon sales target list [--json]` vs `beacon sales target <user> <amount>`
    if [ "${1:-}" = "list" ]; then
        shift
        local json_flag=""
        while [[ $# -gt 0 ]]; do
            case "$1" in --json) json_flag="1"; shift ;; *) shift ;; esac
        done
        BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" sales_target_list
        return
    fi
    local member="" amount=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -?*) _guard_positional "$1" "Usage: beacon sales target <user> <amount> | list" ;;
            *)   if [ -z "$member" ]; then member="$1"; else amount="$1"; fi; shift ;;
        esac
    done
    if [ -z "$member" ]; then
        echo "Usage: beacon sales target <user> <amount> | list"
        exit 1
    fi
    BEACON_TARGET_MEMBER="$member" BEACON_TARGET_AMOUNT="$amount" \
        python3 "$COMMANDS_PY" sales_target
}

# ms-160 e-5981 — 送信元アカウント / 送信 identity の CLI 化。
#
# これらは commands.py にしか無く、営業の手順書 (Skill) が
# `python3 commands.py sales_account_add` 等を環境変数ベタ書きで直叩きしていた。
# CORE doc architecture-tool-skill-separation §2 の「Skill → CLI → local/API」を
# 飛ばす経路で、実際に sales_account_remove / sales_identity_show の 2 つは
# 手順書からも呼ばれず到達不能になっていた。
#
# 動詞名は `send-account` (送信元) とし、既存の `beacon account` (顧客 = Account)
# と読み違えないようにする。内部 verb 名 (sales_account_*) は据え置き。
#
# 値の受け渡しは「そのまま透過」を守る: 署名や議事録取得元の clear と値の共存拒否は
# python 側 (lib/sales_entities.py) が唯一の判定点なので、bash 側で先回りして
# 弾いたり空文字に潰したりしない。

cmd_sales_send_account() {
    ensure_project
    local sub="${1:-}"; shift 2>/dev/null || true
    case "$sub" in
        add)
            local label="" email=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --email) email="${2:-}"; shift 2 ;;
                    -?*) _guard_positional "$1" "Usage: beacon sales send-account add <label> --email <address>" ;;
                    *)   _guard_extra_positional "$label" "$1" "Usage: beacon sales send-account add <label> --email <address>"
                         label="$1"; shift ;;
                esac
            done
            if [ -z "$label" ] || [ -z "$email" ]; then
                echo "Usage: beacon sales send-account add <label> --email <address>"
                exit 1
            fi
            BEACON_SEND_LABEL="$label" BEACON_SEND_EMAIL="$email" \
                python3 "$COMMANDS_PY" sales_account_add
            ;;
        list|ls)
            local json_flag=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json) json_flag="1"; shift ;;
                    -?*)    _guard_flag "$1" ;;
                    *)      shift ;;
                esac
            done
            BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" sales_account_list
            ;;
        remove|rm)
            local label=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -?*) _guard_positional "$1" "Usage: beacon sales send-account remove <label>" ;;
                    *)   _guard_extra_positional "$label" "$1" "Usage: beacon sales send-account remove <label>"
                         label="$1"; shift ;;
                esac
            done
            if [ -z "$label" ]; then
                echo "Usage: beacon sales send-account remove <label>"
                exit 1
            fi
            BEACON_SEND_LABEL="$label" python3 "$COMMANDS_PY" sales_account_remove
            ;;
        route)
            local label="" service="" namespace="" alias_val=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --service)   service="${2:-}"; shift 2 ;;
                    --namespace) namespace="${2:-}"; shift 2 ;;
                    --alias)     alias_val="${2:-}"; shift 2 ;;
                    -?*) _guard_positional "$1" "Usage: beacon sales send-account route <label> --service <gmail|calendar|drive> --namespace <ns> [--alias <account>]" ;;
                    *)   _guard_extra_positional "$label" "$1" "Usage: beacon sales send-account route <label> --service <gmail|calendar|drive> --namespace <ns> [--alias <account>]"
                         label="$1"; shift ;;
                esac
            done
            if [ -z "$label" ] || [ -z "$service" ] || [ -z "$namespace" ]; then
                echo "Usage: beacon sales send-account route <label> --service <gmail|calendar|drive> --namespace <ns> [--alias <account>]"
                exit 1
            fi
            BEACON_SEND_LABEL="$label" BEACON_SEND_SERVICE="$service" \
                BEACON_SEND_NAMESPACE="$namespace" BEACON_SEND_ALIAS="$alias_val" \
                python3 "$COMMANDS_PY" sales_account_route
            ;;
        resolve)
            local label="" service=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --service) service="${2:-}"; shift 2 ;;
                    -?*) _guard_positional "$1" "Usage: beacon sales send-account resolve [<label>] --service <gmail|calendar|drive>" ;;
                    *)   _guard_extra_positional "$label" "$1" "Usage: beacon sales send-account resolve [<label>] --service <gmail|calendar|drive>"
                         label="$1"; shift ;;
                esac
            done
            if [ -z "$service" ]; then
                echo "Usage: beacon sales send-account resolve [<label>] --service <gmail|calendar|drive>"
                echo "  <label> 省略時は既定の送信 identity を使う。"
                exit 1
            fi
            BEACON_SEND_LABEL="$label" BEACON_SEND_SERVICE="$service" \
                python3 "$COMMANDS_PY" sales_account_resolve
            ;;
        signature)
            # clear と値の共存拒否は python 側が唯一の判定点。ここでは透過させる。
            local label="" signature="" clear=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --signature) signature="${2:-}"; shift 2 ;;
                    --clear)     clear="1"; shift ;;
                    -?*) _guard_positional "$1" "Usage: beacon sales send-account signature <label> (--signature <text> | --clear)" ;;
                    *)   _guard_extra_positional "$label" "$1" "Usage: beacon sales send-account signature <label> (--signature <text> | --clear)"
                         label="$1"; shift ;;
                esac
            done
            if [ -z "$label" ]; then
                echo "Usage: beacon sales send-account signature <label> (--signature <text> | --clear)"
                exit 1
            fi
            BEACON_SEND_LABEL="$label" BEACON_SEND_SIGNATURE="$signature" \
                BEACON_SEND_SIGNATURE_CLEAR="$clear" \
                python3 "$COMMANDS_PY" sales_account_signature
            ;;
        transcript-source)
            local ts_sub="${1:-}"; shift 2>/dev/null || true
            case "$ts_sub" in
                get)
                    local acc_id=""
                    while [[ $# -gt 0 ]]; do
                        case "$1" in
                            -?*) _guard_positional "$1" "Usage: beacon sales send-account transcript-source get <acc-id>" ;;
                            *)   _guard_extra_positional "$acc_id" "$1" "Usage: beacon sales send-account transcript-source get <acc-id>"
                                 acc_id="$1"; shift ;;
                        esac
                    done
                    if [ -z "$acc_id" ]; then
                        echo "Usage: beacon sales send-account transcript-source get <acc-id>"
                        exit 1
                    fi
                    BEACON_ACCOUNT_ID="$acc_id" \
                        python3 "$COMMANDS_PY" sales_account_transcript_source_get
                    ;;
                set)
                    # clear と値の共存拒否は python 側が唯一の判定点。ここでは透過させる。
                    local acc_id="" ts_type="" folder_id="" naming="" tool="" ts_clear=""
                    while [[ $# -gt 0 ]]; do
                        case "$1" in
                            --type)      ts_type="${2:-}"; shift 2 ;;
                            --folder-id) folder_id="${2:-}"; shift 2 ;;
                            --naming)    naming="${2:-}"; shift 2 ;;
                            --tool)      tool="${2:-}"; shift 2 ;;
                            --clear)     ts_clear="1"; shift ;;
                            -?*) _guard_positional "$1" "Usage: beacon sales send-account transcript-source set <acc-id> (--type <meet_calendar|drive_folder|external|manual> [--folder-id <id>] [--naming <pattern>] [--tool <name>] | --clear))" ;;
                            *)   _guard_extra_positional "$acc_id" "$1" "Usage: beacon sales send-account transcript-source set <acc-id> (--type <meet_calendar|drive_folder|external|manual> [--folder-id <id>] [--naming <pattern>] [--tool <name>] | --clear))"
                                 acc_id="$1"; shift ;;
                        esac
                    done
                    if [ -z "$acc_id" ]; then
                        echo "Usage: beacon sales send-account transcript-source set <acc-id> (--type <meet_calendar|drive_folder|external|manual> [--folder-id <id>] [--naming <pattern>] [--tool <name>] | --clear)"
                        exit 1
                    fi
                    BEACON_ACCOUNT_ID="$acc_id" BEACON_TS_TYPE="$ts_type" \
                        BEACON_TS_FOLDER_ID="$folder_id" BEACON_TS_NAMING="$naming" \
                        BEACON_TS_TOOL="$tool" BEACON_TS_CLEAR="$ts_clear" \
                        python3 "$COMMANDS_PY" sales_account_transcript_source_set
                    ;;
                *)
                    echo "Usage: beacon sales send-account transcript-source get <acc-id>"
                    echo "       beacon sales send-account transcript-source set <acc-id> (--type <meet_calendar|drive_folder|external|manual> [--folder-id <id>] [--naming <pattern>] [--tool <name>] | --clear)"
                    ;;
            esac
            ;;
        *)
            echo "Usage: beacon sales send-account add <label> --email <address>"
            echo "       beacon sales send-account list [--json]"
            echo "       beacon sales send-account remove <label>"
            echo "       beacon sales send-account route <label> --service <gmail|calendar|drive> --namespace <ns> [--alias <account>]"
            echo "       beacon sales send-account resolve [<label>] --service <gmail|calendar|drive>"
            echo "       beacon sales send-account signature <label> (--signature <text> | --clear)"
            echo "       beacon sales send-account transcript-source get|set <acc-id> ..."
            ;;
    esac
}

cmd_sales_identity() {
    ensure_project
    local sub="${1:-}"; shift 2>/dev/null || true
    case "$sub" in
        show)
            local json_flag=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json) json_flag="1"; shift ;;
                    -?*)    _guard_flag "$1" ;;
                    *)      shift ;;
                esac
            done
            BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" sales_identity_show
            ;;
        set)
            local identity=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -?*) _guard_positional "$1" "Usage: beacon sales identity set <label|email>" ;;
                    *)   _guard_extra_positional "$identity" "$1" "Usage: beacon sales identity set <label|email>"
                         identity="$1"; shift ;;
                esac
            done
            if [ -z "$identity" ]; then
                echo "Usage: beacon sales identity set <label|email>"
                exit 1
            fi
            BEACON_SEND_IDENTITY="$identity" python3 "$COMMANDS_PY" sales_identity_set
            ;;
        check)
            local from_val="" label=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --from)  from_val="${2:-}"; shift 2 ;;
                    --label) label="${2:-}"; shift 2 ;;
                    -?*) _guard_flag "$1" ;;
                    *)   shift ;;
                esac
            done
            if [ -z "$from_val" ]; then
                echo "Usage: beacon sales identity check --from <address> [--label <label>]"
                echo "  一致なら exit 0 + 'OK: ...'、不一致なら exit 1 + 'BLOCK: ...'。"
                exit 1
            fi
            BEACON_SEND_FROM="$from_val" BEACON_SEND_LABEL="$label" \
                python3 "$COMMANDS_PY" sales_identity_check
            ;;
        *)
            echo "Usage: beacon sales identity show [--json]"
            echo "       beacon sales identity set <label|email>"
            echo "       beacon sales identity check --from <address> [--label <label>]"
            ;;
    esac
}

cmd_sales_gmail_permalink() {
    ensure_project
    local from_val="" msgid=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --from)  from_val="${2:-}"; shift 2 ;;
            # rfc822 の Message-ID を期待する (Gmail の thread-id / API id ではない)。
            --msgid|--rfc822-msgid) msgid="${2:-}"; shift 2 ;;
            -?*) _guard_flag "$1" ;;
            *)   shift ;;
        esac
    done
    if [ -z "$from_val" ]; then
        echo "Usage: beacon sales gmail-permalink --from <address> --msgid <rfc822 Message-ID>"
        exit 1
    fi
    BEACON_SEND_FROM="$from_val" BEACON_RFC822_MSGID="$msgid" \
        python3 "$COMMANDS_PY" sales_gmail_permalink
}

cmd_sales_reply_watch() {
    ensure_project
    local sub="${1:-}"
    case "$sub" in
        ensure) python3 "$COMMANDS_PY" sales_reply_watch_op_ensure ;;
        *)      echo "Usage: beacon sales reply-watch ensure   (返信ウォッチャーを回す Operation を用意する; 冪等)" ;;
    esac
}
