---
name: beacon-pr-create
description: PRを切る側のための対話Skill。対象MSの確認、intent（このPRで何を達成したいか・なぜ）の引き出し、ブランチ/コミットの整合性チェックを経て、gh pr create + beacon pr add まで一気通貫で実行する。1人開発でも2-5人チームでも同じフローで回せる軽量PR体験。
version: 0.1.0
triggers:
  - /beacon-pr-create
  - PRを切る
  - PRを作る
  - プルリクエストを作る
  - create pull request
---

# Beacon PR Create

> いまの作業を **PR としてレビュー可能な単位に出す** ための対話Skill。
> intent（このPRで何を達成したいか・なぜ）を会話で引き出し、`gh pr create + beacon pr add` まで一気に走らせる。1人開発でも体験が冗長にならないように、確認は最小限。

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
- `gh auth status` が失敗する場合、ユーザーに「gh CLI のログインが必要です」と案内して終了。

## Step 0: 起動引数の解釈

ユーザーが `/beacon-pr-create ms-XX` のように引数付きで呼んだ場合、`ms-XX` を **対象 MS の有力候補** として扱う（Step 2 で最終確認）。
引数なしの場合は Step 2 の自動推定に進む。

## Step 1: ブランチ・コミットの整合性チェック

Bash ツールで **並列に** 実行:

```bash
# 1a. 現在のブランチ
git branch --show-current

# 1b. ローカルとリモートの差分（push 必要か）
git status -sb

# 1c. main / master からの分岐コミット
git log --oneline $(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD origin/master 2>/dev/null || echo "HEAD~10")..HEAD
```

判定:

| 状態 | アクション |
|---|---|
| ブランチが main/master | ユーザーに「main ブランチから PR は切れません。`git checkout -b <name>` で作業ブランチを作ってください」と返して終了 |
| 未push のコミットあり | Step 4 直前で `git push -u origin <branch>` するよう案内（自動 push はしない） |
| 分岐コミットがゼロ | 「PR にできる変更がありません」と返して終了 |

## Step 2: 対象 MS の自動推定（e-607）

Bash ツールで:

```bash
beacon status --json
```

推定優先順位（**上から順に試し、確度が十分なものを採用**）:

1. **Step 0 でユーザーが引数指定** → そのMS
2. **ブランチ名から推定**: 正規表現 `ms-(\d+)` でブランチ名から MS ID を抽出。該当MSが project にあれば採用
3. **直近 commit のメッセージ**: `git log --pretty=%s -5 HEAD` で取得し、`ms-(\d+)` または `\(e-\d+\)` パターンを探す。`e-N` が見つかった場合は `beacon task list` でその task が属する MS を逆引き
4. **active MS** (`status == "in_progress"`): 1 つだけなら採用、複数あれば候補として提示
5. **どれも該当しない**: ユーザーに「対象 MS を教えてください（候補: [active MS 一覧]）」と尋ねる

推定根拠を **必ずユーザーに 1 行で明示** したうえで確認を取る。例:

```
対象 MS: ms-42 "2-5人チームでのPR駆動共同開発体験"
  根拠: ブランチ名 `ms-42/work` から推定
  これでよろしいですか？ (Enter で確定、別MSなら ms-XX を入力)
```

## Step 3: intent の引き出し（対話）

PR record の **intent** は「このPRで何を達成したいか・なぜ」を 1〜3 文で記録するもの。
レビュー時 (`/review`) で実装との乖離チェックに使われる（e-608）。

`git log --pretty=%s` の差分コミットを材料にしつつ、ユーザーに次のように問いかける:

```
このブランチには N 件のコミットがあります:
  - [hash] [subject]
  - ...

このPRで達成したいこと（intent）を一言で教えてください。
例: 「2-5人チームでPRレビュー往復を可視化する」「lost update バグを構造的に塞ぐ」

何のために、何を変えるのか — レビュアーが verdict を出す基準になります。
```

ユーザーが「とりあえず」「特に無い」と回答した場合は、Claude が **コミット履歴と SPEC から intent 案を 1 つ提案** し、ユーザーに承認させる（**完全に空のまま PR にしない**）。

SPEC が紐づいている場合は参考にする:
```bash
beacon doc list --scope spec --ms <ms-id> --json
```
（最初の SPEC を読んで「SPEC §設計方針 X と整合する」のように intent 提案）

## Step 4: PR タイトルと本文の draft

**ms-68 / e-1642 補足 (= entry-writing principle の draft 表示)**: 本 Step は既に draft 提示型 (= 書き込み前にユーザー承認を取る形) で設計されており、ms-68 SPEC が要求する「書き込み直前の draft 表示」の要件を満たしている。提示前に self-review 4 原則 (読み手目線 1 行 / 横文字 3 段階 / ID 参照に文脈 / 尻切れトンボ禁止) を 1 度通し、特に **本文 What / Why / How / Test の各セクションで非開発者を排除する横文字や尻切れトンボが無いか** を自問する。PR 本文は merge 後にプロジェクトの歴史記録として残るため、silent write 禁止。

intent と差分コミット履歴から **PR タイトル候補と本文 draft** を生成し、ユーザーに見せる:

```
PR タイトル案:
  feat(cli): beacon update — brew upgrade + skill install in one shot (e-576)

PR 本文案:
  ## What
  - beacon update コマンドを新設 (e-576 / UC6-K1)
  ## Why
  - ユーザー側の brew upgrade + skill install を 1 コマンドに集約
  ## How
  - インストール方法を自動検出 (brew / git / unknown)
  - --check, --skill-only, -y フラグを提供
  ## Test
  - tests/test_update.py で 7 ケースカバー

タイトル・本文ともこれで PR 作成しますか？編集したい箇所があれば指示してください。
```

## Step 5: 未 push なら push、その後 PR 作成

ユーザー承認後:

1. **未 push がある場合**: `git push -u origin <branch>` をユーザー承認を取ってから実行
2. **PR 作成**: 以下を Bash ツールで実行

```bash
beacon pr create -m <ms-id> --intent "<intent>" --title "<title>" --body-file /tmp/pr-body-<ms>.md
```

※ `--title` / `--body-file` 等の `gh pr create` フラグはそのまま `beacon pr create` に渡して passthrough される（`beacon pr create` 実装参照: `BEACON_GH_ARGS` で gh に転送）。

PR 本文は heredoc ではなく **必ず一時ファイル経由**で渡す:
```bash
# Write tool で /tmp/pr-body-<ms>.md に本文を書き出してから
beacon pr create -m ms-42 --intent "..." -- --title "..." --body-file /tmp/pr-body-ms-42.md
```

`beacon pr create` は内部で `gh pr create` を呼んで PR を作り、URL を捕捉して `beacon pr add` 相当の記録を残す。

## Step 6: 完了報告

成功したら以下を **テキストで** 出力:

```
✓ PR 作成完了

  PR: [URL]
  beacon entry: [e-XXX]
  intent: "<intent>"
  対象 MS: ms-XX

次のステップ:
  - レビュアーを指定したい場合: gh pr edit <num> --add-reviewer <user>
  - レビューを依頼: /review はレビュアー側の Skill です（このPR作成者は使いません）
  - 1人開発で自己レビューする場合: /review <PR-num> で intent vs 実装の整合をチェックできます (e-611)
  - merge 時は `gh pr merge --merge` を使う (= hash 保持、CORE doc 0KqFUbmJ7V0lmJZcW230 参照)
```

### AX レビューの自動発火 (= ms-119 e-4003)

`beacon pr add` (= `beacon pr create` が内部で行う PR 記録) は、PR 作成を「interface 変更の節目」とみなして **AX レビューを自動 bind** する (`.beacon/triggers/ax-review-due-<num>.json` を書き出す)。AX 原典は AX を interface 変更 (= PR / 差分) に紐づけるため、target のライフサイクル遷移ではなく PR 作成がその発火点になる。

この trigger は消えずに残り、`beacon trigger check` と session-start が毎回再提示するので「人が思い出した時だけ走る」状態にならない。PR が close / merge されると自動でクリアされる。作成直後にその場で走らせたい場合は:

```
/beacon-review-run --type ax --pr <num>
```

を実行すると、文脈ゼロの独立 judge に AX 原典と差分を渡して AI 体験の drift を確認できる。

## 取り込み戦略の構造防御 (= ms-80 e-1823)

PR を merge する時は **`gh pr merge --merge`** で merge commit を作る (= linear branch + fast-forward 風)。

| 動線 | 採否 | 理由 |
|------|------|------|
| `gh pr merge --merge` | ✓ 採用 | branch 上の commit hash が保持される、beacon entry が指す dead hash 発生せず |
| `gh pr merge --rebase` | ✗ 禁止 | base に rebase で hash 再生成、過去 `beacon log` entry が dead hash になる |
| `gh pr merge --squash` | ✗ 禁止 | 全 commit を 1 つに圧縮、commit 単位 1:1 trace が壊れる |

GitHub UI 経路の構造防御 (= repo admin 操作、別 task) は Settings > General > Pull Requests で:
- Allow merge commits: ✓ ON
- Allow squash merging: ✗ OFF
- Allow rebase merging: ✗ OFF

詳細: CORE doc `0KqFUbmJ7V0lmJZcW230` (= PR の取り込み戦略: hash 保持と beacon entry 整合)。

## Reviewer 任命動線 (= ms-80 e-1819)

Step 6 で PR が作成された後、reviewer の任命を 1 度の対話で完結させる。1 名・複数名の両対応、Beacon DM 通知の有無を選択可能。

### Step 6.5: Reviewer の任命確認

PR 作成成功直後、ユーザーに 1 度だけ確認:

```
Reviewer を任命しますか? (空 Enter で skip)
  GitHub username をスペース or カンマ区切りで指定:
    例) alice / alice bob / alice,bob,charlie
  skip した場合は、後で gh pr edit <num> --add-reviewer <user> を手動で叩けます。
```

空 Enter で skip した場合は Step 6.5 / 6.6 をスキップして「1人開発時の挙動」 へ。

### Step 6.5a: GitHub 上で reviewer assign

入力された username 群を ',' 区切りで連結し、Bash で実行:

```bash
gh pr edit <pr_number> --add-reviewer <user1>,<user2>,...
```

成功すると GitHub UI / 通知メールで reviewer に届く (= GitHub 標準経路)。

失敗時 (= 例: user が repo collaborator でない / typo):
```
Reviewer assign に失敗しました。原因:
  <gh のエラーメッセージ>
対処:
  - typo の確認 (GitHub 上の username は大文字小文字を区別)
  - repo collaborator でない場合は GitHub 側で先に追加 (`gh repo edit --add-collaborator <user>`)
```

### Step 6.5b: Beacon DM 通知の有無

GitHub 標準通知に加え、Beacon 経由で「何を見ればいいか」 を伝える DM を送るか確認:

```
GitHub 通知に加えて、Beacon DM で「何を見てほしいか」 も送りますか? (= /beacon-dm-send の pr-review template 経路)
  [y] = 各 reviewer に DM 送信 → /beacon-dm-send 起動
  [n] = GitHub 通知のみで完了
```

y の場合:
- 各 reviewer に対し /beacon-dm-send を 1 回ずつ起動 (= multi-reviewer なら N 回)
- /beacon-dm-send は Step 0 mode=send + Step 5c で `pr-review` template を選択するよう内部で hint
- 注視ポイント / merge 条件 / 緊急度は Step 3 で引き出した intent を流用 (= 二度同じ質問しない)
- /beacon-dm-send の Step 6 draft 表示で reviewer 個別に edit 可能

### Step 6.6: 完了報告 (= 多 reviewer 対応の Step 6 拡張)

```
✓ PR 作成 + reviewer 任命完了
  PR: [URL]
  reviewers: [user1, user2, ...]
  beacon DM: [N 件送信 / skip]

次のステップ:
  - reviewer の応答待ち (= GitHub Notifications / Beacon DM 受信側 /beacon-dm-respond)
  - 緊急度の変更: gh pr edit <num> --add-label urgent
```

### multi-reviewer の責務分配メモ (= 参考)

- 2-3 名: 全員 approve 揃いで merge (= safe path)
- 4+ 名: code-owner 必須 + 任意レビューに分けるのが現実的、Skill は code-owner 強制をしない (= GitHub 側の CODEOWNERS file 機能を使う)
- reviewer 間の意見対立は author が判断、reviewer 同士の Beacon DM で議論する経路もあり (= 自由形式)

## 1人開発時の挙動（e-611）

`gh pr list --author "@me" --state open` で他にもオープン PR がない、かつ project に member が 1 名しかいない場合、**冗長な確認をスキップして次のように軽量化**:

- Step 3 の intent 引き出しで「とりあえず」と言われたら、自動で intent 提案 → 即承認確認 1 回で確定
- Step 4 のタイトル/本文確認は 1 ターンで（OK/編集を 1 メッセージで尋ねる）
- Step 6 完了報告で「1人開発検出: `/review <PR-num>` で自己レビューもどうぞ」と一文添える

## エラーハンドリング

| エラー | 対処 |
|---|---|
| `gh pr create` が `pull request already exists` | 既存PR の URL を取得し、`beacon pr add <url> -m <ms>` で記録だけ追加 |
| ブランチが追跡リモートを持たない | `git push -u origin <branch>` をユーザー承認後に実行 |
| `gh` 未ログイン | `gh auth login` を案内して終了 |
| intent が空のまま続行を強要された | warning 出力（「intent 無しの PR は /review が機能しません」）したうえで作成 |

## 出力規範

- 各 Step の出力は 5 行以内を目安に
- 推定の根拠は必ず 1 行で明示する（ブラックボックス化させない）
- ユーザーの承認なしに `gh pr create` を実行しない
- PR が作成されたあと、自動で `gh pr edit --add-reviewer` などをしない（reviewer 指定はユーザー判断）

## 関連 Skill / 関連 task

- `/review` — レビュアー側 Skill。本 Skill で作った PR の intent と実装の整合を判定 (e-608 対応で intent チェック追加予定)
- `/beacon-log` — コミット直後にPostToolUse hook 経由で起動。PR と commit の連結は別途 (e-610) 対応
- 関連 task: e-606 (本Skillの起票), e-607 (対象MS自動推定), e-611 (1人開発軽量化), e-608 (review側 intent チェック)
