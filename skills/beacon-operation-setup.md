---
name: beacon-operation-setup
description: 会話形式でOperationを作成し、ログ取得手順をSPECドキュメントとして自動生成する。定期バッチ・運用監視の記録基盤を新規セットアップする際に使う。
version: 1.0.0
triggers:
  - /beacon-operation-setup
  - Operationをセットアップ
  - 運用監視を始めたい
  - バッチの記録を始めたい
---

# Beacon Operation Setup

> 会話形式でOperationを作成し、SPECドキュメント（ログ取得手順）を自動生成する。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` の場合: 「このディレクトリに beacon が初期化されていません（`/beacon-init` を先に実行してください）」と伝えて終了する。

## Step 1: 情報収集（会話）

以下の情報をユーザーから収集する。**すでに会話の中で言及されていれば改めて聞かない**。

### 1a. 監視対象の確認

```
何を監視・記録しますか？

例）
- 「プロフィール抽出バッチ（毎朝実行）」
- 「データ同期ジョブ（平日のみ）」
- 「週次レポート生成」
```

`log_source` は監視対象の識別名（英数字・アンダースコア、例: `profile_extractor`）として収集または推測する。

### 1b. ログの取得方法

```
ログはどのように取得しますか？

例）
- ファイルパス: ~/projects/pe/logs/latest.json
- コマンド: python fetch_logs.py --date today
- URL: http://localhost:8080/api/stats
```

### 1c. 正常・異常の判断基準

```
どのような状態を「正常」「警告」「エラー」と判断しますか？

例）
- エラー率 < 5% → ok
- エラー率 5〜10% → warning
- エラー率 > 10% → error
```

### 1d. チェックスケジュール

```
どのくらいの頻度でチェックしますか？

  daily     — 毎日（土日含む）
  weekdays  — 平日のみ（月〜金）
  weekly    — 週1回（金曜日）

（デフォルト: weekdays）
```

省略・「デフォルト」と答えた場合は `weekdays` を使う。

### 1e. タイトル

収集した情報を元に Operation のタイトルを提案する（例: 「2026年5月第4週」）。

## Step 2: 確認

収集した内容を確認する:

```
以下の内容でOperationを作成します:

  タイトル: [title]
  log_source: [log_source]
  スケジュール: [schedule]
  ログ取得手順（SPECドキュメントに保存）:
    取得方法: [1b の内容]
    判断基準: [1c の内容]

よろしいですか？（変更があれば教えてください）
```

ユーザーが変更を求めた場合は Step 1 に戻る。

## Step 3: Operation作成

Bash ツールで実行:

```bash
beacon operation open "[title]" --schedule [daily|weekdays|weekly] --log-source [log_source]
```

stdout から Operation ID（例: `op-1`）を取得する。

## Step 4: SPECドキュメント生成

Step 1 で収集した情報を元に、ログ取得手順を Markdown で作成し、Operation に紐づける:

```bash
cat <<'SPEC_EOF' | beacon doc add "[log_source] ログ取得・解釈手順" --scope spec --op [op-id] --stdin
# [log_source] ログ取得・解釈手順

## ログ取得

[1b で収集した取得方法の具体的な手順]

## ステータス判定

[1c で収集した判断基準を箇条書きで]

## 記録ガイドライン

- `ok`: 正常範囲内。処理件数・主要指標を description に含める
- `warning`: 閾値接近または軽微な問題。傾向（増加/減少）も記録
- `error`: 対処が必要な問題。原因候補を含め、Incident 起票を検討
SPEC_EOF
```

## Step 5: 完了メッセージ

```
✅ Operation セットアップ完了

  Operation: [op-id] "[title]"
  スケジュール: [schedule]
  SPEC doc: "[log_source] ログ取得・解釈手順"

次回セッション開始時からチェックトリガーが自動で届きます。
トリガーが届いたら /beacon-operation-review で記録を残してください。
```

## 制約

- Bash ツールで CLI コマンドを実行する（ユーザーに `!` で実行させない）
- ユーザーが明示的に答えた情報は再度聞かない
- Operation ID は `beacon operation open` の stdout から取得する（仮定しない）
