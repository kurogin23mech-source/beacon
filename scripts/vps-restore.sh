#!/usr/bin/env bash
#
# ms-96 e-2382 — S3 退避物からの MySQL 復旧 + 復旧検証。
#
# 退避 (vps-backup.sh) の対になる復旧側。「退避物から本当に戻せるか」を確かめて
# 初めてバックアップは成立する (SPEC f2ERhlUYJIrzytdBf7C5 の受入条件: リストア成功
# の実機確認)。
#
# 2 つのモード:
#   verify (既定): 退避物を **検証用の別 DB** (<db>_restore_check) に流し込み、主要
#                  テーブルの行数を出す。本番 DB には一切触れないので、6 時間毎の
#                  退避が「戻せる状態」であることを無停止で確かめられる。
#   restore     : 実際の復旧。--into <db> 先に流し込む。本番 DB を上書きする場合は
#                  事故防止で --force を必須にする。
#
# キー指定:
#   --latest (既定)   : s3://bucket/prefix/env/ の中で最新のオブジェクトを使う。
#   --key <s3-key>    : バケット相対キーを明示。
#
# 認証情報は db.env / backup.env から (vps-backup.sh と同じ)。
set -euo pipefail

main() {
  local DB_HOST="${BEACON_MYSQL_HOST:-${MYSQL_HOST:-127.0.0.1}}"
  local DB_PORT="${BEACON_MYSQL_PORT:-${MYSQL_PORT:-3306}}"
  local DB_USER="${BEACON_MYSQL_USER:-${MYSQL_USER:-}}"
  local DB_PASS="${BEACON_MYSQL_PASSWORD:-${MYSQL_PASSWORD:-}}"
  local DB_NAME="${BEACON_MYSQL_DB:-${MYSQL_DB:-beacon}}"
  local S3_BUCKET="${BEACON_BACKUP_S3_BUCKET:-}"
  local S3_PREFIX="${BEACON_BACKUP_S3_PREFIX:-mysql}"
  local ENV_NAME="${BEACON_ENV:-prod}"

  local MODE="verify" KEY="" USE_LATEST="1" INTO="" FORCE="0"
  while [ $# -gt 0 ]; do
    case "$1" in
      verify)   MODE="verify"; shift ;;
      restore)  MODE="restore"; shift ;;
      --latest) USE_LATEST="1"; shift ;;
      --key)    KEY="$2"; USE_LATEST="0"; shift 2 ;;
      --into)   INTO="$2"; shift 2 ;;
      --force)  FORCE="1"; shift ;;
      *) echo "Usage: vps-restore.sh [verify|restore] [--latest|--key <s3-key>] [--into <db>] [--force]" >&2; exit 2 ;;
    esac
  done

  log() { logger -t beacon-restore -- "$*" 2>/dev/null || true; echo "[beacon-restore] $*"; }

  [ -z "$DB_USER" ]   && { log "ERROR: MySQL user 未設定 (/etc/beacon/db.env)"; exit 2; }
  [ -z "$S3_BUCKET" ] && { log "ERROR: S3 バケット未設定 (/etc/beacon/backup.env)"; exit 2; }

  # 復旧元キーを決める。
  if [ "$USE_LATEST" = "1" ]; then
    # aws s3 ls は日時順で列挙するので末尾が最新。キーだけ取り出す。
    KEY=$(aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/${ENV_NAME}/" \
            | awk 'NF{print $NF}' | grep -E '\.sql\.gz$' | tail -n1 || true)
    [ -z "$KEY" ] && { log "ERROR: s3://${S3_BUCKET}/${S3_PREFIX}/${ENV_NAME}/ に退避物が無い"; exit 1; }
    KEY="${S3_PREFIX}/${ENV_NAME}/${KEY}"
  fi
  local SRC="s3://${S3_BUCKET}/${KEY}"

  # 流し込み先 DB を決める。
  local TARGET_DB
  if [ "$MODE" = "verify" ]; then
    TARGET_DB="${DB_NAME}_restore_check"
  else
    TARGET_DB="${INTO:-$DB_NAME}"
    if [ "$TARGET_DB" = "$DB_NAME" ] && [ "$FORCE" != "1" ]; then
      log "ERROR: 本番 DB '${DB_NAME}' への上書き復旧は --force 必須 (事故防止)"
      exit 2
    fi
  fi

  log "復旧(${MODE}): ${SRC} → ${TARGET_DB}@${DB_HOST}:${DB_PORT}"

  # verify モードは検証用 DB を作り直してから流す (本番 DB は不可侵)。
  if [ "$MODE" = "verify" ]; then
    MYSQL_PWD="$DB_PASS" mysql --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" \
      -e "DROP DATABASE IF EXISTS \`${TARGET_DB}\`; CREATE DATABASE \`${TARGET_DB}\`;"
  fi

  # S3 → gunzip → mysql をパイプ直結。dump 内の CREATE DATABASE/USE を無視して
  # TARGET_DB へ流すため、--one-database で対象を固定する。
  # errexit を一時解除して PIPESTATUS で 3 段の成否を自前判定 (vps-backup.sh と同型)。
  set +e
  aws s3 cp "$SRC" - --only-show-errors \
    | gunzip -c \
    | MYSQL_PWD="$DB_PASS" mysql --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" \
        --one-database "$TARGET_DB"
  local st=("${PIPESTATUS[@]}")
  set -e
  if [ "${st[0]}" -ne 0 ] || [ "${st[1]}" -ne 0 ] || [ "${st[2]}" -ne 0 ]; then
    log "ERROR: 復旧失敗 (aws=${st[0]} gunzip=${st[1]} mysql=${st[2]})"
    exit 1
  fi

  # verify モードは主要テーブルの行数を出して「戻った」ことを可視化する。
  if [ "$MODE" = "verify" ]; then
    log "復旧検証: ${TARGET_DB} のテーブル別行数 —"
    MYSQL_PWD="$DB_PASS" mysql --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" \
      -N -e "SELECT table_name, table_rows FROM information_schema.tables \
             WHERE table_schema='${TARGET_DB}' ORDER BY table_name;" \
      | while read -r name rows; do echo "  ${name}: ${rows}"; done
    log "検証完了。検証用 DB を残さない場合: DROP DATABASE \`${TARGET_DB}\`;"
  fi

  log "復旧成功: ${SRC} → ${TARGET_DB}"
}

main "$@"
