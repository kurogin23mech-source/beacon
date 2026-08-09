# shellcheck shell=bash
# beacon CLI — deadline family (1 function)
# ms-142 e-5010: 職種横断の締切列挙を占う単一経路 (occupation.iter_deadline_candidates
# を consume する `beacon deadline due`)。session-start の締切表示がこれを呼ぶ。
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.

cmd_deadline_due() {
    ensure_project
    local json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json_flag="1"; shift ;;
            -?*) _guard_flag "$1" ;;
            # `deadline due` takes no positional args; a stray one (e.g. a typo'd
            # id) must error, not be silently dropped — symmetric with the flag
            # guard above and with dispatch.py's argparse (which rejects it).
            *)      echo "beacon deadline due は引数を取りません (不明: '$1')" >&2; return 2 ;;
        esac
    done
    BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" deadline_due
}
