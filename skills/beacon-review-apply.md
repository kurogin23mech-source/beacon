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

## Step 5: レビューMS を observe 化

```bash
beacon milestone observe <レビューMS-id> --reason "UC全レビュー完了、メタレビュー実施、MS再分割を適用済み。タスク本体は新MS群へ移動した。レビューMS本体は方法論の参照記録として残置"
```

## Step 6: 完了報告

```
MS再分割完了。

新規MS:
  ms-N: "<名前>" (X tasks)
  ms-N+1: "<名前>" (Y tasks)
  ...

旧レビューMS:
  <レビューMS-id> [observing]
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
