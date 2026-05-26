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

## Step 2.5: バージョン判定（オプション）

プロジェクトが **opt-in している場合のみ** 実行する。判定基準:

```bash
beacon doc show version-rules 2>/dev/null
```

stdoutに本文があれば opt-in。空（exit code非ゼロ含む）ならこのStepをスキップしてStep 3へ。

### 判定ロジック

opt-inしている場合、Bash ツールで以下を実行:

```bash
python3 -c "
import sys, json
sys.path.insert(0, '$(beacon --help 2>&1 | head -1 | grep -oE '/[^ ]+' | head -1)')
# (実際には beacon インストール先の lib/ を sys.path に追加)
" 2>/dev/null || true

# 簡易版: git tag + 未push commits から判定
CURRENT_TAG=$(git describe --tags --abbrev=0 --match='v[0-9]*' 2>/dev/null || echo "v0.0.0")
COMMITS=$(echo '<Step1のJSON>' | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(c['message'] for c in d['commits']))")
```

`version-rules` ドキュメントの内容を読み、commits を分類して次バージョンを判定する:

1. **MAJOR候補**: `BREAKING CHANGE` / `BREAKING:` を含むメッセージ、または `feat!:` / `fix!:` プレフィックス
2. **MINOR候補**: `feat:` / `feat(...):` プレフィックス
3. **PATCH候補**: その他すべて

最大の昇格度を採用し、`git describe --tags --abbrev=0 --match='v[0-9]*'` で取得した現tagをbumpする。

### ユーザーへの提示

```
バージョン判定:
  現tag: v0.1.0
  次:    v0.2.0  (MINOR bump)
  根拠:  feat 5本, fix 4本, BREAKINGなし

このpushにタグを切ってGitHub Releaseを作成しますか？ [y/N]
```

### 承認時の処理

ユーザーが承認したら Bash ツールで実行:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
gh release create vX.Y.Z --title vX.Y.Z --notes "Release vX.Y.Z" 2>/dev/null || true
```

却下またはスキップ時は何もしない。Step 3 へ進む。

## Step 3: 書き込み

Step 2 で生成した説明文を使って Bash ツールで実行:

```bash
beacon push record --desc "<Step2の説明文>"
```

Step 2.5 でバージョンを切った場合は、`--meta` で記録できる場合は version を含める（CLI未対応ならスキップ）。

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
