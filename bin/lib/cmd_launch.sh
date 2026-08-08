# shellcheck shell=bash
# beacon CLI — launch family (1 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project
# requires-cmd: cmd_status
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_launch() {
    # e-764 (ms-4): legacy tmux/curses dashboard archived to
    # .trash/lib-dashboard-py-e764/. The bare `beacon` invocation now
    # prints status plus pointers to the Tauri Desktop App and Web UI,
    # which are the two supported front-ends going forward.
    ensure_project

    # Show the current status summary (same output as `beacon status`)
    cmd_status

    echo ""
    echo "UI:"
    echo "  - Tauri Desktop App: launch the bundled desktop app if installed"
    echo "    (see README → Desktop App)"

    # Show project-specific web URL when the project is cloud-linked.
    if [ -f .beacon/cloud.json ]; then
        local cloud_project_id
        cloud_project_id=$(python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id',''))" 2>/dev/null || true)
        if [ -n "$cloud_project_id" ]; then
            echo "  - Web UI: https://beacon-ai.dev/?project=$cloud_project_id"
        else
            echo "  - Web UI: https://beacon-ai.dev"
        fi
    else
        echo "  - Web UI: https://beacon-ai.dev  (run 'beacon cloud upload-initial' to sync this project)"
    fi
    echo ""
    echo "Run 'beacon help' for the full command list."
}
