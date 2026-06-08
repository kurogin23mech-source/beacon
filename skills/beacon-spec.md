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

## 起動ポリシー (重要、CORE doc `PU9HG2IVQdW3tLiAJvix` / `4AS5ehyJc8mGU1gsiFvz` 準拠)

このSkillは **ユーザーが明示的に呼んだ時だけ動く**。`/beacon-init` `/beacon-roadmap` などからの **自動チェーンはしない**。

- Beacon の北極星 (Philosophy) は「最低限の情報でまずアウトプット、FB で質を上げる」
- SPEC = 要求書 / 判断軌跡 は「触ってみて見えてきた判断を残したい」「他人に説明する必要が出てきた」と感じたユーザーの機能 (default off)
- バイブコーダーの default は SPEC 無しで進む、欲しくなった時に取りに行く
- 「SPEC 書きたい」「判断軌跡残したい」「要求書を作りたい」と明示された時だけ価値を出す

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

## 文章の書き方 (必読)

SPEC 本文 + 起票する各 task の description / motivation / acceptance_criteria は、CORE doc **`entry-writing-principle`** (doc_id `F3ZkqT0pKS6JpR8dn70n`) の **3 層構造 + 横文字 3 段階 + ID 参照ルール** に従う。Beacon のターゲットには非開発者が含まれるため、開発者の癖 (横文字濫用 / 別 task ID への click-through 前提 / 主語省略) は読み手を排除する。原則の要点:

1. **description は読み手目線で 1 行**: 「何が嬉しいか」をユーザー体験の言葉で。横文字最小、価値で書く
2. **motivation は背景 2-4 文**: 現状の症状 → 放置できない理由 → どう改善されると嬉しいか
3. **acceptance_criteria は bullet で技術詳細**: 横文字には初出時に日本語注 (`allowlist (= 許可リスト)`)、別 task ID 参照には『何の話か』1 行を添える

横文字 3 段階:
- そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP` / `WebSocket`)
- 初出時に日本語注: 技術概念 (`allowlist` / `opt-in` / `subcollection`)
- 日本語化が望ましい: 一般概念 (configure → 設定 / receiver → 受信側 / audit → 監査)

SPEC 自体は Step 4 で記述する時、task は Step 6 で分割する時、両方で必ず参照する。

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

## Step 3: 未知の決定だけ質問 (Socratic 最小化)

CORE doc `AeN9aPpjvh6URTQlFmb6` (確認は最小、FB はアウトプット主体) に従い、**「ユーザーから新情報を引き出す質問」だけを聞く**。AI の推測を summary back して OK 取るためだけの質問は **禁止**。

### 価値ある質問 vs 価値ない質問

| ❌ アンチパターン (聞かない) | ✅ 価値ある質問 (聞く) |
|---|---|
| 「背景こうですか？」(project-vision + objective から推測可能、AI が draft に書けばよい) | 「データソースは A / B / C のどれですか？」(技術的核心、ユーザー判断必須) |
| 「問題こうですか？」(objective + AC から推測可能) | 「月 1 の手動 DL は許容できますか？」(scope tradeoff、ユーザーの好みが必要) |
| 「実装順序こうですか？」(AI が依存順で決められる) | 「ms-3 を先にやる？それとも ms-2 から順？」(優先順位、ユーザー判断) |
| 「スコープ こうですか？」(vision の「やらないこと」から推測可能) | 「ms-1 で○○もやる？それとも別 MS にして scope を絞る？」(境界判断) |

判断基準: **「もしユーザーが何もコメントしなくても AI が draft に書ける情報か？」**  
書けるなら聞かない。書けない (未知の決定が含まれる) なら聞く。

### 必須 4 セクションの扱い方 (Socratic ではなく "未知の決定" だけ)

| セクション | 通常の扱い | 質問する場合 |
|---|---|---|
| **背景** | project-vision + MS objective から AI が推測して draft に直接書く | ほぼ聞かない (推測で十分) |
| **解決すべき問題** | MS objective + AC + 既存 task から AI が推測して draft に直接書く | ほぼ聞かない (推測で十分) |
| **設計方針** | **未知の核心決定がある場合だけ質問** (技術選定、データ経路、自動化 vs 手動 等) | 質問する: 1〜3 個に絞る |
| **スコープ** | vision の「やらないこと」+ 他 MS との関係から AI が推測 | 質問する: ms 間の境界が曖昧な時のみ |

### 終了条件

未知の決定への質問が全部終わった (or 最初から不要だった) ら即 Step 4 へ。  
**「draft を全部出して見せる前に summary 確認を取る」ことは禁止**。draft は Step 4 でまとめて出す。

### 例: 健全な対話パターン

```
AI: 「ms-1 の SPEC 作ります。
     project-vision + objective から、AI が draft をほぼ書けます。
     1 点だけ判断が必要: データソースは A / B / C のどれにしますか？理由含めて簡単に。」
User: 「B でいい。理由は X」
AI: (draft 完成 → Step 4 で記録)
```

質問は 1 ターンで終わる。

### 例: アンチパターン (今までやっていた挙動)

```
AI: 背景これでいい？      ← user "OK" 以上の情報出ない → 質問の意味なし
User: OK
AI: 問題これでいい？      ← 同上
User: OK
AI: 設計方針 PC か スマホ？  ← ここで初めて価値ある質問
User: スマホ
AI: スコープこうでいい？  ← 推測のままで draft に書ける、聞く意味なし
User: OK
```

これは 4 ターン中 1 ターンしか価値が無い (4 ターンとも user が "OK" 以外を返さない)。
新しい原則では:
```
AI: 設計方針: スマホ/PC どっち？ 1 問だけ判断ください。
User: スマホ
AI: (draft 完成 → 即記録)
```
で済む。

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

書き込み完了後、結果報告 (確認は取らない、次フェーズへ即進む)。**mode 別に保存先案内を分岐**:

**cloud mode の場合**:
```
ms-XX の SPEC を記録しました。
🔗 https://beacon-ai.dev/?project=<project_id>#doc/<doc-id>
   (Web UI の Documents タブ → 「ms-XX SPEC: [title]」)
   受入条件と実装順序は AI が推測で埋めました — 触ってみて気になれば指示してください、`beacon doc update <doc-id>` で直せます。

続けてタスクに分割して起票します。
```

**local mode の場合** (Beacon Desktop App 起動中):
```
ms-XX の SPEC を記録しました。
📄 Beacon Desktop App の Documents タブ → 「ms-XX SPEC: [title]」 で確認できます。
   受入条件と実装順序は AI が推測で埋めました — 触ってみて気になれば指示してください、`beacon doc update <doc-id>` で直せます。

続けてタスクに分割して起票します。
```

そのまま Step 6 へ進む (ユーザー応答待たず act)。`project_id` は `.beacon/cloud.json` から取得、`doc-id` は `beacon doc add` の戻り値から。

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

すべて起票完了後、ユーザーに結果を報告し、**MS planning レベルで次手を確認**。

ポイント: 「最初のタスクの実装に進みますか？」のような **タスク単位の質問は粒度が小さすぎる**。  
ユーザーが本当に判断したいのは「この MS を今すぐ進めるか / 他の MS も詰めてから並列実装するか / 一旦止めるか」という MS planning レベルの選択。

### 次手判定 (active MS の数による分岐)

まず `beacon status --json` で他のアクティブ MS の状況を把握 (Step 1 で取得済みの情報を流用):

#### A. 他に SPEC 無しのアクティブ MS がある場合
```
タスクを N 件起票しました:
  - [entry-id-1] [priority] [description]
  ...

ms-XX の SPEC とタスク分割が完了しました。
気になるタスクがあれば指示してください (修正は `beacon task update`)。

次の手:
  1. **ms-XX をこのまま実装に着手** — タスク順に進める
  2. 他のアクティブ MS (ms-Y, ms-Z, ...) の SPEC も先に詰めてから、まとめて並列実装 (`/beacon-dispatch`)
  3. ここで一旦中断

どうしますか？(私の推奨は [文脈に応じて 1 or 2、理由付き])
```

#### B. 他のアクティブ MS が全部 SPEC 済み (or 単独 active MS) の場合
```
タスクを N 件起票しました:
  ...

ms-XX の SPEC とタスク分割が完了しました。
気になるタスクがあれば指示してください (修正は `beacon task update`)。

次の手:
  1. **ms-XX の実装に着手** — タスク順に進める
  2. 他のアクティブ MS [ms-Y, ms-Z] と並列で実装 (`/beacon-dispatch`)
  3. ここで一旦中断

どうしますか？
```

#### C. 単独 active MS で他に SPEC 必要な MS も無い場合
```
タスクを N 件起票しました:
  ...

ms-XX の SPEC とタスク分割が完了しました。

実装に着手しますか？それともここで中断？
```

### 推奨の出し方

選択肢には **AI の推奨を理由付きで添える** (Act first の精神: 立場を明示)。  
例:
- 「私の推奨は **1 (今すぐ実装)**: ms-XX は他の MS の前提になるので、先に動くものができていると後段が見通しやすくなります」
- 「私の推奨は **2 (並列前準備)**: 他 4 つの MS も SPEC が無く、並列実装の足場を先に整えるほうが効率的です」

ユーザーは番号で返してもよいし、自由テキストで「ちょっと中断」「ms-2 から先にやる」など指示してもよい (open-ended)。

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
