---
name: beacon-session-start
description: Beaconプロジェクトのセッション開始時にコンテキストを復元。アクティブMS・未消化タスク・summaryを提示する。
version: 0.7.0
---

# Beacon Session Start

> セッション開始時に beacon CLI 経由でプロジェクトの現状を取得し、ユーザーに提示する。読み取り専用。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 0: 環境チェック（beacon doctor 軽量版 + BEACON_BIN gate）

### Step 0a-pre: BEACON_BIN gate (= ms-93 / e-2276)

PATH 上の古い `beacon` が新しい install を黙って隠すケースを、doctor 任せにせず resolver で hard fail / soft warn を明示判定する (背景と dogfood 経緯は e-2276 を参照)。

repo root を見つけて (= `beacon-find-root` の出力)、 resolver script を直接叩く:

```bash
__ROOT=$(beacon-find-root) && python3 "$(beacon _install-root)/scripts/beacon-bin-resolver.py" 2>&1
```

stdout は 1 行 JSON。 以下の field を読む:
- `verdict`: `ok` / `soft_warn` / `hard_fail` / `no-candidate`
- `selected_bin`: 選ばれた絶対パス
- `selected_source`: `env` / `install_root` / `path`
- `advice`: 1 行の user 向け推奨メッセージ

#### `verdict == "hard_fail"` または `"no-candidate"` の場合

セッション開始を **中断せず** 進めるが、 最上位に必ず表示する:

```
⚠ BEACON_BIN gate: <advice>
  selected: <selected_bin> (source=<selected_source>)
```

これは Step 3 出力ヘッダの先頭 (Web UI / Incident セクションよりさらに上) に置く。
ユーザーが `BEACON_BIN=<absolute path>` を export し直すまで他 session-start
操作がノイズ混じりで動く可能性があるため、 視認性最優先。

#### `verdict == "soft_warn"` の場合

Step 3 出力ヘッダに 1 行だけ添える:

```
ℹ BEACON_BIN gate: <advice>
```

詳細な signal は出力しない (= `candidates_probed` は debug 用、 default では非表示)。

#### `verdict == "ok"` の場合

何も表示しない。

#### script 自体が叩けない場合 (= beacon repo が古くて resolver 未配置 等)

`scripts/beacon-bin-resolver.py` が存在しない、 もしくは Python が無い場合は
**silent skip** して次の Step 0a に進む。 この gate は best-effort、 旧 beacon
で session-start を呼んだ user を block しない。

### Step 0a: 旧 doctor 軽量チェック (= 既存の挙動を継承)

Bash ツールで実行:
```bash
beacon doctor 2>&1
```

- 出力が `OK:` で始まる場合 → 何もせず次へ進む
- 警告が含まれる場合 → その警告をそのまま提示し、次へ進む（中断しない）
- `beacon` コマンドが存在しない場合 → スキップして次へ進む

BEACON_BIN gate (Step 0a-pre) が PATH 関連の構造化判定を行うので、 doctor の
PATH 警告は冗長になることがあるが、 そのほかの警告 (= hooks / skills-drift /
legacy-mode-field / ms81 系) は doctor 経由でしか出ないため両方走らせる。

### Step 0a-skew: version skew 検知 (= ms-93 / e-3135)

古い beacon が daemon / hook として混じっていないか (= 動いているようで別物を見ている穴) を、受信 daemon の stamp した version と CLI version の skew として session 開始時に surface する (背景と stamp 経路の詳細は e-3135 を参照)。

Bash ツールで実行 (fail-safe、 daemon / hook が無ければ何も出ない):

```bash
__ROOT=$(beacon-find-root) && python3 "$(beacon _lib-path)/version_skew.py" \
  --current-version "$(beacon --version 2>/dev/null | tail -1)" \
  --cwd "$__ROOT" 2>/dev/null
```

- `--cwd` で **daemon skew** (この cwd で走る受信 daemon の版)、 `$CODEX_HOME`
  (既定 `~/.codex`) の hooks.json 経由で **hook skew** を自動判定する。
- 出力が **空** → skew 無し、 何も表示しない
- 出力に `⚠ version skew:` 行があれば → Step 3 出力ヘッダに **そのまま転記**
  (daemon skew は「daemon を再起動」、 hook skew は
  「`beacon-codex-bridge install-hook` を再実行」が対処。 PATH 上の複数 binary
  skew は Step 0a-pre の BEACON_BIN gate と重複しうるので、 daemon / hook 行を
  優先して出す)

`lib/version_skew.py` が無い (= 古い beacon repo) 場合は silent skip。 この Step
は **読み取り専用** かつ **never block** (= skew は warning、 session 開始を
止めない)。

## Step 0c: 引数チェック

ユーザーが `/beacon-session-start ms-XX` のように引数付きで呼んだ場合、`ms-XX` を **スコープMS** として記憶する。
複数指定も可能: `/beacon-session-start ms-16 ms-17`

スコープMSが指定された場合、Step 1a のコマンドが変わる。

## Step 0b: bus heartbeat — 廃止 (ms-54 e-1319)

このステップは **何もしない** (no-op)。heartbeat の真値源は bridge の poll loop に一本化済み。責務分界の経緯は commit 78048b6 / test `test_session_heartbeat_responsibility` を参照。

## Step 1: プロジェクト状態の取得（並列実行可）

以下の3つを **Bash ツール** で **並列に** 実行する:

### 1a. プロジェクト全体の状態

スコープMSが **指定されている** 場合:
```bash
beacon status --json --ms <ms-id> [--ms <ms-id> ...]
```

スコープMSが **指定されていない** 場合（従来通り）:
```bash
beacon status --json
```
stdout に JSON が返る。以下のフィールドを使う:
- `name`: プロジェクト名
- `summary`: 前セッションの経緯・背景
- `profession`: この instance の職種 (= `dev` / `sales` 等、無ければ `dev` 扱い)。以下の職種分岐の判定に使う
- `milestones[]`: 各MSの `id`, `title`, `status`, `progress`, `total_tasks`, `done_tasks` (**開発 instance のみ**、営業では空)
- `targets[]`: 職種非依存の「対象」投影 (ms-108 e-3269)。各要素は `id` / `label` / `status` / `kind` (`milestone` or `opportunity`) / `work_items_total` / `work_items_done` / `detail` (職種固有の付帯情報)。**開発では Milestone、営業では Opportunity (商談) が同じ形で並ぶ**

**職種分岐 (ms-108 e-3269 / SPEC 方針6)**: session-start は③共有フレーム Skill なので、骨格 (文脈復元・次の一手) は職種不変だが「何を投影するか」が職種で変わる。以下を守る:
- **`profession == "dev"` (または未設定)**: 従来通り `milestones[]` を主軸に読む。以降の全 Step は現行動作。
- **`profession == "sales"` 等 (非 dev)**: `milestones[]` は空なので `targets[]` (= 商談) を主軸に読む。Step 2 (アクティブ MS 詳細) は「アクティブ商談 (`status` が進行中の Opportunity)」に読み替える。Step 2.9 の「次の一手」は **`/beacon-sales-cockpit` に委譲** する (SPEC 方針5 = 営業の『次の一手』は cockpit に一本化。session-start 側で商談ごとの next-action を再実装しない)。Step 3 出力は「Active」欄を商談 (`[opp-id] label — フェーズ / ball / 消化数`) に置き換える。

### 1b. git の最新コミット
```bash
git log --oneline -5
```

### 1c. ワーキングツリーの状態
```bash
git status --short
```

### 1d. COREドキュメント一覧
```bash
beacon doc list --scope core --json
```
stdout に JSON 配列が返る。各要素は `doc_id`, `title`, `scope`, `updated_at` を持つ。
空配列の場合はこのステップをスキップする。

### 1d-2. メモスコープのドキュメント一覧 (ms-43 e-564)

前セッション末で memo scope に昇格させた重要メモを引き継ぐため、memo scope も一覧する:
```bash
beacon doc list --scope memo --json
```

memo は CORE と違って常時参照ではないので、**取得するのは「直近 7 日以内に作成/更新された memo」のみ**。古い memo は無視する (ノイズ削減)。

`updated_at` を ISO8601 でパースし、今日からの差分日数で 7 日以内を残す。3件を超える場合は最新 3 件に絞る。

### 1d-3. 全貌マップの有無チェック (ms-104 e-3153)

Step 1d の core doc 一覧に **`doc_id == "application-map"`** (= アプリケーション全貌マップ、
「今このプロダクトに何ができるか」を写した現在地の索引) が含まれるかを確認する。

**profession gate (ms-109 e-3404)**: 全貌マップは **開発インスタンスの surface (= コード / CLI / Skill の入口)
索引** なので、`beacon status --json` の `profession` が `dev` 以外 (例 `sales`) のプロジェクトでは、この Step を
**丸ごとスキップ** する (未生成提案を出さない)。営業等は map を持たないのが正しい状態。以下は `profession == "dev"`
(または未設定) のときのみ適用する。

- **含まれる** → 通常通り Step 1e で 1 行サマリー化する。ただしこの doc は **全貌把握の主索引** なので、
  他の CORE doc より先頭に置き、「新機能を足す前に引く索引」である旨を 1 行添える。
- **含まれない (= 未生成)** → **提案を記憶** し、Step 3 出力に以下を 1 行加える (read-only、自動生成しない):
  ```
  ℹ 全貌マップ (application-map) が未生成です。プロジェクトが育つと「近い既存機能があるか」を
    引けず二重実装が起きやすくなります。`/beacon-map` で現在の全 surface から生成できます。
  ```

これは既存の `spec-needed-ms` 提案と同じ「無いものを提案する」パターン (= 新規プロジェクトは init が箱を
作るが、init 前から在る既存プロジェクトには箱が無いので、session-start が生成契機を出す backfill 経路)。

この Step は **読み取り専用**。自動で `/beacon-map` を起動したり doc を作ったりしてはならない。

## Step 1e: COREドキュメントの内容取得 (要約モード)

Step 1d の結果が空でなければ、各ドキュメントの内容を **Bash ツール** で **並列に** 取得する:

```bash
beacon doc show <doc_id>
```

stdout にドキュメント本文（Markdown）が返る。frontmatter（`---` で囲まれた部分）は除去して本文のみ使う。

### ms-43 e-566: 出力情報量の最適化

CORE doc は **全文を Step 3 出力に展開しない** (トークン浪費)。代わりに以下の戦略を取る:

1. 各 CORE doc に対し、AI が「**1 行サマリー**」を生成する (元本文の見出し・冒頭段落・タイトルから推定。50〜80 文字目安)
2. Step 3 では `[CORE] [title]: [1行サマリー]` のみを出力する
3. 詳細が必要な場合の参照経路をフッタで明示:
   - Web UI を開いている場合 → 「Documents タブで全文閲覧可能」
   - CLI 派の場合 → `beacon doc show <doc_id>` でいつでも展開できる旨を末尾に 1 行添える

例 (旧出力):
```
[CORE] doc-classification:
（500 トークン分の本文全文…）
```

例 (新出力):
```
[CORE] doc-classification: ドキュメントを core/spec/memo/report の 4 つで分類する原則。
```

**例外**: ユーザーが `/beacon-session-start --verbose` 引数で呼んだ場合、または対象 CORE doc が 200 文字以下なら全文を出してよい。
**スコープ MS 指定時**は、その MS に関連すると AI が判断した CORE doc のみ全文展開してよい (作業に直結するため)。

### Step 1e-2: memo scope の取得 (ms-43 e-564)

Step 1d-2 で 1 件以上残っていれば、各 memo doc も **Bash ツール** で **並列に** 取得:

```bash
beacon doc show <doc_id>
```

CORE と同じく **1 行サマリー** に圧縮して Step 3 出力に含める。「前セッションからの引き継ぎメモ」セクションとして提示する。

## Step 1f: アクティブMSおよびアクティブOperationのSPECドキュメント取得

Step 1a の結果から `status == "in_progress"` のマイルストーンIDと、`operations[]` のうち `status == "open"` のOperation IDを特定する。

あれば **Bash ツール** で **並列に** 実行:

```bash
# アクティブMSのSPEC
beacon doc list --scope spec --ms <active-ms-id> --json

# アクティブOperationのSPEC（各op-idに対して）
beacon doc list --scope spec --op <op-id> --json
```

結果が空でなければ、各ドキュメントの内容を Step 1e と同様に `beacon doc show <doc_id>` で **並列に** 取得する。

> SPEC 無し active MS の warning は Step 4 の `spec-needed-<ms-id>` トリガーが担う (MS 追加時 fire)。session-start 側で重ねてスキャンしない (= 重複表示を避ける)。

## Step 1g: GitHub PR 自動検知（fail-safe）

Bash ツールで **2つ並列に** 実行:

```bash
# 1. オープンPRの一覧（未記録PR検知に使用）
gh pr list --json number,title,url,author,headRefName,state 2>/dev/null

# 2. 全PR一覧（クローズ/マージ済み検知に使用）
gh pr list --state all --json number,title,url,state,mergedAt 2>/dev/null
```

どちらも失敗した場合（gh未設定、リポジトリ外など）は無視してスキップする。

取得できた場合、Step 1a または Step 2 の結果から beacon に記録済みの PR エントリ（`type == "pr"`）を収集し、以下の2つを照合する。

### 未記録オープンPRの検知

コマンド1の結果から、beacon に記録されていないオープンPRを特定する（URL照合）。

**未記録PRがある場合**、Step 3 の出力に含める:
```
未記録のPR:
  - PR#42: [title] (author: [login]) → beacon pr add で記録できます
```

### クローズ/マージ済みPRの検知（e-368）

コマンド2の結果を使い、beacon に `status == "open"` または `status == "in_review"` で記録されているが、GitHub 上では `state == "CLOSED"` または `state == "MERGED"` になっているPRを特定する。

beacon エントリの URL からPR番号を抽出し、コマンド2の結果と突き合わせる。

**該当PRがある場合**、Step 3 の出力に含める:
```
クローズ/マージ済みPR（beacon未更新）:
  - [e-xxx] PR#N: [title] — GitHub上では [closed / merged] → beacon pr close で更新できます
```

### レビュー待ちPRの検知

beacon に `review_status == "pending"` または `review_status == "changes_requested"` の PR がある場合:
```
レビュー待ちのPR:
  - [e-xxx] PR#N: [title] [in_review / review: pending]
```
→ この場合は Step 3 出力の後に `/review <pr_number>` を即時起動する（beacon trigger より優先）。

この Step は **読み取り専用**。自動で `beacon pr add` や `beacon pr close` を実行してはならない。

## Step 1h: GitHub Issue 自動検知（fail-safe）

Step 1g と **並列に** Bash ツールで実行:

```bash
beacon issue list --json 2>/dev/null
```

失敗した場合（gh未設定、リポジトリ外など）は無視してスキップする。

結果が空でなければ、Step 3 の出力に含める:
```
未インポートのIssue:
  - #42: [title] → beacon issue import 42 で取り込めます
```

3件以上ある場合は先頭2件を表示し「他N件: beacon issue sync で一括インポート」と追記する。

この Step は **読み取り専用**。自動で `beacon issue import` や `beacon issue sync` を実行してはならない。

## Step 1i: beacon-bus channel install 検知（ms-54 e-1173）

session-start が走った cwd で beacon-bus channel が未 install だと、送信は効くのに受信だけ silent に死ぬ (= 「送れたから繋がっている」と誤認する) 非対称が起きる。`.mcp.json` / `beacon-bus` MCP entry の存在を検知して警告する (背景・実害事例・worktree 遷移で同穴に落ちる注意は e-1173 を参照)。

Bash ツールで実行（fail-safe、常に終了コード 0）:

```bash
python3 "$(beacon _install-root)/scripts/check-mcp-receive-capability.py" 2>/dev/null
```

スクリプトが何も出力しなければ受信 bridge は健全（= 出力に含めない）。**stdout に「⚠ この cwd は「送信専用」の恐れ」で始まるバンドが出たら、それを Step 3 出力にそのまま転記する**。`detail:` 行に `NO_MCP_JSON` / `NO_BEACON_BUS_ENTRY` / `MCP_JSON_MALFORMED` のどれかが入る。判定ロジックとバンド文言は script 側 (`detect_status` / `format_warning`) が所管。

この Step は **読み取り専用**。自動で `beacon channel install` を実行してはならない（session-start 全体の読み取り専用原則に従う）。

## Step 1k: branch / workspace 乖離の警告検知 (ms-65 e-1481)

同 cwd で複数 bclaude が並走している状態 + 自セッションが main project root に居て non-default ブランチに乗っている、という **silent な branch share 事故** の温床条件を検知する。構造修正 (= e-1477 cwd-aware milestone start) は事故が起きる経路を狭めているが、ユーザーが手で `git checkout` した残余ケースは捕まえられない。本ステップが検知側 forcing function。

Bash ツールで実行 (fail-safe、出力がそのままユーザーへの警告になる、終了コードは常に 0):

```bash
python3 "$(beacon _install-root)/scripts/check-branch-focus-divergence.py" 2>&1
```

スクリプトが何も出さなければ安全 (= warning 不要)。**stderr に「⚠ branch / workspace 乖離の警告」が含まれていたら、それを Step 3 の出力ヘッダ部分にそのまま転記する**。

このスクリプトは **block しない** (= session-start を中断しない)。あくまで気付かせるための表示。

## Step 1m: 親セッション情報の取得 (ms-67 e-1551)

このセッションが `/beacon-session-fork` で立ち上げられた子セッションの場合、`.beacon/fork.json` に親 ↔ 子の紐付け情報が記録されている。これを読んで Step 3 の出力ヘッダに親情報を表示する。

Bash ツールで実行 (fail-safe、ファイル無ければ silent skip):

```bash
test -f .beacon/fork.json && cat .beacon/fork.json
```

JSON が返れば parse して以下のフィールドを Step 3 で表示するために保持:

- `parent_session_id`: 親セッションの sid
- `parent_branch`: 親が乗ってた branch
- `parent_repo_path`: 親 repo の絶対パス
- `target_ms_id` / `target_ms_title`: この fork で取り組む対象 MS
- `child_branch`: 自分が乗ってる branch (= `<target_ms_id>-fork-<short>`)
- `channel_install.ok`: fork 時に親が叩いた `beacon channel install` が成功したか

`channel_install.ok == false` の場合は、Step 3 ヘッダに「fork 時の channel install が失敗、自分で `beacon channel install` を打ち直してください」の警告を添える。

`target_ms_id` が取れたら、それを **Step 2.9 の「次の一手」決定の最優先入力** として扱う。fork の意図は明確 (= その MS を進めるためにこの worktree を立てた) なので、session log の next-action と同等の確信度で推奨できる。

この Step は **読み取り専用**。

## Step 1n / 1n-2: DM inbox の取得 (ms-70 e-1714 + ms-54 e-2974、ms-85 e-3180 で統合)

session-start が start 時に取り込むべき DM は 2 系統ある:

- **保留中 DM action** (Step 1n / ms-70 e-1714): terminal close 中に届いた cross-user DM の action 付き envelope。ディスパッチャ・ゲートが `bus_event_approvals` sidecar に `approval_status="pending"` を立てて auto-act を抑止しており、これを human に提示して「閉じている間に来た action 系 DM が次回起動時に必ず目に入る」経路を作る。
- **user-scoped DM catch-up** (Step 1n-2 / ms-54 e-2974): 受信 bridge が e-1209 filter で意図的に drop する user-scoped 情報 DM。過去セッションの inbox に出ないため「留守中に届いた DM」が見落とされる穴を、server bus events を直接 query して埋める。

e-3180 でこの 2 系統を **1 スクリプトに統合**: project_id / user_id / id_token の解決を 1 回だけ行い、両セクションを 1 回の呼び出しで出す。Bash ツールで実行 (fail-safe、cloud 未設定 / endpoint 不在ならスキップ):

```bash
python3 "$(beacon _install-root)/scripts/session-start-dm-inbox.py" 2>/dev/null
```

出力が空でなければ Step 3 の出力ヘッダ部に **そのまま転記** する (保留中 action → catch-up の順、両方あれば空行区切り)。空ならセクションごと省略。フェッチ統合 = `scripts/session-start-dm-inbox.py`、整形 = `lib/dm_pending.format_pending_dm_summary` / `filter_user_scoped_catchup` / `format_user_scoped_catchup` (単体テスト済み)。

- 保留中 action は `event_id from sender_user_id at created_at` の 1 行サマリー。envelope 本文は sidecar に持たない設計 (ms-70 / e-1712) なので、詳細は `beacon dm show <event_id>` (e-1716 で primitive 化予定) + `beacon dm respond approve|deny <event_id>`。決定は `/beacon-dm-respond` Skill 経由でのみ (読み取り専用)。
- catch-up は preview 80 文字まで、詳細は `beacon bus receive --channel dm`、返信は `/beacon-dm-send` (reply mode)。既読 stamp は AI が read した際にサーバ側で自動記録 (Skill 側で明示 ack しない)。

local mode (= `.beacon/cloud.json` 不在) / 未認証 / endpoint タイムアウトはすべて silent skip。session-start を中断しない。この Step は **読み取り専用**。

## Step 1o / 1o-2: Trek 状態の取得 (ms-75 e-1813 + e-1854 + e-2047、ms-85 e-3180 で統合)

Trek (= 缶詰の徹夜作業部屋、 user が join した瞬間に scope 内 action が事前承認スコープになる作業空間) に関する 2 つの可視化を session-start で行う:

- **join 中 Trek 一覧** (Step 1o / e-1813 + e-1854): join 済 trek のリストと goal_state / halt 状態。ms-70 (= cross-user DM 承認ゲート) は Trek 参加中だけ blanket 自動承認 (= 都度確認なしで配信) になるため、「自分が今どの Trek の blanket 例外を受けているか」を毎セッション可視化する。
- **armed セルフチェック** (Step 1o-2 / e-2047): active Trek に join 中なら、このセッションが armed (= 自律実行モード) かを確認する。`--no-arm` opt-out / 旧バージョン join / 別 worktree の budget 不在で not-armed が起こりうる。

e-3180 でこの 2 つを **1 スクリプトに統合**: `beacon trek list --joined` を 1 回だけ叩き、active trek がある時だけ armed 判定用の `beacon bus auto-execute list` / `beacon bus budget show` を追加取得する。Bash ツールで実行:

```bash
python3 "$(beacon _install-root)/scripts/session-start-trek-status.py" 2>/dev/null
```

出力が空ならセクションごと省略。空でなければ **Step 3 ヘッダにそのまま転記**。整形/判定は `lib/trek_status` (`format_joined_treks` / `has_active_trek` / `is_armed`、単体テスト済み) が所管し、以下を保証する:

- 各 trek: `[trek_id] title` / `status` / `halt` non-null 時「⚠ HALTED: {reason}」/ `goal_state` 非空時「目標: {goal_state}」/ 自分以外の数「他 N 名」。
- active trek が 1 つ以上あれば blanket 自動承認リマインダを 1 行添える (e-1854 AC 1: 「自分が今 blanket 自動承認の対象になっている」ことを毎セッション可視化)。撤回は `beacon trek leave <trek-id>`、デプロイ / リリースのみ user 確認境界。実際の承認は server 側 dm_gate.py が `shared_trek_member` 判定で行う。
- active trek に join 中かつ not-armed なら「⚠ Trek 参加中だが自律実行モードが not-armed」警告を添える (armed 条件 AND: trek 系 channel が auto-execute allowlist にある かつ budget が total > 0 / used < total)。armed なら何も出さない。planning / archived のみの join は判定不要。

local mode (= `.beacon/cloud.json` 不在) でも `~/.beacon/treks/` から拾うので動作する。この Step は **読み取り専用**。

## Step 1p: 締切超過 (overdue) work items の surface (ms-139 e-4952)

期日を過ぎた作業 (開発の milestone `target_date` / task `deadline`、営業の activity
`deadline`) を、起動のたびに冪等に表示する。サーバ発の締切リマインダ (e-4953 = サーバ
tick が claim 者セッションへ DM) が本命だが、その駆動が落ちても最低限の可視化を保証する
二重化 (真値源はサーバ、ここは毎回再計算する冪等表示)。判定規則は L2 締切エンジン
`lib/deadline.py` (今日 > 締切 かつ status が terminal(done/cancelled) でない) に一本化
されている。

Bash ツールで実行 (fail-safe、常に exit 0):

```bash
python3 "$(beacon _install-root)/scripts/session-start-deadlines.py" 2>/dev/null
```

出力が空なら期日超過なし、何も表示しない。**「⏰ 締切超過 (overdue) work items:」で
始まる block が出たら、Step 3 出力ヘッダ部にそのまま転記する** (未解決 Incident の直後、
DM catch-up と並ぶ位置)。各行は `[種別] label / 期日 YYYY-MM-DD (⚠超過/⏰本日) — 文脈`、
古い期日順。データ取得 (milestone/task/activity の 3 源を best-effort JSON で集約) と整形は
script (`scripts/session-start-deadlines.py`、単体テスト済み) が所管。この Step は
**読み取り専用** (超過を消すには done / update --deadline / cancel を user/AI が別途行う)。

## Step 1j: 前セッションの session log 読み込み（ms-43 e-1360）

前セッション末で `/beacon-session-end` Skill が `beacon session end` で集約した session log には、**「次セッション最優先 / top of queue / 次にやること」セクションが summary 内に明文化されている**ことが多い。これは trigger より優先順位が高い (人間/AI が curate した継続意図そのもの)。

Bash ツールで実行:
```bash
beacon session log list --json
```

JSON 配列の **最新エントリ 1 件** (`created_at` 降順 / 配列先頭) の `summary` フィールドを取得する。
0 件 (= 新規プロジェクト or session-end 未実行) なら何もしない。

`summary` テキストから、以下のキーワード/見出しを含むセクションを **AI が文脈で抽出** する:

- 「次セッション最優先」「top of queue」「次にやること」「次の一手」
- 「次の塊」「continue here」「next action」
- 直後に箇条書き (1. / 2. / - / ▸) で並んでいる task / 作業項目

抽出したものを **「session log 由来 next-action」リスト** として記憶する。これは Step 2.9 の「次の一手」決定ロジックの **最優先入力** になる。

該当セクションが無い場合 (例: `"no entries"` だけの session log) は、このリストは空のまま次へ進む。

この Step は **読み取り専用**。

## Step 2: アクティブMSの詳細取得

Step 1a の結果から `status == "in_progress"` のマイルストーンを特定する。
あれば **Bash ツール** で実行:

```bash
beacon task list --json --ms <active-ms-id>
```

stdout の JSON から:
- 未完了タスク: `entries[]` のうち `type == "task"` かつ `status != "done"` のもの（ネストされた `entries[]` 内も再帰的に確認）
- 直近コミット: `entries[]` のうち `type == "commit"` の最新3件

## Step 2.5: コンサルタントモード（次のマイルストーンが必要な場合）

以下のいずれかに該当する場合、通常の Step 3 出力の代わりに **`/beacon-archaeology` Skill にチェイン** する:

- `milestones[]` が空（新規プロジェクト）
- `status == "in_progress"` または `status == "todo"` のマイルストーンが一件もない（done/observing/waitingのみ）

「やるべきことが前に存在しない」状態 = 次のマイルストーンを作るタイミング。`/beacon-archaeology` が git 履歴 + ソースコードを読んで「これまでの歩み」と「次の MS 候補」を提案する (フロー A: Archaeology / フロー B: 白紙提案、F29/F30 の橋渡しメッセージ含む。詳細ロジックは `beacon-archaeology` skill が所管、ms-85 e-3179 で分離)。

session-start で取得済みの `beacon status --json` 結果と CORE doc (Step 1a/1d/1e) はそのまま渡してよい。`/beacon-archaeology` の後は Step 4（トリガーチェック）に戻る。通常の Step 3 出力は不要。

## Step 2.7: フロントエンドを自動オープン（cloud=Web UI / local=Desktop）

Beacon の作業形態は「ターミナル + フロントエンド 並列表示」が前提。  
session-start 時にフロントエンドを立ち上げ直す（既に開かれていれば既存ウィンドウ / タブが focus する）。

```bash
python3 "$(beacon _install-root)/scripts/open-webui.py" 2>/dev/null
```

このスクリプトは **プロジェクトのモード (= どこにデータの真値があるか) に応じて起動先を選ぶ**。cloud プロジェクトの生きた front-end は Web UI (真値はサーバ)、local プロジェクトは desktop アプリ (サーバ URL が無い) — データを持つ側が、それを描画する front-end を決める (背景・macOS の URL handler 回避策は `scripts/open-webui.py` の docstring と ms-46 e-737 を参照):

- **cloud mode** (`.beacon/cloud.json` に `project_id` あり): ブラウザで Web UI を開き (Beacon.app への URL handler 誤ルーティングを回避して default browser / Safari を明示指定)、`WEBUI_URL=<url>` を stdout に出す。
- **local mode** (`.beacon/cloud.json` 無し / project_id 無し): Beacon Tauri desktop アプリ (= ローカルの `.beacon/project.json` を読む) を起動し、`DESKTOP_LAUNCHED=<name>` を stdout に出す。desktop アプリ未インストールなら何も出さない (best-effort, session-start を止めない)。

取得した `WEBUI_URL` / `DESKTOP_LAUNCHED` は Step 3 の出力ヘッダに表示する。

## Step 2.9: 次セッション最初の作業の特定 (ms-43 e-568)

Step 3 の出力末尾に「**次の一手**」を **AI が決定的に選ぶ** ためのロジック。
これまで session-start は「何から始めますか？」とユーザーに丸投げしていたが、文脈情報を全部持っているのは AI なので、AI が **最有力候補を 1 つ提案** し、ユーザーは承認 or 別案で応答するだけにする。

### 優先順位 (上から順に評価し、最初にヒットしたものを採用)

1. **未解決 Incident がある** → `/beacon-incident-report` で close + report 作成
2. **レビュー待ち PR がある** → `/review <pr_number>`
3. **Step 1m で .beacon/fork.json から `target_ms_id` が取れた (ms-67 e-1551)** → その MS の最優先タスクを推奨アクションにする。fork は「この MS のために worktree を立てた」という直前の明示的意図そのものなので、session log より新しい強シグナル。
4. **Step 1j で抽出した session log 由来 next-action がある** → その先頭項目を推奨アクションにする。**trigger より優先**。前セッションが意図的に積んだ次の塊を見落とさないため (2026-06-09 朝に実害発生、e-1360 で構造化)。
5. **`beacon trigger check` で active なトリガーがある** → そのトリガーの推奨アクション
5.5. **`profession` が非 dev (= 営業等) の場合 (ms-108 e-3269 / SPEC 方針5)** → next-action を `/beacon-sales-cockpit` に委譲する。以降の 6〜9 は「アクティブ MS のタスク / SPEC / MS 提案」という **開発 instance 専用**のロジックなので、営業では評価しない。営業の『今日やること・各商談の次の一手』は cockpit が商談を横断して出すのが正 (= session-start 側で商談ごとの next-action を再実装しない)。ただし項目 3 (fork) / 項目 4 (session log 由来 next-action) が取れている場合はそちらを優先する (職種に依らない強シグナルのため)。
6. **アクティブ MS に未消化タスクが 1 つ以上ある**
   - そのうち `priority == "highest"` があればそれを最優先
   - 次に `in_progress` 状態のタスク
   - 次に `assignee` が自分 (current member) のタスク
   - それも無ければ todo 状態の先頭タスク
7. **アクティブ MS の SPEC が無い** → `/beacon-spec <ms-id>` で SPEC 作成
8. **アクティブ MS が無い** → 「次のマイルストーンを決めましょう」(コンサルタントモード Step 2.5 と同じ)
9. **どれも該当しない** → 「観察モード: 直近 retro を見直すか、cleanup 作業に着手するか」

### 2層 claim フィルタ (ms-112 e-3675) — 候補を決めた後に必ず通す

上の優先順位で「次の一手」の候補 target / task を 1 つ選んだら、**その target の 2層 claim 状態 (= いま誰が作業中か + 誰が永続担当か) を読んでから推奨を確定する**。「自分ひとりが全 target を見る」個人前提を、チームで同じプロジェクトを触っても壊さないための claim-aware 化。

1. 候補 target (task の場合はその親 MS / task が指す target) の claim を取得:
   ```bash
   beacon claim view --target <kind>:<target-id> --json
   ```
   `kind` は milestone=`ms` / opportunity=`opp` / account=`acc`。全 target を横断で見たいときは `beacon claim view --json`。

2. 返り JSON の `flags` を読んで推奨を調整する。**非排他が大原則 — 候補を外す (block) ことはしない。警告と別案の提示に留める** (SPEC 設計方針3 / AC5):
   - `live_by_others == true` (他セッションが今 LIVE 作業中) → 推奨は残すが「根拠」に **⚠ 別セッションが作業中 (二重作業の恐れ)** を明記し、「別の選択肢」に未 claim の task / MS を 1 つ添える。協働してよい旨も 1 行。
   - `assigned_to_others == true` かつ `assigned_to_me == false` (他人の永続担当) → 「根拠」に **担当は <assignees>** を明記。自分担当 or 未 claim の候補が他にあれば、そちらの優先度を上げて推奨を差し替える。
   - `live_by_me` / `assigned_to_me` / `unclaimed` のいずれか → 衝突なし。そのまま推奨する。
   - `※ liveness 未確認` (local mode で directory 不通) が出た → LIVE 警告は「作業中の可能性」と可能性表現に弱める (健全性を検証できていないため)。

3. **2層 fallback (AC6)**: LIVE な claimer が居なくても `assigned` を必ず読む。「LIVE で誰も居ない=空いている」と即断せず、他人の永続担当なら上の警告を出す。自分担当 / 未担当ならそのまま。

`beacon claim view` が cloud 不通等で失敗したら、この filter は skip して従来通り推奨する (best-effort、session-start を止めない)。

### 出力フォーマット

```
次の一手: [推奨アクション (短く、1 行)]
  根拠: [なぜこれを推奨するか — 上記優先順位のどれにヒットしたか]
  claim: [2層 claim フィルタの結果 — 衝突 (⚠) があれば明記、衝突なしなら省略] (ms-112 e-3675)
  別の選択肢: [候補 2 つを箇条書き、なければ省略]
```

「別の選択肢」は同じ優先度の他タスク・next MS 提案など。**断定し過ぎず、ユーザーが override しやすい** 余地を残す。

## Step 3: 照合と提示

Step 1〜2 の結果を組み合わせて、以下のフォーマットで **テキスト出力** する。
不要なセクション（未消化タスクがない、未記録コミットがない等）は省略してよい。

```
Beacon: [name]
📊 Web UI: $WEBUI_URL  ← cloud mode で WEBUI_URL が出た場合のみ
🖥 Desktop: $DESKTOP_LAUNCHED (Tauri) を起動  ← local mode で DESKTOP_LAUNCHED が出た場合のみ
🔗 fork from: [target_ms_id] [target_ms_title]  ← Step 1m / .beacon/fork.json があれば
   parent: [parent_session_id 短縮] (branch=[parent_branch], repo=[parent_repo_path basename])
   child:  [child_branch] (この worktree)
   ⚠ fork 時の channel install が失敗しています — `beacon channel install` を打ち直してください  ← channel_install.ok == false の場合のみ
---
⚠️  未解決 Incident: [N]件  ← e-595 / 一件でもあれば最上位に出す。無ければセクションごと省略
  - [e-id] "[title]" (op-X) — open since [created_at]
  - ...
  → 解決済みなら /beacon-incident-report で close + report を作成してください。
  → /beacon-operation-review でも close 誘導が走ります。

⚠ beacon-bus channel が未 install です (この cwd で `beacon channel install` を実行してください)   ← Step 1i / MCP_STATUS が OK/UNKNOWN 以外の場合のみ
  detail: [NO_MCP_JSON / NO_BEACON_BUS_ENTRY / MCP_JSON_MALFORMED]
  影響: 他セッションからの DM (channel/bus.mjs 経由) がこの session に届きません

留守中に届いた DM (user-scoped catch-up):   ← Step 1n-2 の出力があれば転記、なければセクションごと省略
  [event_id 短縮] from [sender 短縮] at [created_at] [(既読)]
    [preview 80 chars]...
  … 他 N 件 (詳細は `beacon bus receive --channel dm`)

ドキュメント (core=設計原則・常時参照 / spec=仕様・技術詳細 / memo=検討メモ):
  [CORE] [title]: [1行サマリー (ms-43 e-566)]
  ...
  [SPEC] [title] (ms-xx): [要約]
  [SPEC] [title] (op-x): [要約]
  ...
  [MEMO] [title] ([N日前]): [1行サマリー]   ← ms-43 e-564 / 直近7日以内のみ
  ※ 詳細は Web UI Documents タブ または `beacon doc show <doc_id>` で。

前回の経緯: [summary]

Active Operation: [op-id] "[title]" [schedule.frequency]  ← openのOperationがある場合
  直近のrun: [date] [✓ok/⚠warning/✗error] / [date] ... （最新3件）

Pending Operation: [op-id] "[title]"  ← todo/in_progressのOperationがある場合
  status: [todo/in_progress]
  activation_hint: [hint があれば]
  準備項目: [done]/[total] 完了
    - ○ [entry-id] [operation_task description]
    - ●  ...
  → 「活性化議論」セクション参照

Active: [ms-id] [title] ([progress]%) [done_tasks]/[total_tasks]タスク完了
  未消化タスク:
  - [entry-id] [description]
  - [entry-id] [description]
  直近コミット:
  - [hash] [description]

他のマイルストーン:
  [status-icon] [ms-id] [title] ([progress]%)
  ...

（営業 instance = `profession != dev` の場合: 上の「Active」「他のマイルストーン」を
  `targets[]` (商談) に置き換える。ms-108 e-3269）
アクティブ商談:
  [opp-id] [label] — フェーズ:[detail.phase] / ball:[detail.who_has_the_ball] / 活動 [work_items_done]/[work_items_total]
  ...
  → 次の一手は `/beacon-sales-cockpit`（Step 2.9 項目 5.5）

未記録のコミット: [git logのハッシュがbeaconエントリに存在しないもの]
uncommitted changes: [git statusの結果があれば]
---
次の一手 (ms-43 e-568): [Step 2.9 で決定した推奨アクション]
  根拠: [なぜこれを推奨するか]
  別の選択肢: [候補 1] / [候補 2]  ← 任意

どうしますか？（このまま進めるなら「OK」「進めて」、別の作業なら指示ください）
```

**未解決 Incident セクション (e-595):**
Step 2 で取得した entries (および Operation 配下の incident エントリ) のうち `type == "incident"` かつ `status == "open"` のものを抽出する。一件でもあれば **必ず最上位に表示**する。これは UX レビュー (UC7-L8) で「閉じられていないインシデントが見落とされる」実害があったため、構造的にユーザーの目に最初に入る位置に固定する。

status-icon の対応: done=●, in_progress=◐, todo=○, waiting=◌, observing=◔

**照合ルール**:
- git log の各コミットハッシュ（先頭7文字）が、Step 1a または Step 2 のエントリの `meta.hash` に存在するか確認
- 存在しないものを「未記録のコミット」として表示

## Step 3.5: セッションメモの表示

Bash ツールで実行:
```bash
beacon note list --json
```

結果が空でなければ、Step 3 の出力に追加:
```
セッションメモ（前回セッションから引き継ぎ）:
  [HH:MM] [context]: [text]
  ...
```

## Step 3.7: pending Operation の活性化議論 (ms-61 / e-1843 で再設計)

Step 1a の結果の **`pending_operations[]` フィールド** (= `beacon status --json` が出す todo / in_progress の Operation 一覧) を取得する。空配列 (= 通常プロジェクトの定常状態) ならこの Step はスキップ。

### 構造的分類は helper が行う (Python 側)

`lib/operation_activation.format_pending_activation_section(pending_operations)` が、各 pending Operation を以下 4 verdict (= 判定) のどれかに分類して bullet 文字列を返す:

- `unparseable`: `activation_hint` 未設定 → 「ヒントを書け」と促す
- `prep-first`: OperationTasks が未完了 → 「先に準備項目を消化せよ」と促す
- `needs-ai-judgement`: 構造的前提 (hint あり + tasks 完了) は満たされている → **AI に文脈評価を委ねる** (Skill 側で判断)
- `activate-now`: 予約 (helper が default で返さない、test / 将来 CLI flag 用)

helper 出力が `""` (= 空文字列) なら、このセクションは丸ごと出力しない (= `dm_pending.format_pending_dm_summary` と同じ「空なら省略」契約)。

### AI の判断材料 (verdict ごとに上乗せ)

helper の verdict を **そのまま出すだけでは AI 価値が無い**。`needs-ai-judgement` verdict の Operation について、AI は以下を **総合的に評価** して、最終的な discussion 文を組み立てる:

1. **activation_hint の自然言語条件**: 例「本番デプロイ後」「ユーザー 10 人超えたら」 — これがプロジェクトの現状で満たされているか
2. **プロジェクト全体の状態**: Step 1a の MS 状態、進捗、ビジョン (= project-vision CORE doc)
3. **直近 commit / session log**: 「最近何をやっていたか」 から hint 条件の充足を推定

### 判断パターン (verdict → 出力)

各 pending Operation について、AI は以下のいずれかを選ぶ:

- **verdict=needs-ai-judgement + 「動かす時が来た」と AI 推論**: hint 条件が満たされている。
  → 議論をユーザーに振る:
  ```
  op-X "Service health monitoring" の準備が整っているように見えます。
    根拠: ms-22 が完了して本番稼働中 / OperationTasks 3/3 done
    activation_hint: "Cloud Run 本番稼働後に有効化"
  動かしますか？それともまだ早い？
  ```

- **verdict=needs-ai-judgement + 「まだ早い」と AI 判定**: 触れない (= 出力にも含めない、ノイズ防止)。 helper bullet も省略するか、 verdict のみ表示で済ませる。

- **verdict=prep-first**: helper bullet をそのまま転記 + `/beacon-operation-setup` を促す:
  ```
  op-Y "Cost watch" は活性化前に OperationTasks 2/3 件の消化が必要。
  /beacon-operation-setup で進めますか？
  ```

- **verdict=unparseable**: helper bullet をそのまま転記 + ヒント記述を促す:
  ```
  op-Z "Daily snapshot" は activation_hint が未設定で活性化条件を判定できません。
  /beacon-operation-setup で hint を書き起こしてください。
  ```

### 出力位置

Step 3 の出力の末尾、Step 4 トリガーチェックの直前に挿入。
論点が無い (= 全 verdict が「まだ早い」AI 判定で省略) なら、 セクションごと出さなくてよい。

> 責任分界 (CLI = `pending_operations[]` shape / helper `lib/operation_activation.py` = verdict 分類 / この Skill = verdict→discussion 対応表 + AI 判断) と drift 防止の forcing function (`tests/test_session_start_operation_activation.py`) の詳細は CORE doc `architecture-tool-skill-separation` と e-1843 (Step 3.7 再設計) を参照。verdict literal を変える時は本 markdown の対応表も更新する。

## Step 4: トリガーチェック

Bash ツールで実行:
```bash
beacon trigger tick && beacon trigger check
```

`tick` は auto-fire + cleanup を明示的に走らせて trigger state を refresh、 直後の `check` は refresh 済みの `.beacon/triggers/*.json` を local read で表示する (ms-98 / e-2764 の分離、 セッション開始時は fresh state を surface したいので明示 tick する)。

JSON 配列が返る。空でなければ、各トリガーの `message` を出力の末尾に追加:
```
Beacon trigger: [message]
```

トリガーへの対応（例: `/beacon-retro` の実行）はユーザーの判断に委ねる。自動実行してはならない。

## 制約

- **読み取り専用**: project.json への書き込みは一切行わない。`beacon log`, `beacon task add/done`, `beacon summary "text"` 等の書き込みコマンドを実行してはならない。
- **データ取得は Bash ツール経由の beacon CLI `--json` 出力のみ**: Read ツールで `.beacon/project.json` を直接読んではならない。
- **出力はコンパクトに**: 完了済みマイルストーンの配下エントリは展開しない。
