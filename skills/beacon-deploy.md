# Beacon Deploy

> デプロイ後に自動実行。コミット・マイルストーン情報からAIが価値ベースの説明を生成し、deploy recordを記録する。

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

## Step 1.5: Deployバージョン判定（オプション）

CORE doc `version-rules` の **deploy軸** 設定に従って処理する。

### deploy軸の設定取得

Bash ツールで実行（`<beacon_lib>` は `beacon` インストール先のlib/、例: `/opt/homebrew/Cellar/beacon/X.Y.Z/libexec`）:

```bash
python3 -c "
import sys
sys.path.insert(0, '<beacon_lib>')
from version_rules import propose_next_version, describe_from_tag
import json, subprocess
prepare = subprocess.run(['beacon','deploy','record','--prepare'], capture_output=True, text=True).stdout
d = json.loads(prepare)
commits = d.get('new_commits', [])
info = propose_next_version(commits, axis='deploy', repo_path='.')
push_tag = describe_from_tag(prefix='v', repo_path='.')
print(json.dumps({'deploy': info, 'push_tag': push_tag}))
"
```

### 分岐: deploy軸が有効か

#### A. deploy軸が **無効** （デフォルト）

`enabled == False` の場合、deployにはバージョンを切らない。表示のみ：
- `git describe --tags --match='v[0-9]*' HEAD` で push軸の現在地を表示
  - exactなら `Deploy対象 v0.2.1`
  - 非exact（commits先）なら `Deploy対象 v0.2.1+3 (server変更3件先)`
  - tag無しなら `未タグ (abc123)`
- バージョン提案・タグ切りはしない

#### B. deploy軸が **有効**

`enabled == True` の場合、push軸と同様の挙動：
- `scope_paths` が指定されていればそのpath配下を変更したcommitsのみ対象
- bump判定 → 次バージョン提案
- ユーザーに提示:
  ```
  Deployバージョン判定:
    現tag: deploy-v1.4.2
    次:    deploy-v1.5.0  (MINOR bump)
    根拠:  feat 2本（serverスコープ内）
    BREAKING: なし

  このdeployにタグ deploy-v1.5.0 を切ってGitHub Releaseを作成しますか？ [y/N]
  ```
- 承認時:
  ```bash
  git tag deploy-vX.Y.Z
  git push origin deploy-vX.Y.Z
  gh release create deploy-vX.Y.Z --title deploy-vX.Y.Z --notes "Deploy release"
  ```

### 出力への反映

Step 4 の結果報告に追加:
```
Deploy対象バージョン: v0.2.1+3   （またはdeploy-v1.5.0、または「未タグ (abc123)」）
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
