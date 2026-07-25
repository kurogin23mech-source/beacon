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

## 定期ティック (Trek/Operation 自律の駆動) — e-1391 / ms-66

Trek と Operation の server-side 自律発火は、`POST /api/system/trek-scheduler/tick`
を **定期的に叩く外部ドライバ** に依存する (endpoint 自体は Trek fanout に加えて
`lib/tick_scheduler` 経由で due な Operation も相乗り発火する = 単一 endpoint が
自律の全駆動)。Cloud Run 時代は Cloud Scheduler がこれを叩いていたが、VPS 移行
(ms-102/ms-96) でこのドライバが**無音で消失**し、本番で Trek/Operation が発火
しなくなっていた (2026-07-24 Trek review で発覚)。復旧のため systemd timer で叩く。

### セットアップ (VPS で 1 回だけ)

```bash
ssh ubuntu@beacon-ai.dev
cd /opt/beacon
git fetch && git reset --hard origin/main
chmod +x scripts/vps-tick.sh

# 内部認証キーを app.env に設定 (未設定だと app が起動拒否 = e-4115)。
#   openssl rand -hex 32 で生成し /etc/beacon/app.env に追記:
#   BEACON_SCHEDULER_INTERNAL_KEY=<生成した値>
# app と tick timer の双方がこの同じ app.env を読む。
sudo systemctl restart beacon-api.service    # 鍵反映のため再起動

# tick unit / timer を設置
sudo cp deploy/systemd/beacon-tick.service /etc/systemd/system/
sudo cp deploy/systemd/beacon-tick.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now beacon-tick.timer

# 動作確認
sudo systemctl start beacon-tick.service       # 手動で 1 回叩く
journalctl -t beacon-tick -n 20 --no-pager     # "tick ok (HTTP 200): {...}" を確認
curl -fsS http://127.0.0.1:8000/api/system/tick-health | python3 -m json.tool
```

### 死活監視 (out-of-box watchdog)

tick が**再び無音で止まる**のを検知するため、GitHub Actions cron
(`.github/workflows/deploy-health-monitor.yml`) が 10 分毎に
`GET /api/system/tick-health` を polling し、`status=stale` / `unreachable` なら
所有者へ beacon-bus DM で通知する (`scripts/tick-health-monitor.py` +
`lib/tick_health.py`)。deploy 鮮度監視の twin。box の外で走るので box 全体が
落ちても検知できる (自分の停止は自分で報告できない、が設計原則)。

- **一時停止**: `sudo systemctl disable --now beacon-tick.timer`。
- **間隔変更**: `beacon-tick.timer` の `OnUnitActiveSec` を編集 → daemon-reload。
- **ログ**: `journalctl -t beacon-tick` または `journalctl -u beacon-tick.service`。

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

## 必須アプリ env (`/etc/beacon/app.env`) — ms-96 e-3196 / e-3197

`beacon-api.service` が `EnvironmentFile=/etc/beacon/app.env` で読み込む
アプリレベルの env。**テンプレートは repo の `deploy/app.env.example`** にあり、
移設で「必須 env が無音欠落」する事故を防ぐため repo 側に固定してある。

| env | 必須 | 説明 |
|-----|------|------|
| `BEACON_OAUTH_CLIENT_ID` | ✅ (provider=firebase) | Web UI ログインの Google OAuth client_id (PUBLIC 値)。空だとログインボタンが消える。 |
| `BEACON_SCHEDULER_INTERNAL_KEY` | ✅ (SECRET) | 定期ティック内部認証キー (e-1391 / e-4115)。beacon-tick.timer と app が同値で照合。未設定だと prod は起動拒否 (refuse-to-boot)。`openssl rand -hex 32` で生成。**repo に実値を書かない。** → 下の「定期ティック」節参照。 |

**beacon-ai.dev 本番の実値** (PUBLIC 値、secret ではない):

```
BEACON_OAUTH_CLIENT_ID=192136550838-evgdpddgcpsim62jrc4bh77j6p9if716.apps.googleusercontent.com
```

この実値は runbook (この文書) と本番の `/etc/beacon/app.env` だけが持つ。repo の
`deploy/app.env.example` は placeholder を置くテンプレートで、固有値は焼き込まない
(= ms-105 e-3313)。別ドメインに展開する場合は、そのドメインを許可した OAuth アプリの
client_id に差し替えること。

### なぜ loud に落ちるのか (e-3197)

この値はソースにハードコードしていない (= silent fallback を作らないため)。
`BEACON_AUTH_PROVIDER=firebase` かつ `BEACON_ENV=prod` でこの env が空のとき、
`/health` は **503** を返す。`scripts/vps-pull-deploy.sh` の health check
(`curl -fsS`) が 503 を検出して deploy を **ERROR** にするので、ログイン不能が
「気付けない本番障害」ではなく「赤くなった deploy」として顕在化する。

### セットアップ手順

```bash
# repo テンプレート (必須 env の一覧) を元に本番 app.env を作成
sudo install -m 0644 /opt/beacon/deploy/app.env.example /etc/beacon/app.env
# テンプレは placeholder なので、上表の実値を /etc/beacon/app.env にセットする
# (別ドメインなら、そのドメインを許可した OAuth アプリの client_id を入れる)
sudo sed -i \
  's#^BEACON_OAUTH_CLIENT_ID=.*#BEACON_OAUTH_CLIENT_ID=192136550838-evgdpddgcpsim62jrc4bh77j6p9if716.apps.googleusercontent.com#' \
  /etc/beacon/app.env
sudo systemctl restart beacon-api
curl -fsS https://beacon-ai.dev/health   # 200 なら OK、503 なら env 欠落 (placeholder のまま等)
```

> ⚠ ハードコード default を撤去した変更 (e-3196/e-3197) を deploy する **前** に
> `/etc/beacon/app.env` へ `BEACON_OAUTH_CLIENT_ID` を入れておくこと。先に入れて
> おけば無停止で切り替わる。入れ忘れると (狙い通り) deploy が 503 で止まる。
