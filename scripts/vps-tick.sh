#!/usr/bin/env bash
#
# e-1391 (ms-66) — 本番の定期ティック駆動 (Trek/Operation 自律発火のドライバ)。
#
# Cloud Run 廃止 → VPS 移行 (ms-102/ms-96) の際、Cloud Scheduler が叩いていた
# `POST /api/system/trek-scheduler/tick` を叩く駆動が消失し、Trek/Operation の
# server-side 自律が本番で「無音で」止まっていた (2026-07-24 Trek review で発覚)。
# このスクリプトが systemd timer (beacon-tick.timer) から oneshot で定期起動され、
# ローカルの uvicorn (127.0.0.1:8000) の tick endpoint を 1 回叩く。
#
# tick endpoint は Trek fanout に加えて lib/tick_scheduler 経由で due な Operation
# も相乗り発火する (= 単一 endpoint が server-side 自律の全駆動)。したがってこの
# 1 本の timer で Trek/Operation 両方が駆動される。
#
# 認証: endpoint は X-Beacon-Scheduler-Key ヘッダに BEACON_SCHEDULER_INTERNAL_KEY を
# 要求する。値は systemd service の EnvironmentFile (= /etc/beacon/app.env、
# beacon-api.service と共用) から渡す。未設定なら app 側が起動拒否する (e-4115)。
#
# 全ロジックを main() に包むのは self-update 対策 (vps-pull-deploy.sh と同じ理由:
# このファイル自身が git reset で書き換わりうる)。
set -euo pipefail

main() {
  local TICK_URL="${BEACON_TICK_URL:-http://127.0.0.1:8000/api/system/trek-scheduler/tick}"
  local KEY="${BEACON_SCHEDULER_INTERNAL_KEY:-}"
  local TIMEOUT="${BEACON_TICK_TIMEOUT:-30}"

  log() { logger -t beacon-tick -- "$*" 2>/dev/null || true; echo "[beacon-tick] $*"; }

  if [[ -z "$KEY" ]]; then
    # 鍵が渡っていなければ endpoint は 403 を返すだけなので、ここで loud に落として
    # systemd unit を failed にする (= `systemctl status beacon-tick` で気付ける)。
    log "ERROR: BEACON_SCHEDULER_INTERNAL_KEY is empty; tick endpoint will 403. Refusing to run."
    exit 1
  fi

  # -fsS: 非 2xx で失敗し (systemd unit を failed に)、進捗バーは出さずエラーは出す。
  # レスポンス body (tick report JSON) は journal に残して観測できるようにする。
  # PR #491 review 2b: the scheduler key is a SECRET, so it must NOT go on the
  # curl argv (`-H "X-...: $KEY"` would expose it in /proc/<pid>/cmdline to any
  # local user for the life of the request). Pass it through a curl --config
  # file supplied via process substitution instead: the value travels through
  # an anonymous pipe (/dev/fd/N), and `printf` is a shell builtin so the key
  # never appears in any process's argv. No temp file → nothing to clean up.
  local body http_code
  body=$(curl -fsS \
    --max-time "$TIMEOUT" \
    -X POST \
    --config <(printf 'header = "X-Beacon-Scheduler-Key: %s"\n' "$KEY") \
    -H "Content-Type: application/json" \
    -w $'\n%{http_code}' \
    -d '{}' \
    "$TICK_URL") || {
      log "ERROR: tick request failed (curl exit) against ${TICK_URL}"
      exit 1
    }

  http_code="${body##*$'\n'}"
  body="${body%$'\n'*}"
  log "tick ok (HTTP ${http_code}): ${body}"
}

main "$@"
