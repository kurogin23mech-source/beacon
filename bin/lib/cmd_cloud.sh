# shellcheck shell=bash
# beacon CLI — cloud family (1 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: 
# requires-var: COMMANDS_PY
# requires-cmd: cmd_launch
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_cloud_launch() {
    local project_id="${1:-}"

    # If no project_id, show list and let user pick
    if [ -z "$project_id" ]; then
        echo "Cloud projects:"
        echo ""
        python3 "$COMMANDS_PY" cloud_list
        echo ""
        read -rp "Project ID to open: " project_id
        if [ -z "$project_id" ]; then
            echo "Cancelled."
            return 1
        fi
    fi

    # Verify the project exists in cloud before writing cloud.json
    if ! python3 "$COMMANDS_PY" cloud_check_project "$project_id" 2>/dev/null; then
        echo "Error: project '$project_id' not found in cloud."
        echo "Run 'beacon cloud list' to see available projects."
        return 1
    fi

    # Guard: warn if cloud.json already exists with a different project
    if [ -f .beacon/cloud.json ]; then
        local existing_id
        existing_id=$(python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id',''))" 2>/dev/null)
        if [ -n "$existing_id" ] && [ "$existing_id" != "$project_id" ]; then
            echo "Warning: .beacon/cloud.json already points to '$existing_id'."
            read -rp "Switch to '$project_id'? [y/N]: " confirm
            if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
                echo "Cancelled."
                return 1
            fi
        fi
    fi

    # Ensure .beacon dir and cloud.json exist
    # e-1861 (ms-61): cloud.json existence is the sole source of truth. The
    # legacy config.json {"mode": "cloud"} marker is no longer written
    # (closes the silent-drift attack surface — sub-agents can't flip the
    # CLI off cloud by rewriting config.json anymore).
    mkdir -p .beacon
    cat > .beacon/cloud.json <<EOJSON
{
  "project_id": "$project_id",
  "api_url": "https://beacon-ai.dev"
}
EOJSON

    # Create a minimal project.json if it doesn't exist (cloud mode will override reads)
    if [ ! -f "$BEACON_PROJECT_FILE" ]; then
        echo '{"name":"cloud","milestones":[]}' > "$BEACON_PROJECT_FILE"
    fi

    # Launch with BEACON_CLOUD=1
    export BEACON_CLOUD=1
    cmd_launch
}
