---
name: beacon-task
description: beaconのタスク操作（追加・完了・更新・削除・一覧）をCLI経由で実行する。
version: 0.2.0
triggers:
  - /beacon-task
  - beacon にタスクを追加
  - タスクを起票
  - タスクを完了
  - タスクを削除
---

# Beacon Task

> beacon のタスク操作を CLI 経由で行う。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## 操作の判定

ユーザーの指示から、以下のどの操作かを判定する:

| 操作 | キーワード例 |
|------|-------------|
| add | 追加、起票、作る、入れて |
| done | 完了、終わった、done |
| update | 更新、変更、修正 |
| delete | 削除、消す、キャンセル |
| list | 一覧、確認、見せて |
| show | 詳細、中身 |

## Step 1: 対象マイルストーンの確認

操作に対象MSの指定がない場合、Bash ツールで実行:
```bash
beacon status --json
```
- `status == "in_progress"` のマイルストーンをデフォルトの対象とする
- 複数ある場合はユーザーに確認する

## Step 2: 操作の実行

判定した操作に応じて、Bash ツールで対応するコマンドを実行する:

### add

タスク追加前に、以下の3つを会話文脈から生成する:

**motivation（なぜ必要か）**: 「なぜ今このタスクが必要か」を1文で。ユーザーの発言・作業文脈・紐づくMSのタイトルから推論する。

**acceptance_criteria（完了条件）**: 「これを満たしたらdoneにできる」を具体的に1〜3項目。曖昧な場合は「〜が動作すること」「〜が確認できること」形式で記述する。

**priority（優先度）**: 以下の定義を参照して判定する:

| 優先度 | 基準 |
|---|---|
| `highest` | サービスの価値が成立しないレベルの影響 |
| `high` | 大コンポーネントに致命的な影響 |
| `middle` | 大コンポーネントに使いにくいレベル、または小機能に致命的 |
| `low` | 小機能に使いにくいレベル |
| `lowest` | 軽微（誤字・表示系など、修正も軽微） |

タスクの性質（バグ修正・新機能・改善・ドキュメント等）と影響範囲から判断する。

```bash
beacon task add "<description>" --ms <ms-id> \
  --motivation "<生成したmotivation>" \
  --acceptance-criteria "<生成したacceptance_criteria>" \
  --priority <生成したpriority>
```
- detail がある場合は `--detail "<text>"` も付加

### done

完了前に `--reason`（完了根拠）を会話文脈から生成する:
- コミットハッシュがあれば「コミット XXXXXXX で実装済み、動作確認済み」
- ユーザーの発言から「〜を確認したため」「〜が不要となったため」等
- acceptance_criteria が記録されている場合はそれを参照して満足度を記述する

```bash
beacon task done <entry-id> --reason "<生成したreason>"
```
- entry-id はユーザーが指定するか、description からの照合で特定する
- 照合する場合は先に `beacon task list --json --ms <ms-id>` で一覧を取得し、未完了タスクから一致するものを探す
- **タスク完了後、Step 3（進捗率の自動評価）に進む**

### update
```bash
beacon task update <entry-id> [--description "<text>"] [--status <status>] [--detail "<text>"]
```

### delete
```bash
beacon task delete <entry-id>
```

### list
```bash
beacon task list --ms <ms-id>
```
- デフォルトでは cancelled を非表示。ユーザーが「全部見せて」等の場合は `--all` を付加

### show
```bash
beacon task show <entry-id>
```

## Step 3: 進捗率の自動評価（done 操作時のみ）

done 操作の場合、タスク完了後にマイルストーンの進捗率を定性評価して更新する。

### 3a. コンテキスト取得

Bash ツールで実行:
```bash
beacon log --prepare
```

### 3b. 進捗率の評価

prepare の JSON を読み、以下の基準で進捗率（0-100の整数）を決定する:

- `milestone.title`（目標）に対して、現在どの程度到達しているかを **定性的に** 評価する
- `done_tasks / total_tasks` の比率は参考値であり、そのまま進捗率にしてはならない
- タスクの重さは均一ではない
- `milestone.progress`（現在の進捗率）からの変化は、完了したタスクの貢献度に見合った幅にする
- 前回より下がることは通常ない（スコープ拡大時を除く）

### 3c. 進捗率の書き込み

```bash
beacon milestone update <ms-id> --progress <進捗率>
```

## Step 4: 結果の報告

コマンドの stdout をユーザーに簡潔に報告する。
done 操作時は進捗率の更新結果も含める:
```
Done: [entry-id] description
Progress: ms-id (N% → M%)
```

## Step 5: トリガーチェック（done 操作時のみ）

done 操作の報告後、Bash ツールで実行:
```bash
beacon trigger check
```

JSON 配列が返る。空でなければ、各トリガーの `message` をユーザーに提示する:
```
Beacon trigger: [message]
```

トリガーへの対応（例: `/beacon-retro` の実行）はユーザーの判断に委ねる。自動実行してはならない。

## 制約

- データ取得・操作は必ず `beacon` CLI 経由。project.json を直接読み書きしない。
- ID は `e-{連番}` 形式。CLI が自動採番するため、Skill で ID を生成しない。
- 進捗率の評価は prepare の JSON に含まれる情報のみで判断する。
