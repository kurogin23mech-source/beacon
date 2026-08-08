# shellcheck shell=bash
# beacon CLI — doc family (cmd_doc)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires: ensure_project _guard_flag COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). This machine-readable
#   seam names cross-file deps so a context-zero reader need not read all
#   of bin/beacon.

cmd_doc() {
    ensure_project
    case "${1:-}" in
        add)
            shift
            local doc_title=""
            local doc_id=""
            local doc_content=""
            local doc_scope=""
            local json_flag=""
            local doc_ms=""
            local doc_op=""
            local doc_trek=""
            local doc_account=""
            local doc_opportunity=""
            local doc_target=""
            local doc_bus_origin=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --id)         doc_id="${2:-}"; shift 2 ;;
                    --scope|-s)   doc_scope="${2:-}"; shift 2 ;;
                    -m|--ms)         doc_ms="${2:-}"; shift 2 ;;
                    --op)         doc_op="${2:-}"; shift 2 ;;
                    --trek)       doc_trek="${2:-}"; shift 2 ;;
                    --account|--acc)      doc_account="${2:-}"; shift 2 ;;
                    --opportunity|--opp)  doc_opportunity="${2:-}"; shift 2 ;;
                    --target)     doc_target="${2:-}"; shift 2 ;;
                    --json)       json_flag="1"; shift ;;
                    --content)    doc_content="${2:-}"; shift 2 ;;
                    --bus-origin) doc_bus_origin="1"; shift ;;
                    --stdin)      shift ;;  # stdin is handled by Python side
                    -?*)           _guard_flag "$1" ;;
                    *)            doc_title="$1"; shift ;;
                esac
            done

            if [ -z "$doc_title" ]; then
                echo "Usage: beacon doc add \"title\" [--scope core|spec|memo|retro|report] [--ms ms-id] [--op op-id] [--trek trek-id] [--account acc-id] [--opportunity opp-id] [--target id] [--id slug] [--content text] [--json]"
                echo "  Content can also be piped via stdin."
                exit 1
            fi
            BEACON_TITLE="$doc_title" BEACON_DOC_ID="$doc_id" \
                BEACON_CONTENT="$doc_content" BEACON_SCOPE="$doc_scope" \
                BEACON_MS="$doc_ms" BEACON_OP="$doc_op" \
                BEACON_TREK_ID="$doc_trek" \
                BEACON_ACCOUNT="$doc_account" BEACON_OPPORTUNITY="$doc_opportunity" \
                BEACON_TARGET="$doc_target" \
                BEACON_JSON="$json_flag" \
                BEACON_BUS_ORIGIN="$doc_bus_origin" \
                python3 "$COMMANDS_PY" doc_add
            ;;
        list|ls)
            shift
            local json_flag=""
            local scope_filter=""
            local ms_filter=""
            local op_filter=""
            local trek_filter=""
            local account_filter=""
            local opportunity_filter=""
            local target_filter=""
            local include_trashed=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json)             json_flag="1"; shift ;;
                    --scope|-s)         scope_filter="${2:-}"; shift 2 ;;
                    -m|--ms)               ms_filter="${2:-}"; shift 2 ;;
                    --op)               op_filter="${2:-}"; shift 2 ;;
                    --trek)             trek_filter="${2:-}"; shift 2 ;;
                    --account|--acc)      account_filter="${2:-}"; shift 2 ;;
                    --opportunity|--opp)  opportunity_filter="${2:-}"; shift 2 ;;
                    --target)           target_filter="${2:-}"; shift 2 ;;
                    # e-3895: `--all` is the universal "show hidden too" flag
                    # (parallels milestone --all / incident --all); the
                    # self-describing --include-trashed stays as an alias.
                    --all|--include-trashed)  include_trashed="1"; shift ;;
                    -?*) _guard_flag "$1" ;;
                    *) shift ;;
                esac
            done
            BEACON_JSON="$json_flag" BEACON_SCOPE="$scope_filter" \
                BEACON_MS="$ms_filter" BEACON_OP="$op_filter" \
                BEACON_TREK_ID="$trek_filter" \
                BEACON_ACCOUNT="$account_filter" BEACON_OPPORTUNITY="$opportunity_filter" \
                BEACON_TARGET="$target_filter" \
                BEACON_INCLUDE_TRASHED="$include_trashed" \
                python3 "$COMMANDS_PY" doc_list
            ;;
        update)
            shift
            local doc_id=""
            local doc_content=""
            local doc_title=""
            local doc_scope=""
            local doc_ms=""
            local doc_op=""
            local doc_trek=""
            local doc_account=""
            local doc_opportunity=""
            local doc_target=""
            local json_flag=""
            local doc_bus_origin=""
            # e-1859: distinguish "flag absent" from "flag passed empty".
            # Without this, --ms ms-1 on an op-scoped doc could not know
            # whether the user explicitly wants to switch scope (= drop op)
            # or accidentally keep both. The Python side treats the *_SET
            # marker as "user explicitly set this; honor their value, even
            # if empty (= clear the field)".
            local doc_ms_set=""
            local doc_op_set=""
            # ms-131 e-4497: same absent-vs-empty marker for --target so
            # `--target ""` detaches (clears the linkage).
            local doc_target_set=""

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --content)    doc_content="${2:-}"; shift 2 ;;
                    --title)      doc_title="${2:-}"; shift 2 ;;
                    --scope|-s)   doc_scope="${2:-}"; shift 2 ;;
                    -m|--ms)         doc_ms="${2:-}"; doc_ms_set="1"; shift 2 ;;
                    --op)         doc_op="${2:-}"; doc_op_set="1"; shift 2 ;;
                    --trek)       doc_trek="${2:-}"; shift 2 ;;
                    --account|--acc)      doc_account="${2:-}"; shift 2 ;;
                    --opportunity|--opp)  doc_opportunity="${2:-}"; shift 2 ;;
                    --target)     doc_target="${2:-}"; doc_target_set="1"; shift 2 ;;
                    --json)       json_flag="1"; shift ;;
                    --bus-origin) doc_bus_origin="1"; shift ;;
                    --stdin)      shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)            doc_id="$1"; shift ;;
                esac
            done

            if [ -z "$doc_id" ]; then
                echo "Usage: beacon doc update <doc-id> [--title text] [--scope core|spec|memo|retro|report] [--ms ms-id] [--op op-id] [--trek trek-id] [--account acc-id] [--opportunity opp-id] [--target id] [--content text] [--json]"
                echo "  Content can also be piped via stdin."
                echo "  Note: --content は table-doc (format: table) では拒否されます。行の変更は 'beacon doc table' を使ってください (紐づけ/タイトル/スコープ変更は可)。"
                exit 1
            fi
            BEACON_DOC_ID="$doc_id" BEACON_TITLE="$doc_title" \
                BEACON_CONTENT="$doc_content" BEACON_SCOPE="$doc_scope" \
                BEACON_MS="$doc_ms" BEACON_OP="$doc_op" \
                BEACON_MS_SET="$doc_ms_set" BEACON_OP_SET="$doc_op_set" \
                BEACON_TARGET_SET="$doc_target_set" \
                BEACON_TREK_ID="$doc_trek" \
                BEACON_ACCOUNT="$doc_account" BEACON_OPPORTUNITY="$doc_opportunity" \
                BEACON_TARGET="$doc_target" \
                BEACON_JSON="$json_flag" \
                BEACON_BUS_ORIGIN="$doc_bus_origin" \
                python3 "$COMMANDS_PY" doc_update
            ;;
        show|get)
            shift
            local doc_id=""
            local json_flag=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json) json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)      doc_id="$1"; shift ;;
                esac
            done
            if [ -z "$doc_id" ]; then
                echo "Usage: beacon doc show <doc-id> [--json]"
                echo "  exit: 0=found / 3=not found / その他非ゼロ=lookup 失敗 (API 障害等)"
                exit 1
            fi
            BEACON_DOC_ID="$doc_id" BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" doc_show
            ;;
        delete|rm)
            shift
            local doc_id=""
            local doc_reason=""
            local json_flag=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -r|--reason) doc_reason="${2:-}"; shift 2 ;;
                    --json)      json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)           doc_id="$1"; shift ;;
                esac
            done
            if [ -z "$doc_id" ]; then
                echo "Usage: beacon doc delete <doc-id> [--reason \"text\"] [--json]"
                exit 1
            fi
            BEACON_DOC_ID="$doc_id" BEACON_REASON="$doc_reason" BEACON_JSON="$json_flag" \
                python3 "$COMMANDS_PY" doc_delete
            ;;
        history)
            ensure_project
            BEACON_DOC_ID="${3:-}" python3 "$COMMANDS_PY" doc_history
            ;;
        restore)
            ensure_project
            shift
            local doc_id=""
            local rev=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --rev)       rev="${2:-}"; shift 2 ;;
                    -?*)           _guard_flag "$1" ;;
                    *)           [[ -z "$doc_id" ]] && doc_id="$1"; shift ;;
                esac
            done
            BEACON_DOC_ID="$doc_id" BEACON_REV="$rev" \
                python3 "$COMMANDS_PY" doc_restore
            ;;
        image-upload)
            ensure_project
            shift
            local image_path=""
            local json_flag=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json) json_flag="1"; shift ;;
                    -?*)           _guard_flag "$1" ;;
                    *)      [[ -z "$image_path" ]] && image_path="$1"; shift ;;
                esac
            done
            if [ -z "$image_path" ]; then
                echo "Usage: beacon doc image-upload <local-file> [--json]"
                echo "  Uploads an image to GCS and prints a markdown img tag to stdout."
                echo "  Paste the output into a doc body (or use with beacon doc update)."
                exit 1
            fi
            BEACON_DOC_IMAGE_PATH="$image_path" BEACON_JSON="$json_flag" \
                python3 "$COMMANDS_PY" doc_image_upload
            ;;
        table)
            # ms-131 e-4496 — table-doc (行×列の構造化ドキュメント) 操作。
            # 行の書き込みは型検査と履歴追記を必ず通す唯一の経路。
            shift
            local table_sub="${1:-}"
            shift 2>/dev/null || true
            local t_doc_id="" t_title="" t_columns="" t_cells="" t_row_id=""
            local t_col_key="" t_value="" t_scope="" t_target="" t_ms="" t_op=""
            local t_json=""
            local t_value_set=""
            local -a t_pos=()
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --columns)  t_columns="${2:-}"; shift 2 ;;
                    --cells)    t_cells="${2:-}"; shift 2 ;;
                    --title)    t_title="${2:-}"; shift 2 ;;
                    --value)    t_value="${2:-}"; t_value_set="1"; shift 2 ;;
                    --scope|-s) t_scope="${2:-}"; shift 2 ;;
                    --target)   t_target="${2:-}"; shift 2 ;;
                    -m|--ms)    t_ms="${2:-}"; shift 2 ;;
                    --op)       t_op="${2:-}"; shift 2 ;;
                    --id)       t_doc_id="${2:-}"; shift 2 ;;
                    --json)     t_json="1"; shift ;;
                    -?*)        _guard_flag "$1" ;;
                    *)          t_pos+=("$1"); shift ;;
                esac
            done
            case "$table_sub" in
                create)
                    # create: title は位置引数 or --title。
                    [[ -z "$t_title" && ${#t_pos[@]} -gt 0 ]] && t_title="${t_pos[0]}"
                    if [ -z "$t_title" ] || [ -z "$t_columns" ]; then
                        echo "Usage: beacon doc table create \"title\" --columns '<json>' [--scope ...] [--ms|--op|--target id] [--id slug] [--json]"
                        echo "  columns 例: '[{\"key\":\"name\",\"label\":\"名前\",\"type\":\"text\"},{\"key\":\"phase\",\"type\":\"enum\",\"values\":[\"lead\",\"won\"]}]'"
                        exit 1
                    fi
                    BEACON_TITLE="$t_title" BEACON_COLUMNS="$t_columns" \
                        BEACON_DOC_ID="$t_doc_id" BEACON_SCOPE="$t_scope" \
                        BEACON_MS="$t_ms" BEACON_OP="$t_op" BEACON_TARGET="$t_target" \
                        BEACON_JSON="$t_json" python3 "$COMMANDS_PY" doc_table_create
                    ;;
                add-row)
                    t_doc_id="${t_doc_id:-${t_pos[0]:-}}"
                    if [ -z "$t_doc_id" ] || [ -z "$t_cells" ]; then
                        echo "Usage: beacon doc table add-row <doc-id> --cells '<json>' [--json]"
                        echo "  cells 例: '{\"name\":\"Acme\",\"phase\":\"lead\"}'"
                        exit 1
                    fi
                    BEACON_DOC_ID="$t_doc_id" BEACON_CELLS="$t_cells" \
                        BEACON_JSON="$t_json" python3 "$COMMANDS_PY" doc_table_add_row
                    ;;
                set-cell)
                    # positional: <doc-id> <row-id> <col-key> [value]
                    t_doc_id="${t_doc_id:-${t_pos[0]:-}}"
                    t_row_id="${t_pos[1]:-}"
                    t_col_key="${t_pos[2]:-}"
                    # A 4th positional counts as an explicitly-provided value, so
                    # a forgotten value is rejected downstream (not written empty).
                    if [[ -z "$t_value_set" && ${#t_pos[@]} -gt 3 ]]; then
                        t_value="${t_pos[3]}"; t_value_set="1"
                    fi
                    if [ -z "$t_doc_id" ] || [ -z "$t_row_id" ] || [ -z "$t_col_key" ]; then
                        echo "Usage: beacon doc table set-cell <doc-id> <row-id> <col-key> <value> [--json]"
                        echo "  値に空白や記号を含む場合は --value \"...\" を使う（空にするなら --value \"\"）"
                        exit 1
                    fi
                    BEACON_DOC_ID="$t_doc_id" BEACON_ROW_ID="$t_row_id" \
                        BEACON_COL_KEY="$t_col_key" BEACON_VALUE="$t_value" \
                        BEACON_VALUE_SET="$t_value_set" \
                        BEACON_JSON="$t_json" python3 "$COMMANDS_PY" doc_table_set_cell
                    ;;
                rm-row)
                    t_doc_id="${t_doc_id:-${t_pos[0]:-}}"
                    t_row_id="${t_pos[1]:-}"
                    if [ -z "$t_doc_id" ] || [ -z "$t_row_id" ]; then
                        echo "Usage: beacon doc table rm-row <doc-id> <row-id> [--json]"
                        exit 1
                    fi
                    BEACON_DOC_ID="$t_doc_id" BEACON_ROW_ID="$t_row_id" \
                        BEACON_JSON="$t_json" python3 "$COMMANDS_PY" doc_table_rm_row
                    ;;
                show)
                    t_doc_id="${t_doc_id:-${t_pos[0]:-}}"
                    if [ -z "$t_doc_id" ]; then
                        echo "Usage: beacon doc table show <doc-id> [--json]"
                        exit 1
                    fi
                    BEACON_DOC_ID="$t_doc_id" BEACON_JSON="$t_json" \
                        python3 "$COMMANDS_PY" doc_table_show
                    ;;
                *)
                    echo "Usage: beacon doc table [create|add-row|set-cell|rm-row|show]"
                    echo "  create   \"title\" --columns '<json>'        型付き列で表を作成"
                    echo "  add-row  <doc-id> --cells '<json>'          行を追加 (型検査 + 履歴)"
                    echo "  set-cell <doc-id> <row-id> <col-key> <val>  セルを更新 (過去値は履歴に残る)"
                    echo "  rm-row   <doc-id> <row-id>                  行を削除 (soft-delete)"
                    echo "  show     <doc-id>                           表を描画 (--json で構造)"
                    # AX review of PR #544: an unknown subcommand (typo) must be a
                    # non-zero exit so a caller checking $? doesn't read success.
                    exit 1
                    ;;
            esac
            ;;
        *)
            echo "Usage: beacon doc [add|list|show|update|delete|restore|history|image-upload|table]"
            echo "  add          \"title\" --scope core|spec|memo|retro|report [--ms ms-id]"
            echo "  update       <doc-id> [--title text] [--content text]"
            echo "  list         [--scope core|spec|memo|retro|report] [--ms ms-id] [--include-trashed]"
            echo "  show         <doc-id>"
            echo "  delete       <doc-id> [--reason \"text\"]"
            echo "  history      <doc-id>"
            echo "  restore      <doc-id> --rev <n>"
            echo "  image-upload <local-file>                           Upload image, get markdown img tag"
            echo "  table        [create|add-row|set-cell|rm-row|show]  行×列の構造化ドキュメント (ms-131)"
            ;;
    esac
}
