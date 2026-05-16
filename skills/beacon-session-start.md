---
name: beacon-session-start
description: Beaconプロジェクトのセッション開始時にコンテキストを復元。アクティブMS・未消化タスク・summaryを提示する。
version: 0.5.0
triggers:
  - セッション開始
  - /beacon-start
  - /beacon-session-start
  - beacon の状態を確認
  - 現在のマイルストーンを確認
  - 開発を再開
  - 再開しよう
  - 前回の続き
  - 現状を確認
  - 今どうなってる
  - 状況を教えて
---

# Beacon Session Start

> セッション開始時に beacon CLI 経由でプロジェクトの現状を取得し、ユーザーに提示する。読み取り専用。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 0: 引数チェック

ユーザーが `/beacon-session-start ms-XX` のように引数付きで呼んだ場合、`ms-XX` を **スコープMS** として記憶する。
複数指定も可能: `/beacon-session-start ms-16 ms-17`

スコープMSが指定された場合、Step 1a のコマンドが変わる。

## Step 1: プロジェクト状態の取得（並列実行可）

以下の3つを **Bash ツール** で **並列に** 実行する:

### 1a. プロジェクト全体の状態

スコープMSが **指定されている** 場合:
```bash
beacon status --json --ms <ms-id> [--ms <ms-id> ...]
```

スコープMSが **指定されていない** 場合（従来通り）:
```bash
beacon status --json
```
stdout に JSON が返る。以下のフィールドを使う:
- `name`: プロジェクト名
- `summary`: 前セッションの経緯・背景
- `milestones[]`: 各MSの `id`, `title`, `status`, `progress`, `total_tasks`, `done_tasks`

### 1b. git の最新コミット
```bash
git log --oneline -5
```

### 1c. ワーキングツリーの状態
```bash
git status --short
```

### 1d. COREドキュメント一覧
```bash
beacon doc list --scope core --json
```
stdout に JSON 配列が返る。各要素は `doc_id`, `title`, `scope`, `updated_at` を持つ。
空配列の場合はこのステップをスキップする。

## Step 1e: COREドキュメントの内容取得

Step 1d の結果が空でなければ、各ドキュメントの内容を **Bash ツール** で **並列に** 取得する:

```bash
beacon doc show <doc_id>
```

stdout にドキュメント本文（Markdown）が返る。frontmatter（`---` で囲まれた部分）は除去して本文のみ使う。

## Step 1f: アクティブMSに紐づくSPECドキュメント取得

Step 1a の結果から `status == "in_progress"` のマイルストーンIDを特定する。
あれば **Bash ツール** で実行:

```bash
beacon doc list --scope spec --ms <active-ms-id> --json
```

結果が空でなければ、各ドキュメントの内容を Step 1e と同様に `beacon doc show <doc_id>` で **並列に** 取得する。

## Step 1g: GitHub PR 自動検知（fail-safe）

Bash ツールで実行:

```bash
gh pr list --json number,title,url,author,headRefName,state 2>/dev/null
```

失敗した場合（gh未設定、リポジトリ外など）は無視してスキップする。

取得できた場合、Step 1a または Step 2 の結果から beacon に記録済みの PR URL（`type == "pr"` のエントリの `meta.url`）を収集し、未記録のオープンPRがないか照合する。

**未記録PRがある場合**、Step 3 の出力に含める:
```
未記録のPR:
  - PR#42: [title] (author: [login]) → beacon pr add で記録できます
```

**review_status == "pending" または "changes_requested" の PR がある場合**:
```
レビュー待ちのPR:
  - [e-xxx] PR#N: [title] [in_review / review: pending]
```
→ この場合は Step 3 出力の後に `/review <pr_number>` を即時起動する（beacon trigger より優先）。

この Step は **読み取り専用**。自動で `beacon pr add` を実行してはならない。

## Step 2: アクティブMSの詳細取得

Step 1a の結果から `status == "in_progress"` のマイルストーンを特定する。
あれば **Bash ツール** で実行:

```bash
beacon task list --json --ms <active-ms-id>
```

stdout の JSON から:
- 未完了タスク: `entries[]` のうち `type == "task"` かつ `status != "done"` のもの（ネストされた `entries[]` 内も再帰的に確認）
- 直近コミット: `entries[]` のうち `type == "commit"` の最新3件

## Step 3: 照合と提示

Step 1〜2 の結果を組み合わせて、以下のフォーマットで **テキスト出力** する。
不要なセクション（未消化タスクがない、未記録コミットがない等）は省略してよい。

```
Beacon: [name]
---
ドキュメント (core=設計原則・常時参照 / spec=仕様・技術詳細 / memo=検討メモ):
  [CORE] [title]: [本文（短ければ全文、長ければ要約）]
  ...
  [SPEC] [title] (ms-xx): [要約]
  ...

前回の経緯: [summary]

Active: [ms-id] [title] ([progress]%) [done_tasks]/[total_tasks]タスク完了
  未消化タスク:
  - [entry-id] [description]
  - [entry-id] [description]
  直近コミット:
  - [hash] [description]

他のマイルストーン:
  [status-icon] [ms-id] [title] ([progress]%)
  ...

未記録のコミット: [git logのハッシュがbeaconエントリに存在しないもの]
uncommitted changes: [git statusの結果があれば]
---
何から始めますか？
```

status-icon の対応: done=●, in_progress=◐, todo=○, waiting=◌, observing=◔

**照合ルール**:
- git log の各コミットハッシュ（先頭7文字）が、Step 1a または Step 2 のエントリの `meta.hash` に存在するか確認
- 存在しないものを「未記録のコミット」として表示

## Step 4: トリガーチェック

Bash ツールで実行:
```bash
beacon trigger check
```

JSON 配列が返る。空でなければ、各トリガーの `message` を出力の末尾に追加:
```
Beacon trigger: [message]
```

トリガーへの対応（例: `/beacon-retro` の実行）はユーザーの判断に委ねる。自動実行してはならない。

## 制約

- **読み取り専用**: project.json への書き込みは一切行わない。`beacon log`, `beacon task add/done`, `beacon summary "text"` 等の書き込みコマンドを実行してはならない。
- **データ取得は Bash ツール経由の beacon CLI `--json` 出力のみ**: Read ツールで `.beacon/project.json` を直接読んではならない。
- **出力はコンパクトに**: 完了済みマイルストーンの配下エントリは展開しない。
