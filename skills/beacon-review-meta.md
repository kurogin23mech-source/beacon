---
name: beacon-review-meta
description: 全UCレビュー完了後、発見した全タスクを俯瞰してクラスタリング・優先度再評価・MS再分割案を作成する。ユーザーとの議論で案を確定し、SPECドキュメントとして保存する。
version: 0.1.0
triggers:
  - /beacon-review-meta
  - メタレビューする
  - タスクを整理したい
---

# Beacon Review - Meta Review

> 全UCレビュー完了後、findings を俯瞰してクラスタリング、優先度再評価、MS再分割案を作る。

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

- レビューMSの全UCレビュータスクが done になっている
- 配下に多数の finding タスク（実装すべき改善項目）が active で存在する

## 方法論の参照（必読）

```
Read("~/.claude/skills/beacon-review/methodology.md")
```

methodology はメタレビューの作法を定義する（クラスタリング基準・MS再分割の判断・SPEC化のフォーマット）。

## Step 1: タスク全件取得

```bash
beacon task list --ms <レビューMS-id> --json
```

active なタスクを全て取得。 UCタスク自体は done のはずなので、findings タスクが対象。

## Step 2: クラスタリング

AI が findings を意味で分類する。デフォルトクラスタ候補:

- **データ整合性** (lost update / race / 認証期限 / etc.)
- **AI主導の体験** (リズム提案 / retrospection / cwd-aware化 / 自動化)
- **要求書/SPEC運用** (MS追加時のSPEC化、判断軌跡記録)
- **オンボーディング** (新規ユーザー導線・低リテラシー対応)
- **共同開発** (メンバー管理・PR・並列実装・リリース)
- **UI/UX磨き** (Web UI 検索・モバイル・細部)
- **その他/個別バグ**

クラスタはプロジェクト性質で異なる。AI は findings を読んで適切なクラスタを動的に生成する。

## Step 3: 優先度の再評価

全体観で優先度を見直す:
- 個別UCで「middle」と付けた tasks も、横断的に見ると **「他の改善の前提」** となるなら **「high」** に格上げ
- 「high」でも個別の影響が局所的なら格下げ候補
- データ整合性系は基本的に最優先（他改善の前提）

## Step 4: MS再分割案の作成

各クラスタを 1 MS にする想定で:

```
案:
  新規 ms-N: <クラスタ名>
    objective: <そのクラスタが何を達成するか>
    ac: <完了の判定基準>
    priority: <highest/high/middle/low>
    含むタスク数: K件
```

クラスタが大きすぎる場合は分割、小さすぎる場合は統合。

## Step 5: ユーザーへの提示と議論

ユーザーに 1 画面で全クラスタ案を提示:

```
メタレビュー結果

[Cluster A: データ整合性] (X件)
  代表タスク: ...
  → 新MS提案: "<名前>" priority: highest

[Cluster B: ...] (Y件)
  ...

このクラスタリングで進めて OK ですか？
- 追加/分割したいクラスタ
- 名前を変えたい MS
- 優先度の調整
あれば教えてください。
```

ユーザーと数ターン議論して合意形成。

## Step 6: メタレビュー SPEC として記録

**heredoc は必ず quoted EOF (`<<'EOF'` または `<< 'EOF'`) を使う**: 非引用 `<<EOF` だと shell が中身の backtick (`` ` ``) を command substitution として展開し、本文が silent corrupt する (2026-06-10 LPS dogfood で観察された病理、e-1401)。

合意した内容を SPEC ドキュメントとして記録:

```bash
beacon doc add "メタレビュー: タスククラスタリングとMS再分割案" \
  --scope spec --ms <レビューMS-id> --stdin <<'EOF'
# メタレビュー: タスククラスタリングとMS再分割案

## 全体統計
[件数・優先度分布]

## クラスタリング
[各クラスタの詳細、含まれるタスクのリスト]

## MS再分割案
[新規MS提案の一覧、対応クラスタ]

## 横断的テーマ
[個別UCを跨いだ気づき]

## 次のステップ
/beacon-review-apply で適用する。
EOF
```

## Step 7: 次への案内

```
メタレビュー完了。SPEC doc に記録しました。
MS再分割を実行するには /beacon-review-apply を実行してください。
```

## 制約

- クラスタリングはAIの推論だが、必ずユーザーと議論する（一方的決定は禁止）
- 全 finding タスクが何らかのクラスタに分類される（漏れがないこと）
- 「その他」クラスタは可能な限り作らない（個別性が高いものでも理由を考えて分類）
- 既存MSへの統合も検討対象（必ず新規MSを作るわけではない）
