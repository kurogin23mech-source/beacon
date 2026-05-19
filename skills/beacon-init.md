---
name: beacon-init
description: Beaconプロジェクトを会話形式で初期化。既存リポジトリはProject Archaeologyでgit logから自動分析。beacon init --name/--objective フラグで非対話実行。
version: 1.0.0
triggers:
  - beacon init
  - プロジェクトをbeaconで管理したい
  - beacon を始めたい
---

# Beacon Init

> 会話でプロジェクト情報を収集し、`beacon init` を非対話で実行する。既存リポジトリはProject Archaeologyを提案する。

## 前提条件チェック

Bash ツールで実行:
```bash
test -f .beacon/project.json && echo "EXISTS" || echo "OK"
```

- `EXISTS` の場合: 「このディレクトリにはすでに beacon が初期化されています（`beacon status` で確認できます）」と伝えて終了する。

## Step 1: リポジトリ分析

以下を **並列に** Bash ツールで実行:

```bash
git log --oneline 2>/dev/null | wc -l | tr -d ' '
```

```bash
basename "$(pwd)"
```

```bash
cat README.md 2>/dev/null | head -5
```

- コミット数を取得する
- ディレクトリ名をデフォルトのプロジェクト名候補として記憶する

## Step 2: Project Archaeology の提案（コミット数 >= 10 の場合のみ）

コミット数が10以上の場合、ユーザーに提案する:

```
このリポジトリには [N] 件のコミットがあります。

git log を読んで、これまでの開発フェーズを Beacon のマイルストーンとして
自動構造化できます（Project Archaeology）。

初期化後に /beacon-session-start を実行すると自動的に分析が始まります。
```

コミット数が10未満の場合はこのステップをスキップする。

## Step 3: 情報収集（会話）

以下の情報をユーザーから収集する。**すでに会話の中で言及されていれば改めて聞かない**。

### 3a. プロジェクト名

ディレクトリ名を提案しつつ確認する:
```
プロジェクト名を教えてください。
（デフォルト: "[ディレクトリ名]"、Enterでそのまま使えます）
```

### 3b. 大目的（Objective）

```
このプロジェクトで最終的に何を実現したいですか？

あなたとAIが共有するゴール宣言になります。
「何を作るか」ではなく「誰がどんな状態になるか」で書くと効果的です。

例）「個人開発者が、AIと一緒にマイルストーンを管理しながら開発に集中できるようにする」
```

### 3c. Retro day（任意）

```
週次振り返りを行う曜日を教えてください。
（デフォルト: friday。「デフォルトで」「fridayで」などと答えてください）
```

省略・「デフォルト」と答えた場合は `friday` を使う。

### 3d. Storage

```
ストレージを選んでください:
  local  — ローカルのみ（推奨。Cloud は後から追加できます）
  cloud  — クラウド同期（Google アカウントが必要）

（デフォルト: local。「ローカルで」「デフォルトで」などと答えてください）
```

省略・「デフォルト」と答えた場合は `local` を使う。

## Step 4: 確認

収集した内容を確認する:

```
以下の内容で初期化します:

  プロジェクト名: [name]
  大目的: [objective]
  Retro day: [retro_day]
  Storage: [storage]

よろしいですか？（変更があれば教えてください）
```

ユーザーが変更を求めた場合はStep 3に戻る。

## Step 5: 実行

Bash ツールで実行:

```bash
beacon init --name "[name]" --objective "[objective]" --retro-day [retro_day] --storage [storage]
```

成功したら:
```
✅ 初期化完了！

次のステップ:
  - /beacon-session-start を実行してコンテキストを読み込みます
  [コミット数 >= 10 の場合]: → Project Archaeology が走り、過去のフェーズが自動構造化されます
  [コミット数 < 10 の場合]: → 最初のマイルストーンを一緒に考えます
```

## Step 6: session-start の起動

`/beacon-session-start` を即時実行する。

## 制約

- `beacon init` はBash ツールで実行する（ユーザーに `!` で実行させない）
- `rm` は使わない
- ユーザーが明示的に答えた情報は再度聞かない
