---
name: beacon-session-start
description: Beaconプロジェクトのセッション開始時にコンテキストを復元。アクティブMS・未消化タスク・summaryを提示する。
version: 0.7.0
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
  # archaeology / project history 系 (F30: ユーザーが概念名で呼ぶケース)
  - archaeology して
  - Archaeology して
  - 過去掘って
  - 過去の経緯まとめて
  - これまでの流れまとめて
  - コード読んで提案して
  - リポジトリ分析して
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

## Step 0b: bus heartbeat (ms-54 e-1150)

Bash ツールで実行:
```bash
beacon session id > /dev/null 2>&1 || true
```

これだけ。`beacon session id` は `lib/session.update_last_active()` を叩いて以下を起こす:
- `.beacon/session.json` を mint / 更新 (last_active bump)
- cloud mode なら sessions/ subcollection に push (debounce 内)

これにより:
- 起動した session が `beacon bus directory --live` で discoverable になる
- 同プロジェクトの他 session から DM を打つ宛先として現れる
- channel/bus.mjs が起動時に走らせる経路と同じものを idempotent に補完 (channel 未 install の session でも heartbeat が成立)

失敗 (`.beacon/project.json` 不在等) しても無視。session-start 全体は止めない。

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

### 1d-2. メモスコープのドキュメント一覧 (ms-43 e-564)

前セッション末で memo scope に昇格させた重要メモを引き継ぐため、memo scope も一覧する:
```bash
beacon doc list --scope memo --json
```

memo は CORE と違って常時参照ではないので、**取得するのは「直近 7 日以内に作成/更新された memo」のみ**。古い memo は無視する (ノイズ削減)。

`updated_at` を ISO8601 でパースし、今日からの差分日数で 7 日以内を残す。3件を超える場合は最新 3 件に絞る。

## Step 1e: COREドキュメントの内容取得 (要約モード)

Step 1d の結果が空でなければ、各ドキュメントの内容を **Bash ツール** で **並列に** 取得する:

```bash
beacon doc show <doc_id>
```

stdout にドキュメント本文（Markdown）が返る。frontmatter（`---` で囲まれた部分）は除去して本文のみ使う。

### ms-43 e-566: 出力情報量の最適化

CORE doc は **全文を Step 3 出力に展開しない** (トークン浪費)。代わりに以下の戦略を取る:

1. 各 CORE doc に対し、AI が「**1 行サマリー**」を生成する (元本文の見出し・冒頭段落・タイトルから推定。50〜80 文字目安)
2. Step 3 では `[CORE] [title]: [1行サマリー]` のみを出力する
3. 詳細が必要な場合の参照経路をフッタで明示:
   - Web UI を開いている場合 → 「Documents タブで全文閲覧可能」
   - CLI 派の場合 → `beacon doc show <doc_id>` でいつでも展開できる旨を末尾に 1 行添える

例 (旧出力):
```
[CORE] doc-classification:
（500 トークン分の本文全文…）
```

例 (新出力):
```
[CORE] doc-classification: ドキュメントを core/spec/memo/report の 4 つで分類する原則。
```

**例外**: ユーザーが `/beacon-session-start --verbose` 引数で呼んだ場合、または対象 CORE doc が 200 文字以下なら全文を出してよい。
**スコープ MS 指定時**は、その MS に関連すると AI が判断した CORE doc のみ全文展開してよい (作業に直結するため)。

### Step 1e-2: memo scope の取得 (ms-43 e-564)

Step 1d-2 で 1 件以上残っていれば、各 memo doc も **Bash ツール** で **並列に** 取得:

```bash
beacon doc show <doc_id>
```

CORE と同じく **1 行サマリー** に圧縮して Step 3 出力に含める。「前セッションからの引き継ぎメモ」セクションとして提示する。

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

### SPEC 無し active MS の検出 (warning only, never block)

active な MS (`status == "in_progress"`) で SPEC が **1 つも存在しない** ものを **SPEC 無し MS** として記憶する。

これは CORE doc `doc-classification` および ms-41 SPEC で確立した運用:
- SPEC = 要求書 / 判断軌跡 (詳細仕様書ではない)
- SPEC 無しでも作業は続行可能 (hard block しない)
- 但しサブエージェント dispatch・retrospection・onboarding の質が下がるため、warning で促進

Step 3 の出力でこのリストを表示する (後述):
```
SPEC 無し active MS:
  - [ms-id] [title] → `/beacon-spec [ms-id]` で対話駆動作成できます
```

なお、これと並行して `beacon trigger check` (Step 4) も `spec-needed-<ms-id>` トリガーを返す。両者は同じ事象を別経路で通知している (trigger は MS 追加時 fire、こちらは session-start 時のスキャン)。重複表示は冗長なので、**warning 表示はどちらか一方** (典型的には trigger を優先) で構わない。

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

### F30: 起動原因の橋渡しメッセージ

ユーザーが「archaeology して」「過去掘って」「リポジトリ分析して」等の **概念名** で起動した場合、ユーザー視点では「archaeology Skill」を呼んだつもりが `/beacon-session-start` が動くため、Skill 名のミスマッチで一瞬「あれ違う Skill？」となる。

以下のキーワードが直近の user 発話に含まれていたら、コンサルタントモードの出力の **最初の 1 行** に橋渡しメッセージを添える:

- `archaeology` / `Archaeology`
- `掘って` / `経緯` / `これまでの流れ`
- `リポジトリ分析` / `コード読んで`

```
(Archaeology を含む /beacon-session-start を起動します — git log とコードを読んで提案します)

このリポジトリを分析しました。
...
```

含まれていなければ橋渡し行は不要。

### 分岐: 常に B (code reading)、git 履歴あれば A も追加 (F29)

排他分岐ではなく **加算構成**:

```bash
git log --oneline 2>/dev/null | wc -l
```

- **常に B (code reading) を実行**: README/source/設定ファイルを読んでプロジェクトの現状を理解する
- **追加で `git_commits >= 10` の時のみ A (Archaeology) を実行**: git log clustering で過去フェーズを推測する
- B の結果と A の結果を **統合して提案を出す**

つまり「コード文脈は常に拾う、git 履歴がある時は追加で過去経緯も拾う」。閾値で **排他にしない** (commit 少の既存リポでも code reading は走る)。

A 単独実行時のエッジケースは自然に degrade:
- `commits == 1` (初期コミットのみ) → A は phase 0〜1 個しか作れない、B の code reading が主軸になる
- `commits >= 10` → A の phase clustering が主軸、B が補完
- `commits == 0` (git 未初期化) → A スキップ、B のみ

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

## Step 2.7: Web UI を自動オープン（cloud mode の場合）

Beacon の作業形態は「ターミナル + Web UI 並列表示」が前提。  
session-start 時に Web UI を立ち上げ直す（既に開かれていればブラウザが既存タブを focus する）。

```bash
# Bash 呼び出し
# ms-46 e-737: open URL は macOS が Beacon.app を URL handler として解釈し
# Tauri を起動してしまうケースがある (cloud mode で Tauri が起動すると
# ローカルキャッシュ表示で混乱)。ブラウザを明示的に指定して回避。
# macOS: Python webbrowser は LSGetDefaultRoleHandler を使うので Beacon.app
# を回避できないことがある → -b で default browser app ID を直接渡す。
# 検出失敗時は Safari にフォールバック (System 標準で必ず存在)。
if [ -f .beacon/cloud.json ]; then
  PROJECT_ID=$(python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id',''))")
  if [ -n "$PROJECT_ID" ]; then
    WEBUI_URL="https://beacon-ai.dev/?project=$PROJECT_ID"
    # macOS: 既定の https handler を取得 (Beacon.app になっていたら Safari にフォールバック)
    DEFAULT_BROWSER=$(python3 -c "
import subprocess, plistlib, os, sys
p = os.path.expanduser('~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist')
try:
    with open(p, 'rb') as f: d = plistlib.load(f)
    for h in d.get('LSHandlers', []):
        if h.get('LSHandlerURLScheme') == 'https':
            r = h.get('LSHandlerRoleAll', '')
            if r and 'beacon' not in r.lower():
                print(r); sys.exit(0)
except Exception: pass
print('com.apple.Safari')
" 2>/dev/null || echo 'com.apple.Safari')

    (open -b "$DEFAULT_BROWSER" "$WEBUI_URL" 2>/dev/null \
      || open -a Safari "$WEBUI_URL" 2>/dev/null \
      || xdg-open "$WEBUI_URL" 2>/dev/null \
      || cmd.exe /c start "$WEBUI_URL" 2>/dev/null \
      || powershell.exe -Command "Start-Process '$WEBUI_URL'" 2>/dev/null) &
    echo "WEBUI_URL=$WEBUI_URL"
  fi
fi
```

取得した URL は Step 3 の出力ヘッダに表示する。  
local mode（cloud.json 無し）の場合はこのステップをスキップ。

## Step 2.9: 次セッション最初の作業の特定 (ms-43 e-568)

Step 3 の出力末尾に「**次の一手**」を **AI が決定的に選ぶ** ためのロジック。
これまで session-start は「何から始めますか？」とユーザーに丸投げしていたが、文脈情報を全部持っているのは AI なので、AI が **最有力候補を 1 つ提案** し、ユーザーは承認 or 別案で応答するだけにする。

### 優先順位 (上から順に評価し、最初にヒットしたものを採用)

1. **未解決 Incident がある** → `/beacon-incident-report` で close + report 作成
2. **レビュー待ち PR がある** → `/review <pr_number>`
3. **`beacon trigger check` で active なトリガーがある** → そのトリガーの推奨アクション
4. **アクティブ MS に未消化タスクが 1 つ以上ある**
   - そのうち `priority == "highest"` があればそれを最優先
   - 次に `in_progress` 状態のタスク
   - 次に `assignee` が自分 (current member) のタスク
   - それも無ければ todo 状態の先頭タスク
5. **アクティブ MS の SPEC が無い** → `/beacon-spec <ms-id>` で SPEC 作成
6. **アクティブ MS が無い** → 「次のマイルストーンを決めましょう」(コンサルタントモード Step 2.5 と同じ)
7. **どれも該当しない** → 「観察モード: 直近 retro を見直すか、cleanup 作業に着手するか」

### 出力フォーマット

```
次の一手: [推奨アクション (短く、1 行)]
  根拠: [なぜこれを推奨するか — 上記優先順位のどれにヒットしたか]
  別の選択肢: [候補 2 つを箇条書き、なければ省略]
```

「別の選択肢」は同じ優先度の他タスク・next MS 提案など。**断定し過ぎず、ユーザーが override しやすい** 余地を残す。

## Step 3: 照合と提示

Step 1〜2 の結果を組み合わせて、以下のフォーマットで **テキスト出力** する。
不要なセクション（未消化タスクがない、未記録コミットがない等）は省略してよい。

```
Beacon: [name]
📊 Web UI: $WEBUI_URL  ← cloud mode の場合のみ
---
⚠️  未解決 Incident: [N]件  ← e-595 / 一件でもあれば最上位に出す。無ければセクションごと省略
  - [e-id] "[title]" (op-X) — open since [created_at]
  - ...
  → 解決済みなら /beacon-incident-report で close + report を作成してください。
  → /beacon-operation-review でも close 誘導が走ります。

ドキュメント (core=設計原則・常時参照 / spec=仕様・技術詳細 / memo=検討メモ):
  [CORE] [title]: [1行サマリー (ms-43 e-566)]
  ...
  [SPEC] [title] (ms-xx): [要約]
  [SPEC] [title] (op-x): [要約]
  ...
  [MEMO] [title] ([N日前]): [1行サマリー]   ← ms-43 e-564 / 直近7日以内のみ
  ※ 詳細は Web UI Documents タブ または `beacon doc show <doc_id>` で。

前回の経緯: [summary]

Active Operation: [op-id] "[title]" [schedule.frequency]  ← openのOperationがある場合
  直近のrun: [date] [✓ok/⚠warning/✗error] / [date] ... （最新3件）

Pending Operation: [op-id] "[title]"  ← todo/in_progressのOperationがある場合
  status: [todo/in_progress]
  activation_hint: [hint があれば]
  準備項目: [done]/[total] 完了
    - ○ [entry-id] [operation_task description]
    - ●  ...
  → 「活性化議論」セクション参照

Active: [ms-id] [title] ([progress]%) [done_tasks]/[total_tasks]タスク完了
  未消化タスク:
  - [entry-id] [description]
  - [entry-id] [description]
  直近コミット:
  - [hash] [description]

他のマイルストーン:
  [status-icon] [ms-id] [title] ([progress]%)
  ...

SPEC 無し active MS (warning):     ← Step 1f で検出した SPEC 無しactive MS がある場合のみ
  ⚠ [ms-id] [title] — `/beacon-spec [ms-id]` で対話駆動作成できます
  ※ SPEC = 要求書/判断軌跡。dispatch / retrospection の質が下がるため、作成を推奨

未記録のコミット: [git logのハッシュがbeaconエントリに存在しないもの]
uncommitted changes: [git statusの結果があれば]
---
次の一手 (ms-43 e-568): [Step 2.9 で決定した推奨アクション]
  根拠: [なぜこれを推奨するか]
  別の選択肢: [候補 1] / [候補 2]  ← 任意

どうしますか？（このまま進めるなら「OK」「進めて」、別の作業なら指示ください）
```

**未解決 Incident セクション (e-595):**
Step 2 で取得した entries (および Operation 配下の incident エントリ) のうち `type == "incident"` かつ `status == "open"` のものを抽出する。一件でもあれば **必ず最上位に表示**する。これは UX レビュー (UC7-L8) で「閉じられていないインシデントが見落とされる」実害があったため、構造的にユーザーの目に最初に入る位置に固定する。

status-icon の対応: done=●, in_progress=◐, todo=○, waiting=◌, observing=◔

**照合ルール**:
- git log の各コミットハッシュ（先頭7文字）が、Step 1a または Step 2 のエントリの `meta.hash` に存在するか確認
- 存在しないものを「未記録のコミット」として表示

## Step 3.5: セッションメモの表示

Bash ツールで実行:
```bash
beacon note list --json
```

結果が空でなければ、Step 3 の出力に追加:
```
セッションメモ（前回セッションから引き継ぎ）:
  [HH:MM] [context]: [text]
  ...
```

## Step 3.7: pending Operation の活性化議論

Step 1a の結果から `status == "todo"` または `status == "in_progress"` の Operation を取得する。空ならこの Step はスキップ。

各pendingOperationについて、AIが **総合的に判断する** （機械的なルールではなく文脈推論）:

### 判断材料
1. **activation_hint**: 設計時に書かれたヒント（例: 「本番デプロイ後」「ユーザー10人超えたら」）
2. **OperationTasks の消化状況**: `beacon operation task list -o <op-id> --json` で取得した未完了 / 完了状況
3. **プロジェクト全体の状態**: Step 1a の MS状態、進捗、ビジョン
4. **CORE ドキュメント (project-vision など)**: 「現時点でこの運用が必要か？」を判断する文脈

### 判断ロジック（AI が文脈で）

各 pending Operation に対して、AI は以下のいずれかを選ぶ:

- **「動かす時が来た」と推論**: hint の条件が満たされている、または状態的に活性化が筋。  
  → 議論をユーザーに振る:
  ```
  op-X "Service health monitoring" の準備が整っているように見えます。
    根拠: ms-22 が完了して本番稼働中 / OperationTasks 3/3 done
    activation_hint: "Cloud Run 本番稼働後に有効化"

  動かしますか？それともまだ早い？
  ```

- **「まだ早い」と判定**: 触れない（出力にも含めない、ノイズを避ける）

- **「OperationTasks の消化が先」と判定**:
  ```
  op-Y "Cost watch" は活性化前にOperationTasks 2件の消化が必要。
  /beacon-operation-setup で進めますか？
  ```

### 出力位置

Step 3 の出力の末尾、Step 4 トリガーチェックの直前に挿入。  
論点が無いなら出力に含めない。

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
