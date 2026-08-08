# shellcheck shell=bash
# beacon CLI — setup family (1 functions)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: 
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

cmd_setup() {
    echo "Beacon Setup (手動フォールバック)"
    echo "================================"
    echo ""
    # ms-133 e-4671: the canonical onboarding path is the AI-driven setup prompt
    # the Web UI hands out (empty-state "Setup prompt" → paste into Claude Code /
    # Codex, and the agent runs install/auth/skill/init for you). This
    # interactive wizard is the MANUAL fallback for people who prefer to answer
    # in the terminal — it deliberately overlaps that path. See report doc
    # "オンボーディング経路の責務境界と判断" (e-4671).
    echo "推奨: Web UI 空状態の「Setup prompt」を Claude Code / Codex に貼ると、"
    echo "      AI が install / auth / skill / init を代行します (ターミナル対話不要)。"
    echo "      本ウィザードは、ターミナルで自分で進めたい人向けの手動フォールバックです。"
    echo ""

    # Step 1: Auth
    if python3 "$COMMANDS_PY" auth_check &>/dev/null; then
        echo "Step 1/5: Already signed in."
    else
        echo "Step 1/5: Sign in with Google"
        python3 "$COMMANDS_PY" auth_login
    fi
    echo ""

    # Step 2: Claude Code integration
    echo "Step 2/5: Installing Claude Code hooks and Skills..."
    python3 "$COMMANDS_PY" common_setup
    echo ""

    # Step 3: Project
    echo "Step 3/5: Project setup"
    echo "  1) Create a new local project"
    echo "  2) Join a cloud project (by project ID)"
    echo "  3) Skip (hooks only)"
    read -rp "Choose [1]: " proj_choice
    proj_choice="${proj_choice:-1}"
    echo ""

    case "$proj_choice" in
        1)
            if [ -f "$BEACON_PROJECT_FILE" ]; then
                echo "  .beacon/project.json already exists — skipping init."
            else
                # ms-133 e-4648: ask the occupation FIRST. Step 2 above installed
                # only the core Skills (project.json didn't exist yet, so the
                # profession gate saw {core}); after init writes the profession
                # we re-install so the occupation's Skills (e.g. beacon-sales-*)
                # actually land. Blank = dev.
                echo "  職種 (profession):"
                echo "    1) 開発 (dev) [既定]   2) 営業 (sales)   3) バックオフィス (backoffice)"
                echo "    （その他の職種名を直接入力してもOK）"
                read -rp "  > [1]: " prof_choice
                local proj_profession=""
                case "${prof_choice:-1}" in
                    1|"") proj_profession="dev" ;;
                    2) proj_profession="sales" ;;
                    3) proj_profession="backoffice" ;;
                    *) proj_profession="$prof_choice" ;;
                esac
                echo ""
                echo "  Project name:"
                echo "    リポジトリ名や略称で構いません。ダッシュボードの表示名になります。"
                read -rp "  > " proj_name
                echo ""
                # ms-133 e-4648: frame the objective prompt by the occupation's
                # onboarding plan (WHAT to ask + vision role) instead of the
                # hardcoded dev "何を作るか". Falls back to the dev wording if the
                # plan can't be read (older engine).
                local plan_vision_role
                plan_vision_role=$(BEACON_PROFESSION="$proj_profession" \
                    python3 "$COMMANDS_PY" onboarding_plan 2>/dev/null \
                    | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('vision_role',''))
except Exception: pass" 2>/dev/null)
                echo "  Objective (${plan_vision_role:-このプロジェクトで実現したいこと}):"
                if [ "$proj_profession" = "dev" ] || [ -z "$plan_vision_role" ]; then
                    echo "    「何を作るか」ではなく「誰がどんな状態になるか」で書いてください。"
                fi
                read -rp "  > " proj_objective
                echo ""
                echo "  Weekly retro day:"
                echo "    毎週この曜日にAIが振り返りを促します。週の締めに近い曜日がおすすめです。"
                echo "    1=Mon  2=Tue  3=Wed  4=Thu  5=Fri  6=Sat  7=Sun"
                read -rp "  > [5]: " retro_choice
                retro_choice="${retro_choice:-5}"
                local retro_day
                case "$retro_choice" in
                    1) retro_day="monday" ;;
                    2) retro_day="tuesday" ;;
                    3) retro_day="wednesday" ;;
                    4) retro_day="thursday" ;;
                    5) retro_day="friday" ;;
                    6) retro_day="saturday" ;;
                    7) retro_day="sunday" ;;
                    *) retro_day="friday" ;;
                esac
                # ms-63 / e-1441: setup-driven init also honours the
                # default-high sensitivity posture (no opt-in path from the
                # interactive setup yet; users get the safe default).
                BEACON_NAME="$proj_name" BEACON_OBJECTIVE="$proj_objective" \
                    BEACON_RETRO_DAY="$retro_day" \
                    BEACON_PROFESSION="$proj_profession" \
                    BEACON_SENSITIVITY="high" python3 "$COMMANDS_PY" init
                # ms-133 e-4648: re-install Skills now that project.json carries
                # the profession, so the occupation's Skills install (Step 2 ran
                # before init and could only see {core}). No-op for dev.
                if [ -f "$BEACON_PROJECT_FILE" ] && [ "$proj_profession" != "dev" ]; then
                    echo "  職種 '$proj_profession' の Skill を導入中..."
                    python3 "$COMMANDS_PY" skill_install 2>&1 | sed 's/^/  /'
                fi
            fi
            ;;
        2)
            echo ""
            echo "  Fetching accessible projects..."
            local projects_json
            projects_json=$(BEACON_JSON=1 python3 "$COMMANDS_PY" cloud_list 2>/dev/null || echo "[]")

            # Print numbered list and capture IDs via Python
            local list_output
            list_output=$(echo "$projects_json" | python3 -c "
import sys, json
projects = json.load(sys.stdin)
if not projects:
    print('EMPTY')
else:
    for i, p in enumerate(projects, 1):
        name = p.get('name', '')
        obj = p.get('objective', '')[:50]
        suffix = ('  ' + obj) if obj else ''
        print(f'{i}) {p[\"project_id\"]}  \"{name}\"{suffix}')
    print('IDS:' + ','.join(p['project_id'] for p in projects))
" 2>/dev/null)

            if [ "$list_output" = "EMPTY" ] || [ -z "$list_output" ]; then
                echo "  No accessible projects found."
                echo "  Ask your team owner to invite you first, then re-run beacon setup."
            else
                local ids_line
                ids_line=$(echo "$list_output" | grep "^IDS:")
                local display
                display=$(echo "$list_output" | grep -v "^IDS:")
                echo "$display" | sed 's/^/  /'
                echo ""
                read -rp "  Select project [1]: " proj_num
                proj_num="${proj_num:-1}"
                local selected_id
                selected_id=$(echo "${ids_line#IDS:}" | cut -d',' -f"$proj_num")
                if [ -n "$selected_id" ]; then
                    BEACON_CLOUD_PROJECT_ID="$selected_id" python3 "$COMMANDS_PY" cloud_join
                else
                    echo "  Invalid selection — skipped."
                fi
            fi
            ;;
        3)
            echo "  Skipped."
            ;;
    esac
    echo ""

    # Step 4: DM channel auto-install (ms-54 e-1238)
    # Routes through the same opt-out gate as `beacon channel install`. If
    # any of (env BEACON_NO_BUS, project flag, global flag) is set we
    # silently skip + audit-log a single line. If install itself fails
    # (no node, no .mcp.json writable, etc.), surface the error but do
    # not block the rest of setup — the user can re-run `beacon channel
    # install` later.
    echo "Step 4/5: DM channel (multi-session messaging)"
    if [ -f "$BEACON_PROJECT_FILE" ]; then
        # Use a dry probe of the status command to decide whether to attempt.
        # The python command refuses with exit 1 if opted out, which we treat
        # as expected and silent.
        if python3 "$COMMANDS_PY" channel_install 2>&1 | sed 's/^/  /'; then
            :
        else
            # channel_install returns non-zero on opt-out or genuine failure.
            # We don't differentiate here — the python side already printed
            # the cause. Setup continues either way.
            :
        fi
    else
        echo "  (no project initialized — skipping DM channel install)"
    fi
    echo ""

    # Step 5: Launch
    echo "Step 5/5: Show status + UI pointers"
    read -rp "Show now? [Y/n]: " launch_choice
    launch_choice="${launch_choice:-Y}"
    if [[ "$launch_choice" =~ ^[Yy] ]]; then
        if [ -f "$BEACON_PROJECT_FILE" ]; then
            cmd_launch
        else
            echo "  No project initialized — skipping launch."
        fi
    fi

    echo ""
    echo "✅ Setup complete!"
    echo ""
    echo "Next step:"
    echo "  Start Claude Code in this project directory and talk to /beacon-init."
    echo "  It will guide you through naming the project and shaping milestones"
    echo "  in a conversational form."
    echo ""
    echo "  Example:"
    echo "    cd ~/projects/your-project"
    echo "    claude          # launch Claude Code"
    echo "    > /beacon-init"
    echo ""
    echo "Tip: run 'beacon' any time to open the dashboard."
}
