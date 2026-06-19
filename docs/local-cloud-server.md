# ローカルで Beacon Cloud サーバを動かす

本番 (Google Cloud Run) と同じ FastAPI サーバを、クラウド認証情報なしでローカルに丸ごと立てるための手順です。動作確認・開発・デバッグに使います。

データは DynamoDB Local のディスクボリュームに永続化されるため、`docker compose down` してもプロジェクトは残ります。

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

停止（データは残ります）:

```bash
docker compose down
```

データもまるごと破棄してまっさらに戻したいとき:

```bash
docker compose down -v   # -v で永続ボリュームも削除
```

## 仕組み

`docker-compose.yml` は 3 つのサービスを立てます。

| サービス | 役割 |
|----------|------|
| `dynamodb-local` | `amazon/dynamodb-local`。データを named volume (`dynamodb-data`) にディスク永続化する DB |
| `dynamodb-init` | 起動時にテーブル群を作成する使い捨てサービス（冪等。既存テーブルはスキップ） |
| `beacon-server` | 既存 `Dockerfile` の FastAPI サーバ（本番 Cloud Run と同一コード） |

クラウド依存を外すために、サーバへ次の環境変数を渡しています。

| 環境変数 | 値 | 意味 |
|----------|----|------|
| `BEACON_API_AUTH` | `1` | 認証は有効。ログインは下記のローカル開発ログインで行う（Firebase / Cognito 不要） |
| `BEACON_LOCAL_DEV` | `1` | IdP 不要のローカル開発ログイン（`/api/auth/dev-login`）を有効化。本番では絶対に設定しない |
| `BEACON_STORE_BACKEND` | `dynamodb` | ストレージバックエンド。ローカル検証は DynamoDB Local 経由で永続化する |
| `BEACON_DYNAMODB_ENDPOINT` | `http://dynamodb-local:8000` | `dynamodb_client.py` がこの値を `endpoint_url` に使い、実 AWS ではなくローカル DB に繋ぐ |
| `BEACON_ENV` | `dev` | テーブル接頭辞。`dev` → `beacon-dev-projects` / `beacon-dev-users` … |
| `AWS_REGION` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | `ap-northeast-1` / `local` / `local` | boto3 用のダミー値。DynamoDB Local は値を検証しないが、未設定だと boto3 が起動できないため固定値を渡す |

> **注意**: このスタックはローカル開発専用です。インターネットに公開しないでください。

### 永続化の仕組み

- `dynamodb-local` は `-sharedDb -dbPath /data` で起動し、`/data` に named volume をマウントしています。
- `docker compose down` ではコンテナだけ消え、ボリューム（＝データ）は残ります。
- `docker compose down -v` で初めてボリュームも消えます。

> 補足: 以前は Firestore エミュレータを使っていましたが、エミュレータはメモリ専用で
> `down` のたびにデータが消え、`export-on-exit` も Docker のシグナル経路と相性が悪く
> 永続化できませんでした（gcloud / cloud_firestore_emulator のラッパーが SIGINT/SIGTERM を
> Java 本体へ転送しない）。そのため Beacon 正式サポートの DynamoDB バックエンドへ
> 切り替えました（task e-1987）。本番 (Cloud Run) は引き続き Firestore 経路です。
> 両バックエンドは `server/store.py` 経由で同じ public API を提供するので、サーバコードは
> 無改修で両対応します（ローカル検証=DynamoDB / 本番=Firestore の対称構成）。

## ログイン（ローカル開発ログイン）

`BEACON_LOCAL_DEV=1` のとき、IdP なしで任意のメールアドレスに対して CLI トークンを発行できます。

```bash
curl -s -X POST http://localhost:8080/api/auth/dev-login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","name":"You"}'
```

`id_token` が返ります。これを `Authorization: Bearer <id_token>` で各 API に付けます。メールごとに別アカウント（owner / member）として扱われます。

## 動作確認

別ターミナルで:

```bash
curl http://localhost:8080/docs   # FastAPI の Swagger UI が返ればOK
```

データが永続化されることの確認:

```bash
docker compose up -d            # 起動してプロジェクトを 1 つ作る
docker compose down             # 停止（-v なし）
docker compose up -d            # 再起動 → さっきのプロジェクトが残っている
```

## CLI / Desktop から接続する

接続先 (`api_url`) をローカルサーバに向けると、CLI や Desktop からこのローカルサーバを叩けます。
`.beacon/cloud.json` の `api_url` を `http://localhost:8080` に変更してください
（本番設定を上書きしないよう、検証用プロファイルを分けることを推奨します）。

## テーブル定義の真値源

作成されるテーブルの一覧と PK / SK は `server/dynamodb_client.py` の `TABLES` と
`TABLE_KEY_SCHEMA` が単一の真値源です。`dynamodb-init` サービスが起動時に
`server/create_local_tables.py` を実行し、この定義から未作成のテーブルだけを作ります
（冪等なので毎回 `up` しても安全）。実 AWS 環境では同じ定義に沿って IaC でテーブルを
作成します。
