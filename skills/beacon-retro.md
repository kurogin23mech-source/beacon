---
name: beacon-retro
description: 週次振り返りドキュメントを生成し、ユーザーとディスカッションする。課題→判断→結果の文脈で再構成。
version: 0.2.0
triggers:
  - /beacon-retro
  - 週次振り返り
  - 今週のふりかえり
  - retro
  - 振り返りしよう
---

# Beacon Retro

> 週次の活動を「課題→判断→結果」の文脈で再構成し、振り返りドキュメントを生成する。

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

## cwd 解決

trigger 経由起動時は hook の `(project: ...)` パスを `$PROJECT_DIR` として使う。明示が無い場合は `pwd` を使い、ホーム直下なら abort。以降全 Bash 呼び出しに `cd "$PROJECT_DIR" && ...` を前置。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
cd "$PROJECT_DIR" 2>/dev/null; beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 0: catch-up モード判定 (= ms-79 / e-1837 / UC5-F2)

retro trigger が示す overdue 週数が 2 以上の時、または user が `/beacon-retro catch-up` / 「まとめて消化したい」 と発話した時は、catch-up フローに分岐する。

```bash
cd "$PROJECT_DIR" && beacon trigger tick && beacon trigger check 2>&1 | head -20
```

`tick` で retro-due の overdue 週数を fresh 化してから読む (ms-98 / e-2764)。

trigger payload (= `.beacon/triggers/retro.json`) に `overdue_slots` が 2 件以上ある場合は catch-up モードで開始することを提案:

```
振り返り未完の週が N 件たまっています ({W1, W2, W3})。
- 1 週ずつ詳しく振り返る (通常モード)
- まとめて消化 (catch-up モード: 各週で要約だけ確認、最後に期間メタ retro を生成)
- やめる

どうしますか?
```

user が catch-up を選んだ場合 (= 自動で catch-up に倒さず、ここは user 判断を尊重)、Step 1 で `--catch-up` フラグを付ける。1 件以下 / user が通常モード選択時は従来通り Step 1 へ進む。

## Step 1: 週次データの収集

Bash ツールで以下を **並列に** 実行:

### 1a. beacon エントリ
```bash
# 通常モード:
cd "$PROJECT_DIR" && beacon retro --prepare [--since YYYY-MM-DD] [--until YYYY-MM-DD]

# catch-up モード (= Step 0 で選択時、ms-79 e-1837):
cd "$PROJECT_DIR" && beacon retro --catch-up [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```
- デフォルト挙動 (ms-43 e-570 で柔軟化):
  - `.beacon/retro/.reviewed` がある → 「最後にreviewした週の翌週月曜」が since
  - なければ「直近の retro_day から 6日遡った日」が since
  - 後方互換: 古い install は date 演算で「今週月曜 (月曜起動時は先週月曜)」にフォールバック
- これにより **金曜 retro を忘れて翌週火曜に retro した場合でも、先週月曜〜今週火曜の全期間がカバーされる**
- catch-up モード時は payload に `catch_up: { overdue_slots, count, since_first_overdue }` が同梱される (= ms-79 e-1837)。Step 2 / Step 3 はこのリストを順に処理する
- ユーザーが期間を指定した場合は `--since` / `--until` を付加
- payload は ms-79 / e-1836 で `source_breakdown` (= human N / auto-op M / dm K の facet) も同梱する。Step 2 の出力でこの内訳を「数字で示す事実セクション」 に反映できる

### 1b. git コミット一覧（コンテキスト補完用）
```bash
cd "$PROJECT_DIR" && git log --since="YYYY-MM-DD" --until="YYYY-MM-DD" --oneline --no-merges
```
- beacon エントリにないコミットがあれば補足情報として使用

## Step 2: 振り返りドキュメントの生成

Step 1 のデータを元に、以下の構造でマークダウンドキュメントを生成する。

### 構造

```markdown
# Weekly Retro: YYYY-MM-DD 〜 YYYY-MM-DD

## 今週の取り組み

### [課題/目的のタイトル]
- **課題**: なぜこれに取り組んだのか（背景・動機）
- **手段**: どういうアプローチを取ったか（技術選定・設計判断）
- **結果**: 何が達成されたか、何が残ったか

### [次の課題/目的のタイトル]
...

## 方向性チェック
- [観察] 事実ベースの振り返り（何が起きたか）
- [問い] 次週に向けた問い（この方向でいいのか、優先度は正しいか）

## 得た知見
- 今週の開発で学んだこと、発見したパターン
- CLAUDE.md や memory に反映すべき候補があれば明示

## 次週のヒント
- 残タスク・未着手MSから、次に取り組むべき候補を提案
```

### 生成ルール

- **「今週やったこと」は表層的なリストにしない**。コミット一覧やタスク消化リストではなく、課題単位でグルーピングし、文脈（なぜ→どうした→どうなった）を再構成する
- 同じ課題に対する複数のコミット/タスクは1つにまとめる
- beacon の summary フィールドに蓄積された経緯・判断を活用する
- 完了したMSだけでなく、進行中のMSの進捗も含める

## Step 3: ドキュメントの保存

**ms-68 / e-1642 補足 (= entry-writing principle の draft 表示)**: `beacon retro save` を実行する **前** に、Step 2 で生成した retro 本文 (= 課題ごとにグルーピング・文脈再構成済) を 1 度ユーザーに提示する。retro は週単位の振り返り記録として将来 retrospection / dispatch / onboarding で広く読まれるため、silent write は読み手 (非開発者を含む) を排除する。提示時に self-review 4 原則 (読み手目線 1 行 / 横文字 3 段階 / ID 参照に文脈 / 尻切れトンボ禁止) を 1 度通し、違反があれば書き直してから保存する。

```
以下の内容で週次 retro を保存します:

  week: YYYY-WNN

  <本文 draft>

このまま保存しますか? (= OK / 書き直し)
```

**heredoc は必ず quoted EOF (`<<'EOF'` または `<< 'EOF'`) を使う**: 非引用 `<<EOF` だと shell が中身の backtick (`` ` ``) を command substitution として展開し、本文が silent corrupt する (2026-06-10 LPS dogfood で観察された病理、e-1401)。

生成したドキュメントを **Bash ツール経由で `beacon retro save` CLI に渡す**:

```bash
cd "$PROJECT_DIR" && beacon retro save --week YYYY-WNN --stdin <<'EOF'
[本文]
EOF
```

- `YYYY-WNN` は ISO 週番号（例: `2026-W23`）
- CLI が cloud mode を判別し、cloud subcollection または `.beacon/retro/YYYY-WNN.md` に書き込む

⚠️ **Write ツールで `.beacon/retro/*.md` を直接書かないこと**。cloud mode では Web UI Reviews タブに反映されず、retro が orphan になる。local/cloud の判定は CLI 層に任せ、Skill は本文生成だけに専念する。

## Step 3.5: catch-up 連続消化 (= ms-79 / e-1837)

catch-up モードのとき (= Step 0 で user が catch-up を選び、Step 1 payload に `catch_up.overdue_slots` が同梱されているとき) は、Step 2 → Step 3 を `overdue_slots` の各週について **順に** 繰り返す。

各週で:

1. その週の since / until を ISO 週から計算して `beacon retro --prepare --since ... --until ...` を再実行
2. user に短い 3 択を提示:
   ```
   {YYYY-WNN}: <この週の主要 entry を 2-3 行で要約>
   - 詳しく振り返る (= 通常 Step 2-3 を回す)
   - 要約だけ確認して進む (= Step 2 軽量版で save)
   - skip (= 何も書かずに .reviewed だけ進める)

   どれにしますか?
   ```
3. user の選択に応じて分岐:
   - 「詳しく」 → 通常 Step 2-3 を回す (= 通常モードと同じ深さ)
   - 「要約だけ」 → 課題ごとのグルーピングは省略し、entry リスト + source_breakdown を箇条書きで save
   - 「skip」 → `beacon retro done` 相当の `.reviewed` 更新のみ (= 内容は書かない、後から振り返り不可なので user に明示)

4. 1 週終わったら次の `overdue_slots[i+1]` に進む

全週終わったら **期間メタ retro** を 1 本生成して save:

```bash
cd "$PROJECT_DIR" && beacon retro save --week "{YYYY-WNN-meta}" --stdin <<'EOF'
# Catch-up Meta Retro: {since_first_overdue} 〜 {today}

## 対象期間
- 振り返った週: W1, W2, W3 (= N 件)
- 各週の概要: (各週から要約 1 行 ×N)

## 期間全体の方向性チェック
- (期間を通して見えてきたパターン / drift / 学び)

## 次週のヒント
- (期間メタの観点から、次に着手すべき塊)
EOF
```

(`--week` の値は最終週のスロット名のままで OK。期間メタ retro が複数の週を内包していることは本文で示す。)

## Step 4: ディスカッションの開始

ドキュメントをユーザーに提示し、以下の観点でディスカッションを促す:

```
振り返りドキュメントを生成しました: .beacon/retro/YYYY-WNN.md

確認したいポイント:
1. [方向性チェックの問いから最も重要なもの]
2. [得た知見でCLAUDE.mdに反映すべきもの]
3. [次週のヒントで優先度を決めたいもの]

どこから話しましょうか？
```

## Step 4.5: CORE doc 昇格の提案（e-574 / UC5-J1'）

Step 4 のディスカッションで **永続化価値のある気づき** が出た場合、それを CORE doc として残すことをユーザーに提案する。

### 判定基準（AI 主体で判断）

retro のディスカッション内容を読み返し、以下のいずれかに該当する発見があれば「CORE doc 候補」として抽出する:

1. **新しい設計原則・方針の言語化**: 「○○は△△で統一する」「□□の場合は××する」のような、今後も繰り返し参照されるルール
2. **既存 CORE doc と矛盾する判断**: アーキテクチャや運用方針の転換（既存 CORE の更新候補）
3. **再発しそうな失敗パターンの教訓**: 「○○すると××で破綻するので避ける」のような、未来の自分への戒め

該当しない場合（単なる進捗確認・既知ルールの再確認のみ）はこの Step をスキップする。retro doc 自体に「学び」として残せば十分。

### 提案文（実行は user 承認後）

```
今回の振り返りで、以下の気づきは CORE doc 化する価値がありそうです:

  1. [気づきの要約 — 1〜2文]
     scope: core / 既存 CORE doc 更新（doc_id: xxx）
     理由: [なぜ永続化価値があるか]

  2. ...

これらを CORE doc に昇格させますか？（部分選択も可）
```

### 承認時の実行

ユーザーが承認したら Bash ツールで実行:

- 新規 CORE doc:
  ```
  cd "$PROJECT_DIR" && beacon doc add "<title>" --scope core --stdin <<'EOF'
  [本文]
  EOF
  ```
- 既存 CORE doc 更新:
  ```
  cd "$PROJECT_DIR" && beacon doc update <doc_id> --stdin <<'EOF'
  [更新後の本文]
  EOF
  ```

### 設計判断（なぜこのStepが必要か）

retro doc は履歴として永続化されるが、`scope: retro` は session-start で **自動読み込みされない**（core / spec / 関連spec のみ）。一方 CORE doc は全セッションで常時参照される。

「重要な学びだけど retro doc に埋もれる」を避けるため、AI が能動的に CORE 昇格を提案する。これは /beacon-log Step 6 (ドキュメント評価) と同じ発想で、retro の文脈内で適用する。

## Step 5: レビュー完了の記録

ユーザーが振り返りの議論を終えたと判断できたら（「OK」「以上」「振り返り終わり」等）、Bash ツールで実行:
```bash
cd "$PROJECT_DIR" && beacon retro done
```
これによりダッシュボードの振り返り警告が消え、e-575 の persistent retro trigger も解除される。

**判断基準**: ユーザーが方向性チェックの問いに回答し、次のアクションが決まった時点でレビュー完了とみなす。ドキュメント生成直後に自動で叩いてはならない。

## 制約

- データ取得は Bash ツール経由の beacon CLI のみ。project.json を直接読まない。
- ドキュメント保存は `beacon retro save --week YYYY-WNN --stdin` 経由のみ。`.beacon/retro/*.md` を Write ツールで直接書かない（cloud 反映漏れの原因になる）。
- 振り返りは提案であり、CLAUDE.md の更新やタスク操作は **ユーザーの合意を得てから** 行う。
- CORE doc 昇格は **必ず user 承認後**。AI 単独で `beacon doc add --scope core` を叩かない。
- 全 Bash 呼び出しに `cd "$PROJECT_DIR" && ...` を前置する。
