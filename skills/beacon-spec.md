---
name: beacon-spec
description: マイルストーンの SPEC (要求書 / 判断軌跡) を対話駆動で作成し、続けてタスク 3〜10 個に分割して一括起票する。フォーム入力ではなく Socratic 対話で 6 セクション (背景・問題・設計方針・スコープ・受入条件・実装順序) を埋める。
version: 0.1.0
triggers:
  - /beacon-spec
  - SPECを作る
  - 要求書を作る
  - 判断軌跡を残す
  - MSの要求を整理したい
---

# Beacon Spec

> マイルストーン単位の **要求書 / 判断軌跡** を対話駆動で作成し、続けて task に分解して一括起票する。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## 方法論の参照 (必読)

このSkillを実行する前に、必ず companion ドキュメントを読む:

```
Read("~/.claude/skills/beacon-spec/methodology.md")
```

methodology は SPEC の意味、対話の作法、各セクションの掘り方、タスク分割の基準を定義する。
読まずに進めると SPEC が「詳細仕様書」化して価値を失う。

## Step 0: 対象 MS の特定

ユーザーが `/beacon-spec ms-XX` のように引数付きで呼んだ場合は `ms-XX` を採用。
引数なしの場合は以下で対象を決める:

```bash
beacon status --json
```

判定:

| 状態 | アクション |
|---|---|
| `in_progress` の MS が 1 つだけ | それを対象に提示 (ユーザー確認) |
| `in_progress` が複数 / なし | ユーザーに「どの MS の SPEC を書きますか？」と尋ねる |

確定したら `$MS_ID` として保持。

## Step 0a: 既存 SPEC の確認

```bash
beacon doc list --scope spec --ms <ms-id> --json
```

既に SPEC が存在する場合、ユーザーに確認:

```
ms-XX にはすでに SPEC があります: [doc title]

今回は:
  1) 既存を更新する (変えたい部分を会話で話す)
  2) 新しい SPEC として追加する (revision ではなく別 doc)
  3) 何もしない

どうしますか？
```

「2」を選んだ場合は新規作成。「1」を選んだ場合は既存内容を読み込んでから対話に入る。「3」なら終了。

## Step 1: 起点材料の収集 (並列実行)

Bash ツールで以下を **並列に** 実行:

```bash
# 1a. プロジェクトビジョン
beacon doc show project-vision 2>/dev/null

# 1b. 対象 MS の詳細 (objective / ac / 既存 description)
beacon status --json --ms <ms-id>

# 1c. 関連 CORE doc 一覧
beacon doc list --scope core --json

# 1d. 既存タスク (motivation / ac 含む)
beacon task list --json --ms <ms-id>

# 1e. 直近コミット (最近の関心領域を把握)
git log --oneline -20
```

これらから AI は以下を抽出する:
- MS が解こうとしている課題の仮説
- 関連しそうな CORE 原則
- 既存タスクから読み取れる「やるべきこと」の輪郭

## Step 2: 起点提示 (オープニング)

AI が起点材料を踏まえ、以下のフォーマットで提示する:

```
ms-XX "[title]" の SPEC を作成します。

このMSの objective:
  [MS の objective 全文]

acceptance_criteria:
  [MS の ac 全文]

既存タスク: [N 件]
  - [entry-id] [description]
  ...

私が読んだ起点材料 (project-vision / CORE doc / 直近コミット) から、
このMSはこんな課題に対するものだと推測しました:

【推測】
  [背景・なぜこのMSが必要か の AI 仮説 を 2〜4 文]

この推測、合っていますか？訂正や追加があれば教えてください。
合っていれば、次に "解決すべき問題" を一緒に整理していきます。
```

ユーザーの応答を待つ。承認 or 訂正があれば内部状態 `spec_draft["背景"]` に確定情報として保持。

## Step 3: Socratic 対話 (必須 4 セクションのみ)

CORE doc `AeN9aPpjvh6URTQlFmb6` (確認は最小、FB はアウトプット主体) に従い、**Socratic で掘るのは 4 セクションだけ**:

| セクション | engagement | 理由 |
|---|---|---|
| 1 背景 | **必須 Socratic** | ユーザー本人しか語れない動機 |
| 2 解決すべき問題 | **必須 Socratic** | ユーザー本人しか語れない痛み |
| 3 設計方針 | **必須 Socratic** (高インパクト決定) | 技術選定は影響大、ユーザーの好み・経験が要る |
| 4 スコープ | **必須 Socratic** | やる/やらない の大枠はユーザー判断 |
| 5 受入条件 | **AI auto fill** | 詳細仕様に属する、AI が推測で十分 |
| 6 実装順序 | **AI auto fill** | 同上、AI が判断 |

**一度に1問だけ**。methodology の対話原則 (推測明示、ユーザー言葉を使う、解決策に飛ばない) を厳守。

内部追跡:
```
spec_draft = {
  "背景":            "<unknown | vague: ... | fill: ...>",    # 必須 Socratic
  "解決すべき問題":   "<unknown | vague: ... | fill: ...>",    # 必須 Socratic
  "設計方針":        "<unknown | vague: ... | fill: ...>",    # 必須 Socratic
  "スコープ":        "<unknown | vague: ... | fill: ...>",    # 必須 Socratic
  "受入条件":        "<AI auto fill>",                         # Step 4 で AI が埋める
  "実装順序":        "<AI auto fill>"                           # Step 4 で AI が埋める
}
```

### セクションごとの掘り方 (Socratic 必須 4 セクション)

| セクション | 質問の例 | 注意点 |
|---|---|---|
| **背景** | 「このMSはどんなきっかけで生まれましたか？放置するとどう困りますか？」 | 推測で起点提示済みなら、訂正を受けるだけで OK |
| **解決すべき問題** | 「対処したい具体的な事象を 1〜5 個に絞ると、どれですか？」既存タスクから推測案を提示 | task description との対応を意識 |
| **設計方針** | 「やり方として、どんな指針で進めますか？特に "こうはしない" と決めたことがあれば」 | **必ず "なぜ" を聞く**。判断軌跡の核心 |
| **スコープ** | 「やる範囲を狭めるとしたら、どこを切りますか？混同されがちで対象外、というものは？」 | 「やらない」を必ず聞く |

### 推測でセクションを埋めるとき

起点材料から推測可能な部分は、内部状態を「推測:〜」として埋めて OK。
**Step 4 のアウトプット提示で「推測した」と明示** する (ユーザーが触ってから訂正できる)。

### 終了条件

以下のいずれかで Step 4 へ:
- 必須 4 セクションが `fill` (推測含む)
- ユーザーが「もうこれくらいで」と打ち切る発言
- 同じセクションを掘ろうとして新情報が 2 ターン出ない

## Step 4: SPEC draft 構築 + 即記録 (Act first)

CORE doc `AeN9aPpjvh6URTQlFmb6` の「Act first, confirm next-step」に従い、**ユーザーに事前確認を取らずに SPEC を記録する**。SPEC は `beacon doc update` で後から編集可能なので、まず形にして見せるほうが goal への最短経路。

### 4a. AI が auto fill (受入条件・実装順序)

Step 3 で必須 4 セクションが埋まったら、残り 2 セクションを AI が推測で埋める:

- **受入条件**: 設計方針とスコープから観察可能な完了条件を 5〜10 個。具体的・測定可能な形に
- **実装順序**: 依存順 (環境構築 → 中核機能 → 周辺機能 → 検証) で 3〜7 ステップ。各 step に何をやるかの 1 行

### 4b. SPEC draft の構造化

完全な SPEC を内部生成 (テンプレートは下記)。

```
# ms-XX SPEC: [title]

## 背景・なぜこのMSが必要か
[1〜3 段落、Socratic で確定済み]

## 解決すべき問題
| ID | 問題 |
|---|---|
| [task-id] | [問題の説明] |

## 設計方針 (判断軌跡)
### 1. [指針タイトル]
- 理由: [なぜそう決めたか]
### 2. ...

## スコープ
### やる
- [箇条書き]
### やらない (別MS or 後回し)
- [箇条書き]

## 受入条件 (AI 推測 — 修正自由)
1. [観察可能な事実 / 状態]
2. ...

## 実装順序のヒント (AI 推測 — 修正自由)
1. [依存・着手順]
2. ...

## 関連
- CORE doc: [doc-id] [title]
- 関連 MS: [ms-id] [title]
```

### 4c. 即記録 → 報告 (事前確認なし)

draft が完成したら **そのまま** Step 5 で `beacon doc add` 実行。事前に「これでいいですか？」とは聞かない (CORE doc アンチパターン)。

## Step 5: SPEC の書き込み (Act)

Bash ツールで実行:

```bash
beacon doc add "ms-XX SPEC: [title]" --scope spec --ms <ms-id> --stdin <<'EOF'
# ms-XX SPEC: [title]

## 背景・なぜこのMSが必要か

[Step 4 で確定した内容]

## 解決すべき問題

[Step 4 で確定した内容]

## 設計方針 (判断軌跡)

[Step 4 で確定した内容、各方針に Why を明記]

## スコープ

[Step 4 で確定した内容、やる/やらない]

## 受入条件

[Step 4 で確定した内容]

## 実装順序のヒント

[Step 4 で確定した内容 (任意)]

## 関連

[Step 4 で確定した内容]
EOF
```

**既存 SPEC を更新する場合** (Step 0a で「1) 既存を更新」を選んだ場合):
```bash
beacon doc update <doc-id> --content "[新しい内容]"
```
(長文の場合、`/tmp/` に書き出して `--content "$(cat ...)"` ではなく stdin リダイレクト `< /tmp/...md` を使う)

書き込み完了後、結果報告 (確認は取らない、次フェーズへ即進む):

```
SPEC を記録しました ([doc-id])。
🔗 https://beacon-ai.dev/?project=<project_id>#doc/<doc-id>  (cloud mode の場合)
受入条件と実装順序は AI が推測で埋めました — 触ってみて気になれば指示してください。

続けてタスクに分割して起票します。
```

そのまま Step 6 へ進む (ユーザー応答待たず act)。

## Step 6: タスク分割と一括起票 (Act first)

SPEC を読み返し、タスクに分割して **そのまま起票する**。事前確認 (「起票していいですか？」) は取らない (CORE doc アンチパターン)。

### 6a. AI による分割案の作成

SPEC の「解決すべき問題」「設計方針」「実装順序のヒント」を起点に、task を 3〜10 個に分割する。
既に同じ MS に存在する task は重複起票しない (`beacon task list --json --ms <ms-id>` で確認済み)。

各 task に以下を AI 推定で付与:
- **description**: 短く具体的に (50 文字程度)
- **priority**: `highest` / `high` / `middle` / `low` のいずれか
- **motivation**: なぜこの task が必要か (SPEC の問題項目と紐づける)
- **acceptance_criteria**: 完了の判定基準 (観察可能な事実 or 状態)

### 6b. 即起票 (事前確認なし)

採用された task それぞれについて Bash ツールで実行:

```bash
beacon task add "[description]" -m <ms-id> \
  --priority [priority] \
  --motivation "[motivation]" \
  --acceptance-criteria "[ac]"
```

### 6c. 結果報告 + 次手確認

すべて起票完了後、ユーザーに結果を報告し、**次手だけ確認**:

```
タスクを N 件起票しました:
  - [entry-id-1] [priority] [description]
  - [entry-id-2] [priority] [description]
  ...

ms-XX の SPEC 作成と task 分割が完了しました。
気になるタスクがあれば指示してください (description / priority / motivation / ac の修正は `beacon task update` でできます)。

最初のタスク ([entry-id-1] [description]) の実装に進みますか？
```

ここで初めて **次フェーズ (実装開始) への確認** を 1 回取る。タスク内容の事前確認は取らない (修正は事後で十分)。

## Step 7: トリガークリア

`milestone add` 直後の SPEC 作成トリガーが立っている場合、クリアする:

```bash
beacon trigger clear "spec-needed-<ms-id>" 2>/dev/null || true
```

(トリガー名は e-603 の実装で確定。存在しなくてもエラーにしない)

## 制約

- **methodology.md を読まずに進めない** (作法が崩れる)
- **対話は 1 問 1 答**: ユーザーが処理しきれない量を投げない
- **推測した部分は明示**: AI が補完した部分は「推測」と書き、ユーザーが触ってから訂正できる状態にする
- **詳細実装は SPEC に書かない**: 関数名・行数レベルは task の motivation/ac で扱う
- **設計方針には必ず "なぜ (Why)" を添える**: 判断軌跡の核心
- **すべての Bash 呼び出しで beacon CLI 経由**: `.beacon/project.json` を直接 Read/Write してはならない
- **Act first, confirm next-step (CORE doc `AeN9aPpjvh6URTQlFmb6`)**:
  - Step 5 (SPEC 書き込み) と Step 6 (タスク起票) は **事前確認なしで実行**。SPEC / task は後から編集可能 (`beacon doc update` / `beacon task update`)、修正は事後で十分
  - 確認は **次フェーズ遷移の手前** (Step 6 完了後の「実装に進みますか？」) で 1 回のみ
  - 「これでいいですか？」「起票していいですか？」を事前に取らない
  - 5 択メニュー (全て / 選択 / 修正 / キャンセル / etc) は出さない
- **Beacon 独自用語の初出説明 (CORE doc `e-597` 方針)**:
  - 「SPEC」「マイルストーン」「タスク」「Operation」等は初出時に短い説明を併記
  - 例: ✗「SPEC ドキュメントを記録します」 / ✓「『何をどう作るか』を整理した記録 (Beacon では SPEC と呼ぶ) を作ります」
