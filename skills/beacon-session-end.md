---
name: beacon-session-end
description: Beaconプロジェクトのセッション終了時にサマリーを更新し、未完了タスクを整理する。
version: 0.1.0
triggers:
  - /beacon-end
  - /beacon-session-end
  - セッション終了
  - 今日はここまで
  - 作業を終わる
  - 終わろう
  - おしまい
  - また明日
  - 今日は終わり
  - ここで切ろう
  - 一旦終了
  - 切り上げ
---

# Beacon Session End

> セッション終了時にサマリーを更新し、次セッションへの引き継ぎを整備する。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 1: 現状の収集（並列実行可）

以下を **Bash ツール** で **並列に** 実行:

### 1a. プロジェクト状態
```bash
beacon status --json
```

### 1b. アクティブMSのタスク一覧
```bash
beacon task list --json --ms <active-ms-id>
```
（active-ms-id が不明な場合は 1a の結果を待ってから実行）

### 1c. git 状態
```bash
git status --short
git log --oneline -3
```

## Step 2: 未コミット変更の確認

Step 1c で uncommitted changes がある場合、ユーザーに通知:
```
未コミットの変更があります:
  M file1.py
  M file2.py
コミットしてから終了しますか？
```
ユーザーの判断を待つ。

## Step 2.5: MS・タスク棚卸し（自動整理）

Step 1a の結果を読み、以下を自動整理する。

### A. 完了済みMSのステータス修正

`progress == 100` かつ `done_tasks == total_tasks` かつ `status == "in_progress"` のMSを検出:

```bash
beacon milestone observe <ms-id>
```

自動的に `observing` に移行し「[ms-id] を observing に移行しました」と報告する。
完全クローズ（done）にしてよいかはユーザーに確認する。

### B. 実装済みタスクの自動done候補提示

アクティブMSの `pending_tasks` と Step 1c の `git log` を照合し、「最近のコミットで解決されたと思われるが done になっていないタスク」を特定:

- コミットメッセージにタスクIDが含まれる
- コミットメッセージがタスクの description と明確に対応する

候補がある場合はユーザーに提示:
```
タスク完了の候補:
  - [e-256] beacon setup コマンド実装
  → done にしますか？
```

ユーザーが承認したら:
```bash
beacon task done <entry-id>
```

候補がない場合はこのステップをスキップする。

## Step 3: サマリーの生成

Step 1 の情報を元に、以下の基準でサマリーを生成:

### 記載すること
- このセッションで何に取り組み、何が決まったか
- なぜ今の方針に至ったか（背景・判断の経緯）
- 次セッションで最初に知るべきこと
- ブロッカーや懸念点があれば

### 記載しないこと
- 進捗率やタスク消化数（beacon status で見える）
- コミット一覧（git log で見える）

### 出力形式
2-4文の日本語テキスト。

## Step 4: サマリーの書き込み

Bash ツールで実行:
```bash
beacon summary "<Step3のテキスト>"
```

## Step 5: 終了レポート

ユーザーに結果を提示:
```
Beacon セッション終了
---
Summary: [更新したサマリー]

Active: [ms-id] [title] ([progress]%)
  残タスク: [N]件
  - [id] [description]
  ...
---
```

## 制約

- データ取得は Bash ツール経由の beacon CLI のみ。project.json を直接読まない。
- サマリーの書き込みは `beacon summary` コマンド経由のみ。
- 未コミット変更がある場合、勝手にコミットしない。ユーザーに判断を委ねる。
