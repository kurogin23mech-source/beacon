# Beacon 仕様書

## 概要

Beacon は、AI駆動開発においてマイルストーンベースのプロジェクト進捗を常時可視化し、開発者が方向性を見失わずに実装を進められるツール。

### 設計思想

- **監査可能性**: AI間のセッション引き継ぎを透明にし、人間が進捗を追跡・監査できる
- **マイルストーン駆動**: 全ての作業はマイルストーンに紐づき、進捗が一目でわかる
- **ツール主導**: Claude Code の操作はプロンプト指示ではなく、固定化されたツール（CLI）経由で行う。判断が必要なステップのみ Claude に生成を委譲する

## アーキテクチャ

```
beacon (bin/beacon)                   - CLI エントリポイント (bash)
lib/commands.py                       - サブコマンド実装 (Python)
lib/core.py                           - 純粋なビジネスロジック (バリデーション, CRUD)
lib/dashboard.py                      - tmux左ペイン用リアルタイムダッシュボード (Python/curses)
lib/store.py                          - ストレージ抽象化 (Protocol + ファクトリ)
lib/store_local.py                    - ローカルJSONファイルバックエンド
lib/store_api.py                      - クラウドAPIバックエンド (HTTP + WebSocket)
lib/api_client.py                     - クラウドAPI用HTTPクライアント
lib/ws_client.py                      - WebSocketクライアント (標準ライブラリのみ)
lib/auth.py                           - Google OAuth認証
server/app.py                         - FastAPIクラウドAPIサーバー
server/firestore_client.py            - API用Firestoreラッパー
desktop/                              - Tauriデスクトップアプリ (Rust + Web UI)
.beacon/project.json                  - プロジェクト状態ファイル (JSON)
~/.claude/skills/beacon-*/SKILL.md    - Claude Code 用 Skill 定義
```

### レイヤー構成

```
┌─────────────────────────────────────────┐
│  Skill（最外層・薄いラッパー）            │
│  beacon-session-start / beacon-log /     │
│  beacon-task / beacon-session-end        │
├─────────────────────────────────────────┤
│  beacon CLI（ワークフロー制御）           │
│  bin/beacon + lib/commands.py            │
│  - CRUD操作（確定的処理）                │
│  - --prepare: 判断材料をJSON出力          │
│  - --finalize: 生成結果を受けて書き込み   │
├─────────────────────────────────────────┤
│  Store抽象化 (lib/store.py)              │
│  - StoreLocal: .beacon/project.json      │
│  - StoreApi: クラウドAPI + WebSocket      │
│  .beacon/config.json でモード選択         │
└─────────────────────────────────────────┘
```

**原則**: Skill は prepare の実行と finalize への橋渡しのみを行う。ビジネスロジックはツール側が持つ。ストレージ層は透過的 — CLIコマンドはローカルモードでもクラウドモードでも同一の動作をする。

## 起動フロー

### 重要: Claude Code との併用

Beacon の tmux ダッシュボードと Claude Code を同時に使う場合、以下の順序で起動する:

1. ターミナルで `beacon` を実行 → tmux セッションが起動（左: ダッシュボード、右: シェル）
2. 右ペイン（作業用シェル）で `claude` を起動

**注意**: Claude Code 内から `! beacon`（引数なし）を実行してはならない。`tmux attach-session` が Claude Code のプロセスを破壊する。

Claude Code 内からステータスを確認したい場合は `! beacon status` を使う。

## CLI コマンド

### milestone サブコマンド

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon milestone add "タイトル" [-d 目標日] [--description "説明"]` | マイルストーンを追加 | - |
| `beacon milestone list` | マイルストーン一覧 | - |
| `beacon milestone start <id>` | マイルストーンをアクティブに設定 | - |
| `beacon milestone done <id>` | マイルストーンを完了に設定 | - |
| `beacon milestone close <id>` | マイルストーンをクローズ（進捗保持） | - |
| `beacon milestone observe <id>` | マイルストーンを監視中に設定 | - |
| `beacon milestone show <id>` | 単一マイルストーンの詳細 | 対応済 |
| `beacon milestone update <id> [opts]` | 任意フィールドを更新 | 対応済 |
| `beacon milestone delete <id>` | 論理削除（cancelled） | 対応済 |
| `beacon milestone depends <id> --on <id>[,id]` | 依存関係を設定 | 対応済 |
| `beacon milestone depends <id> --clear` | 依存関係を解除 | 対応済 |
| `beacon milestone graph` | 依存グラフを表示（wave配置） | 対応済 |

### task サブコマンド

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon task add "説明" [-m ms-id] [-t type] [-d detail]` | エントリを追加 | - |
| `beacon task done <entry-id> [-p progress]` | エントリを完了に設定 | - |
| `beacon task list [-m ms-id]` | エントリ一覧 | 対応済 |
| `beacon task show <entry-id>` | エントリ詳細 | 対応済 |
| `beacon task detail <entry-id> [text]` | detailの表示/更新 | - |
| `beacon task update <id> [opts]` | 任意フィールドを更新 | 対応済 |
| `beacon task delete <id>` | 論理削除（cancelled） | 対応済 |

### doc サブコマンド

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon doc add "タイトル" [--scope scope] [--id slug] [--content text]` | ドキュメントを追加 | 対応済 |
| `beacon doc list [--scope scope]` | ドキュメント一覧 | 対応済 |
| `beacon doc show <doc-id>` | ドキュメント内容表示 | 対応済 |
| `beacon doc update <doc-id> --content "text"` | ドキュメント内容更新 | 対応済 |

ドキュメントスコープ: `core`（設計原則・常時参照）, `spec`（仕様・技術詳細）, `memo`（検討メモ・揮発してもよい情報）

stdinからコンテンツを渡す場合: `echo 'content' | beacon doc add "タイトル" --scope spec --stdin`

### save サブコマンド（非コミット行為の記録）

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon save "説明" -m ms-id [--source src]` | 非コミット行為を記録 | 対応済 |
| `beacon save "説明" -m ms-id --hash <hash>` | 関連コミットに紐づけて記録 | 対応済 |
| `beacon save "説明" --source google_docs --url "..."` | 外部リソース付きで記録 | 対応済 |

`save` タイプは、git commit以外の行為（ドキュメント作成、データ分析、調査など）をマイルストーンの証跡として記録する。`--hash` を指定すると関連コミットとの紐づけが可能になり、1コミットが複数MSに影響した場合のトラッキングに使える。

重複検出: `source` + (`url` または `revision_id`) の組み合わせで判定。`source=manual` は重複チェックをスキップする。

### ログ・同期

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon log [message] [-m ms-id] [-p progress]` | HEAD コミットを記録 | 対応済 |
| `beacon log --prepare` | 進捗評価用の判断材料をJSON出力（書き込みしない） | 対応済 |
| `beacon log --finalize [--progress N] [--summary text]` | 評価結果を受けて書き込み | 対応済 |
| `beacon sync` | 直近 git コミットをアクティブMSに同期 | - |
| `beacon summary [text]` | サマリーの表示/更新 | 対応済 |

### pr サブコマンド

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon pr create [-m ms-id] [--intent "text"] [gh フラグ...]` | `gh pr create` を実行しPRを自動記録 | - |
| `beacon pr add <github-url> [-m ms-id] [--intent "text"]` | 既存PRをbeaconに登録 | 対応済 |
| `beacon pr approve <entry-id> [--rationale "text"]` | PRを承認（rationale必須） | 対応済 |
| `beacon pr request-changes <entry-id> [--rationale "text"]` | 修正依頼 | 対応済 |
| `beacon pr reject <entry-id> [--rationale "text"]` | PRを却下 | 対応済 |
| `beacon pr merge <entry-id>` | マージ済みに設定 | 対応済 |
| `beacon pr close <entry-id>` | マージなしでクローズ | 対応済 |

AIコードレビューには `beacon pr review` ではなく `/review` Claude Code Skill を使う。

### deploy サブコマンド

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon deploy record [--revision <rev>] [--semver <v>] [--desc "text"]` | デプロイを記録（major/minor自動判定） | 対応済 |
| `beacon deploy record --prepare` | デプロイ判断材料をJSON出力（書き込みしない） | 対応済 |
| `beacon deploy record --finalize --desc "text" [--semver v]` | AI生成説明を書き込み | 対応済 |
| `beacon deploy list` | デプロイ履歴一覧 | 対応済 |

**major/minor の自動判定**:
- **major**: 前回デプロイ以降に1つ以上のマイルストーンが新たに完了した場合
- **minor**: 既完了MSへのバグ修正・ホットフィックス（新規MS完了なし）

### 振り返り

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon retro [--since DATE] [--until DATE]` | 週次振り返りデータ生成 | - |
| `beacon retro done` | 振り返りをレビュー済みにする | - |

### トリガー

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon trigger fire <name> [message]` | トリガーを発火（ダッシュボードから使用） | - |
| `beacon trigger check` | 未処理トリガーを確認 | 対応済 |
| `beacon trigger clear <name>` | トリガーを消化 | - |

### クラウド・認証

| コマンド | 説明 |
|---------|------|
| `beacon auth login` | Googleログイン |
| `beacon auth logout` | 認証情報を削除 |
| `beacon auth status` | ログイン状態を表示 |
| `beacon cloud push` | プロジェクトをクラウドにアップロード（クラウドモードに自動切替） |
| `beacon cloud pull` | クラウドからプロジェクトをダウンロード |
| `beacon cloud list` | クラウドプロジェクト一覧 |
| `beacon cloud [project-id]` | クラウドプロジェクトを開く（対話選択またはID指定） |
| `beacon cloud status` | クラウド設定を表示 |
| `beacon cloud off` | ローカルモードに戻す |

### その他コマンド

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon` | tmux ダッシュボード + シェルを起動 | - |
| `beacon init` | `.beacon/` をカレントディレクトリに初期化 | - |
| `beacon status` | プロジェクト全体の状態 | 対応済 |
| `beacon search <query> [-m ms-id]` | マイルストーン・エントリ・PR全文検索 | 対応済 |
| `beacon entry move <entry-id> -t <task-id>` | エントリをタスク配下に移動 | - |
| `beacon help` | ヘルプ表示 | - |
| `beacon --version` | バージョン表示 | - |

### 共通オプション

| オプション | 短縮形 | 説明 |
|-----------|--------|------|
| `--json` | - | JSON形式で出力 |
| `--ms <id>` | `-m` | 対象マイルストーンを指定 |
| `--progress <N>` | `-p` | 進捗率（0-100） |
| `--type <type>` | `-t` | エントリタイプ |
| `--detail <text>` | `-d` | 詳細テキスト |
| `--task <id>` | `-t` | 移動先タスクID（entry move） |
| `--all` | `-a` | cancelledを含む全件表示 |

## データモデル

### プロジェクト状態 (.beacon/project.json)

```json
{
  "name": "プロジェクト名",
  "objective": "大目的",
  "summary": "現在の状況（定性的な背景・経緯・判断）",
  "milestones": [
    {
      "id": "ms-1",
      "title": "マイルストーンタイトル",
      "description": "説明（任意）",
      "status": "todo | in_progress | done | observing | cancelled",
      "progress": 0,
      "target_date": "YYYY-MM-DD | null",
      "depends_on": ["ms-2", "ms-3"],
      "workspace": "ワークスペース識別子（任意）",
      "entries": [
        {
          "id": "e-1",
          "type": "commit | task | save | note | pr",
          "description": "エントリの説明",
          "date": "YYYY-MM-DDThh:mm:ssZ",
          "created_at": "YYYY-MM-DDThh:mm:ssZ",
          "done_at": "YYYY-MM-DDThh:mm:ssZ | null",
          "status": "todo | in_progress | in_review | waiting | done | cancelled",
          "detail": "詳細テキスト（任意）",
          "meta": {
            "hash": "(commit/save) 7文字短縮ハッシュ",
            "message": "(commit) コミットメッセージ",
            "source": "(save) manual | google_docs | notion | ...",
            "url": "(save/pr) 外部リソースURL",
            "revision_id": "(save, 任意) 外部システムの識別子",
            "pr_number": "(pr) GitHub PR番号",
            "author": "(pr) GitHubユーザー名",
            "pr_status": "(pr) in_review | approved | merged | closed",
            "review_status": "(pr) pending | changes_requested | approved | rejected",
            "intent": "(pr) このPRを作った理由・意図",
            "review_rationale": "(pr) 承認/却下の根拠"
          },
          "entries": [
            "(ネストされた子エントリ。タスクやPR配下のコミット等)"
          ]
        }
      ]
    }
  ],
  "deployments": [
    {
      "id": "deploy-20260517-1",
      "type": "major | minor",
      "date": "2026-05-17T12:00:00Z",
      "environment": "prod",
      "git_hash": "abc1234",
      "commit_hashes": ["abc1234", "def5678"],
      "description": "AIが生成したデプロイ説明",
      "newly_completed_ms": ["ms-5"],
      "patch_ms": [],
      "milestones": ["ms-5"],
      "milestone_commits": {"ms-5": ["abc1234"]},
      "linked_release": "release-20260517-1 | null",
      "unassigned_commits": []
    }
  ],
  "releases": [
    {
      "id": "release-20260517-1",
      "date": "2026-05-17",
      "milestones": ["ms-5"],
      "semver": "v1.2.0 | null",
      "description": "リリース説明",
      "deploy_ids": ["deploy-20260517-1"]
    }
  ]
}
```

### ドキュメント (.beacon/documents/)

ドキュメントはYAMLフロントマター付きのMarkdownファイル:

```yaml
---
scope: core
---
# ドキュメントタイトル

Markdownで記述した内容。
```

クラウドモードではAPI経由で保存され、push/pullで同期される。

### クラウド設定 (.beacon/cloud.json)

```json
{
  "project_id": "project-slug-abc123",
  "api_url": "https://beacon-ai.dev"
}
```

### モード設定 (.beacon/config.json)

```json
{
  "mode": "cloud"
}
```

`mode` が `cloud` の場合、全CLIコマンドはクラウドAPI経由で動作する。`local`（デフォルト）の場合は `.beacon/project.json` を直接読み書きする。

### ディレクトリ構成

```
.beacon/
  project.json    # プロジェクト状態（マイルストーン、エントリ、サマリー）
  config.json     # モード設定（local/cloud）
  cloud.json      # クラウドプロジェクト紐付け（project_id, api_url）
  documents/      # プロジェクトドキュメント（フロントマター付きMarkdown）
  retro/          # 週次振り返りドキュメント
  triggers/       # 非同期メッセージキュー（ダッシュボード ↔ Claude Code）
```

### ID命名規則

| 対象 | 形式 | 例 |
|------|------|---|
| マイルストーン | `ms-{連番}` | ms-1, ms-2, ms-8 |
| エントリ | `e-{連番}` | e-1, e-22, e-39 |

連番はプロジェクト内でグローバルにユニーク。マイルストーン追加時に既存の最大値+1、エントリ追加時に全MS横断で最大値+1を採番する。

### ステータスライフサイクル

マイルストーン:
```
todo → in_progress → done
     ↘ observing     ↗
                   ↘ cancelled（論理削除）
```

エントリ:
```
todo → in_progress → done
                   ↘ cancelled（論理削除）
```

追加のエントリステータス: `in_review`, `waiting`（ワークフロー追跡用）

cancelled のエントリ/マイルストーンは `list` のデフォルト表示から除外される。`--all` フラグで表示可能。

### summary の役割

`summary` はタスクリストを見ればわかる情報（進捗率、アクティブMS名など）を書かない。

記載すべきこと:
- なぜ今のタスクに取り組んでいるのか
- どういう経緯でこうなったか
- 次セッションで知っておくべき背景や判断

## Skill（Claude Code 統合）

### 概要

beacon の Skill は Claude Code が beacon を操作するためのインターフェース。グローバル（`~/.claude/skills/`）に配置し、`.beacon/project.json` の有無で発火を制御する。

### ツール主導・Claude生成組み込みアーキテクチャ

```
従来: Skill のプロンプト → Claude が判断 → CLI を叩く（ブレやすい）
採用: CLI がワークフロー制御 → 特定ステップで Claude に生成を要求 → 結果を書き込み
```

**2段階呼び出し**:
1. `beacon log --prepare`（または `beacon deploy record --prepare`）: 判断材料をJSONで出力。書き込みしない。
2. Skill が Claude に固定テンプレートで進捗評価またはデプロイ説明の生成を指示。
3. `beacon log --finalize --progress N --summary "text"`（または `beacon deploy record --finalize --desc "text"`）: 生成結果を project.json に書き込み。

これにより、CLAUDE.md のプロンプト指示がスルーされる問題が構造的に解消される。

### Skill 一覧

| Skill | トリガー | 責務 | 書き込み |
|-------|---------|------|---------|
| `beacon-session-start` | セッション開始, `/beacon-start` | 現状把握・提示 | なし（読み取り専用） |
| `beacon-log` | PostToolUse hook（コミット時自動）, `/beacon-log` | コミット記録+進捗評価+summary更新 | あり（finalize経由） |
| `beacon-task` | `/beacon-task` | タスク操作（add/done/update/delete） | あり |
| `beacon-session-end` | ユーザーの終了シグナル, Claude自身の提案前, `/beacon-end` | summary更新+未完了整理 | あり |
| `beacon-deploy` | PostToolUse hook（デプロイ時自動）, `/beacon-deploy` | AI説明付きデプロイ記録 | あり（finalize経由） |
| `beacon-retro` | `/beacon-retro`, 週次トリガー | 週次振り返りドキュメント生成・ディスカッション | あり |
| `beacon-dispatch` | `/beacon-dispatch`, 並列実装依頼 | 実行可能MSを特定し並列サブエージェントを起動 | なし（オーケストレーションのみ） |

### Skill の制約

- データ取得は必ず `beacon` CLI の `--json` 出力を使う。`.beacon/project.json` を Read ツールで直接読まない。
- これにより、将来バックエンドAPI に差し替えた際に Skill の変更が不要になる。

## ダッシュボード (lib/dashboard.py)

- tmux 左ペインで常時表示
- **ローカルモード**: `project.json` のファイルハッシュでポーリング
- **クラウドモード**: WebSocketプッシュでリアルタイム更新（切断時はスロットルHTTPポーリングにフォールバック）
- 変更検出時に自動再描画
- 3つの表示モード: プロジェクト（デフォルト）、振り返り、ドキュメント

### キーボードショートカット

| キー | アクション |
|------|----------|
| `j` / `↓` | 下移動 / スクロール |
| `k` / `↑` | 上移動 / スクロール |
| `Enter` / `Space` | 展開・折りたたみ（プロジェクト） / ドキュメント選択（ドキュメント） |
| `d` | 完了エントリの表示切替（プロジェクト） |
| `s` | サマリーの展開・折りたたみ（プロジェクト） |
| `D` | ドキュメントビューの切替 |
| `r` | 振り返りビューの切替 |
| `h` / `ESC` / `←` | 戻る（ドキュメント詳細 → 一覧） |
| `q` | 終了（tmuxセッションを閉じる） |

## tmux セッション構成

```
+------------------+----------------------------------------+
| Dashboard (33%)  |  Working Shell (67%)                   |
| (dashboard.py)   |  ← ここで claude を起動               |
+------------------+----------------------------------------+
```

セッション名: `beacon-<ディレクトリパスハッシュ先頭8文字>`

## マルチユーザー・クラウド

### ロール

| ロール | 読み取り | 書き込み | メンバー管理 |
|--------|---------|---------|-------------|
| owner | 可 | 可 | 可 |
| editor | 可 | 可 | 不可 |
| viewer | 可 | 不可 | 不可 |

### 認証

- `beacon auth login` でGoogle OAuth認証
- 認証情報は `~/.beacon/credentials.json` に保存
- APIリクエストごとにトークンを自動リフレッシュ

### クラウドAPI

- Cloud Run上にデプロイされたFastAPIサーバー
- Firestoreでプロジェクトデータを永続化
- WebSocketエンドポイントでダッシュボードにリアルタイム通知
- 全書き込みエンドポイントにロールベース認可
