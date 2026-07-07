# VPS 自動デプロイ (ms-102) — Pull 型

`main` が進んだら Kagoya VPS 上の Beacon API が **自分で** 追従して更新される
Pull 型デプロイのセットアップと運用メモ。

ms-96 でバックエンドを Cloud Run から VPS 1 台構成へ移したあと、デプロイ (= 本番
反映) が手作業のままだった。それを自動化する。

## なぜ Push 型 (GitHub Actions が SSH) ではなく Pull 型か

beacon リポジトリは **public (OSS)**。Push 型だと GitHub Actions に VPS の SSH 鍵を
Secret として置く必要がある。Secret 自体は暗号化され public repo でも露出しない
(fork PR にも渡らない) が、**「public repo に本番サーバへの鍵を預ける」構図そのもの**を
避けたい。

Pull 型は VPS 側が main を polling して self-update するので:

- **GitHub 側に Secret も SSH 鍵設定も一切不要** (public repo を匿名 fetch するだけ)
- **インバウンド接続不要** (VPS が外へ出るだけ。CI から VPS へ入らない)
- 漏れうる秘密がゼロ

トレードオフはデプロイの即時性で、**最大で timer 間隔 (既定 2 分) の遅延**が出る。

## 本番サーバの構成 (2026-07 時点)

| 項目 | 値 |
|---|---|
| ホスト | `srv1.beacon-ai.dev` (`133.18.161.232`, Kagoya VPS) |
| OS / ユーザー | Ubuntu 24.04 LTS / `ubuntu` (パスワード無し sudo 可) |
| リポジトリ | `/opt/beacon` (ubuntu 所有, origin=GitHub public, branch=main) |
| Python venv | `/opt/beacon/.venv` |
| サービス | systemd `beacon-api.service` |
| 起動 | `/opt/beacon/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000` |
| 設定 | `/etc/beacon/db.env` + `/etc/beacon/app.env` (`BEACON_STORE_BACKEND=mysql`, `BEACON_ENV=prod`) |
| データ | MySQL (127.0.0.1:3306) + Redis (127.0.0.1:6379) |
| 公開 | Caddy が :80/:443 → uvicorn:8000 にリバースプロキシ |

GitHub 認証情報はサーバに **一切無い** (public リポの匿名 fetch のみ = 他リポへ到達不可)。

## 仕組み

- `scripts/vps-pull-deploy.sh` — repo 内のデプロイスクリプト。`git fetch` して
  `origin/main` が現在 HEAD より進んでいれば `git reset --hard` → (requirements が
  変わっていれば) `pip install` → `sudo systemctl restart beacon-api.service` →
  `/health` ポーリング。変化がなければ fetch だけして即終了。
- `deploy/systemd/beacon-deploy.service` / `.timer` — 上記を 2 分間隔で oneshot 起動する
  systemd timer。VPS の `/etc/systemd/system/` に設置する。

スクリプトは repo 内にあるので、`git reset` で **スクリプト自身も自動更新される**
(self-update)。全ロジックを `main()` に包んであるため、実行中に本体が差し替わっても
走っている処理は壊れない (次回起動から新版が効く)。

## セットアップ (VPS で 1 回だけ)

```bash
ssh ubuntu@beacon-ai.dev
cd /opt/beacon
git fetch && git reset --hard origin/main      # 本 PR を含む main を取り込む
chmod +x scripts/vps-pull-deploy.sh

# systemd unit / timer を設置
sudo cp deploy/systemd/beacon-deploy.service /etc/systemd/system/
sudo cp deploy/systemd/beacon-deploy.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now beacon-deploy.timer

# 動作確認
systemctl status beacon-deploy.timer
sudo systemctl start beacon-deploy.service     # 手動で 1 回走らせる
journalctl -t beacon-deploy -n 30 --no-pager   # ログ確認
```

### 前提 (ms-96 構築時点で満たされている)

- `/opt/beacon` が ubuntu 所有で git clone 済 (origin=GitHub public, branch=main)
- `/opt/beacon/.venv` に Python venv
- ubuntu が `sudo systemctl restart beacon-api.service` をパスワード無しで実行可

## 運用

- **通常**: main に merge するだけ。最大 2 分で本番へ反映される。
- **即時反映したい**: `ssh ubuntu@beacon-ai.dev sudo systemctl start beacon-deploy.service`
  (timer を待たずに 1 回走らせる)。
- **ログ**: `journalctl -t beacon-deploy` (syslog tag) または `journalctl -u beacon-deploy.service`。
- **一時停止**: `sudo systemctl disable --now beacon-deploy.timer`。
- **間隔変更**: `beacon-deploy.timer` の `OnUnitActiveSec` を編集 → daemon-reload。

## 手動デプロイ (スクリプトを使わない緊急時)

```bash
ssh ubuntu@beacon-ai.dev
cd /opt/beacon
git fetch && git reset --hard origin/main
.venv/bin/pip install -r requirements.txt -r server/requirements.txt
sudo systemctl restart beacon-api.service
curl -fsS https://beacon-ai.dev/health
```

## 注意

- 更新対象は **サーバ (server/lib)** のみ。bridge 側 (`channel/bus.mjs`) は pip
  パッケージ経由で各ユーザーに配布される別経路 (リリース) で届く。
- `git reset --hard` はサーバ repo のローカル変更を破棄する。設定は `/etc/beacon` に
  あり repo 外なので影響しない (repo は常にクリーンな前提)。
- 旧 Cloud Run 経路 (`.github/workflows/deploy-cloud-run.yml`) は Cloud Run 廃止に伴い
  push トリガーを無効化済み (`workflow_dispatch` のみ残置)。

## systemd unit (beacon-api 本体 / 参照・復旧用)

`/etc/systemd/system/beacon-api.service`:

```ini
[Unit]
Description=Beacon API (FastAPI, MySQL backend) — ms-96 e-2378
After=network.target mysql.service redis-server.service
Wants=mysql.service redis-server.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/beacon/server
EnvironmentFile=/etc/beacon/db.env
EnvironmentFile=/etc/beacon/app.env
Environment=BEACON_STORE_BACKEND=mysql
Environment=BEACON_ENV=prod
ExecStart=/opt/beacon/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```
