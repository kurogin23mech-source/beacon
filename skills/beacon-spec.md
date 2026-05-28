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

## Step 3: Socratic 対話 (6 セクションを順に埋める)

**一度に1問だけ**。methodology の対話原則 (推測明示、ユーザー言葉を使う、解決策に飛ばない) を厳守。

内部追跡:
```
spec_draft = {
  "背景":            "<unknown | vague: ... | fill: ...>",
  "解決すべき問題":   "<unknown | vague: ... | fill: ...>",
  "設計方針":        "<unknown | vague: ... | fill: ...>",
  "スコープ":        "<unknown | vague: ... | fill: ...>",
  "受入条件":        "<unknown | vague: ... | fill: ...>",
  "実装順序":        "<unknown | vague: ... | fill: ...>"  (任意)
}
```

### セクションごとの掘り方

| セクション | 質問の例 | 注意点 |
|---|---|---|
| **背景** | 「このMSはどんなきっかけで生まれましたか？放置するとどう困りますか？」 | 推測で起点提示済みなら、訂正を受けるだけで OK |
| **解決すべき問題** | 「対処したい具体的な事象を 1〜5 個に絞ると、どれですか？」既存タスクから推測案を提示 | task description との対応を意識 |
| **設計方針** | 「やり方として、どんな指針で進めますか？特に "こうはしない" と決めたことがあれば」 | **必ず "なぜ" を聞く**。判断軌跡の核心 |
| **スコープ** | 「やる範囲を狭めるとしたら、どこを切りますか？混同されがちで対象外、というものは？」 | 「やらない」を必ず聞く |
| **受入条件** | 「どうなれば "完了" と言えますか？観察可能な事実 / 状態で」 | 計測可能な形に誘導 |
| **実装順序** | 「実装する順番に好みはありますか？最初にやらないと他が決まらない、というものは？」 | 依存関係があれば明記、なければスキップ可 |

### 推測でセクションを埋めるとき

起点材料から推測可能な部分は、内部状態を「推測:〜」として埋めて OK。
**Step 4 確認フェーズで「推測した」と明示** する (ユーザーが訂正可能な状態にする)。

### 終了条件

以下のいずれかで Step 4 へ:
- 6 セクション中 **5 つ以上** が `fill` (推測含む)
- ユーザーが「もうこれくらいで」と打ち切る発言
- 同じセクションを掘ろうとして新情報が 2 ターン出ない

## Step 4: 確認フェーズ

これまでの対話を踏まえ、構造化された SPEC を **テキストとして** 提示:

```
そろそろ整理できそうな感じがしてきたので、いったんまとめてみます。
違う部分があれば遠慮なく直してください。

---

# ms-XX SPEC: [title]

## 背景・なぜこのMSが必要か

[1〜3 段落]

## 解決すべき問題

| ID | 問題 |
|---|---|
| [task-id] | [問題の説明] |
| ... | ... |

## 設計方針 (判断軌跡)

### 1. [指針タイトル]
- 理由: [なぜそう決めたか]
- (代替案を却下した場合、その理由)

### 2. [指針タイトル]
- 理由: ...

## スコープ

### やる
- [箇条書き]

### やらない (別MS or 後回し)
- [箇条書き]

## 受入条件

1. [観察可能な事実 / 状態]
2. ...

## 実装順序のヒント  (任意セクション)

1. [依存・着手順]
2. ...

## 関連

- CORE doc: [doc-id] [title]
- 関連 MS: [ms-id] [title]

---

ここまでで「あ、そこ違う」「もっとこうしたい」とかありますか？
特に **設計方針の理由 (Why)** と **やらないこと** は、推測してる部分があれば訂正してください。

問題なければ「OK」と返してもらえれば、SPEC ドキュメントとして記録し、続けてタスクの分割案を提案します。
```

ユーザーから修正があれば、該当部分を更新して再提示。
**承認 (「OK」「これで」等) が来るまで Step 5 に進まない**。

## Step 5: SPEC の書き込み

ユーザー承認後、Bash ツールで実行:

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

書き込み完了後、ユーザーに `doc-id` を含めて報告:
```
SPEC を記録しました。
  doc-id: [returned doc-id]
  scope: spec, milestone: [ms-id]

続けて、SPEC からタスクを分割して一括起票します。
```

## Step 6: タスク分割の提案

SPEC を読み返し、task に分割する案を提示する。methodology の「タスク分割の基準」に従う。

### 6a. AI による分割案の作成

SPEC の「解決すべき問題」「設計方針」「実装順序のヒント」を起点に、task を 3〜10 個に分割する。
既に同じ MS に存在する task は重複起票しない (`beacon task list --json --ms <ms-id>` で確認済み)。

各 task に以下を AI 推定で付与:
- **description**: 短く具体的に (50 文字程度)
- **priority**: `highest` / `high` / `middle` / `low` のいずれか
- **motivation**: なぜこの task が必要か (SPEC の問題項目と紐づける)
- **acceptance_criteria**: 完了の判定基準 (観察可能な事実 or 状態)

### 6b. ユーザー確認

```
SPEC を読み、以下のタスクに分割する案を作りました。
既存タスク (N 件) と重複しないものだけです。

新規タスク候補:
1. [priority: high] [description]
   motivation: [理由]
   ac: [完了判定]

2. [priority: middle] [description]
   motivation: [理由]
   ac: [完了判定]

...

この案で起票してよいですか？
  - 全て: 全タスクを一括起票
  - 選択: 採用するタスク番号を指定 (例: "1,3,4")
  - 修正: 個別タスクの description / priority / motivation / ac を直したい
  - キャンセル: 起票しない
```

### 6c. 一括起票

ユーザー承認後、採用された task それぞれについて Bash ツールで実行:

```bash
beacon task add "[description]" -m <ms-id> \
  --priority [priority] \
  --motivation "[motivation]" \
  --acceptance-criteria "[ac]"
```

すべて起票完了後、ユーザーに結果を報告:

```
タスクを N 件起票しました:
  - [entry-id-1] [description]
  - [entry-id-2] [description]
  ...

ms-XX の SPEC 作成と task 分割が完了しました。
作業を始める場合は /beacon-session-start [ms-id] でコンテキストを読み込んでください。
```

## Step 7: トリガークリア

`milestone add` 直後の SPEC 作成トリガーが立っている場合、クリアする:

```bash
beacon trigger clear "spec-needed-<ms-id>" 2>/dev/null || true
```

(トリガー名は e-603 の実装で確定。存在しなくてもエラーにしない)

## 制約

- **methodology.md を読まずに進めない** (作法が崩れる)
- **対話は 1 問 1 答**: ユーザーが処理しきれない量を投げない
- **推測した部分は明示**: AI が補完した部分は「推測」と書き、ユーザーが訂正できる状態にする
- **詳細実装は SPEC に書かない**: 関数名・行数レベルは task の motivation/ac で扱う
- **設計方針には必ず "なぜ (Why)" を添える**: 判断軌跡の核心
- **すべての Bash 呼び出しで beacon CLI 経由**: `.beacon/project.json` を直接 Read/Write してはならない
- **Step 5 (書き込み) は承認後のみ**: 「OK」など明示の承認なしに進まない
- **Step 6 (一括起票) は SPEC 書き込みの後**: 順序を入れ替えない
