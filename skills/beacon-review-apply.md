---
name: beacon-review-apply
description: メタレビューで合意したMS再分割案を実行する。新MSを起票し、タスクを各MSへ移動、レビューMSをobserve化する。
version: 0.1.0
triggers:
  - /beacon-review-apply
  - MS再分割を実行
  - レビュー結果を適用
---

# Beacon Review - Apply

> メタレビューで合意した再分割案を実行する。

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

## 前提条件

- メタレビュー SPEC ドキュメントが存在する（/beacon-review-meta で作成済み）
- ユーザーが案に合意済み

## 方法論の参照

```
Read("~/.claude/skills/beacon-review/methodology.md")
```

## Step 1: メタレビュー SPEC を読み込む

```bash
beacon doc list --scope spec --ms <レビューMS-id> --json
```

「メタレビュー: タスククラスタリングとMS再分割案」を取得。クラスタ別の MS 提案と、各クラスタに含まれるタスクID 一覧を抽出。

## Step 2: 確認

ユーザーに最終確認:

```
以下を実行します:

新規作成するMS:
  ms-N: "<名前>" priority: highest
  ms-N+1: "<名前>" priority: high
  ...

タスク移動:
  ms-N に X 件 (e-..., e-..., ...)
  ms-N+1 に Y 件 (...)
  ...

レビューMS:
  <レビューMS-id> を observe 化

進めてよろしいですか？
```

承認を待つ。

## Step 3: 新規MS起票

各クラスタについて:

```bash
beacon milestone add "<クラスタ名>" \
  --priority <priority> \
  --objective "<クラスタのobjective>" \
  --ac "<クラスタのac>"
```

返ってきた ms-id を記録（タスク移動で使う）。

## Step 4: タスク移動

各タスクを対応する新MSに移動:

```bash
beacon task update <entry-id> -m <新ms-id>
```

beacon task update は内部で entry_move を呼ぶので、タスクは新MSへ移動する（複製ではなく移動）。

進捗報告:
```
ms-N (Cluster A): X 件移動完了
ms-N+1 (Cluster B): Y 件移動完了
...
```

## Step 4.5: 新規 MS の active 化確認 (ms-61 e-1892)

新 MS 群を起票し配下タスクを移し終えた直後、 **どの新 MS から手を着けるかが誰の active 視野にも入らないと、 land 直後に棚卸し残務が宙に浮く構造バグになる**。 これは 2026-06-17 に ms-71 (= 棚卸しレビュー MS) で新規 6 MS (ms-75〜80) を land した後、 どの新 MS も `todo` のまま放置されて誰の active にも入らなかった事故から学んだ:

- `beacon status` / session-start の active MS には新 MS が 1 件も載らない (全部 todo のまま)
- 「次の一手」 推奨にも上がらない
- 棚卸し残務 6 件 (= I クラスタ: dead-paper / superseded / 命名 / Skill 整理) が誰にも assign されないまま忘却された

この活性化忘れを構造的に塞ぐため、 Step 4 完了直後に **必ず以下の確認 step を踏む** (= ユーザー直接介入で「次に動かす MS」 を明示する forcing function)。

### 4.5a: 着手候補の提示

Step 3 で起票した新 MS のうち priority が `highest` のもの (なければ priority `high` の先頭) を「最有力着手候補」 として 1 つピックアップする。

```
新 MS を起票し配下タスクを移し終えました。 次に着手する MS を選んでください:

  最有力候補: ms-N "<タイトル>" (priority: highest, X tasks)

このまま ms-N を active 化しますか? (= `beacon milestone start ms-N` を実行、
status を `in_progress` に flip、 自分を assignee に登録、 worktree を作成)
(y/n、 default y、 別 MS を指定する場合は ms-id を入力)
```

回答待ち。

### 4.5b: ユーザー回答の分岐

- y / Enter / 「はい」 / 「OK」 → 4.5c で最有力候補を active 化
- ms-id (= 別 MS 指定) → 4.5c でその MS を active 化
- n / 「いいえ」 / 「後で」 → skip して Step 5 へ。 **明示的に「全 MS が todo のまま残る」 旨を 1 行 warn 表示** (= 「⚠ 新 MS 群はすべて todo です。 着手前に `beacon milestone start <ms-id>` を打ち忘れないでください」)。 これがユーザー自身による「あとで自分で start する」 宣言の記録になる。

### 4.5c: `beacon milestone start` を実行

```bash
beacon milestone start <選んだms-id>
```

これは ms-81 e-1920 の正規経路で、 以下を atomic に実施する:

- MS status を `in_progress` に flip
- 現在の actor を assignee に追加 (= self-add)
- worktree を `.worktrees/<ms-branch>/` に作成 (= 並走 session の HEAD 衝突を防ぐ)
- ms-81 occupation claim を立てる (= 他 session に「この MS は私が占有中」 のシグナルを残す)

これにより「Skill が新 MS を起票した直後に手動で `beacon milestone start` を打ち忘れる」 という人的ミスが構造的に発生しない経路に固定される (= 手動 start 忘れ事故ゼロ、 AC #1)。

### 後方互換

既存 fork session 等で「もうどれかが active になっていて、 さらに別の MS を start すべきか」 が曖昧なケースは、 ユーザーが 4.5b で n を選べば silent に skip するので no-op。 強制 start ではなく明示確認 + 1 click default 経路として組む。

### 補助経路 (= session-start 側 trigger)

session-start の trigger check で「新 MS 起票後 N 日経過しても親 MS が todo のまま放置されている」 ケースを検知する補助 trigger (= `meta-review-parent-stale`) も理想形だが、 本 PR では Skill 改修を優先する。 trigger 側は別 task で扱う (= e-1892 split candidate、 完了報告で明示)。

## Step 5: レビューMS を observe 化（目的達成ゲート経由）

`observing` は「基本目的は達成済み・運用に回してよい」という**完了主張**であり、目的達成 / 思想レビューのゲート対象 (ms-119)。ここは完了主張として正当（UC 全レビューを完遂した = そのレビュー MS の目的を果たした）なので、bare observe ではなく `--review` でゲートを通す。AI セッションの直接 observe は構造的に拒否される (e-4008)。

```bash
beacon milestone observe <レビューMS-id> --review --reason "UC全レビュー完了、メタレビュー実施、MS再分割を適用済み。タスク本体は新MS群へ移動した。レビューMS本体は方法論の参照記録として残置"
```

これは即座に observing へ遷移させず、**目的達成レビュー依頼（人間承認待ち）** を作成する。承認されると observing に遷移する。承認前は `in_progress` のまま。

## Step 6: 完了報告

```
MS再分割完了。

新規MS:
  ms-N: "<名前>" (X tasks)
  ms-N+1: "<名前>" (Y tasks)
  ...

旧レビューMS:
  <レビューMS-id> [observe を目的達成ゲートに依頼済み — 人間承認で observing に遷移]
  配下タスクは新MS群へ移動済み

次の実装フェーズは、優先度順に新MSから着手することをおすすめします:
  1. <priority highest の MS>
  2. <priority high の MS>
  3. ...
```

オプションで、関連commitを行うべきか確認:
```
このメタレビュー結果を git で記録しますか？（変更があれば commit / push）
```

## 制約

- ユーザー承認なしで実行しない（破壊的操作のため）
- タスク移動は entry_move を使う（cancel + re-add ではない、entry ID 維持）
- 失敗時は中断、巻き戻し（既に移動済みのタスクは残す、未移動分は次回に）
- レビューMSは削除せず observe 化（参照記録として残す）

## 関連 SPEC

メタレビューSPECに加え、UC1-3 / UC4-9 / UC10-12 のフロー documentation が SPEC scope で残っているはず。これらは新MS群とは独立に「方法論の例示」として残す価値がある。
