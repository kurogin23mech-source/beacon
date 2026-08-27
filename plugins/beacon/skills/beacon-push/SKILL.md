---
name: beacon-push
description: git push 後に自動で実行し、コミットから「何が変わったか」の説明を生成してプッシュ記録を残す。
---

# Beacon Push

> git push後に自動実行。コミット情報からAIが価値ベースの説明を生成し、push recordを記録する。

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

## Step 2.5: リリース判定と起動経路 (forcing function — ms-52 e-959)

`release.yml` の有無で経路を分岐する。CORE doc `rMlHx9n0LYFJ2kWIQELi`
(リリース 5 配信チャネル整合の原則) が定める「5 配信チャネル (Web UI / Server
/ CLI / Skill / Desktop) が揃った時にだけリリース完了」原則を、Skill 層から
構造的に支える forcing function。

### Step 2.5a: release.yml 検知 + opt-in 確認

Bash ツールで実行:
```bash
test -f .github/workflows/release.yml && echo "RELEASE_YML_EXISTS" || echo "NO_RELEASE_YML"
beacon doc show version-rules 2>/dev/null | head -1
```

- 1 行目に `RELEASE_YML_EXISTS` → **経路 A (Step 2.5b)** に進む
- `NO_RELEASE_YML` で かつ `version-rules` doc が空 → Step 3 へスキップ (release 体験未 opt-in)
- `NO_RELEASE_YML` で `version-rules` doc が存在 → **経路 B (Step 2.5c, fallback)** に進む

### Step 2.5b: 経路 A — `release.yml` が存在 (第一推奨)

これが **forcing function の正規経路**。`release.yml` が semver bump → tag → formula
→ homebrew tap mirror を実行し、最終 step で `deploy-cloud-run.yml` (await) と
`release-build.yml` (fire-and-forget) を fan-out する (ms-52 e-953/e-954)。**手動で
`git tag` + `gh release create` を打たない** — 同じ tag に対する GitHub Release
の二重作成 / workflow fan-out 抜けが起きる。

#### release-due trigger の確認

Bash ツールで実行:
```bash
beacon trigger tick && beacon trigger check 2>/dev/null | python3 -c "import json,sys
ts = json.load(sys.stdin)
due = [t for t in ts if t.get('kind') == 'release-due']
for t in due:
    print(f\"- {t['message']}\")
"
```

push 直後は commit 追加で release-due の判定が変わっている可能性が高いので、 `tick` で明示的に refresh してから release-due kind を抽出する (ms-98 / e-2764)。

release-due トリガー (ms-52 e-958: feat 3+ または fix 5+ で fire) が出ていれば
「リリースの頃合い」のシグナル。fire していなくても、ユーザー意志での release
判断は妨げない (閾値はあくまで promotion であって gate ではない)。

#### bump 区分の予測 (= MS 駆動ルールに沿った確認)

CORE doc `version-rules` (= spec doc、ms-52) で定義された MS 駆動 bump ルール:

- **MINOR**: 新規 MS の提供価値が初めて land した時 (= その MS の最初の release)
- **PATCH**: 既存 MS の改善・修正・refactor
- **MAJOR**: BREAKING change

commit prefix から release.yml が自動判定するが、**Skill 側で「MS 駆動の妥当性」を AI が事前確認** する (= e-1659 v3 期に観測した MINOR 乱発の reset 効果)。

Bash で対象 commits の prefix を集計:
```bash
LAST_TAG=$(git tag --sort=-creatordate --merged HEAD | head -1)
git log --pretty="%s" "$LAST_TAG..HEAD" | head -20
```

AI が以下を判定:

1. `feat(ms-XX):` の commit があるか?
2. その `ms-XX` は **これまでに release されていない MS** か? (= MS 別 release 履歴は `version-rules` の編集規律で「`feat:` は新規 MS 初 land のみ」を前提とするため、`feat:` が出ているなら原則「新規 MS land」と扱う)
3. もし「既存 MS の改修なのに `feat:` を使ってしまった」疑いがあれば、ユーザーに確認:

```
⚠ 機械判定では MINOR bump になりますが、以下の commit は既存 MS の改修に見えます:
  - [hash:7] feat(ms-XX): ...
  - ...
既存 MS の改修なら fix: / refactor: に書き直す (= amend / rebase で prefix 修正) か、
今回は PATCH bump 上書きで通す (= 後者は version-rules の編集規律から逸脱)。
どう進めますか？ [prefix 修正 / PATCH 上書き / そのまま MINOR で続行]
```

#### ユーザーへの提示

```
このプロジェクトは release.yml を持っています (5 配信チャネル整合 forcing function 完備)。
[release-due trigger があれば その message を 1 行で添える]
[bump 予測: PATCH / MINOR / MAJOR — 根拠を 1 行で]

リリース起動経路 (推奨):
  1) Dry-run: `gh workflow run release.yml -f dry_run=true` 
     計画 (bump 判定 / 対象 commits) を確認する。
  2) 本番: `gh workflow run release.yml -f dry_run=false`
     bump → tag → formula → tap mirror → deploy-cloud-run.yml (await) → release-build.yml (fan-out)。
  3) 貯める: いまは release を切らず、もう少し commit を貯めてから出す。

このタイミングで release を切りますか？ [dry-run / 本番 / 貯める / skip]
```

「貯める」は **v0.37.x 期の MINOR 乱発反省を反映した新選択肢**: per-commit に release を機械起動するのではなく、まとまった単位 (= MS の最初の release、または積み残しが揃ったタイミング) で出す運用を推す。

#### 承認時の処理

- `dry-run` → `gh workflow run release.yml -f dry_run=true` を実行、結果は Actions UI / 
  `gh run watch` で見る (release は記録されない)
- `本番` → `gh workflow run release.yml -f dry_run=false` を実行。release.yml の終了後
  (deploy-cloud-run.yml の health check pass + release-build.yml が fan-out 起動済) に
  `beacon trigger check` を再実行すると `release-X.Y.Z` トリガーが立つ
- `貯める` / `skip` → 何もせず Step 3 へ。次の commit 後の `/beacon-push` で再判断

### Step 2.5c: 経路 B (fallback) — `release.yml` が存在しない

> **⚠ これは `release.yml` 不存在時の fallback です。**
> 多くのプロジェクトでは経路 A が正解。Beacon 自身や 5 配信チャネル整合の雛形
> (CORE doc `rMlHx9n0LYFJ2kWIQELi` 参照) を採用しているプロジェクトには `release.yml`
> があるはずなので、まず A 経路を検討してください。手動経路は誤って bypass された場合、
> workflow fan-out (deploy + Tauri build) が走らない / GitHub Release が release.yml の
> 出力と二重化する等のリスクがあります。

`version-rules` doc が opt-in されているが `release.yml` が無い場合のみ、以下を実行:

#### 判定ロジック

`version-rules` doc の内容と `git log <last-tag>..HEAD` の commit 群から:
1. **MAJOR**: `BREAKING CHANGE` / `BREAKING:` を含む、または `feat!:` / `fix!:` prefix
2. **MINOR**: `feat:` / `feat(...):` prefix
3. **PATCH**: その他

最大の昇格度を採用し、現 tag (`git describe --tags --abbrev=0 --match='v[0-9]*'`) を bump。

#### ユーザーへの提示 (fallback 明示)

```
⚠ release.yml が無いプロジェクトの fallback 経路です (本来は release.yml を整備して経路 A を使う)。

バージョン判定:
  現tag: v0.1.0
  次:    v0.2.0  (MINOR bump)
  根拠:  feat 5本, fix 4本, BREAKINGなし

このpushにタグを切ってGitHub Releaseを作成しますか？ [y/N]
```

#### 承認時の処理

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
gh release create vX.Y.Z --title vX.Y.Z --notes "Release vX.Y.Z" 2>/dev/null || true
```

却下またはスキップ時は何もしない。Step 3 へ進む。

## Step 3: 書き込み

**ms-68 / e-1642 補足 (= entry-writing principle の draft 表示)**: `beacon push record` を実行する **前** に、Step 2 で生成した説明文を 1 度ユーザーに提示し、self-review 4 原則 (読み手目線 1 行 / 横文字 3 段階 / ID 参照に文脈 / 尻切れトンボ禁止) で違反が無いか自問する。違反があればその場で書き直してから書き込む。push record の本文は将来 retrospection や release note 生成で参照されるため、silent write は読み手 (非開発者を含む) を排除する。

```
push を以下の説明文で記録します:

  <Step2 で生成した説明文>

このまま記録しますか? (= OK / 書き直し)
```

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

## Step 4.5: 全貌マップの reconcile を促す (release 出荷境界 — ms-104 e-3342)

**この Step は Step 2.5 で release を切った場合のみ実行する** (= 通常の push だけで release
を切っていないなら skip)。

pull 型デプロイ (= サーバや配布物が client 側 deploy hook 無しで main merge / release で
更新される形態) では、**出荷境界 = release record** になる。release を切った瞬間が surface
(= 機能の入口: CLI / API / Skill) が世に出た節目なので、ここが全貌マップ (application-map =
今このプロダクトに何ができるかを写した現在地の索引、CORE doc `application-map`) を
**足す＆消す (reconcile)** する自然な契機。Step 2 で「何が変わったか」を既に言語化している
ので、その同じ理解を累積地図に反映する。session-start の map-drift trigger (commit 数の
proxy で無視されがちだった) に代わる **主 forcing function** を出荷境界に置く設計 (e-3342)。

### profession gate + 地図の有無

`beacon status --json` の `profession` が `dev` 以外なら全貌マップは対象外なのでスキップ。
`dev` (または未設定) のとき、地図の有無を確認:

```bash
beacon doc show application-map >/dev/null 2>&1 && echo "MAP_EXISTS" || echo "MAP_MISSING"
```

- `MAP_MISSING` → スキップ (地図が無いので reconcile できない。生成は `/beacon-map` の生成モード)。
- `MAP_EXISTS` → 以下でユーザーに reconcile を促す。

### ユーザーに reconcile を提案する

今回の release で surface が増減したかを Step 1〜2 の内容から AI が判断し、1 行で提案する:

```
release vX.Y.Z で surface (機能の入口) が変わっているようです。全貌マップ (application-map)
を `/beacon-map` で reconcile (= 足す＆消す) して、今回の変化を現在地の地図に反映しますか?
  [reconcile する / 後で (次の session-start で map-drift backstop が再度促します) / skip]
```

- **reconcile する** → `/beacon-map` Skill を起動する (reconcile モード、drift ゼロまで直す)。
- **後で / skip** → 何もしない。次の session-start で map-drift backstop (release 数基準) が
  再度促す。

surface が明らかに変わっていない release と AI が判断できるなら提案を省いてよい (ノイズ抑制)。

## Step 4.6: コード理解グラフ (code-graph) の 0-drift を確認し、ズレていれば再 seed を促す (ms-156 e-5628)

release = ソースが世に出る節目。コード理解グラフ (code-graph = エージェントがコードを全部読まずに
「どこに何があり・何に依存するか」を引くための module + 依存の投影) の機械層 (module node +
depends-on / surfaces-as 辺) は **出荷したソースから導出** されるので、出荷で drift しうる。全貌マップと
違いグラフは機械照合できるので、「変わったかも」で促すのではなく **実際に照合してからだけ** 促す。
push は deploy と違い CLI 側の graph-reseed trigger を残さないので、この照合がグラフ側の主 forcing
function になる。

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
    [再 seed する / 後で (次の deploy / push で再度照合されます)]
  ```
  - **再 seed する** → コマンドを実行し、再度 `check-graph-drift.py` で 0-drift (EXIT=0) を確認する。
  - **後で** → 何もしない。次の出荷フローで再度照合される。
- **EXIT=2** (fatal: グラフ doc の取得失敗) / **EXIT=3** (skip: beacon 本体でない / グラフ未 seed) → 何もしない。

## Step 5: トリガーチェック

Bash ツールで実行:
```bash
beacon trigger tick && beacon trigger check
```

push + release 完了後は release-marker / release-due 等の判定が変わっている可能性が高いので、 `tick` で明示 refresh してから `check` で local read (ms-98 / e-2764)。

空でなければ各トリガーの `message` を提示する。

## 他プロジェクトでの「リリース体験」（e-578）

このSkillは **beacon リポジトリ専用ではない**。version-rules を opt-in している任意のプロジェクトで動作する。

### opt-in 手順

1. プロジェクトに CORE doc `version-rules` を作る（既存beaconプロジェクトの doc を参考に）
2. `lib/version_rules.py` 相当の解釈ロジックは beacon CLI に同梱されているので、追加インストール不要
3. `/beacon-push` を実行すると Step 2.5 が自動で発火する

### scripts/release.py との違い

| | `/beacon-push` Skill | `scripts/release.py` |
|---|---|---|
| 対象 | 任意のプロジェクト | beacon CLI 自身（メンテナ専用） |
| 配布 | beacon CLI に同梱 | リポジトリ内のみ |
| Brew formula 更新 | ❌ | ✓ |
| GitHub Release 作成 | ✓ (opt-in) | ✓ |
| README/CHANGELOG 更新 | （プロジェクト側のhookで） | ✓ (e-582) |
| Discord/Slack 通知 | trigger fire のみ (e-580) | trigger fire + brewまで |

ユーザープロジェクトでは `/beacon-push` を使う。beacon CLI 自体のリリースは `scripts/release.py` を使う。両者を混同しないこと。

### 1 コマンドリリースを目指す未来形

将来的には、たとえば semver bump と notes を引数で渡すだけの単一 CLI コマンドで、tag 切り → GitHub Release → CHANGELOG 更新 → 通知トリガー発火までを 1 ステップで実行する経路を追加する候補がある (= `release` 系の subcommand、現状未実装)。本タスク (e-578) では Skill での opt-in パスを完成させるところまでで止め、CLI 単独コマンドは別タスクで扱う。

## 制約

- Step 1（prepare）は読み取り専用。書き込みは Step 3 のみ。
- project.json を直接読まない。beacon CLI 経由のみ。
- 説明文はユーザーが読んで意味がわかる文章にする。技術的な列挙は避ける。
