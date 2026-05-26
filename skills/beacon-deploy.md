# Beacon Deploy

> デプロイ後に自動実行。コミット・マイルストーン情報からAIが価値ベースの説明を生成し、deploy recordを記録する。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 1: コンテキスト取得（読み取り専用）

Bash ツールで実行:
```bash
beacon deploy record --prepare
```

stdout に JSON が返る:
```json
{
  "deploy_type": "major" | "minor",
  "new_commits": [{"hash": "...", "message": "..."}],
  "newly_completed_ms": [{"id": "...", "title": "...", "commit_entries": [...]}],
  "patch_ms": [{"id": "...", "title": "...", "commit_entries": [...]}],
  "unassigned_commits": [...],
  "last_deploy": {"id": "...", "date": "..."}
}
```

new_commits が空の場合（前回デプロイ以降に新しいコミットなし）は何もせず終了する。

## Step 1.5: 対応バージョンの特定（オプション）

プロジェクトが **opt-in している場合のみ** 実行する。判定基準:

```bash
beacon doc show version-rules 2>/dev/null
```

stdoutに本文があれば opt-in。空ならスキップしてStep 2へ。

### 処理

デプロイは新規バージョンを生まない（pushで切ったtagをデプロイするだけ）。HEADコミットに紐づくtagを取得:

```bash
git describe --tags --exact-match HEAD --match='v[0-9]*' 2>/dev/null
```

- tagが返れば → そのバージョンを「Deploy対象バージョン」として記録対象に含める
- 返らなければ → 「未タグデプロイ（HEAD: {short-hash}）」と扱う（警告レベル、ブロックしない）

### 出力への反映

Step 4 の結果報告に追加:
```
Deploy対象バージョン: v0.2.0    （または「未タグ (abc123)」）
```

## Step 2: 説明文の生成

Step 1 の情報を読み、**日本語で1〜3文の説明文**を生成する。

### 書くべきこと
- このデプロイで何が「できるようになったか」「改善されたか」を具体的に
- major の場合：`newly_completed_ms` の各MSが提供する価値を中心に
- minor の場合：`patch_ms` の修正内容が何を正常化・安定化するかを中心に
- `commit_entries` の description を参考にするが、**そのまま列挙しない**

### 書かないこと
- マイルストーンIDやタスクIDの羅列（ms-14, e-xxx など）
- コミットメッセージのそのままの転記
- 「〜を実装した」という開発者視点（「〜できるようになった」というユーザー視点で）

### 例（major）
良い例: 「複数の開発者がPRレビューフローで並行開発できるようになった。監査ログ・バックアップ・レート制限など本番運用に必要なセキュリティ基盤を整備し、新規ユーザーへのAIコンサルタント型オンボーディングも開始できる状態になった。」

悪い例: 「PR駆動マルチ開発者ワークフロー設計・実装・エンタープライズセキュリティ基盤（監査ログ・データ削除・バックアップ）・コンサルタント型オンボーディング設計・実装」

### 例（minor）
良い例: 「ReleasesタブのUIが正しく表示されるようになった。デプロイ種別のデータモデルの誤解を修正し、メジャー/マイナーの区別が視覚的に明確になった。」

## Step 3: 書き込み（finalize）

Step 2 で生成した説明文を使って Bash ツールで実行:

```bash
beacon deploy record --finalize --desc "<Step2の説明文>"
```

`--semver` オプションが必要な場合（ユーザーが指定）は付加する:
```bash
beacon deploy record --finalize --desc "<説明文>" --semver <version>
```

## Step 4: 結果の提示

finalize の stdout を確認し、ユーザーに簡潔に報告:
```
Deploy: [deploy-id] [major/minor] (env)
  [生成した説明文]
```

## Step 5: トリガーチェック

Bash ツールで実行:
```bash
beacon trigger check
```

空でなければ各トリガーの `message` を提示する。

## 制約

- Step 1（prepare）は読み取り専用。書き込みは Step 3（finalize）のみ。
- project.json を直接読まない。beacon CLI 経由のみ。
- 説明文はユーザーが読んで意味がわかる文章にする。技術的な列挙は避ける。
