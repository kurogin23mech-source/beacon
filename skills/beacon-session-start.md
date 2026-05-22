---
name: beacon-session-start
description: Beaconプロジェクトのセッション開始時にコンテキストを復元。アクティブMS・未消化タスク・summaryを提示する。
version: 0.6.0
triggers:
  - セッション開始
  - /beacon-start
  - /beacon-session-start
  - beacon の状態を確認
  - 現在のマイルストーンを確認
  - 開発を再開
  - 再開しよう
  - 前回の続き
  - 現状を確認
  - 今どうなってる
  - 状況を教えて
---

# Beacon Session Start

> セッション開始時に beacon CLI 経由でプロジェクトの現状を取得し、ユーザーに提示する。読み取り専用。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 0: 環境チェック（beacon doctor 軽量版）

Bash ツールで実行:
```bash
beacon doctor 2>&1
```

- 出力が `OK:` で始まる場合 → 何もせず次へ進む
- 警告が含まれる場合 → その警告をそのまま提示し、次へ進む（中断しない）
- `beacon` コマンドが存在しない場合 → スキップして次へ進む

## Step 0a: 引数チェック

ユーザーが `/beacon-session-start ms-XX` のように引数付きで呼んだ場合、`ms-XX` を **スコープMS** として記憶する。
複数指定も可能: `/beacon-session-start ms-16 ms-17`

スコープMSが指定された場合、Step 1a のコマンドが変わる。

## Step 1: プロジェクト状態の取得（並列実行可）

以下の3つを **Bash ツール** で **並列に** 実行する:

### 1a. プロジェクト全体の状態

スコープMSが **指定されている** 場合:
```bash
beacon status --json --ms <ms-id> [--ms <ms-id> ...]
```

スコープMSが **指定されていない** 場合（従来通り）:
```bash
beacon status --json
```
stdout に JSON が返る。以下のフィールドを使う:
- `name`: プロジェクト名
- `summary`: 前セッションの経緯・背景
- `milestones[]`: 各MSの `id`, `title`, `status`, `progress`, `total_tasks`, `done_tasks`

### 1b. git の最新コミット
```bash
git log --oneline -5
```

### 1c. ワーキングツリーの状態
```bash
git status --short
```

### 1d. COREドキュメント一覧
```bash
beacon doc list --scope core --json
```
stdout に JSON 配列が返る。各要素は `doc_id`, `title`, `scope`, `updated_at` を持つ。
空配列の場合はこのステップをスキップする。

## Step 1e: COREドキュメントの内容取得

Step 1d の結果が空でなければ、各ドキュメントの内容を **Bash ツール** で **並列に** 取得する:

```bash
beacon doc show <doc_id>
```

stdout にドキュメント本文（Markdown）が返る。frontmatter（`---` で囲まれた部分）は除去して本文のみ使う。

## Step 1f: アクティブMSおよびアクティブOperationのSPECドキュメント取得

Step 1a の結果から `status == "in_progress"` のマイルストーンIDと、`operations[]` のうち `status == "open"` のOperation IDを特定する。

あれば **Bash ツール** で **並列に** 実行:

```bash
# アクティブMSのSPEC
beacon doc list --scope spec --ms <active-ms-id> --json

# アクティブOperationのSPEC（各op-idに対して）
beacon doc list --scope spec --op <op-id> --json
```

結果が空でなければ、各ドキュメントの内容を Step 1e と同様に `beacon doc show <doc_id>` で **並列に** 取得する。

## Step 1g: GitHub PR 自動検知（fail-safe）

Bash ツールで **2つ並列に** 実行:

```bash
# 1. オープンPRの一覧（未記録PR検知に使用）
gh pr list --json number,title,url,author,headRefName,state 2>/dev/null

# 2. 全PR一覧（クローズ/マージ済み検知に使用）
gh pr list --state all --json number,title,url,state,mergedAt 2>/dev/null
```

どちらも失敗した場合（gh未設定、リポジトリ外など）は無視してスキップする。

取得できた場合、Step 1a または Step 2 の結果から beacon に記録済みの PR エントリ（`type == "pr"`）を収集し、以下の2つを照合する。

### 未記録オープンPRの検知

コマンド1の結果から、beacon に記録されていないオープンPRを特定する（URL照合）。

**未記録PRがある場合**、Step 3 の出力に含める:
```
未記録のPR:
  - PR#42: [title] (author: [login]) → beacon pr add で記録できます
```

### クローズ/マージ済みPRの検知（e-368）

コマンド2の結果を使い、beacon に `status == "open"` または `status == "in_review"` で記録されているが、GitHub 上では `state == "CLOSED"` または `state == "MERGED"` になっているPRを特定する。

beacon エントリの URL からPR番号を抽出し、コマンド2の結果と突き合わせる。

**該当PRがある場合**、Step 3 の出力に含める:
```
クローズ/マージ済みPR（beacon未更新）:
  - [e-xxx] PR#N: [title] — GitHub上では [closed / merged] → beacon pr close で更新できます
```

### レビュー待ちPRの検知

beacon に `review_status == "pending"` または `review_status == "changes_requested"` の PR がある場合:
```
レビュー待ちのPR:
  - [e-xxx] PR#N: [title] [in_review / review: pending]
```
→ この場合は Step 3 出力の後に `/review <pr_number>` を即時起動する（beacon trigger より優先）。

この Step は **読み取り専用**。自動で `beacon pr add` や `beacon pr close` を実行してはならない。

## Step 1h: GitHub Issue 自動検知（fail-safe）

Step 1g と **並列に** Bash ツールで実行:

```bash
beacon issue list --json 2>/dev/null
```

失敗した場合（gh未設定、リポジトリ外など）は無視してスキップする。

結果が空でなければ、Step 3 の出力に含める:
```
未インポートのIssue:
  - #42: [title] → beacon issue import 42 で取り込めます
```

3件以上ある場合は先頭2件を表示し「他N件: beacon issue sync で一括インポート」と追記する。

この Step は **読み取り専用**。自動で `beacon issue import` や `beacon issue sync` を実行してはならない。

## Step 2: アクティブMSの詳細取得

Step 1a の結果から `status == "in_progress"` のマイルストーンを特定する。
あれば **Bash ツール** で実行:

```bash
beacon task list --json --ms <active-ms-id>
```

stdout の JSON から:
- 未完了タスク: `entries[]` のうち `type == "task"` かつ `status != "done"` のもの（ネストされた `entries[]` 内も再帰的に確認）
- 直近コミット: `entries[]` のうち `type == "commit"` の最新3件

## Step 2.5: コンサルタントモード（次のマイルストーンが必要な場合）

以下のいずれかに該当する場合、通常の Step 3 出力の代わりに以下を行う:

- `milestones[]` が空（新規プロジェクト）
- `status == "in_progress"` または `status == "todo"` のマイルストーンが一件もない（done/observing/waitingのみ）

「やるべきことが前に存在しない」状態 = 次のマイルストーンを作るタイミング。

### 分岐: Project Archaeology か 白紙提案か

まず **git のコミット数** を確認する:

```bash
git log --oneline 2>/dev/null | wc -l
```

- **コミット数 >= 10** かつ git が初期化されている場合 → **Project Archaeology フロー（A）**
- **コミット数 < 10** または git 未初期化の場合 → **白紙提案フロー（B）**

---

### フロー A: Project Archaeology（リポジトリ遡行推測）

#### Step A1: 情報収集（並列 Bash 実行）

以下を **同時に** 実行する:

```bash
# A1-1: コミット履歴（最大200件）
git log --oneline -200

# A1-2: 直近コミットの変更ファイル（傾向把握）
git log --stat -10

# A1-3: タグ一覧（リリース境界の手がかり）
git tag --sort=-creatordate | head -10

# A1-4: README（プロジェクト概要）
cat README.md 2>/dev/null || cat README.rst 2>/dev/null || cat README.txt 2>/dev/null || echo ""

# A1-5: ファイル一覧（技術スタック判定）
ls -la

# A1-6: 言語/フレームワーク判定ファイル（存在するものだけ読む）
cat package.json 2>/dev/null; cat Cargo.toml 2>/dev/null; cat pyproject.toml 2>/dev/null; cat go.mod 2>/dev/null; cat build.gradle 2>/dev/null; cat pom.xml 2>/dev/null
```

#### Step A2: AI 解釈

収集した情報から以下を推測する:

1. **Objective の言語化**
   - ユーザー目線で「このプロジェクトが完成したら何ができるようになるか」を1文で表現
   - 形式: 「〜できるようになる」「〜が実現する」
   - README・package.json の description・コミットメッセージのテーマを総合的に判断

2. **過去フェーズのクラスタリング（3〜7個）**
   - git log のコミットメッセージをテーマでグループ化
   - 手がかり:
     - コミットメッセージの語彙変化（「init」「setup」→「feat」→「fix」→「refactor」等）
     - feat/fix の比率が変わるタイミング
     - タグが打たれた境界
     - ファイル変更の傾向（初期は多数ファイル、後期は特定領域に集中）
   - 各フェーズに「何ができるようになったか」を表すタイトルをつける
   - git log の日付から各フェーズのおよその時期（YYYY年M月頃）を付与

3. **現在地の特定**
   - 直近 30 コミットの傾向から、現在何に取り組んでいるかを推測

4. **次 MS の提案（1〜3個）**
   - 現在地から自然につながる次の一手
   - 「何ができるようになるか」形式でタイトル化

#### Step A3: ユーザーへの提示

```
このリポジトリを分析しました。

プロジェクト概要（推測）: [objective — ユーザー目線の1文]

開発の歩み（推測）:
  ● [フェーズ1タイトル]  (YYYY年M月頃)
  ● [フェーズ2タイトル]  (YYYY年M月頃)
  ● [フェーズ3タイトル]  (YYYY年M月頃)
  ◐ [現在進行中フェーズ]  (YYYY年M月〜)

次のマイルストーン候補:
  1. "[提案1]"
     理由: [なぜこれが次の一手として適切か]

  2. "[提案2]"（別の方向性があれば）
     理由: [...]

調整があれば教えてください。このまま登録しますか？
```

#### Step A4: ユーザー承認後の登録（書き込みフェーズ）

ユーザーが承認（「はい」「登録して」「OK」等）した場合のみ実行する:

```bash
# 1. Objective をサマリーに設定
beacon summary "<推測したobjective>"

# 2. 過去完了フェーズを登録（古い順に）
beacon milestone add "<フェーズ1タイトル>"
# → 返り値の ms-id を使って
beacon milestone done <ms-id>

beacon milestone add "<フェーズ2タイトル>"
beacon milestone done <ms-id>
# ... 完了分をすべて登録

# 3. 現在進行中フェーズを登録・開始
beacon milestone add "<現在進行中フェーズ>"
beacon milestone start <ms-id>

# 4. 次MS候補を登録（todo 状態）
beacon milestone add "<次MS提案1>"
# （提案2があれば続けて追加）
```

**注意**: Step A4 はユーザーの明示的な承認なしに実行してはならない。提示後は必ず確認を取る。

---

### フロー B: 白紙提案（コミット数 < 10 または git 未初期化）

#### やること

1. CORE ドキュメントがあれば読む（Step 1d/1e の結果を利用）
2. **ソースコードを読んで実装状況を把握する**（以下を並列実行）:

```bash
cat README.md 2>/dev/null
```

```bash
find . -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.py" -o -name "*.vue" -o -name "*.go" -o -name "*.rb" \) \
  -not -path "*/node_modules/*" -not -path "*/.git/*" \
  -not -path "*/dist/*" -not -path "*/__pycache__/*" | head -40
```

READMEがあればそれを読む。ファイル一覧からルーター・型定義・ページ・モデル等の主要ファイルを特定し、3〜5件を並列Readする。

**この情報はドキュメントとして保存しない。提案の精度向上のみに使う。**

3. プロジェクト名・objective・ソースコードの実態を踏まえて、**最初のマイルストーン候補を1〜3個提案する**

#### 提案の視点（重要）

- **「何を作るか」ではなく「何ができるようになるか」** でタイトルをつける
- objective を起点に考える。最終ゴールに向かう最初の一歩として、ユーザーが体験できる状態変化を表現する
- 「基盤構築」「パイプライン設計」のような技術的な工程名は避ける
- 例：objective が「家計の無駄遣いを減らして貯金を増やしたい」なら
  - ✗ 「データ取り込みパイプラインの設計」
  - ✓ 「先月の支出を入力して、無駄な出費のパターンを一覧で見られるようにする」

#### 出力フォーマット

```
Beacon: [name] — [MSゼロなら「まだマイルストーンがありません」/ done MSがあれば「次のマイルストーンを決めましょう」]
---
[objective・summary・完了済みMSの流れを一言で解釈]

[完了済みMSがある場合は「ここまで達成しました：〇〇、△△」を一行添える]

次のマイルストーンをこう考えます：

  1. "[提案タイトル]"
     理由: [なぜこれが最初の一手として適切か]

  2. "[提案タイトル]"（もし別の方向性があれば）
     理由: [...]

どれかを選ぶか、別のゴールを教えてもらえれば `beacon milestone add` で登録します。
```

---

コンサルタントモード（フロー A または B）の後は Step 4（トリガーチェック）に進む。通常の Step 3 出力は不要。

## Step 3: 照合と提示

Step 1〜2 の結果を組み合わせて、以下のフォーマットで **テキスト出力** する。
不要なセクション（未消化タスクがない、未記録コミットがない等）は省略してよい。

```
Beacon: [name]
---
ドキュメント (core=設計原則・常時参照 / spec=仕様・技術詳細 / memo=検討メモ):
  [CORE] [title]: [本文（短ければ全文、長ければ要約）]
  ...
  [SPEC] [title] (ms-xx): [要約]
  [SPEC] [title] (op-x): [要約]
  ...

前回の経緯: [summary]

Active Operation: [op-id] "[title]" [schedule.frequency]  ← openのOperationがある場合
  直近のrun: [date] [✓ok/⚠warning/✗error] / [date] ... （最新3件）
  未解決Incident: [N]件                                    ← あれば
    - [e-id] [title]

Active: [ms-id] [title] ([progress]%) [done_tasks]/[total_tasks]タスク完了
  未消化タスク:
  - [entry-id] [description]
  - [entry-id] [description]
  直近コミット:
  - [hash] [description]

他のマイルストーン:
  [status-icon] [ms-id] [title] ([progress]%)
  ...

未記録のコミット: [git logのハッシュがbeaconエントリに存在しないもの]
uncommitted changes: [git statusの結果があれば]
---
何から始めますか？
```

status-icon の対応: done=●, in_progress=◐, todo=○, waiting=◌, observing=◔

**照合ルール**:
- git log の各コミットハッシュ（先頭7文字）が、Step 1a または Step 2 のエントリの `meta.hash` に存在するか確認
- 存在しないものを「未記録のコミット」として表示

## Step 4: トリガーチェック

Bash ツールで実行:
```bash
beacon trigger check
```

JSON 配列が返る。空でなければ、各トリガーの `message` を出力の末尾に追加:
```
Beacon trigger: [message]
```

トリガーへの対応（例: `/beacon-retro` の実行）はユーザーの判断に委ねる。自動実行してはならない。

## 制約

- **読み取り専用**: project.json への書き込みは一切行わない。`beacon log`, `beacon task add/done`, `beacon summary "text"` 等の書き込みコマンドを実行してはならない。
- **データ取得は Bash ツール経由の beacon CLI `--json` 出力のみ**: Read ツールで `.beacon/project.json` を直接読んではならない。
- **出力はコンパクトに**: 完了済みマイルストーンの配下エントリは展開しない。
