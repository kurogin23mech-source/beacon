---
name: beacon-log
description: コミット後にbeaconへ記録し、進捗率とサマリーをAI評価で自動更新する。prepare/finalizeの2段階ワークフロー。
version: 0.3.0
triggers:
  - /beacon-log
  - beacon に記録
  - コミットを記録
  - 進捗を更新
---

# Beacon Log

> コミット記録 + MS選定 + 進捗評価 + サマリー更新を1つのワークフローで完結させる。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 1: コンテキスト取得（読み取り専用）

Bash ツールで実行:
```bash
beacon log --prepare
```

stdout に JSON が返る。2つのパターンがある:

### パターンA: 単一アクティブMS
```json
{
  "commit": {"hash": "...", "message": "...", "date": "...", "summary": "..."},
  "milestone": {"id": "...", "title": "...", "status": "...", "progress": N, "total_tasks": N, "done_tasks": N, "pending_tasks": [...], "recent_entries": [...]},
  "current_summary": "..."
}
```

### パターンB: 複数アクティブMS
```json
{
  "commit": {"hash": "...", "message": "...", "date": "...", "summary": "..."},
  "candidates": [
    {"id": "...", "title": "...", "status": "...", "progress": N, "total_tasks": N, "done_tasks": N, "pending_tasks": [...], "recent_entries": [...]},
    ...
  ],
  "current_summary": "..."
}
```

## Step 1.5: マイルストーン選定（candidatesがある場合のみ）

パターンBの場合、以下の基準で **1つのMSを選定** する:

### 選定基準
- `commit.message` の内容が、どのMSの `title`・`pending_tasks`・`recent_entries` に最も関連するかを判断する
- 直近のエントリの流れ（recent_entries）との連続性を重視する
- 迷う場合は、よりスコープが狭い（具体的な）MSを優先する

### 出力
選定した MS の `id`（例: `ms-9`）。以降の Step では選定した MS の情報を使う。

パターンAの場合はこの Step をスキップし、`milestone` をそのまま使う。

## Step 2: 進捗率の評価

Step 1 (+ Step 1.5) で特定した MS の情報を読み、以下の基準で **進捗率（0-100の整数）** を決定する:

### 評価基準
- `milestone.title`（目標）に対して、現在どの程度到達しているかを **定性的に** 評価する
- `done_tasks / total_tasks` の比率は参考値であり、そのまま進捗率にしてはならない
- タスクの重さは均一ではない。大きなタスクの完了は進捗を大きく動かし、小さなタスクは小さく動かす
- `milestone.progress`（現在の進捗率）からの変化は、今回のコミットの貢献度に見合った幅にする
- 前回より下がることは通常ない（スコープ拡大時を除く）

### 出力形式
整数値のみ（例: `55`）。内部で使用するため、説明文は不要。

## Step 3: サマリーの生成

Step 1 の JSON を読み、以下の基準で **サマリーテキスト** を生成する:

### 記載すること
- なぜ今のタスクに取り組んでいるのか（経緯・背景）
- どういう判断でこうなったか（技術選定、方針変更など）
- 次セッションで知っておくべきコンテキスト

### 記載しないこと
- タスクリストを見ればわかる情報（進捗率、アクティブMS名、完了タスク一覧）
- コミットメッセージの繰り返し

### 出力形式
1-3文の日本語テキスト。簡潔に。

## Step 4: 書き込み（finalize）

Step 1.5〜3 の結果を使って、Bash ツールで実行:

```bash
beacon log --finalize -m <選定したms-id> --progress <Step2の値> --summary "<Step3のテキスト>"
```

ユーザーが `/beacon-log` に引数でコミットの概要を渡した場合は、`--summary` の前にそれも付加する:

```bash
beacon log --finalize -m <ms-id> --summary "<概要>" --progress <値>
```

## Step 5: 結果の提示

finalize の stdout を確認し、ユーザーに結果を簡潔に報告:

```
Beacon: [hash] → [ms-id] [紐づけ先] (progress%)
Summary: [更新したサマリーの要約]
```

## Step 6: ドキュメント評価

今回のコミットが **設計判断・方針変更・新しいルール** を含むかを評価する。

### スコープ定義
- **core**: 設計原則・アーキテクチャ方針。全セッションで常時参照される（session-startで自動読み込み）。変更は慎重に。
- **spec**: 仕様・技術的な詳細。特定の機能やAPIの仕様書。
- **memo**: 一時的な検討メモ・調査記録。揮発してもよい情報。

### 評価基準
以下のいずれかに該当する場合、ドキュメント対応が必要:
1. **新しい設計原則・方針が生まれた**（例: 「○○は△△で統一する」）→ core または spec の新規作成
2. **既存のCOREドキュメントと矛盾する変更をした**（例: アーキテクチャの方針転換）→ core の更新
3. **仕様として記録すべき技術的決定をした**（例: APIの認証方式を変更）→ spec の新規作成/更新

### 該当しない場合
何もせず Step 7 へ進む。大半のコミットはここで終わる。

### 該当する場合

1. 既存ドキュメント一覧を取得:
```bash
beacon doc list --json
```

2. 更新すべき既存ドキュメントがあるか、新規作成が必要かを判断する

3. ユーザーに提案する:
```
Doc: [既存doc更新 or 新規作成] [scope] "[タイトル]"
  理由: [なぜドキュメント化が必要か]
```

4. ユーザーが承認したら実行:
   - 新規作成: `beacon doc add --scope <scope> --title "<title>" --content "<content>"`
   - 更新: `beacon doc update <doc_id> --content "<content>"`
   - stdinからコンテンツを渡す場合: `echo '<content>' | beacon doc add --scope <scope> --title "<title>" --stdin`

5. ユーザーが却下したら何もしない。

## Step 7: トリガーチェック

Step 5 の報告後、Bash ツールで実行:
```bash
beacon trigger check
```

JSON 配列が返る。空でなければ、各トリガーの `message` をユーザーに提示する:
```
Beacon trigger: [message]
```

トリガーへの対応（例: `/beacon-retro` の実行）はユーザーの判断に委ねる。自動実行してはならない。

## 制約

- Step 1（prepare）は読み取り専用。書き込みは Step 4（finalize）のみ。
- 進捗率とサマリーの生成は、Step 1 の JSON に含まれる情報のみで判断する。追加のファイル読み取りやコマンド実行は行わない。
- project.json を Read ツールで直接読んではならない。
