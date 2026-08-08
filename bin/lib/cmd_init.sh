# shellcheck shell=bash
# beacon CLI — init family (1 functions)
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

cmd_init() {
    if [ -f "$BEACON_PROJECT_FILE" ]; then
        echo ".beacon/project.json already exists."
        exit 1
    fi

    # Guard: warn if initializing in home directory
    if [ "$(pwd)" = "$HOME" ]; then
        echo "Warning: You are about to initialize beacon in your home directory."
        echo "This will create ~/.beacon/ and modify ~/CLAUDE.md."
        echo "Beacon is designed for project directories, not the home directory."
        echo ""
        read -rp "Continue anyway? [y/N] " confirm
        if [[ ! "$confirm" =~ ^[Yy] ]]; then
            echo "Aborted."
            exit 1
        fi
    fi

    local name="${BEACON_INIT_NAME:-}"
    local objective="${BEACON_INIT_OBJECTIVE:-}"
    local retro_day="${BEACON_INIT_RETRO_DAY:-}"
    local storage="${BEACON_INIT_STORAGE:-local}"
    # ms-63 / e-1441: default-high sensitivity unless caller opts in to low.
    local sensitivity="${BEACON_INIT_SENSITIVITY:-high}"
    # ms-133 e-4648: chosen occupation. The `--profession` flag sets
    # BEACON_INIT_PROFESSION; when absent we FALL BACK to any inherited
    # BEACON_PROFESSION env (the pre-e-4648 way callers/tests select the
    # occupation). Without this fallback, passing BEACON_PROFESSION=sales but no
    # flag was clobbered to "" below (BEACON_PROFESSION="$profession"), silently
    # creating a dev project — the regression CI caught. Blank still → cmd_init
    # defaults to dev, so a plain `beacon init` keeps the dev schema unchanged.
    local profession="${BEACON_INIT_PROFESSION:-${BEACON_PROFESSION:-}}"
    local storage_choice="1"
    [[ "$storage" == "cloud" ]] && storage_choice="2"

    # Interactive mode: ask for missing fields
    if [ -z "$name" ] || [ -z "$objective" ]; then
        echo "Beacon - Project Initialization"
        echo "================================"
        echo ""

        if [ -z "$name" ]; then
            echo "Project name:"
            echo "  リポジトリ名や略称で構いません。ダッシュボードの表示名になります。"
            read -rp "> " name
            echo ""
        fi

        if [ -z "$objective" ]; then
            echo "Project objective:"
            echo "  このプロジェクトで最終的に何を実現したいですか？"
            echo "  あなたとAIが共有するゴール宣言です。セッションをまたいで判断の基準になります。"
            echo "  「何を作るか」ではなく「誰がどんな状態になるか」で書いてください。"
            echo "  例）「AI駆動で開発するユーザーが、目的と今やっていることを常にクリアに保ちながら開発に集中できるようにする」"
            read -rp "> " objective
            echo ""
        fi

        if [ -z "$retro_day" ]; then
            echo "Weekly retro day:"
            echo "  毎週この曜日にAIが振り返りを促します。週の締めに近い曜日がおすすめです。"
            echo "  1=Mon  2=Tue  3=Wed  4=Thu  5=Fri  6=Sat  7=Sun"
            read -rp "> [5]: " retro_choice_input
            retro_choice_input="${retro_choice_input:-5}"
            case "$retro_choice_input" in
                1) retro_day="monday" ;;
                2) retro_day="tuesday" ;;
                3) retro_day="wednesday" ;;
                4) retro_day="thursday" ;;
                5) retro_day="friday" ;;
                6) retro_day="saturday" ;;
                7) retro_day="sunday" ;;
                *) retro_day="friday" ;;
            esac
            echo ""
        fi

        if [ "$storage_choice" = "1" ]; then
            echo "Storage:"
            echo "  1=Local   .beacon/ をローカルのみで管理。チーム共有・Web UIは使えません。"
            echo "  2=Cloud   クラウドに同期。Web UIとチーム共有が使えます（Googleアカウント必要）。"
            read -rp "> [1]: " storage_choice
            storage_choice="${storage_choice:-1}"
        fi
    fi

    BEACON_NAME="$name" BEACON_OBJECTIVE="$objective" \
        BEACON_RETRO_DAY="${retro_day:-friday}" \
        BEACON_SENSITIVITY="$sensitivity" \
        BEACON_PROFESSION="$profession" \
        python3 "$COMMANDS_PY" init

    # Cloud setup if selected
    if [ "${storage_choice}" = "2" ]; then
        python3 "$COMMANDS_PY" cloud_push
        # e-1861 (ms-61): cloud.json existence is sole source of truth.
        # cmd_cloud_push above already created .beacon/cloud.json — no need
        # to write the legacy `{"mode": "cloud"}` marker into config.json.
        echo "Cloud mode enabled."
    fi
}
