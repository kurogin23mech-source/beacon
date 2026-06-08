---
name: beacon-review
description: プロダクト全体のUXレビューを駆動するエントリ Skill。ユースケース駆動でレビューを進め、最終的にMS再分割まで実行する。状態を見て次に呼ぶべきSkill（beacon-review-uc/meta/apply）を判定する。
version: 0.1.0
triggers:
  - /beacon-review
  - 全機能を見直したい
  - UXレビューしたい
  - プロダクトを棚卸ししたい
---

# Beacon Review

> プロダクト全体の UX を、ユースケース駆動で精査し、改善タスクとして起票・整理する。複数セッションにまたがる長期プロセス。

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

Bash ツールで以下を実行（cwd=$PROJECT_DIR）:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
`NO_BEACON` なら終了。

## 方法論の参照（必読）

このSkillを実行する前に、必ず companion ドキュメントを読む:

```
Read("~/.claude/skills/beacon-review/methodology.md")
```

methodology が、UC駆動レビューの作法・対話の進め方・ギャップ発見のチェックリスト・メタレビューの基準を定義する。

## Step 1: 状態判定

レビュー対象MS（典型的には title に「棚卸し」「UXレビュー」を含むMS）の有無と進捗を確認:

```bash
beacon status --json
```

判定:

| 状態 | 推奨アクション |
|---|---|
| レビューMSが存在しない | **Step 2 (Init)** へ — 新規レビュー開始 |
| レビューMS active, 未完了UC タスクあり | **Step 3 (UC Review)** へ — `/beacon-review-uc` を案内 |
| 全UC完了, メタレビュー未実施 | **Step 4 (Meta)** へ — `/beacon-review-meta` を案内 |
| メタレビュー完了, MS再分割未実施 | **Step 5 (Apply)** へ — `/beacon-review-apply` を案内 |
| MS再分割完了 | レビュー終了状態を報告、レビューMSを observe 化提案 |

## Step 2: Init（新規レビュー開始）

ユーザーに確認:

```
プロダクト全体の UX レビューを開始します。

ユースケース駆動で、プロダクトのフロー全体を順に精査し、
発見したギャップをタスク化、最後に整理してマイルストーンに再分割します。

このプロジェクトの主要なユースケースを列挙してください。
わからない場合は私が提案します（一般的なソフトウェアプロダクトの典型UC を出します）。
```

ユーザーの応答パターン:

**A. ユーザーがUCを提示**: そのまま採用、不足があれば AI が補足提案
**B. ユーザーが「提案して」**: methodology の「典型UC一覧」を参照し、プロジェクト性質に合わせて UC を提案

UC が確定したら:

1. レビューMSを起票:
   ```bash
   beacon milestone add "全機能の棚卸しとUX可用性レビュー" \
     --priority middle \
     --objective "プロダクトの主要ユースケースを順に精査し、ギャップを発見してタスク化、最終的にマイルストーンへ再分割する" \
     --ac "全UCについてレビュー完了、findings がタスク化され、新MSに再配置されている"
   ```
2. 各UCをタスクとして起票（review対象、まだ未着手）:
   ```bash
   beacon milestone start <ms-id>
   for uc in UC1 UC2 ...; do
     beacon task add "$uc review: <UC title>" -m <ms-id> \
       --priority middle --why "全UCレビューの一環として精査する"
   done
   ```
3. ユーザーに `/beacon-review-uc` の起動を案内:
   ```
   レビューMS と UC タスク一覧を起票しました。
   準備ができたら /beacon-review-uc で 1 つ目のUCから精査を始めましょう。
   ```

## Step 3: UC Review 案内

レビューMSの未完了 UC タスクから 1 つを選び:

```
未完了UC: UCn "<title>"
次は、この UC をペルソナ・フロー・ギャップの順に深掘りして、見つかった改善点をタスクとして起票します
(対話で 1 問ずつ詰めていきます)。

進めますか？ (内部的には /beacon-review-uc Skill を起動します)
```

`/beacon-review-uc` Skill 起動。完了したら本 Skill に戻って次のUCを案内。

## Step 4: Meta Review 案内

全UCのレビュー完了を検知:

```
全 N 個のUCのレビューが完了しました。
次は、発見した X 件のタスクを俯瞰してクラスタ化し、新しいマイルストーン群への再分割案を作ります
(対話で案を詰めて、合意したら SPEC ドキュメントとして保存します)。

進めますか？ (内部的には /beacon-review-meta Skill を起動します)
```

## Step 5: Apply 案内

メタレビュー完了を検知（メタレビューのSPEC docが存在する等）:

```
メタレビュー完了済み。次は、合意した MS 再分割案を実際にプロジェクトへ反映します
(新 MS の起票・既存タスクの移動・既存レビュー MS の observe 化を一括実行します)。

進めますか？ (内部的には /beacon-review-apply Skill を起動します)
```

## Step 6: 完了報告

すべて完了:

```
プロダクト全体UXレビューが完了しました。

- レビューMS: <ms-id> "<title>"
- 発見タスク: N 件
- 新規MS: M 個に再分割
- レビューMS の status: observing

このプロセスを別のプロジェクトで再現したい場合、同じ手順を辿れます（CORE doc「メモリ層とエージェント層の分離」参照）。
```

## 制約

- このSkillは状態判定とユーザーへの案内に徹する。個別のレビュー作業は配下の Skill (uc / meta / apply) に委ねる
- methodology.md を読まずに進めない（作法が崩れる）
- レビューMSは1プロジェクトに同時1つまで（複数並行は混乱の元）
- ユーザーとの対話を一方的に進めない（vision/roadmap 同等の作法）
