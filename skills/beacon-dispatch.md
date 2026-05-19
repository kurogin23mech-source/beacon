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

## Step 2: MS分類

Step 1 の `nodes` から、対象MSを以下の2グループに分類する。

### 実行可能（Runnable）
以下の条件を **すべて** 満たすMS:
1. `status` が `todo` または `in_progress`
2. `depends_on` に含まれるMS **すべて** が `done` または `observing`

### 待機中（Blocked）
以下の条件を満たすMS:
1. `status` が `todo` または `in_progress`
2. `depends_on` に、`done`/`observing` 以外のステータスのMSが1つ以上含まれる

各ブロックされたMSについて、「どのMSがブロック要因か」（blocking deps）を記録する。

実行可能MSが1つもなければ:
```
Dispatch: 実行可能なマイルストーンがありません。
依存関係の先行MSが未完了か、すべてのMSが完了済みです。
```
と表示し、待機中MSがあれば一覧を表示して終了。

## Step 2.5: 選択MS間の依存関係チェック

実行可能MSが2つ以上ある場合、それらの**相互の依存関係**を確認する。

### 確認方法

Step 1 の `nodes` データを使い、実行可能MSのすべてのペア (A, B) について:
- A の `depends_on` に B が含まれるか
- B の `depends_on` に A が含まれるか
- どちらも含まれない場合 → **「未定義」**

### 未定義ペアへの判断

未定義ペアが存在する場合、各ペアについて以下を推論する:

- 両MSのタイトル・タスク説明を読み、**同じファイルに触れる可能性があるか**を判断する
- 論理的な実行順序が必要か（例: 片方が削除するものをもう片方が参照するなど）を評価する
- リスクを **「低（並列可）」「中（注意）」「高（順序付け推奨）」** の3段階で評価する

### 出力

未定義ペアがない場合はこのStepをスキップ。

未定義ペアがある場合、Step 4 の Dispatch Plan に含める（後述）。

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

収集した情報をユーザーに提示する。**実行可能MS・待機中MS・相互依存チェックをすべて表示する**:

```
Dispatch Plan:
---
実行可能:
◐ [ms-id] [title] (workspace: [dir or "none"])
  SPEC: [doc titles, comma-separated or "(none)"]
  Tasks: [N pending]
    - [entry-id] [description]

依存関係により待機中:            ← 存在する場合のみ表示
◌ [ms-id] [title]
  ← ブロック: [blocking-ms-id] [blocking-title] ([status])

選択MS間の依存関係:              ← 未定義ペアがある場合のみ表示
  [ms-id-A] ↔ [ms-id-B]: 未定義 [低/中/高] — [1行の理由]
  [ms-id-A] ↔ [ms-id-C]: 未定義 [低/中/高] — [1行の理由]
  ※ リスク「高」のペアは依存関係を定義してから起動することを推奨
---
実行しますか？ [全て / 選択 / キャンセル]
依存関係を先に定義する場合: beacon milestone depends <ms-id> --on <dep-id>
```

ユーザーの回答を待つ:
- **全て**: すべての実行可能MSを並列起動
- **選択**: ユーザーが指定したMS-IDのみ起動
- **キャンセル**: 何もせず終了

## Step 4.5: Worktree作成フェーズ

ユーザーが承認後（「全て」または「選択」）、エージェント起動の前に各MSのworktreeを準備する。

### Worktree作成手順

承認されたMSそれぞれについて、以下を順次実行する:

```bash
beacon milestone workspace <ms-id> --executor ai --json
```

成功した場合（JSON出力あり）:
- `workspace_path` を記録する（Step 5 の prompt に使用）
- `workspace_branch` も記録する

失敗した場合:
- エラーを表示してユーザーに確認を求める
- 「worktreeなしで続行しますか？」と問い、承認されれば workspace_path = "プロジェクトルート" で続行

### Worktreeが既に存在する場合

`beacon milestone workspace` は冪等に動作する（既存のworktreeがあればスキップして情報を返す）ので、再実行しても安全。

## Step 5: サブエージェント起動

Step 4.5 で準備したworktree情報を使い、各MSに対して **Agent tool** でサブエージェントを起動する。

### 起動ルール
- 互いに依存関係のないMS同士は **並列** で起動してよい
- 依存関係のあるMS同士は **直列** で起動する（先行MSの完了を待つ）
- 実際には Step 2 の条件を通過した時点で互いに独立なので、全て並列で起動してよい

### 各エージェントへのPrompt

以下のテンプレートで prompt を構成する（`workspace_path` は Step 4.5 で取得した値を使用）:

```
あなたは beacon プロジェクトのサブエージェントです。以下のマイルストーンを担当します。

## 担当マイルストーン
- ID: [ms-id]
- Title: [title]
- Workspace: [workspace_path from Step 4.5, or "プロジェクトルート"]

## 作業ディレクトリ
Workspace: [workspace_path]
このディレクトリで作業してください（絶対パスで指定するか、cd して作業する）。
Worktreeブランチ: [workspace_branch]
git操作はこのworktree内で完結させてください（mainブランチには直接コミットしない）。

## セッション開始
まず `/beacon-session-start [ms-id]` を実行して、担当MSのコンテキストを復元してください。

## SPECドキュメント
[SPECの全文をここに展開。なければ "(SPECなし)"]

## 未完了タスク
[タスク一覧を展開]
- [entry-id]: [description]
- [entry-id]: [description]

## 作業ルール
1. Workspaceディレクトリ内で作業する（絶対パス: [abs_workspace_path]）
2. タスクを完了したら `beacon task done <entry-id>` で記録する
3. コミット後は `/beacon-log` で進捗を記録する
4. 新しいタスクが必要になったら `beacon task add "description" -m [ms-id]` で追加する
5. 作業完了後、最終状態を簡潔に報告する
6. 作業完了後: オーケストレーターが `beacon milestone workspace-cleanup [ms-id]` でworktreeをクリーンアップする
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
