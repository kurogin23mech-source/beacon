# ローカルで Beacon Cloud サーバを動かす

本番 (Google Cloud Run) と同じ FastAPI サーバを、クラウド認証情報なしでローカルに丸ごと立てるための手順です。動作確認・開発・デバッグに使います。

## 必要なもの

- Docker Desktop（起動済みであること）

それだけです。GCP / AWS のアカウントや認証情報は **不要** です。

## 起動

リポジトリのルートで:

```bash
docker compose up --build
```

- API サーバ: <http://localhost:8080>
- 初回はイメージビルドのため少し時間がかかります。

停止（データは破棄されます）:

```bash
docker compose down
```

## 仕組み

`docker-compose.yml` は 2 つのサービスを立てます。

| サービス | 役割 |
|----------|------|
| `beacon-server` | 既存 `Dockerfile` の FastAPI サーバ（本番 Cloud Run と同一イメージ） |
| `firestore-emulator` | Google 公式 Firestore エミュレータ。GCP に接続せずローカルで DB を提供 |

クラウド依存を外すために、サーバへ次の環境変数を渡しています。

| 環境変数 | 値 | 意味 |
|----------|----|------|
| `BEACON_API_AUTH` | `0` | 認証バイパス。全リクエストが `dev@local` ユーザーとして通る（Firebase / Cognito 不要） |
| `FIRESTORE_EMULATOR_HOST` | `firestore-emulator:8080` | Firestore SDK がこの値を自動検出し、GCP ではなくエミュレータへ接続する |
| `BEACON_GCP_PROJECT_ID` | `beacon-local` | エミュレータ利用時も必須のプロジェクト ID（ダミーで可） |
| `BEACON_STORE_BACKEND` | `firestore` | ストレージバックエンド（既定値） |
| `BEACON_ENV` | `dev` | コレクション接頭辞。`dev` → `projects-dev` / `users-dev` |

> **注意**: `BEACON_API_AUTH=0` は認証を完全に無効化します。ローカル開発専用です。インターネットに公開しないでください。

データはエミュレータのメモリ上に保持され、`docker compose down` で消えます（= 使い捨て）。

## 動作確認

別ターミナルで:

```bash
curl http://localhost:8080/docs   # FastAPI の Swagger UI が返ればOK
```

## CLI / Desktop から接続する

接続先 (`api_url`) をローカルサーバに向けると、CLI や Desktop からこのローカルサーバを叩けます。
`.beacon/cloud.json` の `api_url` を `http://localhost:8080` に変更してください
（本番設定を上書きしないよう、検証用プロファイルを分けることを推奨します）。

## 補足: DynamoDB バックエンドについて

現状このローカルスタックは Firestore エミュレータ経路のみ対応です。AWS DynamoDB 経路
（`BEACON_STORE_BACKEND=dynamodb`）をローカルで動かすには、`server/dynamodb_client.py` を
ローカルエンドポイント (`endpoint_url`) 指定に対応させる改修が別途必要です。
