#!/usr/bin/env bash
#
# ms-102 — Pull 型デプロイ (VPS が main を定期的に見て自分で更新する)。
#
# OSS (public repo) + 本番 VPS の構成では、GitHub Actions に SSH 鍵等の Secret を
# 置く Push 型より、VPS 側が main を polling して self-update する Pull 型のほうが
# 漏れうる秘密がゼロで安全。beacon リポは public なので fetch に認証は不要。
#
# systemd timer (beacon-deploy.timer) から oneshot で定期起動される。
# 定常時 (main に変化なし) のコストは `git fetch` 1 回だけ。main が進んだときだけ
# reset --hard → pip install → サービス再起動 → health check を行う。
#
# 全ロジックを main() に包んでいるのは self-update 対策: このスクリプト自身も
# repo 内 (scripts/) にあり git reset で書き換わりうる。bash は main() の全定義を
# 読み込んでから呼ぶので、実行中に本ファイルが差し替わっても走っている処理は
# 影響を受けない (差し替わった内容は次回 timer 起動から効く)。
set -euo pipefail

main() {
  local REPO="${BEACON_REPO_DIR:-/opt/beacon}"
  local SERVICE="${BEACON_SERVICE:-beacon-api.service}"
  local BRANCH="${BEACON_DEPLOY_BRANCH:-main}"
  local HEALTH_URL="${BEACON_HEALTH_URL:-https://beacon-ai.dev/health}"

  log() { logger -t beacon-deploy -- "$*" 2>/dev/null || true; echo "[beacon-deploy] $*"; }

  cd "$REPO"

  # public repo なので fetch は認証不要。origin/main と現在 HEAD を比較する。
  git fetch --quiet --prune origin "$BRANCH"
  local LOCAL REMOTE
  LOCAL="$(git rev-parse HEAD)"
  REMOTE="$(git rev-parse "origin/${BRANCH}")"

  # e-3222: restart を「git が進んだか」だけで決めると、restart が一度失敗した
  # 時に git は最新・uvicorn は旧コードのまま stale 化し、以降 timer が『git 変化
  # なし』で restart を skip し続けて永久復旧不能になる (/api/version は git rev の
  # live 読みなので stuck を隠す)。そこで「稼働プロセスが確認済みで serving して
  # いる rev」を stamp に記録し、判定を『serving 済み rev != 目標 rev、または
  # service 停止中』にする。restart/health が失敗した run では stamp を更新しない
  # ので、次の timer tick で自動再試行される (self-heal)。
  local DEPLOYED_STAMP="${REPO}/.venv/.beacon-deployed-rev"
  local DEPLOYED=""
  [ -f "$DEPLOYED_STAMP" ] && DEPLOYED="$(cat "$DEPLOYED_STAMP" 2>/dev/null || true)"

  # working tree を目標 rev に合わせる (git が進んだ時のみ reset、冪等)。
  if [ "$LOCAL" != "$REMOTE" ]; then
    log "main advanced ${LOCAL:0:7} -> ${REMOTE:0:7}"
    git reset --hard "$REMOTE"
  fi

  # 定常時の fast path: 目標 rev を serving 済み かつ service 稼働中なら何もしない。
  if [ "$DEPLOYED" = "$REMOTE" ] && systemctl is-active --quiet "$SERVICE"; then
    exit 0
  fi

  if [ "$DEPLOYED" = "$REMOTE" ]; then
    log "serving ${REMOTE:0:7} だが ${SERVICE} が停止中 — restart (self-heal)"
  else
    local LAST="${DEPLOYED:0:7}"; [ -z "$LAST" ] && LAST="none"
    log "deploying ${REMOTE:0:7} (last served: ${LAST})"
  fi

  # 依存が変わったときだけ入れ直したいので requirements 群のハッシュで gate する。
  # 変化なしなら pip を丸ごと skip して定常デプロイを速くする。
  local REQ_FILES=(requirements.txt server/requirements.txt pyproject.toml)
  local STAMP="${REPO}/.venv/.beacon-req-stamp"
  local NEW_HASH OLD_HASH=""
  NEW_HASH="$(cat "${REQ_FILES[@]}" 2>/dev/null | sha256sum | awk '{print $1}')"
  [ -f "$STAMP" ] && OLD_HASH="$(cat "$STAMP" 2>/dev/null || true)"
  if [ "$NEW_HASH" != "$OLD_HASH" ]; then
    log "dependencies changed — pip install"
    "${REPO}/.venv/bin/pip" install --quiet -r requirements.txt -r server/requirements.txt
    echo "$NEW_HASH" > "$STAMP"
  else
    log "dependencies unchanged — skip pip"
  fi

  # health poll のリトライ数 / 間隔は env で調整可能 (test は小さくして高速化、
  # ops も遅い起動に合わせて伸ばせる)。
  local SETTLE="${BEACON_RESTART_SETTLE:-2}"
  local HEALTH_RETRIES="${BEACON_HEALTH_RETRIES:-15}"
  local HEALTH_INTERVAL="${BEACON_HEALTH_INTERVAL:-2}"

  log "restarting ${SERVICE}"
  sudo systemctl restart "$SERVICE"
  sleep "$SETTLE"
  if ! systemctl is-active --quiet "$SERVICE"; then
    log "ERROR ${SERVICE} not active after deploy of ${REMOTE:0:7}"
    exit 1
  fi

  # 公開 endpoint が 200 を返すまでポーリング。成功したら serving 確認済みの rev を
  # stamp に記録する。失敗時は stamp を更新しないので、次 tick で再試行される
  # (= self-heal、e-3222)。
  local i
  for i in $(seq 1 "$HEALTH_RETRIES"); do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      echo "$REMOTE" > "$DEPLOYED_STAMP"
      log "deployed ${REMOTE:0:7} — ${SERVICE} active, health OK"
      exit 0
    fi
    sleep "$HEALTH_INTERVAL"
  done
  log "ERROR health check failed after deploy of ${REMOTE:0:7} (${HEALTH_URL})"
  exit 1
}

main "$@"
