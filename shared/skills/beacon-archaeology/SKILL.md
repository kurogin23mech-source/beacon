---
name: beacon-archaeology
description: アクティブなマイルストーンが無いプロジェクトで、git 履歴とソースコードを読んで「これまでの歩み」と「次のマイルストーン候補」を推測・提案する。session-start がアクティブ MS ゼロ時にチェインする。
version: 0.1.0
---

# Beacon Archaeology（コンサルタントモード）

> 「やるべきことが前に存在しない」状態 (= アクティブ MS ゼロ) のプロジェクトで、次のマイルストーンを作るための材料を集めて提案する。git 履歴があれば過去フェーズを遡行推測し、常にソースコードを読んで現状を把握する。ms-85 e-3179 で `/beacon-session-start` Step 2.5 から切り出した。

## いつ動くか

以下のいずれかに該当するとき、`/beacon-session-start` がこの Skill にチェインする (= 通常の Step 3 出力の代わり):

- `milestones[]` が空（新規プロジェクト）
- `status == "in_progress"` または `status == "todo"` のマイルストーンが一件もない（done/observing/waitingのみ）

ユーザーが直接「archaeology して」「過去掘って」「リポジトリ分析して」等と呼んだときも起動する。

session-start から渡ってきた場合、Step 1a/1d/1e で取得済みの `beacon status --json` 結果と CORE doc は再取得せず再利用してよい。

## F30: 起動原因の橋渡しメッセージ

ユーザーが「archaeology して」「過去掘って」「リポジトリ分析して」等の **概念名** で起動した場合、ユーザー視点では「archaeology Skill」を呼んだつもりが `/beacon-session-start` が動くことがあるため、Skill 名のミスマッチで一瞬「あれ違う Skill？」となる。

以下のキーワードが直近の user 発話に含まれていたら、出力の **最初の 1 行** に橋渡しメッセージを添える:

- `archaeology` / `Archaeology`
- `掘って` / `経緯` / `これまでの流れ`
- `リポジトリ分析` / `コード読んで`

```
(Archaeology を起動します — git log とコードを読んで提案します)

このリポジトリを分析しました。
...
```

含まれていなければ橋渡し行は不要。

## 分岐: 常に B (code reading)、git 履歴あれば A も追加 (F29)

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

## フロー A: Project Archaeology（リポジトリ遡行推測）

### Step A1: 情報収集（並列 Bash 実行）

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

### Step A2: AI 解釈

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

### Step A3: ユーザーへの提示

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

### Step A4: ユーザー承認後の登録（書き込みフェーズ）

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

## フロー B: 白紙提案（コミット数 < 10 または git 未初期化）

### やること

1. CORE ドキュメントがあれば読む（session-start の Step 1d/1e の結果を利用）
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

### 提案の視点（重要）

- **「何を作るか」ではなく「何ができるようになるか」** でタイトルをつける
- objective を起点に考える。最終ゴールに向かう最初の一歩として、ユーザーが体験できる状態変化を表現する
- 「基盤構築」「パイプライン設計」のような技術的な工程名は避ける
- 例：objective が「家計の無駄遣いを減らして貯金を増やしたい」なら
  - ✗ 「データ取り込みパイプラインの設計」
  - ✓ 「先月の支出を入力して、無駄な出費のパターンを一覧で見られるようにする」

### 出力フォーマット

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

コンサルタントモード（フロー A または B）の後は、`/beacon-session-start` から呼ばれた場合は session-start の Step 4（トリガーチェック）に戻る。単独起動の場合はここで完了。通常の Step 3 出力は不要。
