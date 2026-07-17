# VPS バックアップ運用 (MySQL → S3)

> ms-96 e-2382 / SPEC `f2ERhlUYJIrzytdBf7C5`。
> 1 台 VPS 構成で唯一許容できないのは **データ消失**。DB を箱の外 (S3) へ 6 時間毎に
> 退避し、そこから **戻せることまで確かめて** 初めて 1 台構成が成立する。

## 全体像

| 役割 | 実体 |
|------|------|
| 退避 (取る) | `scripts/vps-backup.sh` — `mysqldump \| gzip \| aws s3 cp -` をパイプ直結 (一時ファイルを作らない) |
| 復旧 / 検証 (戻す) | `scripts/vps-restore.sh` — `verify` (別 DB へ流して行数確認) / `restore` (実復旧) |
| 定期起動 | systemd `beacon-backup.timer` (6 時間毎) → `beacon-backup.service` (oneshot) |
| 保持 | S3 ライフサイクル `infra/backup/s3_lifecycle.json` (35 日で自動失効) |
| 設定 | `/etc/beacon/db.env` (MySQL 認証, 既存) + `/etc/beacon/backup.env` (S3 退避先, 新規) |

退避物のキー: `s3://<bucket>/mysql/<env>/beacon-<env>-<UTCタイムスタンプ>.sql.gz`

## セットアップ (VPS 上、一度きり)

### 1. S3 バケットと最小権限 IAM

退避専用ユーザーには **PutObject** (退避) と **GetObject / ListBucket** (復旧・検証) だけを与える。
オブジェクト削除権限は与えない (保持は下記ライフサイクルに一元化する)。

```
# バケット作成 (東京リージョン例)
aws s3api create-bucket --bucket beacon-prod-backups \
  --region ap-northeast-1 \
  --create-bucket-configuration LocationConstraint=ap-northeast-1

# バージョニング + 暗号化 (推奨)
aws s3api put-bucket-versioning --bucket beacon-prod-backups \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket beacon-prod-backups \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# 保持ポリシー (35 日で自動失効)
aws s3api put-bucket-lifecycle-configuration --bucket beacon-prod-backups \
  --lifecycle-configuration file:///opt/beacon/infra/backup/s3_lifecycle.json
```

IAM ポリシー (退避ユーザーにアタッチ):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::beacon-prod-backups/mysql/*" },
    { "Effect": "Allow", "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::beacon-prod-backups",
      "Condition": { "StringLike": { "s3:prefix": ["mysql/*"] } } }
  ]
}
```

### 2. `/etc/beacon/backup.env`

```ini
BEACON_BACKUP_S3_BUCKET=beacon-prod-backups
BEACON_BACKUP_S3_PREFIX=mysql
# 退避ユーザーの鍵 (上記 IAM ポリシーを付与したもの)
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=ap-northeast-1
```

`BEACON_ENV` は `beacon-api.service` と同じく `prod` を使う (`/etc/beacon/app.env` から
継承。timer 起動でも効くよう、必要なら backup.env にも `BEACON_ENV=prod` を書く)。
MySQL 認証は既存の `/etc/beacon/db.env` (`MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DB` …) を
そのまま共用する。退避専用に最小権限の MySQL ユーザー (`SELECT, LOCK TABLES, SHOW VIEW,
TRIGGER, EVENT` のみ) を作って db.env とは別に渡してもよい。

### 3. systemd timer を有効化

```bash
cd /opt/beacon && git fetch && git reset --hard origin/main   # 本 PR を含む main
chmod +x scripts/vps-backup.sh scripts/vps-restore.sh
sudo cp deploy/systemd/beacon-backup.service /etc/systemd/system/
sudo cp deploy/systemd/beacon-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now beacon-backup.timer

# 動作確認
systemctl list-timers beacon-backup.timer
sudo systemctl start beacon-backup.service          # 手動で 1 回退避
journalctl -t beacon-backup -n 30 --no-pager
aws s3 ls s3://beacon-prod-backups/mysql/prod/       # 退避物が積まれているか
```

## 復旧検証 (受入条件: 実機で戻せることを確かめる)

**本番 DB に触れずに** 最新退避物を検証用 DB へ流し込み、行数を確認する:

```bash
cd /opt/beacon
./scripts/vps-restore.sh verify --latest     # → beacon_restore_check に流して行数表示
```

出力の各テーブル行数が本番と概ね一致すれば「退避物から戻せる」ことが確認できる。
検証用 DB を残さない場合:

```bash
MYSQL_PWD="$MYSQL_PASSWORD" mysql -u "$MYSQL_USER" \
  -e "DROP DATABASE IF EXISTS beacon_restore_check;"
```

## 実際の災害復旧 (本番 DB が壊れた/消えたとき)

```bash
cd /opt/beacon
sudo systemctl stop beacon-api.service                # 書き込みを止める
./scripts/vps-restore.sh restore --latest --into beacon --force
sudo systemctl start beacon-api.service
curl -fsS https://beacon-ai.dev/health
```

特定世代へ戻す場合は `--key mysql/prod/beacon-prod-<TS>.sql.gz` を指定する
(`aws s3 ls s3://beacon-prod-backups/mysql/prod/` で世代一覧)。

## 運用メモ

- **失敗は無音にしない**: 退避スクリプトは `mysqldump | gzip | aws` の 3 段を PIPESTATUS で
  全て検査し、どれか失敗したら非ゼロ終了する (壊れた/空のダンプを「成功」と記録しない)。
  `beacon-backup.service` の失敗は `journalctl -u beacon-backup.service` と
  `systemctl status beacon-backup.timer` で拾える。監視 (`deploy-health-monitor` 系) に
  「直近 6 時間で退避成功があるか」を足すのは follow-up。
- **間隔変更**: `beacon-backup.timer` の `OnCalendar` を編集 → `daemon-reload`。
- **保持世代**: `infra/backup/s3_lifecycle.json` の `Expiration.Days` を編集して再適用。
- **一時停止**: `sudo systemctl disable --now beacon-backup.timer`。
