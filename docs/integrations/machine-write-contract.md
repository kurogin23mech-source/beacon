# Machine 直書き契約 — 外部システムが Beacon に記録を書く (ms-151)

人が介在しない外部の常駐プログラム (例: PE detector Lambda) が、自分の **運転記録
(run_record)** と **異常記録 (incident)** を Beacon cloud へ直接書くための最小契約。
参照実装は [`beacon_sink.py`](./beacon_sink.py)。

## 1. 認証 — machine API key

人間のログイン (id_token) は machine では持てないので、project 単位に発行する
**machine API key** で認証する。

### 鍵の発行 (Beacon の owner が実施)

```
beacon machine-key issue --label "PE detector Lambda"
```

- 実行すると raw token が **一度だけ** 表示される (形式: `bmk.<project_id>.<key_id>.<secret>`)。
- サーバーは secret の hash しか保存しないので、**表示直後に安全な場所へ保存** すること
  (環境変数 / AWS Secrets Manager 等)。再取得はできない。
- 一覧: `beacon machine-key list` / 失効: `beacon machine-key revoke <key_id>`。

### リクエストヘッダ

```
Authorization: Bearer bmk.<project_id>.<key_id>.<secret>
Content-Type: application/json
```

- 失効した鍵 / 改竄された鍵 / 別 project の鍵は `401` で弾かれる。
- envelope (action 承認署名) は **不要**。記録は「起きた事実」であって外向き action
  ではないため。

## 2. Endpoint

base URL は Beacon cloud のもの (例: `https://beacon-ai.dev`)。`{project_id}` は鍵が
属する project。**別 project への書き込みは `403`**。

### 2.1 run_record (運転記録)

```
POST {base_url}/api/projects/{project_id}/operations/{op_id}/run-records
```

body:

| field | 型 | 必須 | 説明 |
|---|---|---|---|
| `batch` | string | ✓ | 実行バッチ名 (例 `nightly`) |
| `status` | string | ✓ | `ok` / `warning` / `error` のいずれか |
| `description` | string | | 補足 |
| `date` | string | | ISO8601。省略時は server 時刻 |

例:

```json
{ "batch": "nightly", "status": "ok", "description": "all detectors green" }
```

### 2.2 incident (異常記録)

```
POST {base_url}/api/projects/{project_id}/operations/{op_id}/incidents
```

body:

| field | 型 | 必須 | 説明 |
|---|---|---|---|
| `title` | string | ✓ | 異常のタイトル |
| `description` | string | | 詳細 |
| `priority` | string | | `highest`/`high`/`middle`/`low`/`lowest` (省略可) |
| `opened_at` | string | | ISO8601 発生時刻。省略時は server 時刻 |

例:

```json
{ "title": "latency spike", "description": "p99 > 2s for 5m", "priority": "high" }
```

## 3. レスポンス / エラー

- 成功: `200` + 作成された entry (JSON)。
- `401`: 鍵が無効 / 失効 / 改竄。
- `403`: 鍵が別 project のもの、または machine 認証でない (人間トークン)。
- `404`: `op_id` が project に存在しない。
- `400`: payload 不正 (`status` が ok/warning/error 以外、`priority` が不正値 等)。

## 4. PE detector Lambda の差し替え手順

1. Beacon owner が `beacon machine-key issue --label "PE detector"` で鍵を発行し、
   PE 側に安全な経路で渡す。
2. PE の既存 `beacon_sink` stub を [`beacon_sink.py`](./beacon_sink.py) の `BeaconSink`
   に差し替える。
3. Lambda 環境変数に `BEACON_BASE_URL` / `BEACON_MACHINE_KEY` / `BEACON_PROJECT_ID`
   を設定し、`BeaconSink` を組み立てて `write_run_record` / `write_incident` を呼ぶ。

```python
import os
from beacon_sink import BeaconSink

sink = BeaconSink(
    base_url=os.environ["BEACON_BASE_URL"],
    machine_key=os.environ["BEACON_MACHINE_KEY"],
    project_id=os.environ["BEACON_PROJECT_ID"],
)
sink.write_run_record("op-5", batch="nightly", status="ok")
```

## 5. 疎通確認

- コードレベルの疎通は `tests/test_beacon_sink_contract.py` が参照 sink → 実 endpoint
  (in-memory store) の往復で担保する。
- 実機疎通 (実際の PE Lambda → 本番 Beacon cloud) は PE 側で、発行した鍵と本番 base
  URL を用いて 1 回 `write_run_record` を投げて `200` + Beacon 側に記録が現れることを
  確認する。
