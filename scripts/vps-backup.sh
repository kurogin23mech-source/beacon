#!/usr/bin/env bash
#
# ms-96 e-2382 — MySQL スナップショットを S3 へ定期退避する (6 時間毎)。
#
# 1 台 VPS 構成で唯一許容できないのはデータ消失。DB を箱の外 (S3) へ定期的に
# 退避し、そこから復旧できることまで確かめて初めて 1 台構成が成立する (SPEC
# f2ERhlUYJIrzytdBf7C5)。本スクリプトは退避側。復旧側は scripts/vps-restore.sh。
#
# systemd timer (beacon-backup.timer) から oneshot で定期起動される。
#
# 設計:
#   - mysqldump → gzip → aws s3 cp - を **パイプで直結** し、一時ファイルを作らない
#     (= ディスク圧迫ゼロ / 後始末不要 / 途中の平文ダンプがディスクに残らない)。
#   - パイプ全体の成否を PIPESTATUS で厳密判定する。mysqldump が途中で失敗しても
#     gzip/aws は 0 で返りうるため、どれか 1 つでも失敗したら退避を失敗扱いにする
#     (= 壊れた/空のダンプを「成功」と誤記録しない)。
#   - 認証情報はスクリプトに埋め込まない。MySQL は /etc/beacon/db.env、S3 は
#     /etc/beacon/backup.env から systemd の EnvironmentFile 経由で渡す。
#
# 全ロジックを main() に包むのは self-update 対策 (vps-pull-deploy.sh と同型):
# git reset で本ファイルが差し替わっても、走行中の処理は影響を受けない。
set -euo pipefail

main() {
  # MySQL 接続情報。server/mysql_client.py と同じ優先順位 (BEACON_MYSQL_* を優先、
  # 無ければ db.env の MYSQL_*)。DB 名の既定も mysql_client と揃える (= "beacon")。
  local DB_HOST="${BEACON_MYSQL_HOST:-${MYSQL_HOST:-127.0.0.1}}"
  local DB_PORT="${BEACON_MYSQL_PORT:-${MYSQL_PORT:-3306}}"
  local DB_USER="${BEACON_MYSQL_USER:-${MYSQL_USER:-}}"
  local DB_PASS="${BEACON_MYSQL_PASSWORD:-${MYSQL_PASSWORD:-}}"
  local DB_NAME="${BEACON_MYSQL_DB:-${MYSQL_DB:-beacon}}"

  # S3 退避先。backup.env で配る (BACKUP_VPS.md のテンプレート参照)。
  local S3_BUCKET="${BEACON_BACKUP_S3_BUCKET:-}"
  local S3_PREFIX="${BEACON_BACKUP_S3_PREFIX:-mysql}"
  local ENV_NAME="${BEACON_ENV:-prod}"

  log() { logger -t beacon-backup -- "$*" 2>/dev/null || true; echo "[beacon-backup] $*"; }

  # 前提チェック — 足りない設定は「無音の退避失敗」より即エラーが安全。
  if [ -z "$DB_USER" ]; then
    log "ERROR: MySQL user 未設定 (BEACON_MYSQL_USER / MYSQL_USER)。/etc/beacon/db.env を確認"
    exit 2
  fi
  if [ -z "$S3_BUCKET" ]; then
    log "ERROR: S3 退避先バケット未設定 (BEACON_BACKUP_S3_BUCKET)。/etc/beacon/backup.env を確認"
    exit 2
  fi

  # キーは UTC タイムスタンプで一意に。テストは BEACON_BACKUP_TIMESTAMP で固定できる。
  local TS="${BEACON_BACKUP_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
  local KEY="${S3_PREFIX}/${ENV_NAME}/beacon-${ENV_NAME}-${TS}.sql.gz"
  local DEST="s3://${S3_BUCKET}/${KEY}"

  log "退避開始: ${DB_NAME}@${DB_HOST}:${DB_PORT} → ${DEST}"

  # mysqldump の一貫性オプション:
  #   --single-transaction : InnoDB を lock せず一貫スナップショット (稼働中でも安全)
  #   --routines --triggers --events : スキーマ付随物も含める
  #   --no-tablespaces     : PROCESS 権限不要 (最小権限の backup ユーザーで通す)
  # パスワードは環境変数 MYSQL_PWD で渡す (ps に出さない)。
  # errexit を一時解除してパイプを走らせ、PIPESTATUS で 3 段全ての成否を自前判定する
  # (= set -e だと右端非ゼロで即 abort し、下の PIPESTATUS チェックへ来ないため)。
  set +e
  MYSQL_PWD="$DB_PASS" mysqldump \
      --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" \
      --single-transaction --routines --triggers --events --no-tablespaces \
      "$DB_NAME" \
    | gzip -c \
    | aws s3 cp - "$DEST" --only-show-errors
  local st=("${PIPESTATUS[@]}")
  set -e

  # パイプ 3 段の成否を全て確認 (mysqldump | gzip | aws)。
  if [ "${st[0]}" -ne 0 ] || [ "${st[1]}" -ne 0 ] || [ "${st[2]}" -ne 0 ]; then
    log "ERROR: 退避失敗 (mysqldump=${st[0]} gzip=${st[1]} aws=${st[2]}) — ${DEST} は無効"
    exit 1
  fi

  log "退避成功: ${DEST}"
}

main "$@"
