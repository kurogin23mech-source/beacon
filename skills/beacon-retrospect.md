---
name: beacon-retrospect
description: プロジェクト史を自然言語で問い合わせて、関連する記録（task / commit / document / push / deploy / incident / retro 等）を統一検索 API で集めて要約する。「○○の機能って実装したっけ？どう実装したっけ？」「ms-15 の判断軌跡を教えて」「先月の auth エラーの件」等の問い合わせに答える。
version: 0.1.0
triggers:
  - /beacon-retrospect
  - retrospection
  - 過去を振り返って
  - あの判断
  - いつ実装したか
  - どう実装したっけ
  - プロジェクト史を辿りたい
---

# Beacon Retrospect

> 過去のプロジェクト履歴を自然言語で問い合わせ、関連する記録を統一検索 API で集めて要約する。CORE doc「Beacon 検索基盤の原則」と SPEC「検索基盤の実装方針」に依存。

## 前提条件チェック

Bash ツールで実行（cwd 引数で対象プロジェクトを指定）:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## 方法論の参照 (必読)

Skillを実行する前に、必ず companion ドキュメントを読む:

```
Read("~/.claude/skills/beacon-retrospect/methodology.md")
```

methodology は: クエリ解釈・検索戦略・件数による分岐・引用フォーマット・出力構造を定義する。

## Step 0: クエリの解釈

ユーザーが渡したクエリ（自然言語）から以下を抽出する:

| 抽出対象 | 例 |
|---|---|
| **キーワード** (`q` パラメータに渡す) | 「auth エラー」「PostCompact hook」「retrospection」 |
| **識別子** (`--id`, `--ms`, `--op`) | 「ms-15」「e-632」「op-2」 |
| **タイプ** (`--type`) | 「実装したっけ」→ task,commit / 「どう実装した」→ +document / 「いつデプロイした」→ push,deploy |
| **時間範囲** (`--from`, `--to`) | 「先月」→ 1ヶ月前まで / 「○月」→ 該当月 / 「最近」→ 直近 2 週間 |
| **状態** (`--status`) | 「未消化の」→ todo,in_progress / 「完了した」→ done |
| **優先度** (`--priority`) | 「重要な」「highest priority」→ highest,high |

**識別子が明確に含まれる**（ms-XX / e-XX / op-X）場合、それを最優先で使う。

時間表現が曖昧（「最近」「あの時」）の場合は、ユーザーに「過去 1 週間 / 1 ヶ月 / 全期間のどれですか？」を聞き返す（Step 4 で）。

## Step 1: 検索の実行（戦略 A 単発 sweep）

Bash ツールで実行:

```bash
beacon search "<q>" \
  --type <comma-separated> \
  --status <comma-separated> \
  --priority <comma-separated> \
  --ms <ms-id> --op <op-id> --id <entry-id> \
  --scope <core|spec|memo|retro|report> \
  --from <YYYY-MM-DD> --to <YYYY-MM-DD> \
  --limit 30 --json
```

省略可能なフラグは Step 0 で抽出した内容に応じて付ける（無ければ付けない）。
**戦略 A**: 1 回だけ広く検索する。再帰展開・深掘り (戦略 B/C/D) は将来検討、現状は範囲外。

stdout が JSON で返る:
```json
{"results": [...], "total": N, "limit": L, "offset": 0, "facets": {...}}
```

## Step 2: 件数による分岐 (SPEC §3 通り)

| 件数条件 | アクション |
|---|---|
| 識別子が明確 (`--id` / `--ms` / `--op` 指定) または `total ≤ 5` | **自律** → Step 3 へ |
| `total` が **6-15** | **候補確認** → Step 2a へ |
| `total` が **16+** または時間表現が曖昧 | **聞き返し** → Step 2b へ |
| `total == 0` | **見つからない旨を報告** → Step 5a へ |

### Step 2a: 候補リストの提示 (6-15 件)

```
[q] に関連する記録が [N] 件見つかりました。どれを深掘りしますか？

  1. [e-id] [title] (type / date) — [snippet]
  2. [e-id] [title] ...
  ...

選んでください（番号 / 「全部」/ 「やめる」）
```

ユーザーが選んだら、それを `--id` で再検索 → Step 3。

### Step 2b: 聞き返し (16+ または曖昧時)

```
[q] で [N] 件ヒット。絞り込みが必要です:

  - 期間: 過去 1 週間 / 1 ヶ月 / 全期間 ?
  - タイプ: task のみ / commit のみ / document も含む ?
  - MS: 特定の MS に絞る ?

どう絞りますか？
```

ユーザーの応答に応じて Step 1 を再実行。

## Step 3: 上位ヒットの full read（戦略 A の補強）

戦略 A は単発検索だが、**結果中の文書系エントリは中身が必要** な場合があるので fetch する:

- `entity_type == "document"` の上位 3 件: `beacon doc show <doc_id>` で本文取得
- `entity_type == "milestone"` のヒット: `beacon status --json --ms <ms-id>` で詳細
- `entity_type == "task"`: 既に snippet があるので追加 fetch 不要 (motivation/ac 込みで返る)
- `entity_type == "commit"`: `beacon entry show <e-id>` で linked tasks 等の追加情報 (必要なら)

これは追加 API 呼び出しなのでコストとリターンで判断 (重要そうなヒットのみ深掘り)。

## Step 4: 要約生成（引用付き）

methodology.md の「出力フォーマット」セクション通り:

```
**結論**: [q に対するユーザー視点の答え。1-2 文]

**根拠**:
  - [e-id] (type, status, date) [title]
    - [本文/snippet からの抜粋。30-80 文字]
    - 🔗 https://beacon-ai.dev/?project=<project_id>#<url_hash>
  - [e-id] (type, status, date) [title]
    - ...

**関連**: [補足情報があれば。例: 「同テーマの未完了タスク N 件あり」「retro doc に追加考察あり」]
```

ポイント:
- **結論**: 「実装済み / 未実装 / 部分実装」のような端的な答え
- **根拠**: エントリ ID + 種別 + 日付 を必ず併記 (引用元が辿れる)
- **Web UI deep link**: cloud mode なら必ず添える (ms-43 e-618 deep link 機能との整合)

## Step 5: 結果報告

### Step 5a: 0 件の場合

```
[q] に関する記録は見つかりませんでした。

検索したスコープ: [使用したフィルタ]
- 別キーワードで検索: 「○○」「△△」など類似語で再試行できます
- 期間を広げる: --from を撤去して全期間で再検索
- 関連 MS で広く見る: ms-XX の全エントリを参照 (`beacon search "" --ms ms-XX`)
```

### Step 5b: 通常の場合

Step 4 の要約をそのまま提示。

## 制約

- **検索 API 経由のみ**: `beacon search` を使う。`beacon status` で全件取って自前 grep するような実装はしない (検索基盤 CORE 違反)
- **戦略 A 単発 sweep**: 今は再帰展開・意図分類・深掘りループは実装しない (将来 e-621 拡張)
- **引用必須**: 要約だけで終わらせず、エントリ ID + 種別 + 日付 を必ず付ける
- **新規書き込みなし**: このSkillは読み取り専用。`beacon doc add` や `beacon task add` をしてはならない。気づきを残したいときは `beacon note "..."` の利用をユーザーに勧めるに留める
- **cwd-aware**: hook 経由起動でも独立起動でも `cwd` 引数で対象プロジェクトを明示する
