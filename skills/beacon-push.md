# Beacon Push

> git push後に自動実行。コミット情報からAIが価値ベースの説明を生成し、push recordを記録する。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 1: コンテキスト取得（読み取り専用）

Bash ツールで実行:
```bash
beacon push record --prepare
```

stdout に JSON が返る:
```json
{
  "branch": "main",
  "from_hash": "...",
  "to_hash": "...",
  "commits": [{"hash": "...", "message": "..."}],
  "ms_id": "...",
  "last_push": {"id": "...", "date": "..."}
}
```

`commits` が空の場合（前回push以降に新しいコミットなし）は何もせず終了する。

## Step 2: 説明文の生成

Step 1 の情報を読み、**日本語で1〜3文の説明文**を生成する。

### 書くべきこと
- このpushで開発者や利用者が「何を手にしたか」「何が改善されたか」を具体的に
- コミット群のテーマを統合して、意味のある1〜3文の文章にする
- `commits` の各メッセージを参考にするが、**そのまま連結しない**

### 書かないこと
- コミットメッセージの羅列や「・」区切りのリスト
- ハッシュ・IDの言及（ms-XX, e-XXX, commit hash など）
- 「〜を実装した」という過去形の開発者視点（「〜できるようになった」「〜が改善された」という状態変化で）

### 例
良い例: 「新規プロジェクトのセットアップがClaude Codeとのチャットだけで完結するようになり、git履歴からの過去フェーズ自動推測（Project Archaeology）も利用できるようになった。タブを長時間放置した後のWebSocket切断も自動で復旧するよう改善された。」

悪い例: 「beacon-init Skill・Project Archaeology強化・beacon initフラグ対応・CLIコマンド安全性修正・ms-26 worktreeDispatch・ms-5安定化（16コミット）」

## Step 3: 書き込み

Step 2 で生成した説明文を使って Bash ツールで実行:

```bash
beacon push record --desc "<Step2の説明文>"
```

## Step 4: 結果の提示

finalize の stdout を確認し、ユーザーに簡潔に報告:
```
Push: [push-id] [branch] ([N] commits)
  [生成した説明文]
```

## Step 5: トリガーチェック

Bash ツールで実行:
```bash
beacon trigger check
```

空でなければ各トリガーの `message` を提示する。

## 制約

- Step 1（prepare）は読み取り専用。書き込みは Step 3 のみ。
- project.json を直接読まない。beacon CLI 経由のみ。
- 説明文はユーザーが読んで意味がわかる文章にする。技術的な列挙は避ける。
