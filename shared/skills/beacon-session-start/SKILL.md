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

PATH 上の古い `beacon` が新しい install を黙って隠しているケースを構造的に
検出する。 2026-06-25 PE / Codex dogfood で「`beacon doctor --json` を叩いたら
古い CLI が `OK: all checks passed.` を返して silent success する」 経路が
確認されたため、 doctor 任せにせず resolver で hard fail / soft warn を
明示的に判定する。

repo root を見つけて (= `beacon-find-root` の出力)、 resolver script を直接叩く:

```bash
__ROOT=$(beacon-find-root) && python3 "$__ROOT/scripts/beacon-bin-resolver.py" 2>&1
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

## Step 0a: 引数チェック

ユーザーが `/beacon-session-start ms-XX` のように引数付きで呼んだ場合、`ms-XX` を **スコープMS** として記憶する。
複数指定も可能: `/beacon-session-start ms-16 ms-17`

スコープMSが指定された場合、Step 1a のコマンドが変わる。

## Step 0b: bus heartbeat — 廃止 (ms-54 e-1319)

このステップは **何もしない**。

以前は (e-1150) Bash で `beacon session id` を呼び `lib/session.update_last_active()` 経由で `.beacon/session.json.last_active` と cloud sessions/ subcollection を bump し、自セッションを `beacon bus directory --live` に visible にしていた。

Option C (PR #111 / commit 78048b6) で **bridge の poll loop が真値源** になったため、CLI 側で重ねて書くと「どっちが真実か」あいまいになる。責務分離:

- **mint + heartbeat = bridge**: poll iteration ごとに `last_active` + `last_poll_at` を stamp
- **resolve = CLI**: `beacon session id` は pure getter (mint 1 回だけ、その後は read-only)
- **lifecycle = `beacon session end`**: graceful close

channel が未 install の session は bus directory に出ない — これは「receive 不可だから出ない」が正しい挙動。

> Note: `beacon session id` 自体は今も呼べる (pure getter)。channel/bus.mjs が cold-start で session_id を materialise するために使う。

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
- `milestones[]`: 各MSの `id`, `title`, `status`, `progress`, `total_tasks`, `done_tasks`

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

### SPEC 無し active MS の検出 (warning only, never block)

active な MS (`status == "in_progress"`) で SPEC が **1 つも存在しない** ものを **SPEC 無し MS** として記憶する。

これは CORE doc `doc-classification` および ms-41 SPEC で確立した運用:
- SPEC = 要求書 / 判断軌跡 (詳細仕様書ではない)
- SPEC 無しでも作業は続行可能 (hard block しない)
- 但しサブエージェント dispatch・retrospection・onboarding の質が下がるため、warning で促進

Step 3 の出力でこのリストを表示する (後述):
```
SPEC 無し active MS:
  - [ms-id] [title] → `/beacon-spec [ms-id]` で対話駆動作成できます
```

なお、これと並行して `beacon trigger check` (Step 4) も `spec-needed-<ms-id>` トリガーを返す。両者は同じ事象を別経路で通知している (trigger は MS 追加時 fire、こちらは session-start 時のスキャン)。重複表示は冗長なので、**warning 表示はどちらか一方** (典型的には trigger を優先) で構わない。

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

session-start が走った cwd で beacon-bus channel が install されていない場合、ユーザーが `claude --dangerously-load-development-channels server:beacon-bus` で起動しても `no MCP server configured` で channel 不成立になる。Mac でも cwd 移動で黙って壊れる UX 抜け (memo doc `EZtptg0e8qwBUUhlN2aX` で発覚) を構造的に塞ぐため、session-start 時に `.mcp.json` / `beacon-bus` MCP entry の存在を検知する。

Bash ツールで実行（fail-safe、`.beacon/project.json` 存在前提）:

```bash
python3 - <<'PY' 2>/dev/null || echo "MCP_STATUS=UNKNOWN"
import json, os
status = "OK"
if not os.path.exists(".mcp.json"):
    status = "NO_MCP_JSON"
else:
    try:
        with open(".mcp.json") as f:
            d = json.load(f)
        servers = d.get("mcpServers") or {}
        if "beacon-bus" not in servers:
            status = "NO_BEACON_BUS_ENTRY"
    except Exception:
        status = "MCP_JSON_MALFORMED"
print(f"MCP_STATUS={status}")
PY
```

結果の解釈:

- `OK` → 何もしない（出力に含めない）
- `NO_MCP_JSON` → `.mcp.json` が存在しない。この cwd で `beacon channel install` を実行していない可能性が高い
- `NO_BEACON_BUS_ENTRY` → `.mcp.json` はあるが `beacon-bus` server が登録されていない（他の MCP server だけ install 済み等）
- `MCP_JSON_MALFORMED` → JSON parse エラー、または `mcpServers` キーが想定外の型
- `UNKNOWN` → python3 不在等で判定不能（出力に含めない、Bash の `|| echo "MCP_STATUS=UNKNOWN"` でフォールバック）

`OK` / `UNKNOWN` 以外の場合、Step 3 出力に以下のバンドを追加する:

```
⚠ この cwd は「送信専用」の恐れ (受信 bridge 未設置、ms-93 recipient-stability)
  detail: [NO_MCP_JSON / NO_BEACON_BUS_ENTRY / MCP_JSON_MALFORMED]
  非対称に注意: `beacon bus send` は CLI push なので効きます (= 繋がって見える) が、
    他セッションからの DM は live-wake せず、次回 prompt の catch-up でのみ届きます。
  よくある原因: git worktree に手で cd した等で、起動 cwd と別の .beacon session
    (別 session_id) になり、その session に受信 bridge が無い状態。
  対処: この cwd で `beacon channel install` を実行して受信を有効化してください。
```

**送信は効くのに受信だけ silent に死ぬ** 非対称が本質。「送れたから繋がっている」と誤認させないため、送受信の非対称を明示する (= 2026-07-07 profile-extractor で実害)。

> **補足 (milestone start / worktree 遷移)**: `beacon milestone start <id>` で `.worktrees/<slug>/` に入って作業を続ける場合も同じ穴に落ちる (= 新 worktree に `.mcp.json` が無ければ受信 bridge 無し)。session-start はセッション開始時の 1 回しか走らないので、worktree に cd した直後は改めて `beacon channel status` の `[5] Receive capability` ブロックで受信可否を確認するとよい。

この Step は **読み取り専用**。自動で `beacon channel install` を実行してはならない（session-start 全体の読み取り専用原則に従う）。

## Step 1k: branch / workspace 乖離の警告検知 (ms-65 e-1481)

同 cwd で複数 bclaude が並走している状態 + 自セッションが main project root に居て non-default ブランチに乗っている、という **silent な branch share 事故** の温床条件を検知する。構造修正 (= e-1477 cwd-aware milestone start) は事故が起きる経路を狭めているが、ユーザーが手で `git checkout` した残余ケースは捕まえられない。本ステップが検知側 forcing function。

Bash ツールで実行 (fail-safe、出力がそのままユーザーへの警告になる、終了コードは常に 0):

```bash
python3 scripts/check-branch-focus-divergence.py 2>&1
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

## Step 1n: 保留中 DM action の取得 (ms-70 e-1714)

terminal が close 中に届いた **cross-user DM (= 直接メッセージ) action 付き envelope** は、ms-70 / e-1713 のディスパッチャ・ゲートが `bus_event_approvals` sidecar (= 同 event_id を主キーに別 subcollection で保持する判断記録) に `approval_status="pending"` を立てて auto-act を抑止している。session-start でその pending リストを取り出して human に提示することで、「閉じている間に来た action 系 DM が、次回起動時に必ず目に入る」 経路を作る。

Bash ツールで実行 (fail-safe、cloud 未設定 / endpoint 不在ならスキップ):

```bash
PROJECT_ID=$(python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id',''))" 2>/dev/null)
USER_ID=$(python3 -c "import json,os; p=os.path.expanduser('~/.beacon/auth.json'); print(json.load(open(p)).get('user_id',''))" 2>/dev/null)
if [ -n "$PROJECT_ID" ] && [ -n "$USER_ID" ]; then
  python3 - <<'PY' 2>/dev/null
import json, os, sys, urllib.request, urllib.parse
project_id = os.environ.get("PROJECT_ID") or ""
user_id = os.environ.get("USER_ID") or ""
base = os.environ.get("BEACON_API_BASE") or "https://beacon-api-prod-2dlj7zlbiq-uc.a.run.app"
# token: cloud.json -> id_token, fallback to auth.json
token = ""
try:
    with open(".beacon/cloud.json") as f: token = json.load(f).get("id_token","") or ""
except Exception: pass
if not token:
    try:
        with open(os.path.expanduser("~/.beacon/auth.json")) as f: token = json.load(f).get("id_token","") or ""
    except Exception: pass
q = urllib.parse.urlencode({"receiver_user_id": user_id})
url = f"{base}/api/projects/{project_id}/dm/pending?{q}"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        rows = json.loads(r.read().decode("utf-8"))
except Exception as e:
    print(f"PENDING_DM_FETCH_FAIL: {e}", file=sys.stderr); sys.exit(0)
sys.path.insert(0, os.path.abspath("lib"))
try:
    from dm_pending import format_pending_dm_summary
    print(format_pending_dm_summary(rows))
except Exception as e:
    print(f"PENDING_DM_FORMAT_FAIL: {e}", file=sys.stderr)
PY
fi
```

出力が空でなければ Step 3 の出力ヘッダ部に **そのまま転記** する。空ならセクションごと省略 (= ノイズ削減、`format_pending_dm_summary` が `""` を返す契約)。

提示情報は helper (= `lib/dm_pending.py` の `format_pending_dm_summary`) が 1 行サマリーに圧縮: `event_id from sender_user_id at created_at`。envelope 本文 (= `actions_authorized` 等) は sidecar に持たない設計 (ms-70 / e-1712) なので、詳細展開は出力末尾に書かれた `beacon dm show <event_id>` (= e-1716 で primitive 化予定) と `beacon dm respond approve|deny <event_id>` の案内に従う。

local mode (= `.beacon/cloud.json` 不在) / 未認証 / endpoint タイムアウトはすべて silent skip。session-start を中断しない。

この Step は **読み取り専用**。sidecar の書き換え (= approved / denied 決定) は `/beacon-dm-respond` Skill 経由でのみ行う。

## Step 1n-2: user-scoped DM の catch-up (ms-54 / e-2974)

前回セッション終了以降に **user-scoped で送られた情報 DM (= 直接メッセージ、action 権限なし)** は、受信 bridge (channel/bus.mjs) が e-1209 filter で意図的に drop している (= SPEC doc `wJZrmxZGmT7d5lRQvWnE`「DM primitive の使い分け原則: session-scoped = 即時 wake / user-scoped = 次回 catch-up」に基づく設計)。したがって過去セッションの inbox には出ておらず、AI から見て「留守中に届いた DM」が session-start 時に完全に見落とされる。

本 Step は **server の bus events を直接 query** し、user-scoped で自分宛 (payload.recipient_user_id == 自 user_id) の DM を「catch-up 対象」として拾い、Step 3 の出力に含める。session-scoped で既に届いた DM (bridge 経由で AI が過去に read 済のもの) は対象外 (受信 bridge の inbox 経由で既に見ているはず)。

Bash ツールで実行 (fail-safe、cloud 未設定 / endpoint 不在ならスキップ):

```bash
PROJECT_ID=$(python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id',''))" 2>/dev/null)
USER_ID=$(python3 -c "import json,os; p=os.path.expanduser('~/.beacon/auth.json'); print(json.load(open(p)).get('user_id',''))" 2>/dev/null)
if [ -n "$PROJECT_ID" ] && [ -n "$USER_ID" ]; then
  python3 - <<'PY' 2>/dev/null
import json, os, sys, urllib.request, urllib.parse, subprocess, datetime
project_id = os.environ.get("PROJECT_ID") or ""
user_id = os.environ.get("USER_ID") or ""
base = os.environ.get("BEACON_API_BASE") or "https://beacon-api-prod-2dlj7zlbiq-uc.a.run.app"
token = ""
try:
    with open(".beacon/cloud.json") as f: token = json.load(f).get("id_token","") or ""
except Exception: pass
if not token:
    try:
        with open(os.path.expanduser("~/.beacon/auth.json")) as f: token = json.load(f).get("id_token","") or ""
    except Exception: pass

# "since" は前回 session log の created_at、無ければ 7 日前
since_iso = ""
try:
    r = subprocess.run(["beacon", "session", "log", "list", "--json"],
                       capture_output=True, text=True, timeout=5)
    logs = json.loads(r.stdout or "[]")
    if logs:
        since_iso = str(logs[0].get("created_at","") or "")
except Exception: pass
if not since_iso:
    since_iso = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=7)).isoformat().replace("+00:00","Z")

# server の bus events を取得 (visibility gate 通過分のみ返る)
q = urllib.parse.urlencode({"channel": "dm", "since": since_iso, "limit": 100})
url = f"{base}/api/projects/{project_id}/bus?{q}"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        events = json.loads(r.read().decode("utf-8"))
except Exception as e:
    print(f"CATCHUP_FETCH_FAIL: {e}", file=sys.stderr); sys.exit(0)

# user-scoped で自分宛 (recipient_user_id 一致 かつ recipient_session_id 空) だけ抽出
rows = []
for ev in events or []:
    p = ev.get("payload") or {}
    if not isinstance(p, dict): continue
    r_uid = str(p.get("recipient_user_id") or "")
    r_sid = str(p.get("recipient_session_id") or "")
    if r_uid == user_id and not r_sid:
        rows.append({
            "event_id": ev.get("event_id",""),
            "sender_session_id": ev.get("sender_session_id",""),
            "created_at": ev.get("created_at",""),
            "text_preview": (p.get("text") or "")[:80],
            "opened_at": ev.get("opened_at",""),
        })

if not rows:
    sys.exit(0)  # ノイズ削減、該当なし

print("留守中に届いた DM (user-scoped catch-up):")
for r in rows[:5]:
    opened = " (既読)" if r["opened_at"] else ""
    print(f'  [{r["event_id"][:12]}] from {r["sender_session_id"][:12]} at {r["created_at"][:19]}{opened}')
    print(f'    {r["text_preview"]}...')
if len(rows) > 5:
    print(f'  … 他 {len(rows)-5} 件 (`beacon bus receive --channel dm` で全文)')
PY
fi
```

出力が空でなければ Step 3 の出力ヘッダ部 (「保留中 DM action」セクションの直後、Trek 一覧の直前あたり) に **そのまま転記** する。空ならセクションごと省略。

payload.text は preview 80 文字まで、詳細確認は `beacon bus receive --channel dm` を案内。返信したい場合は `/beacon-dm-send` (reply mode) 経由。

local mode (= `.beacon/cloud.json` 不在) / 未認証 / endpoint タイムアウトはすべて silent skip。session-start を中断しない。

この Step は **読み取り専用**。既読フラグの更新 (opened stamp) は AI が payload を read した際にサーバ側で自動記録されるため、Skill 側で明示 ack しない (= 次回同 Step が「既読」表示するのに任せる)。

## Step 1o: 現在 join 中の Trek 一覧 (ms-75 / e-1813 + e-1854)

Trek (= 缶詰の徹夜作業部屋、 user が join した瞬間に scope 内 action が事前承認スコープになる作業空間) に join 済の場合、 そのリストと goal_state / halt 状態を session-start で必ず可視化する。 ms-70 (= cross-user DM 承認ゲート) は Trek 参加中だけ blanket 自動承認 (= 都度確認なしで配信) になるため、 「自分が今どの Trek の blanket 例外を受けているか」 を session 開始時に user 自身が把握できる必要がある。

Bash ツールで実行:

```bash
beacon trek list --joined --json 2>/dev/null
```

出力が空配列 `[]` ならセクションごと省略。 1 件以上あれば、 各 trek について以下を抽出して **Step 3 ヘッダに転記**:

- `trek_id` と `title` (= 1 行)
- `status` (= active / planning / archived)
- `halt` が non-null なら 「⚠ HALTED: {halt.reason}」 を強調表示
- `goal_state` が空でなければ 「目標: {goal_state}」 を 1 行で追加
- `members` の自分以外の数を 「他 N 名」 と要約

加えて、 Trek 参加中であれば user に以下を 1 行で必ず伝える:

> Trek 参加中: 同 Trek scope 内の DM (= 計画 / 議論 / 実装計画) は自動承認 (= blanket 例外、 ms-70/e-1854) で配信されています。 撤回したい場合は `beacon trek leave <trek-id>` を実行してください。 デプロイ / リリースのみ user 確認境界です。

これは「自分が今 blanket 自動承認の対象になっている」 ことを毎セッション可視化する e-1854 AC 1 の構造的実装。 user が知らないうちに自律応答が走るリスクを構造的に低減する (= 表示は読み取り専用、 実際の承認自体は server 側 dm_gate.py が `shared_trek_member` 判定で行う)。

planning や archived な trek は blanket 例外の対象外なので、 表示はするが警告メッセージは active な trek だけに添える。

local mode (= `.beacon/cloud.json` 不在) でも `~/.beacon/treks/` から拾うので動作する。

### Step 1o-2: 自律実行モード (= armed) のセルフチェック (ms-75 / e-2047)

active な Trek に join 中なら、 このセッションが **armed (= 自律実行モード)** であることを確認する。 `beacon trek join` は AC 1 で auto-arm が default になっているが、 以下のケースで not-armed が起こりうる:

- `--no-arm` で opt-out した
- 古いバージョンで join 済 (= auto-arm 前の trek)
- 別 worktree で join したため `.beacon/bus-budget.json` がこの cwd に無い

判定材料を Bash で並列取得:

```bash
beacon bus auto-execute list --json 2>/dev/null
beacon bus budget show --json 2>/dev/null
```

armed 条件 (AND): `bus_auto_execute_channels` に **少なくとも 1 つの trek 系 channel (= trek-progress-check / trek-trigger / trek-task-review)** が含まれている、 かつ budget が `armed` 状態 (= total > 0 かつ used < total)。

armed でない場合、 Step 3 のヘッダに以下を 1 行で添える:

```
⚠ Trek 参加中だが自律実行モードが not-armed です。 `/beacon-bus-armed` で起動するか、 `beacon trek join <trek-id>` を再実行して auto-arm し直してください (= 進行 DM が wake せず silent-ack 病理を再生します)。
```

armed なら何も表示しない (= ノイズ削減)。

archived / planning trek にしか join していない場合は判定不要 (= scope 内 action が事前承認の対象外)。 この Step は **読み取り専用**。

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

以下のいずれかに該当する場合、通常の Step 3 出力の代わりに以下を行う:

- `milestones[]` が空（新規プロジェクト）
- `status == "in_progress"` または `status == "todo"` のマイルストーンが一件もない（done/observing/waitingのみ）

「やるべきことが前に存在しない」状態 = 次のマイルストーンを作るタイミング。

### F30: 起動原因の橋渡しメッセージ

ユーザーが「archaeology して」「過去掘って」「リポジトリ分析して」等の **概念名** で起動した場合、ユーザー視点では「archaeology Skill」を呼んだつもりが `/beacon-session-start` が動くため、Skill 名のミスマッチで一瞬「あれ違う Skill？」となる。

以下のキーワードが直近の user 発話に含まれていたら、コンサルタントモードの出力の **最初の 1 行** に橋渡しメッセージを添える:

- `archaeology` / `Archaeology`
- `掘って` / `経緯` / `これまでの流れ`
- `リポジトリ分析` / `コード読んで`

```
(Archaeology を含む /beacon-session-start を起動します — git log とコードを読んで提案します)

このリポジトリを分析しました。
...
```

含まれていなければ橋渡し行は不要。

### 分岐: 常に B (code reading)、git 履歴あれば A も追加 (F29)

排他分岐ではなく **加算構成**:

```bash
git log --oneline 2>/dev/null | wc -l
```

- **常に B (code reading) を実行**: README/source/設定ファイルを読んでプロジェクトの現状を理解する
- **追加で `git_commits >= 10` の時のみ A (Archaeology) を実行**: git log clustering で過去フェーズを推測する
- B の結果と A の結果を **統合して提案を出す**

つまり「コード文脈は常に拾う、git 履歴がある時は追加で過去経緯も拾う」。閾値で **排他にしない** (commit 少の既存リポでも code reading は走る)。

A 単独実行時のエッジケースは自然に degrade:
- `commits == 1` (初期コミットのみ) → A は phase 0〜1 個しか作れない、B の code reading が主軸になる
- `commits >= 10` → A の phase clustering が主軸、B が補完
- `commits == 0` (git 未初期化) → A スキップ、B のみ

---

### フロー A: Project Archaeology（リポジトリ遡行推測）

#### Step A1: 情報収集（並列 Bash 実行）

以下を **同時に** 実行する:

```bash
# A1-1: コミット履歴（最大200件）
git log --oneline -200

# A1-2: 直近コミットの変更ファイル（傾向把握）
git log --stat -10

# A1-3: タグ一覧（リリース境界の手がかり）
git tag --sort=-creatordate | head -10

# A1-4: README（プロジェクト概要）
cat README.md 2>/dev/null || cat README.rst 2>/dev/null || cat README.txt 2>/dev/null || echo ""

# A1-5: ファイル一覧（技術スタック判定）
ls -la

# A1-6: 言語/フレームワーク判定ファイル（存在するものだけ読む）
cat package.json 2>/dev/null; cat Cargo.toml 2>/dev/null; cat pyproject.toml 2>/dev/null; cat go.mod 2>/dev/null; cat build.gradle 2>/dev/null; cat pom.xml 2>/dev/null
```

#### Step A2: AI 解釈

収集した情報から以下を推測する:

1. **Objective の言語化**
   - ユーザー目線で「このプロジェクトが完成したら何ができるようになるか」を1文で表現
   - 形式: 「〜できるようになる」「〜が実現する」
   - README・package.json の description・コミットメッセージのテーマを総合的に判断

2. **過去フェーズのクラスタリング（3〜7個）**
   - git log のコミットメッセージをテーマでグループ化
   - 手がかり:
     - コミットメッセージの語彙変化（「init」「setup」→「feat」→「fix」→「refactor」等）
     - feat/fix の比率が変わるタイミング
     - タグが打たれた境界
     - ファイル変更の傾向（初期は多数ファイル、後期は特定領域に集中）
   - 各フェーズに「何ができるようになったか」を表すタイトルをつける
   - git log の日付から各フェーズのおよその時期（YYYY年M月頃）を付与

3. **現在地の特定**
   - 直近 30 コミットの傾向から、現在何に取り組んでいるかを推測

4. **次 MS の提案（1〜3個）**
   - 現在地から自然につながる次の一手
   - 「何ができるようになるか」形式でタイトル化

#### Step A3: ユーザーへの提示

```
このリポジトリを分析しました。

プロジェクト概要（推測）: [objective — ユーザー目線の1文]

開発の歩み（推測）:
  ● [フェーズ1タイトル]  (YYYY年M月頃)
  ● [フェーズ2タイトル]  (YYYY年M月頃)
  ● [フェーズ3タイトル]  (YYYY年M月頃)
  ◐ [現在進行中フェーズ]  (YYYY年M月〜)

次のマイルストーン候補:
  1. "[提案1]"
     理由: [なぜこれが次の一手として適切か]

  2. "[提案2]"（別の方向性があれば）
     理由: [...]

調整があれば教えてください。このまま登録しますか？
```

#### Step A4: ユーザー承認後の登録（書き込みフェーズ）

ユーザーが承認（「はい」「登録して」「OK」等）した場合のみ実行する:

```bash
# 1. Objective をサマリーに設定
beacon summary "<推測したobjective>"

# 2. 過去完了フェーズを登録（古い順に）
beacon milestone add "<フェーズ1タイトル>"
# → 返り値の ms-id を使って
beacon milestone done <ms-id>

beacon milestone add "<フェーズ2タイトル>"
beacon milestone done <ms-id>
# ... 完了分をすべて登録

# 3. 現在進行中フェーズを登録・開始
beacon milestone add "<現在進行中フェーズ>"
beacon milestone start <ms-id>

# 4. 次MS候補を登録（todo 状態）
beacon milestone add "<次MS提案1>"
# （提案2があれば続けて追加）
```

**注意**: Step A4 はユーザーの明示的な承認なしに実行してはならない。提示後は必ず確認を取る。

---

### フロー B: 白紙提案（コミット数 < 10 または git 未初期化）

#### やること

1. CORE ドキュメントがあれば読む（Step 1d/1e の結果を利用）
2. **ソースコードを読んで実装状況を把握する**（以下を並列実行）:

```bash
cat README.md 2>/dev/null
```

```bash
find . -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.py" -o -name "*.vue" -o -name "*.go" -o -name "*.rb" \) \
  -not -path "*/node_modules/*" -not -path "*/.git/*" \
  -not -path "*/dist/*" -not -path "*/__pycache__/*" | head -40
```

READMEがあればそれを読む。ファイル一覧からルーター・型定義・ページ・モデル等の主要ファイルを特定し、3〜5件を並列Readする。

**この情報はドキュメントとして保存しない。提案の精度向上のみに使う。**

3. プロジェクト名・objective・ソースコードの実態を踏まえて、**最初のマイルストーン候補を1〜3個提案する**

#### 提案の視点（重要）

- **「何を作るか」ではなく「何ができるようになるか」** でタイトルをつける
- objective を起点に考える。最終ゴールに向かう最初の一歩として、ユーザーが体験できる状態変化を表現する
- 「基盤構築」「パイプライン設計」のような技術的な工程名は避ける
- 例：objective が「家計の無駄遣いを減らして貯金を増やしたい」なら
  - ✗ 「データ取り込みパイプラインの設計」
  - ✓ 「先月の支出を入力して、無駄な出費のパターンを一覧で見られるようにする」

#### 出力フォーマット

```
Beacon: [name] — [MSゼロなら「まだマイルストーンがありません」/ done MSがあれば「次のマイルストーンを決めましょう」]
---
[objective・summary・完了済みMSの流れを一言で解釈]

[完了済みMSがある場合は「ここまで達成しました：〇〇、△△」を一行添える]

次のマイルストーンをこう考えます：

  1. "[提案タイトル]"
     理由: [なぜこれが最初の一手として適切か]

  2. "[提案タイトル]"（もし別の方向性があれば）
     理由: [...]

どれかを選ぶか、別のゴールを教えてもらえれば `beacon milestone add` で登録します。
```

---

コンサルタントモード（フロー A または B）の後は Step 4（トリガーチェック）に進む。通常の Step 3 出力は不要。

## Step 2.7: Web UI を自動オープン（cloud mode の場合）

Beacon の作業形態は「ターミナル + Web UI 並列表示」が前提。  
session-start 時に Web UI を立ち上げ直す（既に開かれていればブラウザが既存タブを focus する）。

```bash
# Bash 呼び出し
# ms-46 e-737: open URL は macOS が Beacon.app を URL handler として解釈し
# Tauri を起動してしまうケースがある (cloud mode で Tauri が起動すると
# ローカルキャッシュ表示で混乱)。ブラウザを明示的に指定して回避。
# macOS: Python webbrowser は LSGetDefaultRoleHandler を使うので Beacon.app
# を回避できないことがある → -b で default browser app ID を直接渡す。
# 検出失敗時は Safari にフォールバック (System 標準で必ず存在)。
if [ -f .beacon/cloud.json ]; then
  PROJECT_ID=$(python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id',''))")
  if [ -n "$PROJECT_ID" ]; then
    WEBUI_URL="https://beacon-ai.dev/?project=$PROJECT_ID"
    # macOS: 既定の https handler を取得 (Beacon.app になっていたら Safari にフォールバック)
    DEFAULT_BROWSER=$(python3 -c "
import subprocess, plistlib, os, sys
p = os.path.expanduser('~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist')
try:
    with open(p, 'rb') as f: d = plistlib.load(f)
    for h in d.get('LSHandlers', []):
        if h.get('LSHandlerURLScheme') == 'https':
            r = h.get('LSHandlerRoleAll', '')
            if r and 'beacon' not in r.lower():
                print(r); sys.exit(0)
except Exception: pass
print('com.apple.Safari')
" 2>/dev/null || echo 'com.apple.Safari')

    (open -b "$DEFAULT_BROWSER" "$WEBUI_URL" 2>/dev/null \
      || open -a Safari "$WEBUI_URL" 2>/dev/null \
      || xdg-open "$WEBUI_URL" 2>/dev/null \
      || cmd.exe /c start "$WEBUI_URL" 2>/dev/null \
      || powershell.exe -Command "Start-Process '$WEBUI_URL'" 2>/dev/null) &
    echo "WEBUI_URL=$WEBUI_URL"
  fi
fi
```

取得した URL は Step 3 の出力ヘッダに表示する。  
local mode（cloud.json 無し）の場合はこのステップをスキップ。

## Step 2.9: 次セッション最初の作業の特定 (ms-43 e-568)

Step 3 の出力末尾に「**次の一手**」を **AI が決定的に選ぶ** ためのロジック。
これまで session-start は「何から始めますか？」とユーザーに丸投げしていたが、文脈情報を全部持っているのは AI なので、AI が **最有力候補を 1 つ提案** し、ユーザーは承認 or 別案で応答するだけにする。

### 優先順位 (上から順に評価し、最初にヒットしたものを採用)

1. **未解決 Incident がある** → `/beacon-incident-report` で close + report 作成
2. **レビュー待ち PR がある** → `/review <pr_number>`
3. **Step 1m で .beacon/fork.json から `target_ms_id` が取れた (ms-67 e-1551)** → その MS の最優先タスクを推奨アクションにする。fork は「この MS のために worktree を立てた」という直前の明示的意図そのものなので、session log より新しい強シグナル。
4. **Step 1j で抽出した session log 由来 next-action がある** → その先頭項目を推奨アクションにする。**trigger より優先**。前セッションが意図的に積んだ次の塊を見落とさないため (2026-06-09 朝に実害発生、e-1360 で構造化)。
5. **`beacon trigger check` で active なトリガーがある** → そのトリガーの推奨アクション
6. **アクティブ MS に未消化タスクが 1 つ以上ある**
   - そのうち `priority == "highest"` があればそれを最優先
   - 次に `in_progress` 状態のタスク
   - 次に `assignee` が自分 (current member) のタスク
   - それも無ければ todo 状態の先頭タスク
7. **アクティブ MS の SPEC が無い** → `/beacon-spec <ms-id>` で SPEC 作成
8. **アクティブ MS が無い** → 「次のマイルストーンを決めましょう」(コンサルタントモード Step 2.5 と同じ)
9. **どれも該当しない** → 「観察モード: 直近 retro を見直すか、cleanup 作業に着手するか」

### 出力フォーマット

```
次の一手: [推奨アクション (短く、1 行)]
  根拠: [なぜこれを推奨するか — 上記優先順位のどれにヒットしたか]
  別の選択肢: [候補 2 つを箇条書き、なければ省略]
```

「別の選択肢」は同じ優先度の他タスク・next MS 提案など。**断定し過ぎず、ユーザーが override しやすい** 余地を残す。

## Step 3: 照合と提示

Step 1〜2 の結果を組み合わせて、以下のフォーマットで **テキスト出力** する。
不要なセクション（未消化タスクがない、未記録コミットがない等）は省略してよい。

```
Beacon: [name]
📊 Web UI: $WEBUI_URL  ← cloud mode の場合のみ
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

SPEC 無し active MS (warning):     ← Step 1f で検出した SPEC 無しactive MS がある場合のみ
  ⚠ [ms-id] [title] — `/beacon-spec [ms-id]` で対話駆動作成できます
  ※ SPEC = 要求書/判断軌跡。dispatch / retrospection の質が下がるため、作成を推奨

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

> **e-1843 修正経緯**: 旧版はこの Step 1a から「status==todo / in_progress を抽出する」と書いていたが、`beacon status --json` は実装上 `status="open"` のみを出していたため、Step 3.7 は **構造的に空を見せ続けて dead code 化** していた。e-1843 で CLI 側に `pending_operations[]` フィールドを追加 + helper (= `lib/operation_activation.py`) で verdict 分類を構造化、Skill 側はその verdict に従って discussion を組み立てる責務分担に再編した。

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

### 責任分界 (= e-1843 AC #5)

- **CLI (`lib/commands.py:cmd_status`)**: `pending_operations[]` フィールドの shape (= id / title / status / activation_hint / operation_tasks_total / operation_tasks_done / entries) を保証する。 shape を変える時は本 Skill markdown と helper の両方に追従が必要。
- **helper (`lib/operation_activation.py`)**: verdict 分類ロジックの structural 部分を所管。 verdict を増やす / 名前を変える時は tests (= `tests/test_session_start_operation_activation.py`) が fail するので Skill markdown 追従も自動的に強制される (forcing function)。
- **Skill markdown (この section)**: verdict → discussion 文の対応表 + AI 判断 (= `needs-ai-judgement` verdict の qualitative call) を所管。 helper の verdict literal が変わったら本 markdown の対応表も更新する。

shape / verdict literal の drift は tests で捕まる構造 (= `test_verdict_vocabulary_is_closed`、 `test_cli_status_json_has_pending_operations_field_when_zero` 等) のため、 Skill 側の指示文と Python 側の実装が同時に壊れない限り、 silent drift は起きない。

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
