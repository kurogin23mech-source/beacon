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

  # e-5359: systemd unit (メモリ設定 / ワーカー数) を repo から本番へ再適用する経路。
  # 本番の unit を手で編集すると次の再構築 (fresh VPS / setup 再実行) で黙って消える。
  # repo の deploy/systemd/<service> を単一の真値源にし、毎デプロイで /etc/systemd/system
  # へ一致させることで「手入れが消える」穴を構造で塞ぐ。dir は test で差し替え可能。
  # 変数名は SOURCE (repo 側) / DEST (設置先) で向きを名前に固定する (AX review PR#725)。
  local UNIT_SRC="${BEACON_UNIT_SOURCE_DIR:-${REPO}/deploy/systemd}/${SERVICE}"
  local UNIT_DST="${BEACON_SYSTEMD_UNIT_DEST_DIR:-/etc/systemd/system}/${SERVICE}"

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

  # 追加の state stamp (= e-3231, auto-rollback):
  #  - QUAR_STAMP: health が繰り返し落ちて rollback した「隔離 rev」。origin/main が
  #    この rev を指している間は再デプロイせず last-good を serving し続ける (=
  #    pull 型は main が真値源なので、隔離しないと bad rev を毎 tick 再デプロイして
  #    ループする)。
  #  - FAIL_STAMP: "rev count"。同じ rev の health 連続失敗回数。閾値到達で rollback。
  local QUAR_STAMP="${REPO}/.venv/.beacon-quarantine-rev"
  local FAIL_STAMP="${REPO}/.venv/.beacon-deploy-fail"
  local ROLLBACK_AFTER="${BEACON_ROLLBACK_AFTER:-3}"
  local QUARANTINE=""
  [ -f "$QUAR_STAMP" ] && QUARANTINE="$(cat "$QUAR_STAMP" 2>/dev/null || true)"

  # health poll / restart のパラメータ (env で調整可能、test は小さくして高速化、
  # ops も遅い起動に合わせて伸ばせる)。
  local SETTLE="${BEACON_RESTART_SETTLE:-2}"
  local HEALTH_RETRIES="${BEACON_HEALTH_RETRIES:-15}"
  local HEALTH_INTERVAL="${BEACON_HEALTH_INTERVAL:-2}"

  # 依存が変わったときだけ入れ直す (requirements 群のハッシュで gate)。rollback で
  # 別 rev に戻す時も再評価したいので関数化する。
  pip_gate() {
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
  }

  # e-5359: repo の systemd unit を本番 /etc/systemd/system に一致させる。
  # 呼び出し前提: working tree が serving 予定の rev に合っている (= git reset 済) こと。
  # source 無し (= 別 service 名 / 旧 repo) や既に一致していれば何もしない。差分があれば
  # install + daemon-reload する (daemon-reload はこの後の restart より前に効かせる)。
  # sudo 権限が無くて適用できない場合は LOUD にログして続行する (= コード配置自体は
  # 止めない。unit を手で cp する運用に落ちるが、それを「気付ける」形にするのが目的)。
  reconcile_unit() {
    [ -f "$UNIT_SRC" ] || return 0                       # source 無し = 対象外
    if cmp -s "$UNIT_SRC" "$UNIT_DST" 2>/dev/null; then
      return 0                                            # 既に一致
    fi
    if sudo install -m 0644 "$UNIT_SRC" "$UNIT_DST" 2>/dev/null \
       && sudo systemctl daemon-reload 2>/dev/null; then
      log "reconciled ${SERVICE} unit from repo (${UNIT_SRC} -> ${UNIT_DST}); daemon-reload done"
    else
      log "ERROR ${SERVICE} unit を repo から適用できませんでした (sudo install / systemctl daemon-reload 権限を確認) — /etc/systemd/system の unit が repo と乖離したまま。手動 cp が必要"
    fi
  }

  # working tree を $1 に合わせ、pip → restart → health poll を行う。health OK なら
  # serving 確認済み rev を stamp して 0 を返す。失敗時は stamp を更新せず非 0 を
  # 返す (= 次 tick で再試行される self-heal、e-3222)。
  restart_and_health() {
    local target="$1"
    local cur; cur="$(git rev-parse HEAD)"
    [ "$cur" != "$target" ] && git reset --hard "$target"
    reconcile_unit    # e-5359: working tree が target に揃った後で unit を再適用 (非致命的: 失敗は ERROR ログのみで続行、戻り値は見ない)
    pip_gate
    log "restarting ${SERVICE} for ${target:0:7}"
    sudo systemctl restart "$SERVICE"
    sleep "$SETTLE"
    if ! systemctl is-active --quiet "$SERVICE"; then
      log "ERROR ${SERVICE} not active after applying ${target:0:7}"
      return 1
    fi
    local i
    for i in $(seq 1 "$HEALTH_RETRIES"); do
      if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
        echo "$target" > "$DEPLOYED_STAMP"
        log "serving ${target:0:7} — ${SERVICE} active, health OK"
        return 0
      fi
      sleep "$HEALTH_INTERVAL"
    done
    log "ERROR health check failed for ${target:0:7} (${HEALTH_URL})"
    return 1
  }

  # origin/main が隔離 rev を追い越したら (= 誰かが bad rev を直して push した)、
  # 隔離を解除して通常デプロイに戻す。
  if [ -n "$QUARANTINE" ] && [ "$QUARANTINE" != "$REMOTE" ]; then
    log "main advanced past quarantined ${QUARANTINE:0:7} -> ${REMOTE:0:7}; clearing quarantine"
    : > "$QUAR_STAMP"; : > "$FAIL_STAMP"
    QUARANTINE=""
  fi

  # target 決定: 隔離中 (REMOTE == 隔離 rev) は last-good を保持、それ以外は REMOTE。
  local TARGET="$REMOTE"
  if [ -n "$QUARANTINE" ]; then
    if [ -n "$DEPLOYED" ]; then
      TARGET="$DEPLOYED"
    else
      # 隔離中なのに last-good が無い異常系。隔離を諦めて REMOTE を試す。
      log "WARN quarantined ${REMOTE:0:7} but no known-good rev; retrying target"
      : > "$QUAR_STAMP"; QUARANTINE=""
    fi
  fi

  # e-5359: git rev が同じでも、本番の systemd unit が repo と乖離していたら
  # (= 手で編集された / 再構築で古い版が入っている) fast-path を抜けて再適用+restart する。
  # working tree は steady state で HEAD == DEPLOYED == TARGET なので UNIT_SRC と比較できる。
  local UNIT_DRIFT=0
  if [ -f "$UNIT_SRC" ] && ! cmp -s "$UNIT_SRC" "$UNIT_DST" 2>/dev/null; then
    UNIT_DRIFT=1
  fi

  # fast path: target を serving 済み かつ service 稼働中 かつ unit も一致なら何もしない。
  if [ "$DEPLOYED" = "$TARGET" ] && [ "$UNIT_DRIFT" = 0 ] && systemctl is-active --quiet "$SERVICE"; then
    [ -n "$QUARANTINE" ] && log "holding last-good ${TARGET:0:7}; ${REMOTE:0:7} quarantined (main を直して push すると解除)"
    exit 0
  fi

  if [ -n "$QUARANTINE" ]; then
    log "restoring last-good ${TARGET:0:7} (${REMOTE:0:7} quarantined)"
  elif [ "$DEPLOYED" = "$TARGET" ] && [ "$UNIT_DRIFT" = 1 ]; then
    log "systemd unit が repo と乖離 — 再適用 + restart (e-5359)"
  elif [ "$DEPLOYED" = "$TARGET" ]; then
    log "serving ${TARGET:0:7} だが ${SERVICE} が停止中 — restart (self-heal)"
  else
    local LAST="${DEPLOYED:0:7}"; [ -z "$LAST" ] && LAST="none"
    log "deploying ${TARGET:0:7} (last served: ${LAST})"
  fi

  if restart_and_health "$TARGET"; then
    : > "$FAIL_STAMP"   # 成功したら失敗ストリークをリセット
    exit 0
  fi

  # ここから失敗処理。last-good を restore/hold していて それも落ちたなら、戻す先が
  # 無い (= 既知の good rev でも本番が落ちている)。次 tick で再試行する。
  if [ "$TARGET" != "$REMOTE" ]; then
    log "ERROR last-good ${TARGET:0:7} も health 失敗 — 次 tick で再試行"
    exit 1
  fi

  # 新 rev (REMOTE) のデプロイが失敗。同 rev の連続失敗回数を数える。
  local FAILREV="" FAILCOUNT=0
  if [ -f "$FAIL_STAMP" ]; then
    read -r FAILREV FAILCOUNT < "$FAIL_STAMP" 2>/dev/null || true
  fi
  [ "$FAILREV" != "$REMOTE" ] && FAILCOUNT=0
  FAILCOUNT=$((FAILCOUNT + 1))
  echo "$REMOTE $FAILCOUNT" > "$FAIL_STAMP"
  log "health failed for ${REMOTE:0:7} (${FAILCOUNT}/${ROLLBACK_AFTER} before rollback)"

  # 戻せる先 (別の既知 good rev) が無ければ rollback できない。self-heal 再試行。
  if [ -z "$DEPLOYED" ] || [ "$DEPLOYED" = "$REMOTE" ]; then
    exit 1
  fi
  # 閾値未満なら まだ rollback しない (= flaky な単発失敗で churn しない)。
  if [ "$FAILCOUNT" -lt "$ROLLBACK_AFTER" ]; then
    exit 1
  fi

  # 閾値到達 → last-good に戻し、bad rev を隔離する。
  log "ROLLBACK ${REMOTE:0:7} が ${FAILCOUNT} 回 health 失敗 — last-good ${DEPLOYED:0:7} へ戻す"
  if restart_and_health "$DEPLOYED"; then
    echo "$REMOTE" > "$QUAR_STAMP"   # bad rev を隔離 (main が進むまで再デプロイしない)
    : > "$FAIL_STAMP"
    log "ROLLED BACK to ${DEPLOYED:0:7}; quarantined ${REMOTE:0:7} (main を直して push すると解除)"
    exit 1   # target (REMOTE) のデプロイ自体は失敗なので非 0 で run を赤くする
  fi
  log "CRITICAL rollback to ${DEPLOYED:0:7} も health 失敗 — 本番が落ちている"
  exit 1
}

main "$@"
