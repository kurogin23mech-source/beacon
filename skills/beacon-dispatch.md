---
name: beacon-dispatch
description: 依存グラフから実行可能なマイルストーンを特定し、サブエージェントを並列起動する。マルチエージェント協奏のオーケストレーター。
version: 0.1.0
triggers:
  - /beacon-dispatch
  - サブエージェントを起動
  - 並列で実行
  - dispatch
  - 次に実行可能なMSは
---

# Beacon Dispatch

> 依存グラフを解析し、実行可能なマイルストーンをサブエージェントに並列委譲する。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 1: 依存グラフの取得

Bash ツールで実行:
```bash
beacon milestone graph --json
```

stdout に JSON が返る:
```json
{
  "nodes": [
    {"id": "ms-X", "title": "...", "status": "...", "progress": N, "workspace": "...", "depends_on": [...]}
  ],
  "edges": [...],
  "waves": [
    {"wave": 1, "milestones": ["ms-1", "ms-2"]},
    {"wave": 2, "milestones": ["ms-3"]}
  ]
}
```

## Step 2: 実行可能MSの抽出

以下の条件を **すべて** 満たすMSを「実行可能」とする:

1. `status` が `todo` または `in_progress` である
2. `depends_on` に含まれるMS **すべて** が `done` または `observing` ステータスである

ステータスの判定は Step 1 の `nodes` 配列から行う。

実行可能MSがなければ:
```
Dispatch: 実行可能なマイルストーンがありません。
依存関係の先行MSが未完了か、すべてのMSが完了済みです。
```
と表示して終了。

## Step 3: 各MSの詳細情報取得

実行可能な各MSについて、以下を **並列に** Bash ツールで実行:

### 3a. SPECドキュメント一覧
```bash
beacon doc list --scope spec --ms <ms-id> --json
```

結果が空でなければ、各ドキュメントの内容を取得:
```bash
beacon doc show <doc_id>
```

### 3b. 未完了タスク一覧
```bash
beacon task list --json --ms <ms-id>
```

`entries[]` から `type == "task"` かつ `status != "done"` かつ `status != "cancelled"` のものを抽出する（ネストされた `entries[]` 内も再帰的に確認）。

## Step 4: Dispatch計画の提示

収集した情報をユーザーに提示する:

```
Dispatch Plan:
---
[ms-id] [title] (workspace: [dir or "none"])
  SPEC: [doc titles, comma-separated]
  Tasks: [N pending]
    - [entry-id] [description]
    - [entry-id] [description]

[ms-id] [title] (workspace: [dir or "none"])
  SPEC: (none)
  Tasks: [N pending]
    - [entry-id] [description]
---
実行しますか？ [全て / 選択 / キャンセル]
```

ユーザーの回答を待つ:
- **全て**: すべての実行可能MSを起動
- **選択**: ユーザーが指定したMS-IDのみ起動
- **キャンセル**: 何もせず終了

## Step 5: サブエージェント起動

ユーザー承認後、各MSに対して **Agent tool** でサブエージェントを起動する。

### 起動ルール
- 互いに依存関係のないMS同士は **並列** で起動してよい
- 依存関係のあるMS同士は **直列** で起動する（先行MSの完了を待つ）
- 実際には Step 2 の条件を通過した時点で互いに独立なので、全て並列で起動してよい

### 各エージェントへのPrompt

以下のテンプレートで prompt を構成する:

```
あなたは beacon プロジェクトのサブエージェントです。以下のマイルストーンを担当します。

## 担当マイルストーン
- ID: [ms-id]
- Title: [title]
- Workspace: [workspace path or "プロジェクトルート"]

## セッション開始
まず `/beacon-session-start [ms-id]` を実行して、担当MSのコンテキストを復元してください。

## SPECドキュメント
[SPECの全文をここに展開。なければ "(SPECなし)"]

## 未完了タスク
[タスク一覧を展開]
- [entry-id]: [description]
- [entry-id]: [description]

## 作業ルール
1. workspaceが指定されている場合は、そのディレクトリで作業する
2. タスクを完了したら `beacon task done <entry-id>` で記録する
3. コミット後は `/beacon-log` で進捗を記録する
4. 新しいタスクが必要になったら `beacon task add "description" -m [ms-id]` で追加する
5. 作業完了後、最終状態を簡潔に報告する
```

## Step 6: 結果報告

全エージェント完了後、結果をユーザーに報告する:

### 6a. 完了状態の取得
```bash
beacon milestone graph --json
```

### 6b. 報告フォーマット

Step 2 で記録した各MSの元の `progress` と比較して報告する:

```
Dispatch Complete:
---
[ms-id] [title]: [new progress]% (was [old progress]%)
  Completed tasks: [entry-ids of tasks marked done]
  Remaining tasks: [entry-ids of still-pending tasks, if any]

[ms-id] [title]: [new progress]% (was [old progress]%)
  [Agent error summary, if failed]
---
```

### 6c. 次Waveの提示

Step 1 のグラフ情報と最新ステータスから、新たに実行可能になったMSがあれば提示:
```
Next wave available:
  [ms-id] [title] (depends_on: [completed deps])
再度 /beacon-dispatch で起動できます。
```

## 制約

- **データ取得は beacon CLI `--json` 経由のみ**: Read ツールで `.beacon/project.json` を直接読んではならない。
- **Agent tool でサブエージェントを起動**: 直接コードを書いたり実行したりしない。各MSの実装はサブエージェントに委ねる。
- **ユーザー承認必須**: Step 4 でユーザーの明示的な承認がなければ Step 5 に進まない。
- **失敗の報告**: サブエージェントがエラーを返した場合、握りつぶさずそのまま報告する。
- **project.json への直接書き込み禁止**: beacon CLI を通じてのみ状態を変更する。
