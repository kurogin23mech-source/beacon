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
lib/dashboard.py                      - tmux左ペイン用リアルタイムダッシュボード (Python/curses)
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
│  .beacon/project.json（データ層）         │
│  将来的にバックエンドAPI に差し替え可能    │
└─────────────────────────────────────────┘
```

**原則**: Skill は prepare の実行と finalize への橋渡しのみを行う。ビジネスロジックはツール側が持つ。

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
| `beacon milestone add "タイトル" [-d 目標日]` | マイルストーンを追加 | - |
| `beacon milestone list` | マイルストーン一覧 | - |
| `beacon milestone start <id>` | マイルストーンをアクティブに設定 | - |
| `beacon milestone done <id>` | マイルストーンを完了に設定 | - |
| `beacon milestone close <id>` | マイルストーンをクローズ（進捗保持） | - |
| `beacon milestone observe <id>` | マイルストーンを監視中に設定 | - |
| `beacon milestone show <id>` | 単一マイルストーンの詳細 | 対応済 |
| `beacon milestone update <id> [opts]` | 任意フィールドを更新 | 対応済 |
| `beacon milestone delete <id>` | 論理削除（cancelled） | 対応済 |

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

### その他コマンド

| コマンド | 説明 | --json |
|---------|------|--------|
| `beacon` | tmux ダッシュボード + シェルを起動 | - |
| `beacon init` | `.beacon/` をカレントディレクトリに初期化 | - |
| `beacon status` | プロジェクト全体の状態 | 対応済 |
| `beacon log [message] [-m ms-id] [-p progress]` | HEAD コミットを記録 | 対応済 |
| `beacon log --prepare` | 進捗評価用の判断材料をJSON出力（書き込みしない） | 対応済 |
| `beacon log --finalize [--progress N] [--summary text]` | 評価結果を受けて書き込み | 対応済 |
| `beacon sync` | 直近 git コミットをアクティブMSに同期 | - |
| `beacon summary [text]` | サマリーの表示/更新 | 対応済 |
| `beacon entry move <entry-id> -t <task-id>` | エントリをタスク配下に移動 | - |
| `beacon retro [--since DATE] [--until DATE]` | 週次振り返りデータ生成 | - |
| `beacon retro done` | 振り返りをレビュー済みにする | - |
| `beacon trigger fire <name> [message]` | トリガーを発火（ダッシュボードから使用） | - |
| `beacon trigger check` | 未処理トリガーを確認 | 対応済 |
| `beacon trigger clear <name>` | トリガーを消化 | - |

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

## データモデル (.beacon/project.json)

```json
{
  "name": "プロジェクト名",
  "objective": "大目的",
  "summary": "現在の状況（定性的な背景・経緯・判断）",
  "milestones": [
    {
      "id": "ms-1",
      "title": "マイルストーンタイトル",
      "status": "todo | in_progress | in_review | waiting | done | observing | cancelled",
      "progress": 0,
      "target_date": "YYYY-MM-DD | null",
      "entries": [
        {
          "id": "e-1",
          "type": "commit | task | note",
          "description": "エントリの説明",
          "date": "YYYY-MM-DD",
          "created_at": "YYYY-MM-DD",
          "done_at": "YYYY-MM-DD | null",
          "status": "todo | in_progress | done | cancelled",
          "detail": "詳細テキスト（任意）",
          "meta": {
            "hash": "(commit時) 7文字短縮ハッシュ",
            "message": "(commit時) コミットメッセージ"
          },
          "entries": [
            "(ネストされた子エントリ。タスク配下のコミット等)"
          ]
        }
      ]
    }
  ]
}
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
1. `beacon log --prepare`: 判断材料（MS状態、タスク消化率、直近エントリ等）をJSONで出力。書き込みしない。
2. Skill が Claude に固定テンプレートで「進捗率とサマリーを生成せよ」と指示。
3. `beacon log --finalize --progress N --summary "text"`: 生成結果を受けて project.json に書き込み。

これにより、CLAUDE.md のプロンプト指示がスルーされる問題が構造的に解消される。

### Skill 一覧

| Skill | トリガー | 責務 | 書き込み |
|-------|---------|------|---------|
| `beacon-session-start` | セッション開始, `/beacon-start` | 現状把握・提示 | なし（読み取り専用） |
| `beacon-log` | PostToolUse hook（コミット時自動）, `/beacon-log` | コミット記録+進捗評価+summary更新 | あり（finalize経由） |
| `beacon-task` | `/beacon-task` | タスク操作（add/done/update/delete） | あり |
| `beacon-session-end` | ユーザーの終了シグナル, Claude自身の提案前, `/beacon-end` | summary更新+未完了整理 | あり |

### Skill の制約

- データ取得は必ず `beacon` CLI の `--json` 出力を使う。`.beacon/project.json` を Read ツールで直接読まない。
- これにより、将来バックエンドAPI に差し替えた際に Skill の変更が不要になる。

## ダッシュボード (lib/dashboard.py)

- tmux 左ペインで常時表示
- `project.json` のファイルハッシュを 2 秒間隔でポーリング
- 変更検出時に自動再描画
- ツリー形式でマイルストーンとエントリを表示
- キーボード操作: j/k or ↑↓移動、Enter/Space展開/折りたたみ、d完了トグル、r振り返り表示切替、q終了

## tmux セッション構成

```
+------------------+----------------------------------------+
| Dashboard (33%)  |  Working Shell (67%)                   |
| (dashboard.py)   |  ← ここで claude を起動               |
+------------------+----------------------------------------+
```

セッション名: `beacon-<ディレクトリパスハッシュ先頭8文字>`

## 将来計画

### マルチユーザー対応（ms-6）

- マイルストーンごとにオーナーを設定
- PR駆動: PRのライフサイクル（作成→in_review→マージ→done）をエントリステータスにマッピング
- データ分割: マイルストーンごとに独立したファイル/APIリソース
- バックエンド: project.json をAPIに差し替え。CLI がデータアクセス層を抽象化。
