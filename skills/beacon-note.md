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
