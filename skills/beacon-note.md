---
name: beacon-note
description: セッション内の一時メモを記録する。コンパクション対策に使い、セッション終了時に重要なものをDocに昇格して削除する。
version: 1.0.0
triggers:
  - /beacon-note
  - メモして
  - 覚えておいて
  - これ記録して
  - ノートして
---

# Beacon Note

> ユーザーが「メモして」「覚えておいて」と言ったとき、または自分がコンパクション後に知っておくべき重要な文脈を見つけたときに実行する。

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

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## 実行

記録内容と文脈を整理して Bash ツールで実行:

```bash
beacon note "<記録するテキスト>" [--context "<文脈ラベル>"]
```

### 書くこと: 「何を / なぜ / 次に」(ms-57 e-1195)

セッションノートの本質は **状態スナップショットではなく意思決定の記録**。
auto-取得 (現在地・コミット・タスク) は context-usage-monitor が自動でやるので、
このSkillで書くべきは「**判断軌跡**」のみ。

良いノートの 3 要素 (順不同、関係するものを書く):

| 要素 | 質問 |
|---|---|
| **決定事項** | このセッションで何を選んで何を捨てたか? |
| **議論の要点 (なぜ)** | その判断の理由は? 代替案を退けた理由は? ユーザーから出た指摘は? |
| **次のアクション / 残された論点** | 次セッションで先に着手すべきこと、未解決の不確実性は? |

ユーザーが「メモして」「覚えておいて」と短く言ったときも、上記の構造で書く
(1 行で済ませない、判断の why が無いノートは引き継ぎで読まれない)。

### 良い例

```bash
beacon note "## e-1191 channel node_modules の解決方針

### 決定事項
- wheel に node_modules を同梱せず、_ensure_channel_node_modules の subprocess で shutil.which 経由の絶対パスを使う形に統一
- vendoring 案は採用しない

### 議論の要点
- Win user の実機で 'node/npm not found on PATH' が誤メッセージだったことから、subprocess の PATHEXT 解釈問題と特定
- vendoring は wheel サイズ膨張 + 依存 1 個だけのため過剰

### 次のアクション
- post-merge で Win user の trailnode use 経由再試験
- 同種の subprocess PATHEXT bug が他にもあるか follow-up 議論"
```

### 悪い例 (これは避ける)

```bash
# ✗ 状態の羅列のみで判断軌跡がない
beacon note "ms-44 を進めている。e-1191 が pending。"

# ✗ 「決めた」の中身がない
beacon note "PR #79 をマージした。"

# ✗ 1 行で済ませて context が不足
beacon note "Win テスト失敗"
```

### 書かなくていい内容
- 進捗状況の羅列（beacon status で見える）
- コミット履歴（git log で見える）
- タスク一覧（beacon task list で見える）
- auto-取得対象 (context-usage-monitor が記録済)

これらを書くのは情報の二重持ち。代わりに **「なぜ今この状態なのか」「次に何をするべきか」** を書く。

### 参考

CORE doc `beacon-note-writing-principle` に良いノート / 悪いノートのギャラリーがあります。
迷ったらそこを参照。

## 制約

- 書き込みは `beacon note` コマンド経由のみ
- 1回の呼び出しで1件のみ記録する（複数の判断軌跡がある場合は複数回呼ぶ、ただし分けすぎず関連するものは 1 件にまとめる）
- auto-記録された状態スナップショット (context-usage-monitor 由来) を上書き / 重複させない
