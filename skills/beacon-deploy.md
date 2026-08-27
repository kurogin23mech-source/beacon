---
name: beacon-deploy
description: デプロイ後に自動で実行し、コミットとマイルストーンから「何ができるようになったか」の説明を生成してデプロイ記録を残す。
---

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
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
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

**ms-68 / e-1642 補足 (= entry-writing principle の draft 表示)**: `beacon deploy record --finalize` を実行する **前** に、Step 2 で生成した説明文を 1 度ユーザーに提示し、self-review 4 原則 (読み手目線 1 行 / 横文字 3 段階 / ID 参照に文脈 / 尻切れトンボ禁止) で違反が無いか自問する。違反があればその場で書き直してから書き込む。deploy 説明文は CHANGELOG / リリースノート / Releases タブで広く読まれるため、silent write は読み手 (非開発者を含む) を排除する。

```
deploy を以下の説明文で記録します:

  <Step2 で生成した説明文>

このまま記録しますか? (= OK / 書き直し)
```

Step 2 で生成した説明文を使って Bash ツールで実行:

```bash
beacon deploy record --finalize --desc "<Step2の説明文>"
```

`--semver` オプションが必要な場合（ユーザーが指定）は付加する:
```bash
beacon deploy record --finalize --desc "<説明文>" --semver <version>
```

### Step 3.5: backend 指定 (= ms-80 e-1831, optional)

multi-backend (= GCP Cloud Run / AWS Lambda / TrailNode) で運用しているプロジェクトでは、deploy 記録に **backend 名** を残しておくと「どの backend に何が反映されたか」 を後から追える。

明示しない場合は **active な cloud profile 名** が自動で backend として採用される (= `BEACON_PROFILE` / `cwd .beacon/cloud.json.profile` / `~/.beacon/profile.json` の precedence で resolve、典型値: `default` / `aws-ga`)。

明示したい場合 (= 例: TrailNode 配布):
```bash
beacon deploy record --finalize --desc "<説明文>" --backend trailnode
```

backend 別に過去 deploy を絞り込む時:
```bash
beacon deploy list --backend aws-ga    # AWS だけ列挙
beacon deploy list --backend default   # GCP Cloud Run だけ列挙
```

backend 名は固定 enum ではなく **任意文字列** (= 将来 backend が増えてもコード変更不要)。慣例: GCP Cloud Run="default"、AWS GA="aws-ga"、TrailNode 配布="trailnode"、他は profile 名 or 任意の自己説明文字列。

## Step 4: 結果の提示

finalize の stdout を確認し、ユーザーに簡潔に報告:
```
Deploy: [deploy-id] [major/minor] (env)
  [生成した説明文]
```

## Step 4.5: 全貌マップの reconcile を促す (forcing function — ms-104 e-3342)

デプロイ = surface (= 機能の入口: CLI / API / Skill) が世に出る節目。ここが全貌マップ
(application-map = 今このプロダクトに何ができるかを写した現在地の索引、CORE doc `application-map`)
を **足す＆消す (reconcile)** する自然な契機。Step 2 で「何ができるようになったか」を既に言語化
しているので、その同じ理解を累積地図に反映する。これは session-start の map-drift trigger
(commit 数の proxy で発火し無視されがちだった) に代わる **主 forcing function** で、
「出荷した瞬間に地図を直す」ことで地図の腐敗を出荷境界で塞ぐ (e-3342 で再配置)。

### profession gate + 地図の有無

まず対象プロジェクトかを確認する。`beacon status --json` の `profession` が `dev` 以外
(例 `sales`) なら全貌マップは対象外 (開発インスタンス専用の surface 索引) なので、この Step は
**丸ごとスキップ**。`dev` (または未設定) のときのみ以下に進む。

地図がまだ無い (= 未生成) プロジェクトも reconcile 対象外 (生成は別契機 = session-start 提案 /
`/beacon-map` 生成モード)。Bash ツールで確認:

```bash
beacon doc show application-map >/dev/null 2>&1 && echo "MAP_EXISTS" || echo "MAP_MISSING"
```

- `MAP_MISSING` → この Step をスキップ (地図が無いので reconcile できない)。
- `MAP_EXISTS` → 以下でユーザーに reconcile を促す。

### ユーザーに reconcile を提案する

今回のデプロイで surface が増減した (= 新しい CLI / API / Skill が入口として増えた、または
廃止された) かを Step 1〜2 の内容から AI が判断し、ユーザーに 1 行で提案する:

```
このデプロイで surface (機能の入口) が変わっているようです。全貌マップ (application-map) を
`/beacon-map` で reconcile (= 足す＆消す) して、今回の変化を現在地の地図に反映しますか?
  [reconcile する / 後で (次の session-start で map-drift backstop が再度促します) / skip]
```

- **reconcile する** → `/beacon-map` Skill を起動する (reconcile モードで走り、drift ゼロまで直す)。
- **後で / skip** → 何もしない。CLI 側が `map-reconcile` trigger を残しているので、次の
  session-start で backstop として再度目に入る。

surface が明らかに変わっていない (= 文言修正 / infra 調整のみのデプロイ) と AI が判断できる場合は、
提案自体を省いてよい (ノイズ抑制)。判断に迷うなら提案を出す (地図の腐敗より 1 行の確認の方が安い)。

## Step 4.6: コード理解グラフ (code-graph) の 0-drift を確認し、ズレていれば再 seed を促す (ms-156 e-5628)

デプロイ = ソースが世に出る節目。コード理解グラフ (code-graph = エージェントがコードを全部読まずに
「どこに何があり・何に依存するか」を引くための module + 依存の投影) の機械層 (module node +
depends-on / surfaces-as 辺) は **出荷したソースから導出** されるので、出荷で drift しうる。全貌マップと
違いグラフは機械照合できるので、「変わったかも」で促すのではなく **実際に照合してからだけ** 促す
(deploy record が `graph-reseed` trigger を残すのと同じ狙いの、ここは即時プロンプト版)。

Bash ツールで実行 (fail-safe、この Step は判定に徹し自動では seed しない):

```bash
python3 scripts/check-graph-drift.py 2>&1; echo "EXIT=$?"
```

- **EXIT=0** (drift 無し) → 何もしない。
- **EXIT=1** (drift 有り) → 出力の書き漏れ (ソースに在るが graph に無い) / 幽霊 (graph に在るが
  ソースに無い) を 1〜2 行に要約し、再 seed を促す:
  ```
  コード理解グラフが現在ソースとズレています (書き漏れ N / 幽霊 M)。
  `python3 scripts/seed-code-graph.py --derive --update` で再 seed して 0-drift に戻しますか?
    [再 seed する / 後で (deploy record が残した graph-reseed trigger が次の session-start で再掲します)]
  ```
  - **再 seed する** → コマンドを実行し、再度 `check-graph-drift.py` で 0-drift (EXIT=0) を確認する。
  - **後で** → 何もしない。CLI 側が `graph-reseed` trigger を残しているので次の session-start で再度目に入る。
- **EXIT=2** (fatal: グラフ doc の取得失敗) / **EXIT=3** (skip: beacon 本体でない / グラフ未 seed) → 何もしない。

## Step 5: トリガーチェック

Bash ツールで実行:
```bash
beacon trigger tick && beacon trigger check
```

deploy 直後は Operation の run_record 記録や map-reconcile 判定が変わっている可能性があるので、 `tick` で refresh してから `check` (ms-98 / e-2764)。

空でなければ各トリガーの `message` を提示する。`map-reconcile` トリガー (= deploy record が
残した「地図を直せ」の印) が出ていて、Step 4.5 でまだ reconcile していなければ、そこで改めて
`/beacon-map` を促す。

## 制約

- Step 1（prepare）は読み取り専用。書き込みは Step 3（finalize）のみ。
- project.json を直接読まない。beacon CLI 経由のみ。
- 説明文はユーザーが読んで意味がわかる文章にする。技術的な列挙は避ける。
