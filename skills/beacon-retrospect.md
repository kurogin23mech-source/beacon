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

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

### 守ること

1. **読み手目線 1 行から始める**: 「何が嬉しいか」「何が困るか」をユーザー体験の言葉で。技術用語ではなく価値で書く
2. **横文字 3 段階**:
   - そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP` / `Tauri` / `WebSocket`)
   - 初出時に日本語注: 技術概念 (`allowlist (= 許可リスト)` / `opt-in (= 個別許可)` / `subcollection (= 子コレクション)`)
   - 日本語化が望ましい: 一般概念 (configure → 設定 / receiver → 受信側 / audit → 監査 / hit → 一致 / install → 設置 / merge → 取り込み / deploy → 配置)
3. **ID 参照には文脈**: `e-XXXX` / `UC?` / `ms-XX` は初出に必ず『何の話か』1 行添える。例 ✗「e-1140 の AC のうち」→ ✓「e-1140 (自動応答の受信側挙動を hook で扱う) の受入条件のうち」
4. **尻切れトンボ禁止**: 主語と述語を省略しない、論理関係を明示

### Self-review (生成直後に必ず)

文章を書いた直後、自分で読み返して以下を自問:
- (a) 読み手 (非開発者を含む) は 1 度読んで意味が取れるか?
- (b) 一般概念の横文字 (configure / receiver / audit / hit / install / merge / deploy 等) が残ってないか?
- (c) ID 参照に『何の話か』1 行添えたか?
- 違反していたら書き直し。enforce ではないが必須の self-check。

### 詳細

詳しい原則と例 (病理の typology / 例外ケース / 良い例・悪い例) は CORE doc `entry-writing-principle` (doc_id `F3ZkqT0pKS6JpR8dn70n`) 参照。ただし本 Skill では上記 4 項目を **常に top of mind** で適用する (CORE 参照は補足、principal は本文埋め込み)。

---

## 前提条件チェック

Bash ツールで実行（cwd 引数で対象プロジェクトを指定）:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
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

## Step 0.5: ms-79 拡張フィルタの抽出 (= e-1832 / e-1833 / e-1834 / e-1835)

Step 0 のクエリ解釈に加えて、以下のキーワードがクエリに含まれていれば対応する拡張フラグを Step 1 で付ける:

| クエリ表現 | 抽出する拡張 | 渡すフラグ |
|---|---|---|
| 「AI が自律でやった」「auto-op だけ」「envelope 経由の」 | source filter | `--source auto-op` |
| 「人が判断した」「手動で」「human dialog の」 | source filter | `--source human` |
| 「DM 経由の決定」「あの DM」 | source filter + bus archive | `--source dm --include-bus-dm` |
| 「○○ さんが」「user 別」「actor 別」 | actor filter | `--actor <name>` |
| 「○○ さんが claim していた」「△△ の claim」 | claimant filter | `--claimant <name>` |
| 「fork で何を」「あの fork セッション」 | session_log + Trek | `--include-session-logs --include-trek` |
| 「Trek で合意した」「○○ Trek の」 | Trek 取り込み | `--include-trek` |

これらは ms-79 で新設された retrospect 拡張 (= UC10-F1〜F4) で、cmd_search が指定された時のみ retro_query 共通基盤を経由する形になる。指定無しの普通の検索は従来通り。

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
  --limit 30 --json \
  [--source <human,auto-op,dm>] \
  [--actor <name>] [--claimant <name>] \
  [--include-bus-dm] [--include-session-logs] [--include-trek]
```

省略可能なフラグは Step 0 / Step 0.5 で抽出した内容に応じて付ける（無ければ付けない）。
**戦略 A**: 1 回だけ広く検索する。再帰展開・深掘り (戦略 B/C/D) は将来検討、現状は範囲外。

stdout が JSON で返る:
```json
{"results": [...], "total": N, "limit": L, "offset": 0,
 "facets": {"type": {...}, "source": {"human": N, "auto-op": M, "dm": K}, ...}}
```

`facets.source` は ms-79 / e-1833 で追加された source 別件数 (= 「該当 N 件: human X / auto-op Y / DM Z」)。Step 4 の要約に必ず含める (= UC10-F2 / F3 で「source 別の件数を一目で見たい」 を満たす)。

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

**該当 N 件**: human X / auto-op Y / DM Z  ← ms-79 / e-1833、facets.source から
                                              source filter / include_bus_dm 使用時
                                              のみ表示。普通の検索では省略。

**根拠**:
  - [e-id] (type, status, date) [title]
    - [本文/snippet からの抜粋。30-80 文字]
    - 出典: <marker> ← ms-79 / e-1835、fork 由来 / DM 由来 / Trek 由来
                       の row には marker を付ける (例: "fork 由来" / "DM 由来"
                       / "Trek 由来")。通常 commit / task は省略
    - 🔗 https://beacon-ai.dev/?project=<project_id>#<url_hash>
  - [e-id] (type, status, date) [title]
    - ...

**関連**: [補足情報があれば。例: 「同テーマの未完了タスク N 件あり」「retro doc に追加考察あり」]
```

ポイント:
- **結論**: 「実装済み / 未実装 / 部分実装」のような端的な答え
- **該当 N 件**: source 別の breakdown を 1 行で示す (= UC10-F2)。ms-79 拡張フラグ使用時のみ
- **根拠**: エントリ ID + 種別 + 日付 を必ず併記 (引用元が辿れる)
- **出典 marker**: fork 子セッション / DM 由来 / Trek 由来は明示 (= UC10-F4)
  - `result.from_fork == true` → "fork 由来"
  - `result.from_dm == true` → "DM 由来"
  - `result.from_trek == true` → "Trek 由来"
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
