# shellcheck shell=bash
# beacon CLI — watch family (cmd_watch)
# ms-160 e-5981: route the watch_* verbs through the CLI boundary.
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project _guard_flag _guard_extra_positional
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).
#
# Before this file the three watch_* verbs existed only in lib/commands.py and
# were reached by Skills invoking `python3 .../commands.py watch_set` with
# hand-written env vars — bypassing the CLI boundary that CORE doc
# `architecture-tool-skill-separation` §2 makes mandatory (Skill → CLI →
# local/API). The bypass cost: the verbs never appeared in `beacon --help`,
# Windows/pipx users could not reach them at all, and the env var NAMES were
# duplicated in Skill prose where a rename in commands.py would break them
# silently.

cmd_watch() {
    ensure_project
    case "${1:-}" in
        set)
            shift
            local wi_id="" channel="" thread="" cadence=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --channel)  channel="${2:-}"; shift 2 ;;
                    --thread)   thread="${2:-}"; shift 2 ;;
                    --cadence)  cadence="${2:-}"; shift 2 ;;
                    -?*)        _guard_flag "$1" ;;
                    *)          _guard_extra_positional "$wi_id" "$1" "Usage: beacon watch set <work-item-id> --channel <ch> [--thread <ref>] [--cadence <min>]"
                                wi_id="$1"; shift ;;
                esac
            done
            if [ -z "$wi_id" ] || [ -z "$channel" ]; then
                echo "Usage: beacon watch set <work-item-id> --channel <gmail|slack|chatwork|...> [--thread <ref>] [--cadence <minutes; default 60>]"
                exit 1
            fi
            BEACON_WATCH_TARGET="$wi_id" BEACON_WATCH_CHANNEL="$channel" \
                BEACON_WATCH_THREAD="$thread" BEACON_WATCH_CADENCE="$cadence" \
                python3 "$COMMANDS_PY" watch_set
            ;;
        list|ls)
            shift
            local json_flag="" awaiting=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json)      json_flag="1"; shift ;;
                    --awaiting)  awaiting="1"; shift ;;
                    -?*)         _guard_flag "$1" ;;
                    *)           shift ;;
                esac
            done
            BEACON_JSON="$json_flag" BEACON_WATCH_AWAITING="$awaiting" \
                python3 "$COMMANDS_PY" watch_list
            ;;
        clear)
            shift
            local wi_id=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -?*) _guard_flag "$1" ;;
                    *)   _guard_extra_positional "$wi_id" "$1" "Usage: beacon watch clear <work-item-id>"
                         wi_id="$1"; shift ;;
                esac
            done
            if [ -z "$wi_id" ]; then
                echo "Usage: beacon watch clear <work-item-id>"
                exit 1
            fi
            BEACON_WATCH_TARGET="$wi_id" python3 "$COMMANDS_PY" watch_clear
            ;;
        *)
            echo "Usage: beacon watch set <work-item-id> --channel <ch> [--thread <ref>] [--cadence <min>]"
            echo "       beacon watch list [--awaiting] [--json]"
            echo "       beacon watch clear <work-item-id>"
            ;;
    esac
}
