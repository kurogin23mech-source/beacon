---
name: beacon-dispatch
description: 依存グラフから実行可能なマイルストーンまたはタスク群を特定し、サブエージェントを並列起動する。マルチエージェント協奏のオーケストレーター。
version: 0.4.0
triggers:
  - /beacon-dispatch
  - サブエージェントを起動
  - 並列で実行
  - dispatch
  - 次に実行可能なMSは
  - タスクを並列実行
  - task-level dispatch
---

# Beacon Dispatch

> 依存グラフを解析し、実行可能なマイルストーンまたはタスク群をサブエージェントに並列委譲する。

## Step 0.0: モード判定 (MS-level / Task-level、ms-65 e-1480)

ユーザーの起動文字列を読み、以下のどちらのモードで動くかを決める。

| 起動形式 | モード | 後続フロー |
|---|---|---|
| `/beacon-dispatch` (引数なし) | **MS-level** (default) | Step 0.5 permission preflight → Step 1-7 の MS-level dispatch |
| `/beacon-dispatch --tasks e-XXX e-YYY ...` | **Task-level** (e-1480) | Step 0.5 permission preflight → 本文書末尾の「Task Mode」セクション |

Task-level は **同じ MS 配下の複数 task** を nested worktree で並列起動するためのモード。MS の中で互いに干渉しない task 群があるときに使う (例: ファイル領域が違う 3 task を 3 サブエージェントに振る)。**異なる MS の task を混ぜたい場合は MS-level dispatch を使うこと**。

判定結果を `$DISPATCH_MODE` として保持し、以降の分岐に使う。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 0.4: 並列上限の原則 (= 推奨 2 / 最大 3、 ms-61 e-1825)

> **背景 (= memory feedback 「Parallel Agent dispatch を 8+ で投げない」 を構造昇格)**: 2026-06 の Mac dogfood で 8 並列でサブエージェントを起動した結果、 Claude Code harness が permission prompt (= 各サブエージェントが Edit/Write/Bash を叩く度に親に出る承認ダイアログ) を捌ききれず、 親セッション自体が応答不能に陥る事象を観測。 本 Skill は **dispatch の主要動線** であり、 ここで上限を破ると並列実装基盤そのものが停止する。

### 並列上限ポリシー (MS-level / Task-level 共通)

| 同時起動数 | 扱い |
|---|---|
| 1〜2 件 | **推奨** — 親 prompt も捌け、 確認のオーバーヘッドも低い |
| 3 件 | **最大** — ここまでは並列許可。 これを超える時は分割 |
| 4 件以上 | **拒否 (= 構造的に refuse)** — Step 4.4 / Task Mode T4 でユーザー確認の上、 必ず分割 (= queue 化、 順次起動) する |

> この上限は本 Skill 内 (= MS-level の Step 4.4 と Task-level の T4) と Step 5 / Task Mode T6 起動部の両方で **二重に強制**する。 Skill の冒頭で原則だけ宣言しても起動経路で守られないと意味がないため。

### Dispatch / Fork 振り分け原則 (= ms-61 e-1825 + memory feedback「Subagent dispatch の対話帯域ゼロ」)

並列実行のニーズには 2 種類あり、 それぞれ別 Skill を使う:

| ニーズ | 使う Skill | 理由 |
|---|---|---|
| **独立タスクを並列消化** (= 各 task が互いに独立、 AI 単独で進められる) | `/beacon-dispatch` | サブエージェントは対話帯域ゼロ、 自走前提。 本 Skill の本来用途 |
| **人間 + AI 対話的に難しい MS を並列実装** (= 設計判断 / コンテキスト揺らぎが多く、 都度 user 議論が要る) | `/beacon-session-fork` | 別 bclaude (= Claude Code instance) を別 worktree で立ち上げ、 user と直接対話する経路を確保する Skill |

dispatch は「親が黙って待ち、 子が自走完遂して返ってくる」 モデル。 「途中で user に質問したい」「設計判断を相談したい」 タイプの MS は dispatch には向かない (= サブエージェントから親への問い返し帯域が無い)。 そういう MS は **fork で別 bclaude セッションを物理的に立ち上げる方が原理的に正しい**。

dispatch 起動前に、 渡された MS 群が「自走完遂可能」 か「対話必須」 かを **AI が一度判定** し、 後者が混ざっている場合は Step 4.4 のユーザー確認時に `/beacon-session-fork` への切り替えを推奨する。

## Step 0.5: サブエージェント permission preflight (e-1221 fix)

> **背景**: サブエージェント harness はプロジェクトの `.claude/settings.local.json` の `permissions.allow` をそのまま継承する。親セッションで会話的に grant された permission は **継承されない**。`.worktrees/` 配下への Edit/Write が allowlist に無いと、サブエージェントは全 Edit/Write で permission-denied を食らい、**設計だけ綺麗に書いてコミット 0 で silently exit する**。2026-06-09 の TrailNode dogfood で 3/3 のサブエージェントがこのモードで失敗し、約 8 分の dispatch が無駄になった事象がある。
>
> **glob の罠**: `Edit(<repo-root>/**)` が allowlist にあっても、`<repo-root>/.worktrees/...` には **マッチしない**。`.worktrees/` は隠しディレクトリで、親 glob のワイルドカードはこれをスキップする。明示的に `Edit(<repo-root>/.worktrees/**)` と `Write(<repo-root>/.worktrees/**)` を列挙する必要がある。

### 0.5a. プロジェクトルート絶対パス取得

Bash ツールで:
```bash
pwd
```
の結果を `<abs-project-root>` として記録する。

### 0.5b. allowlist の検査

Bash ツールで以下を実行し、`.claude/settings.local.json` の `permissions.allow` に **両方のエントリ** が含まれているかを判定する:

```bash
python3 - <<'PY'
import json, os, sys
root = os.getcwd()
path = os.path.join(root, ".claude", "settings.local.json")
need_edit = f"Edit({root}/.worktrees/**)"
need_write = f"Write({root}/.worktrees/**)"
if not os.path.exists(path):
    print(f"MISSING_FILE\nNEED:\n  {need_edit}\n  {need_write}")
    sys.exit(0)
try:
    with open(path) as f:
        data = json.load(f)
except Exception as e:
    print(f"MALFORMED_JSON: {e}\nNEED:\n  {need_edit}\n  {need_write}")
    sys.exit(0)
allow = data.get("permissions", {}).get("allow", [])
if not isinstance(allow, list):
    print(f"NO_ALLOW_KEY\nNEED:\n  {need_edit}\n  {need_write}")
    sys.exit(0)
has_edit = need_edit in allow
has_write = need_write in allow
if has_edit and has_write:
    print("OK")
else:
    print("MISSING_ENTRIES")
    if not has_edit:
        print(f"  - {need_edit}")
    if not has_write:
        print(f"  - {need_write}")
PY
```

### 0.5c. 判定と分岐

- **`OK`**: 両方揃っている。そのまま Step 1 へ進む。
- **それ以外** (`MISSING_FILE` / `MALFORMED_JSON` / `NO_ALLOW_KEY` / `MISSING_ENTRIES`): **dispatch をその場で停止** し、ユーザーに以下を提示する:

```
⚠ Dispatch 中断: サブエージェント permission preflight が失敗しました

理由:
  サブエージェントの harness はこのプロジェクトの .claude/settings.local.json の
  permissions.allow をそのまま引き継ぎます。親セッションで会話的に grant された
  permission は継承されません。`.worktrees/` 配下への Edit/Write が allowlist に
  無いと、サブエージェントは全 Edit/Write で permission-denied になり、設計だけ
  書いてコミット 0 で silently exit します (2026-06-09 e-1221 で実測)。

glob の罠:
  Edit(<root>/**) が allowlist にあっても `.worktrees/` は隠しディレクトリなので
  ワイルドカードがスキップします。明示的に列挙が必要です。

必要なエントリ:
  - Edit(<abs-project-root>/.worktrees/**)
  - Write(<abs-project-root>/.worktrees/**)

現状: [Step 0.5b の python3 出力をそのまま貼る]

このエントリを .claude/settings.local.json に追加してよいですか？ [yes / no]
  yes → こちらで idempotent に追加して preflight を再実行します
  no  → dispatch をキャンセルします (手動追加後にもう一度 /beacon-dispatch を呼んでください)
```

### 0.5d. ユーザー承認後の自動追加 (yes の場合のみ)

ユーザーが明示的に `yes` と答えた場合のみ、Bash ツールで以下を実行する。冪等 — 既にあるエントリは追加しない:

```bash
python3 - <<'PY'
import json, os
root = os.getcwd()
path = os.path.join(root, ".claude", "settings.local.json")
need = [
    f"Edit({root}/.worktrees/**)",
    f"Write({root}/.worktrees/**)",
]
if os.path.exists(path):
    with open(path) as f:
        try:
            data = json.load(f)
        except Exception:
            data = {}
else:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {}
data.setdefault("permissions", {}).setdefault("allow", [])
allow = data["permissions"]["allow"]
added = []
for entry in need:
    if entry not in allow:
        allow.append(entry)
        added.append(entry)
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print("ADDED:" if added else "NOOP (already present)")
for a in added:
    print(f"  + {a}")
PY
```

追加後、**0.5b の検査を必ず再実行** して `OK` が返ることを確認する。`OK` が返ればそのまま Step 1 へ進む。再度失敗した場合は手動修正を依頼して dispatch をキャンセルする。

## Step 1: 依存グラフの取得

Bash ツールで実行:
```bash
beacon milestone graph --json
```

stdout に JSON が返る:
```json
{
  "nodes": [
    {"id": "ms-X", "title": "...", "status": "...", "progress": N, "workspace": "...", "depends_on": [...]}
  ],
  "edges": [...],
  "waves": [
    {"wave": 1, "milestones": ["ms-1", "ms-2"]},
    {"wave": 2, "milestones": ["ms-3"]}
  ]
}
```

## Step 2: MS分類

Step 1 の `nodes` から、対象MSを以下の2グループに分類する。

### 実行可能（Runnable）
以下の条件を **すべて** 満たすMS:
1. `status` が `todo` または `in_progress`
2. `depends_on` に含まれるMS **すべて** が `done` または `observing`

### 待機中（Blocked）
以下の条件を満たすMS:
1. `status` が `todo` または `in_progress`
2. `depends_on` に、`done`/`observing` 以外のステータスのMSが1つ以上含まれる

各ブロックされたMSについて、「どのMSがブロック要因か」（blocking deps）を記録する。

実行可能MSが1つもなければ:
```
Dispatch: 実行可能なマイルストーンがありません。
依存関係の先行MSが未完了か、すべてのMSが完了済みです。
```
と表示し、待機中MSがあれば一覧を表示して終了。

## Step 2.2: 2層 claim-aware 割り当てフィルタ (ms-112 e-3676)

実行可能 MS (Runnable) が確定したら、**サブエージェントに振る前に各 MS の 2層 claim 状態 (= いま別セッションが LIVE 作業中か + 誰が永続担当か) を読む**。dispatch は「自分ひとりが全 target を割り当てる」個人前提で組まれていたため、チームで別セッションが既に触っている MS に無自覚にサブエージェントを重ねる二重作業が起きうる。それを claim ベースで避ける (非排他 = 機械的な除外ではなく、順位下げと警告)。

### 取得

```bash
beacon claim view --json
```

返り JSON は target-id をキーに各 MS の `flags` (`live_by_others` / `assigned_to_others` / `assigned_to_me` / `unclaimed` …) を持つ。実行可能 MS のぶんだけ参照する。

### 割り当てポリシー (SPEC 設計方針3 / AC3、非排他)

各 Runnable MS を以下で仕分ける。**候補集合から機械的に除外 (block) はしない** — 順位付けと警告に留め、最終判断は user に委ねる:

- `live_by_others == true` (他セッションが今 LIVE 作業中) → **並列起動の第一候補から外す** (二重作業の恐れ)。どうしても回したい時は Step 4 で「⚠ ms-XX は別セッションが LIVE 作業中。重ねて dispatch しますか？」と確認する。
- `assigned_to_others == true` かつ `assigned_to_me == false` (他人の永続担当) → 優先度を下げる。`unclaimed` / `assigned_to_me` の MS を優先的に選ぶ。
- `unclaimed` / `assigned_to_me` → 通常通り最優先で dispatch 対象にする (空き or 自分担当、衝突なし)。
- `※ liveness 未確認` (local mode で directory 不通) → LIVE 判定は「可能性」として弱め、warn は出すが除外しない。

Task-level dispatch (Step 0.0) の場合も同様に、各 task の親 MS / task が指す target に対して `beacon claim view --target <kind>:<id> --json` を引き、他セッションが LIVE 作業中の task を第一候補から外す。

### 2層 fallback (AC6)

LIVE claimer が居なくても `assigned` を読む。「LIVE で空いている」ように見えても他人の永続担当なら優先度を下げる。逆に、assignee 未設定でも LIVE で誰も作業していなければ堂々と割り当ててよい。

`beacon claim view` が失敗したら (cloud 不通等) この filter は skip し、従来の依存グラフだけで割り当てる (best-effort、dispatch を止めない)。

## Step 2.5: 選択MS間の依存関係チェック

実行可能MSが2つ以上ある場合、それらの**相互の依存関係**を確認する。

### 確認方法

Step 1 の `nodes` データを使い、実行可能MSのすべてのペア (A, B) について:
- A の `depends_on` に B が含まれるか
- B の `depends_on` に A が含まれるか
- どちらも含まれない場合 → **「未定義」**

### 未定義ペアへの判断（静的シグナル）

未定義ペアが存在する場合、各ペアについて以下を推論する:

- 両MSのタイトル・タスク説明を読み、**同じファイルに触れる可能性があるか**を判断する
- 論理的な実行順序が必要か（例: 片方が削除するものをもう片方が参照するなど）を評価する

### 動的シグナル: 直近 commit のファイル衝突予測（e-602）

加えて、**直近の作業履歴から「実際にどのファイルを触っているか」**を取得し、衝突可能性を機械的に評価する:

各実行可能MS (X) について、Bash ツールで:

```bash
# X が紐づくブランチがあればその差分、なければ過去14日の MS タグ付き commit
beacon task list --json --ms <ms-id> | jq -r '.entries[] | select(.type=="commit") | .meta.hash' | head -20 \
  | while read h; do git show --name-only --pretty=format: "$h" 2>/dev/null; done | sort -u
```

これで各MSが直近触ったファイルセット `F(X)` を得る。

ペア (A, B) の **衝突可能性** は:

| `F(A) ∩ F(B)` | 解釈 |
|---|---|
| 空 | **静的シグナルだけ見る** — 既存の Title/Task ベース判断を使う |
| 1 ファイル & テストのみ | **中** — テスト追加と本体追加で互いに干渉する可能性。SPEC を読んで判断 |
| 1〜2 ファイル & コア | **高** — 同じ関数 / クラス / モジュールを両側が触る。**順序付け推奨** |
| 3+ ファイル | **高** — 大域変更系。直列実行を強く推奨 |

### リスク評価

静的シグナル + 動的シグナルを統合して、ペアごとに:
**「低（並列可）」「中（注意）」「高（順序付け推奨）」** の3段階で評価する。

リスクが「高」の場合、Step 4 で「並列実行を推奨しません。順序付けして直列実行しますか？」をユーザーに確認する。

### 出力

未定義ペアがない場合はこのStepをスキップ。

未定義ペアがある場合、Step 4 の Dispatch Plan に含める（後述）。各ペアの「触ったファイルセット F(X)」と「F(A) ∩ F(B)」も併せて表示し、判断の透明性を確保する。

## Step 3: 各MSの詳細情報取得

実行可能な各MSについて、以下を **並列に** Bash ツールで実行:

### 3a. SPECドキュメント一覧
```bash
beacon doc list --scope spec --ms <ms-id> --json
```

結果が空でなければ、各ドキュメントの内容を取得:
```bash
beacon doc show <doc_id>
```

### 3b. 未完了タスク一覧
```bash
beacon task list --json --ms <ms-id>
```

`entries[]` から `type == "task"` かつ `status != "done"` かつ `status != "cancelled"` のものを抽出する（ネストされた `entries[]` 内も再帰的に確認）。

### 3c. CORE doc 一覧（全MS共通の前提として渡す）

```bash
beacon doc list --scope core --json
```

結果から `project-vision` doc と、ms-40 等で参照される **重要 CORE doc 群** を特定する。サブエージェントには doc_id とタイトルのリストを渡し、必要に応じて自ら `beacon doc show <doc_id>` で取得させる方針 (prompt 肥大化回避)。

特に以下の CORE doc は **常に明示的に prompt に列挙**する（参照の漏れを防ぐ）:
- `project-vision` — プロジェクトビジョン
- 関連 SPEC が参照している CORE doc（SPEC 本文から `関連 CORE` セクションを抽出して取得）

### 3d. MS が参照する SPEC / CORE doc の依存関係抽出

各 MS の SPEC 本文 (Step 3a で取得) から「関連 CORE」「関連 SPEC」セクションを軽くパースして、サブエージェントが追加で読むべきドキュメント ID を列挙する。これは prompt の「## 前提コンテキスト」セクションに記載される。

## Step 4: Dispatch計画の提示

収集した情報をユーザーに提示する。**実行可能MS・待機中MS・相互依存チェックをすべて表示する**:

```
Dispatch Plan:
---
実行可能:
◐ [ms-id] [title] (workspace: [dir or "none"])
  SPEC: [doc titles, comma-separated or "⚠ (none) — `/beacon-spec [ms-id]` で作成推奨"]
  Tasks: [N pending]
    - [entry-id] [description]

依存関係により待機中:            ← 存在する場合のみ表示
◌ [ms-id] [title]
  ← ブロック: [blocking-ms-id] [blocking-title] ([status])

選択MS間の依存関係:              ← 未定義ペアがある場合のみ表示
  [ms-id-A] ↔ [ms-id-B]: 未定義 [低/中/高] — [1行の理由]
  [ms-id-A] ↔ [ms-id-C]: 未定義 [低/中/高] — [1行の理由]
  ※ リスク「高」のペアは依存関係を定義してから起動することを推奨

SPEC 無し MS warning:            ← SPEC 無し実行可能 MS が 1 つ以上ある場合のみ
  ⚠ 以下の MS には SPEC (要求書/判断軌跡) がありません:
    - [ms-id-X] [title]
    - [ms-id-Y] [title]
  サブエージェントは MS の objective / ac とタスク description のみを材料に判断します。
  「なぜこのMSをやるのか」「どこまでがスコープか」が SPEC で言語化されていないと、
  実装方針がブレやすくなります。
  推奨:
    a) このまま続行 (起動は許可されます。緊急時はこれで OK)
    b) `/beacon-spec [ms-id]` で SPEC を先に作成してから dispatch (推奨)
---
実行しますか？ [全て / 選択 / キャンセル]
依存関係を先に定義する場合: beacon milestone depends <ms-id> --on <dep-id>
SPEC を先に作る場合: 一旦キャンセルして `/beacon-spec <ms-id>` を実行してください
```

**重要**: SPEC 無し warning は **hard block ではない**。ユーザーが「全て」「選択」と答えれば、SPEC が無い MS でもサブエージェントを起動する。ms-41 SPEC で確立した方針 (warning のみ、強制力で動かさない) に従う。

ユーザーの回答を待つ:
- **全て**: すべての実行可能MSを並列起動 (SPEC 無しでも起動可)
- **選択**: ユーザーが指定したMS-IDのみ起動
- **キャンセル**: 何もせず終了 (SPEC 作成は別途 `/beacon-spec` で)

## Step 4.4: 並列上限の構造的強制 (ms-61 e-1825)

Step 4 でユーザーが承認した MS 群 (= 「全て」 or 「選択」 で選ばれた MS) を `$SELECTED_MSS` とする。 件数 `$N = len(SELECTED_MSS)` を Step 0.4 の原則と照合する:

| `$N` | 動作 |
|---|---|
| 1〜2 | そのまま Step 4.5 へ |
| 3 | 「3 件は最大上限です。 このまま進めて良いですか? [yes / 2 件に絞る / cancel]」 と一度確認した上で続行 |
| 4 以上 | **強制分割**: 「並列上限 3 を超えています (= `$N` 件)。 構造的に refuse します。 以下の選択肢から選んでください」 と提示し、 次の degraded mode を案内 |

### Degraded mode: 上限超過時の queue 化 (= 順次起動)

`$N >= 4` の場合、 ユーザーに以下を提示:

```
⚠ 並列上限超過: $N 件のサブエージェント起動を要求されましたが、 上限は 3 件です。
   過去 (= memory: Mac dogfood で 8 並列 → permission prompt 大量で Claude Code 破綻、 約 8 分の dispatch 無駄) の事象を構造的に防ぐためです。

選択肢:
  a) Wave 化して順次起動 (推奨): 先頭 3 件を Wave 1 として起動、 残りはここで queue 化して
     Wave 1 完了後に新規 /beacon-dispatch を案内 (= 親セッションが一度に抱える子は最大 3)
  b) ユーザー指定で 3 件に絞る: どの MS を Wave 1 に入れるかをユーザーが選ぶ
  c) `/beacon-session-fork` を併用: 対話必須 MS は fork で別 bclaude に分離 (= 別 user 端末が
     直接相手する経路)、 残りを dispatch に流す
  d) cancel: 何もせず終了

どれにしますか?
```

各選択肢の遷移:

- **a) Wave 化**: 先頭 3 件 (= 渡された順、 もしくは priority=highest を優先) を `$SELECTED_MSS` として上書き、 残りは `$QUEUED_MSS` として記録。 Wave 1 完了後 Step 6d に到達した時点で「次の Wave: `<queued ms-id list>` 残り — `/beacon-dispatch` で起動可能」 と提示する。
- **b) ユーザー指定**: ユーザーから 3 件以下の MS ID を受け取って `$SELECTED_MSS` を上書き。 残りは破棄 (= 次回 dispatch でまた検討)。
- **c) fork 併用**: dispatch / fork の振り分けを user と相談 (Step 0.4 の振り分け原則を提示)。 dispatch 側に残った件数が 3 以下なら Step 4.5 へ、 まだ 4 以上なら a)/b) に戻る。
- **d) cancel**: Step 5 に進まず Skill 終了。 `beacon milestone start` も実行しない (= MS の活性化はしない)。

ユーザーの応答を待つ。 a)〜c) で `$N <= 3` になったら Step 4.5 へ進む。

### Task-level dispatch も同じゲートを適用

`$DISPATCH_MODE == "Task-level"` (Step 0.0 で判定) の場合、 本 Step は **Task Mode T4 内で同じ上限ロジックを実行**する (= 後述 T4 で wave 単位の同時起動件数を上限チェック)。 MS-level / Task-level どちらも 3 件を超えた時に同じ degraded mode が走る。

## Step 4.5: MS 活性化フェーズ (ms-81 e-1920 で workspace → start に統一)

ユーザーが承認後（「全て」または「選択」）、エージェント起動の前に各MSを活性化し worktree (= MSごとの作業領域、git project の場合のみ作成) を準備する。

### MS 活性化手順

承認されたMSそれぞれについて、以下を順次実行する。`beacon milestone start` が status (= MSの状態) / assignee (= 担当者) / 専有 (= 今このセッションが座っている、というラベル) / worktree (git project の場合) を atomic に確保する:

```bash
beacon milestone start <ms-id>
```

stdout から以下を抽出する (= 出力に表に出るので非開発者ユーザーにも何が起きたか伝わる):
- `branch:` 行 → `workspace_branch` として記録 (Step 5 の prompt に使用)
- `next: cd <path> && bclaude` 行 → `<path>` を `workspace_path` として記録
- `workspace: non-git project, worktree step skipped` 行が出ていれば非 git project → worktree なしで続行、論理専有のみ取得済

### 失敗時の扱い

`beacon milestone start` がエラーで停止した場合:
- エラーを表示してユーザーに確認を求める
- 「worktreeなしで続行しますか？」と問い、承認されれば `workspace_path = "プロジェクトルート"` で続行 (= 非 git project と同じ扱い)

### 既に活性化されている場合

`beacon milestone start` は冪等に動作する (= 既存の worktree があれば再利用、既存の assignee は no-op で重複追加せず、既存専有が他セッションなら警告と takeover event を残して続行)。再実行しても安全。

### Deprecated alias の warning が出た時

`beacon milestone workspace` を直接叩いた legacy 経路は内部で `start` に転送されるが deprecation warning が stderr に出る。Skill としては表記を `start` に統一し、warning を見たら呼び出し側の修正を提案する (= e-1920 drift 防止)。

## Step 5: サブエージェント起動

Step 4.5 で準備したworktree情報を使い、各MSに対して **Agent tool** でサブエージェントを起動する。

### 起動ルール
- 互いに依存関係のないMS同士は **並列** で起動してよい
- 依存関係のあるMS同士は **直列** で起動する（先行MSの完了を待つ）
- 実際には Step 2 の条件を通過した時点で互いに独立なので、全て並列で起動してよい
- **同時並列起動数は 3 を超えてはならない** (= Step 0.4 / Step 4.4 で構造的に強制済の上限、 ms-61 e-1825)。 `$SELECTED_MSS` の件数が 4 以上のまま Step 5 に到達した場合は **Step 5 で起動せず Step 4.4 に戻る** (= 防御線が二重)。 ここを破ると memory に記録された 8 並列破綻事象が再発する

### 各エージェントへのPrompt

以下のテンプレートで prompt を構成する（`workspace_path` は Step 4.5 で取得した値を使用）:

```
あなたは beacon プロジェクトのサブエージェントです。以下のマイルストーンを担当します。

## 担当マイルストーン
- ID: [ms-id]
- Title: [title]
- Workspace: [workspace_path from Step 4.5, or "プロジェクトルート"]
- Branch: [workspace_branch]

## 作業ディレクトリ（重要: cwd を必ず明示）

Workspace 絶対パス: [abs_workspace_path]

- すべての Bash 呼び出しに `cwd=[abs_workspace_path]` を明示する、または `cd "[abs_workspace_path]" && ...` 形式で実行する
- ホームディレクトリで起動された場合でも、上記 workspace を基準に動作させる
- git 操作は `git -C [abs_workspace_path] <subcommand>` 形式が安全
- worktree が無い場合はプロジェクトルートで作業（main ブランチには直接コミットしない）

## ⚠️ 最初の必須ステップ: セッション開始

**何を始めるよりも先に** Bash ツールで:
```
cd "[abs_workspace_path]" && /beacon-session-start [ms-id]
```
を実行し、担当 MS のコンテキスト（CORE doc / SPEC / タスク / Operation / 未解決 Incident）を完全に復元すること。

これは規約であり、スキップしてはならない。session-start 出力に書かれた前提（CORE 原則、SPEC の判断軌跡、未解決 Incident、トリガー）はすべて作業中に尊重する。

## 前提コンテキスト

### Project Vision (重要)
プロジェクト全体のビジョンは CORE doc `project-vision`。session-start が読み込んでくれるので、その内容を熟読してから着手すること。

### 参照すべき CORE doc
以下の CORE doc を session-start 後に必要に応じて `beacon doc show <doc_id>` で取得：
[Step 3c で抽出した CORE doc id とタイトル一覧]

### 関連 SPEC（本 MS の SPEC 本文から抽出した参照リンク）
[Step 3d で抽出した関連 SPEC / CORE doc 一覧]

## SPEC ドキュメント（本 MS 専用）
[SPECの全文をここに展開。なければ "(SPECなし — objective / ac とタスク description のみを材料に判断すること)"]

## 未完了タスク
[タスク一覧を展開]
- [entry-id]: [description]
- [entry-id]: [description]

## 作業ルール
1. 上記「最初の必須ステップ」を **必ず最初に** 実行する（session-start）
2. 全ての Bash 呼び出しに `cwd=[abs_workspace_path]` または `cd "[abs_workspace_path]" && ...` を明示する
3. タスクを完了したら `beacon task done <entry-id> --reason "..."` で記録する (reason 必須)
4. コミット後は `/beacon-log` で進捗を記録する (PostToolUse hook で自動)
5. 新しいタスクが必要になったら `beacon task add "description" -m [ms-id] --untriaged --motivation "..." --acceptance-criteria "..."` で追加する (ms-126: サブエージェントは優先度を判断していないので `--untriaged` sentinel で起票 = 後で人間が triage)
6. **書き込み系コードを新規追加する場合**は `lib/operations.py` の `apply_operation` を経由させる（lost-update protection）
7. 作業完了後、以下を **親エージェントへの報告として** 含める:
   - 完了タスク ID 一覧 + 残タスク ID 一覧
   - 主要コミットハッシュ
   - 学んだこと・判断軌跡で SPEC や CORE doc に昇格すべきもの（提案レベルで OK、親に伝える）
   - 注意点・既知の問題
8. **失敗・中断時の必須報告 (e-601)**: 例外で停止する場合、または途中で作業を諦める場合、必ず以下を返答に含める:
   - `STATUS: failed` または `STATUS: partial` の明示ヘッダ
   - 何処まで進んだか（最後の正常コミットハッシュ）
   - 失敗理由を 1 行サマリ + 詳細
   - worktree を残すべきか破棄してよいか（次セッションで継続可能性）
   - 親側で `beacon trigger fire dispatch-failure-<ms-id>` を打ってほしい場合は明示
9. 作業完了後: オーケストレーターが `beacon milestone workspace-cleanup [ms-id]` でworktreeをクリーンアップする
```

## Step 6: 結果報告

全エージェント完了後、結果をユーザーに報告する:

### 6a. 完了状態の取得
```bash
beacon milestone graph --json
```

### 6b. サブエージェントの最終状態を分類（e-601）

各サブエージェントの返答を以下に分類する。**握りつぶさず**、未完了・失敗も明示的に取り上げる:

| 分類 | 判定基準 | 報告での扱い |
|---|---|---|
| ✓ 完了 (success) | 全担当タスクが done、エラーなし | 「[ms-id] complete: N tasks done」 |
| ⚠ 部分完了 (partial) | 一部 done、残りはタスクとして残置 | 「[ms-id] partial: M/N done, 残: [entry-ids]」 |
| ✗ 失敗 (failed) | 例外で停止 / Workspace破損 / 0タスク完了で終了 | 「[ms-id] FAILED: [first line of error]」 + 続行可能か判定 |
| ⏸ タイムアウト | サブエージェントが応答しない | 「[ms-id] TIMEOUT: kill 推奨」 |

失敗・タイムアウト時は **必ずトリガーを発火**して Web UI 側にも警告を出す:
```bash
beacon trigger fire dispatch-failure-<ms-id> "Sub-agent for <ms-id> failed: <one-line reason>"
```

これがあると、別端末で Web UI を見ている他メンバーも気付ける（e-628 通知系統と統合される素地）。

### 6c. 報告フォーマット

Step 2 で記録した各MSの元の `progress` と比較して報告する:

```
Dispatch Complete:
---
✓ [ms-id] [title]: [new progress]% (was [old progress]%)
  Completed tasks: [entry-ids of tasks marked done]
  Remaining tasks: [entry-ids of still-pending tasks, if any]

✗ [ms-id] [title]: FAILED
  Reason: [one-line summary from sub-agent's last message]
  Commits made before failure: [hashes, if any]
  Recommended next step: [retry / kick to user / abandon worktree]
---
```

### 6d. 次Waveの提示

Step 1 のグラフ情報と最新ステータスから、新たに実行可能になったMSがあれば提示:
```
Next wave available:
  [ms-id] [title] (depends_on: [completed deps])
再度 /beacon-dispatch で起動できます。
```

## Step 7: Worktree merge とコンフリクト解消（e-600）

サブエージェントが成功で返ってきたら、各worktreeブランチを **PR 経由** または **直接 merge** で main に統合する。

### 7a. PR 駆動（推奨、ユーザーが2-5人体制）

各 ms-XX/work ブランチについて:
```bash
git -C <worktree-path> push -u origin <branch>
```
で push し、`/beacon-pr-create` を案内する（オーケストレーター自身が呼ぶことも可）。各PRが merge されたら自然に main に統合される。

### 7b. 直接 merge（1人開発で素早く統合したい場合）

各worktreeブランチを順次 main に merge:
```bash
git -C <project-root> checkout main
for branch in ms-39/work ms-40/work ms-41/work; do
    git merge --no-ff "$branch" -m "Merge $branch"
done
```

### 7c. コンフリクト検知と解消フロー

`git merge` でコンフリクトが発生した場合、**Skill が自動で次の判断を行う**:

1. `git status --porcelain` でコンフリクトファイル一覧を取得
2. 各ファイルを Read で読み込み、**コンフリクトの性質を分類**:

   | 分類 | 判定基準 | 自動解消可否 |
   |---|---|---|
   | **trivial (片側だけ追加)** | 一方が空、他方が新規追加 | ✓ 自動解消可（追加された方を採用） |
   | **non-overlapping additive** | 両側とも追記のみ、行範囲が異なる | ✓ 自動解消可（両方残す） |
   | **semantic overlap** | 同じ関数 / 同じ宣言を両側が修正 | ✗ ユーザー判断必須 |
   | **structural rewrite** | ファイル削除 vs 修正 / リネーム衝突 | ✗ ユーザー判断必須 |

3. 自動解消可のものは AI が解消（`git add` まで）してユーザーに「自動解消したファイル: ..., 確認しますか？」と提示
4. 自動解消不可のものは「以下のファイルは AI では判断できません。手動解消お願いします:」とリストし、`git merge --abort` も選択肢として提示
5. 解消後は **トリガー発火** で Web UI 側にも記録:
   ```bash
   beacon trigger fire dispatch-merge-conflict "<ms-id>: <auto-resolved>件/<manual>件"
   ```

### 7d. merge後のworktree cleanup

merge 完了したMSは:
```bash
beacon milestone workspace-cleanup <ms-id>
```
で worktree を片付ける。`git worktree remove` + ブランチ削除まで一気にやる（既存実装に準拠）。

## 制約

- **データ取得は beacon CLI `--json` 経由のみ**: Read ツールで `.beacon/project.json` を直接読んではならない。
- **Agent tool でサブエージェントを起動**: 直接コードを書いたり実行したりしない。各MSの実装はサブエージェントに委ねる。
- **ユーザー承認必須**: Step 4 でユーザーの明示的な承認がなければ Step 5 に進まない。
- **失敗の報告**: サブエージェントがエラーを返した場合、握りつぶさずそのまま報告する。
- **project.json への直接書き込み禁止**: beacon CLI を通じてのみ状態を変更する。
- **サブエージェント session-start を必ず prompt に強制**: prompt の冒頭に「最初の必須ステップ: `/beacon-session-start <ms-id>`」を明示する。これがないと CORE doc / SPEC のコンテキストが復元されず、実装方針がブレる。
- **CORE doc / project-vision の参照を明示**: Step 3c/3d で抽出した CORE / 関連 SPEC のリストを prompt に必ず含める。サブエージェントは新セッション扱いなので、自動で読まれることに依存しない。

---

## Task Mode: タスクレベル並列 dispatch (ms-65 e-1480、ms-17 e-1221 を吸収)

`/beacon-dispatch --tasks <task-id>+` で起動した時の専用フロー。**親 MS の worktree の中で nested worktree (= 入れ子) を切り、1 task = 1 サブエージェント** で割り当てる。同 MS の中で互いに干渉しない task 群を一気に消化したい時に使う。

異なる MS の task を混ぜたい場合は **MS-level dispatch を使う** こと (= Task Mode は単一 MS 配下のみ)。

### T1: タスク群の親 MS 検証

引数で渡された各 task ID について Bash ツールで `beacon task show <task-id> --json` を **並列に** 実行し、`milestone_id` (= 親 MS) を確認する。

- 全 task が同じ親 MS → 続行
- 異なる MS の task が混ざる → エラーで停止し、ユーザーに以下を提示:
  ```
  Task Mode: 渡された task は複数の MS にまたがっています。Task Mode は同一 MS 配下のみ対応。
    ms-A: e-X, e-Y
    ms-B: e-Z
  MS-level dispatch (= /beacon-dispatch 引数なし) を使うか、 task 群を MS ごとに分けて再実行してください。
  ```

親 MS を `$PARENT_MS_ID` として保持。

### T2: 親 MS の worktree 確認 (なければ準備)

Bash ツールで:
```bash
beacon milestone show $PARENT_MS_ID --json
```
`workspace_path` または `.worktrees/<...>` の実在を確認。

- 親 worktree なし → `beacon milestone start $PARENT_MS_ID` で main project root から起動するよう案内 (= MS の workspace 確保はこの Skill ではなく milestone start の責務、ms-65 e-1477)
- 親 worktree あり → そのパスを `$MS_WORKTREE` として保持し続行

### T3: 並列可能性の AI 判定

各 task について `beacon task show <task-id> --json` の結果から `description` / `motivation` / `acceptance_criteria` を取り出し、以下の観点で「並列可能か」を AI が判定する:

| 観点 | 直列推奨のシグナル |
|---|---|
| ファイル重複の予測 | AC に同じファイルパス / 同じ関数名が複数 task で挙がっている |
| 設計依存 | task A の AC が task B の output (= 新規ファイル / 関数 / API) を前提にしている |
| 同 entry-point 改修 | 同じ CLI コマンド / 同じ API endpoint / 同じ Skill の同セクションを触る |
| 直近 commit の予測衝突 | `git log --stat -10 -- <候補ファイル>` で同領域に変更が走った task が複数 |

判定根拠を bullet で書き出し、「並列 N waves」「直列」「mixed (= 一部並列、一部直列)」のいずれかを推奨として人間に提示。

### T4: 人間承認 + 並列上限の構造的強制 (ms-61 e-1825)

提示された計画について、 **Step 0.4 の並列上限 (= 推奨 2、 最大 3) を Wave 単位で適用**する。 各 Wave のサブエージェント数 `$W` が 3 を超えていれば、 提示前に Wave を自動分割する (= e.g. Wave 1 に 5 task 並列なら Wave 1a / Wave 1b に分割)。

分割ロジック:
- 各 Wave について `$W = len(wave.tasks)` をチェック
- `$W <= 3` なら現状維持
- `$W == 4 or 5` なら 2 個ずつ等分 (= 2+2 / 2+3)
- `$W >= 6` なら 3 個区切りで分割 (= 3+3+... / 最後の塊だけ小さくなる)
- 分割した Wave は元 Wave の直後に挿入 (= 元の直列依存は保持)

分割後の計画をユーザーに提示:

```
タスクレベル dispatch 計画:
  親 MS: $PARENT_MS_ID "<title>"
  親 worktree: $MS_WORKTREE
  
  判定: [並列可能 / 直列必須 / mixed]
  根拠:
    - <task-1 と task-2 はファイル重複なし → 並列可能>
    - <task-3 は task-1 の lib/foo.py 新設を前提 → task-1 後>
  
  実行計画 (= 並列上限 3 / Wave 適用済):
    Wave 1 (並列, 2 件): <task-1>, <task-2>
    Wave 2 (並列, 3 件): <task-3>, <task-4>, <task-5>
    Wave 3 (直列, 1 件): <task-6>
  
  ※ 元の希望は Wave 2 に 5 件並列でしたが、 並列上限 3 を超えるため
    Wave 2 (3 件) + Wave 3 (= 残り 2 件) に自動分割しました
    (= Mac dogfood で 8 並列 → permission prompt 大量で破綻した経験を構造的に防止、 ms-61 e-1825)
  
  この計画で進めますか？ [yes / 修正 (= 計画を口頭で指示) / cancel]
```

`yes` で続行。`修正` ならユーザーの口頭指示で計画を上書き再提示 (= T4 をループ、 上限ロジックも再適用)。`cancel` で中断。

> **note**: 元計画が Wave 1 に 4+ 件並列だった場合、 Step 4.4 と同じ degraded mode の選択肢 (= Wave 化 / 件数絞り / fork 併用 / cancel) を提示してもよい。 Task-level は元々 1 MS 配下の task 群で fork 併用は稀だが、 計画ループの中で user が「これ dispatch でやるより fork で対話実装したい」 と判断した場合は cancel して `/beacon-session-fork` への切り替えを案内する。

### T5: nested worktree 作成

各 task について Bash ツールで:
```bash
cd $MS_WORKTREE && git worktree add .worktrees/<task-id> -b <task-id>/work
```

(git は worktree の入れ子を許容する。`<task-id>/work` ブランチは task 専用の作業ブランチ。)

エラー時 (= 既存) は drop-in:
```bash
cd $MS_WORKTREE && git worktree add .worktrees/<task-id> <task-id>/work   # -b なしで既存ブランチ採用
```

`$TASK_WORKTREE_<task-id>` として絶対パスを記録。

### T5.5: Trek-scoped task の TTL 延長 (ms-95 / e-2308)

**前提判定**: 親 MS が Trek の scope (= 缶詰の作業部屋 / 自律的計画的タスク実行の作業空間) に含まれている場合、 dispatch する各 task は Trek task でもある。 この場合 Trek 側の **TTL safety net** (= 既定 24 時間 (= 1440 分、`DEFAULT_WORKING_TTL_MINUTES`) reaffirm が無いと auto-stall して `leader_review` に強制遷移) が subagent 動作中に false-positive で発火しうる構造問題がある (= 2026-06-23 dogfood で実体験、 e-2308 起票。 当時 TTL は 12 分で高頻度発火だったが e-4117 時点は 24h に緩和済で頻度は低い。 ただし非 member session が pulse 経路に入れない構造要因は残る)。

**理由**: Agent tool subagent は別 `session_id` で起動され、 Trek の joined member ではない (= `beacon trek task-state working` が 403 で reject される)。 main session (= leader) も Agent tool 完了待ちで pulse 経路に入れない。 結果 TTL (= 既定 24 時間) 経過時点で auto-stall が走り、 動作中の task が `leader_review` に降格される。

**対処**: T6 で各 subagent を起動する **直前** に、 leader 代行で TTL 延長を打つ:

```bash
beacon trek extend-ttl <trek-id> <task-id> --minutes 30 \
  --reason "dispatched to Agent subagent (= e-2308 leader-side TTL extension)"
```

`--minutes 30` は subagent 1 件の典型的所要時間 (= 5-15 分) + 安全マージンの 2× を確保する基準値。 task が大きい想定なら `--minutes 60` 等に増やしてよい。 値は task 単位で個別に設定する。

T7 で各 subagent が完了し PR まで land したら、 後片付けで `--minutes 0` で extension を clear する:

```bash
beacon trek extend-ttl <trek-id> <task-id> --minutes 0 \
  --reason "subagent finished, restore normal TTL"
```

**省略可能条件**: 親 MS が Trek scope に含まれていない (= 単独 MS dispatch) 場合、 本 step は無音で skip する。 Trek 判定は `beacon trek list --joined --json` の scope 配列に親 MS が含まれるかで決まる。

### T6: サブエージェント prompt (各 task に 1 通)

各 task について Agent tool で起動する。Wave 1 (並列) は **同時並列**、Wave 2 以降は **前 Wave 完了後** に起動。

prompt テンプレート (= 重要な必須セクション):
```
## 担当タスク
<task-id>: <task.description>

motivation: <task.motivation>
acceptance_criteria: <task.acceptance_criteria>

## 作業ディレクトリ (重要: cwd を必ず明示)
$TASK_WORKTREE_<task-id>

このディレクトリは親 MS <$PARENT_MS_ID> の worktree 内に nested された、
本タスク専用の worktree (branch: <task-id>/work) です。
- ファイル編集は必ずこの cwd 配下で行う
- commit はこの worktree の中で行う (親 MS の worktree や main repo の HEAD を触らない)

## 親 MS の文脈 (参照のみ、変更しない)
<親 MS の SPEC 要約 — beacon doc show <ms-spec-doc-id> の本文を入れる>

## 必須セッション開始
最初に `/beacon-session-start <$PARENT_MS_ID>` を実行して CORE doc / SPEC コンテキストを復元する。

## 完了報告
最終 commit hash と、AC のうち達成 / 未達 / 未検証側面 を 200 words 以内で報告。
**push と PR 作成は親 dispatch session が一括で処理するため、サブエージェントは行わない。**
```

### T7: 結果回収 + 親 MS worktree への merge

全 task サブエージェントが完了したら:

1. 各 nested worktree の最新 commit hash を Bash で回収
2. 親 MS worktree に戻り (`cd $MS_WORKTREE`)、Wave 順に各 task ブランチを merge:
   ```bash
   git merge --no-ff <task-id>/work -m "merge task <task-id> into <ms-branch>"
   ```
3. conflict 発生時は Step 7c (= MS-level dispatch と同じ conflict 解消フロー) を流用
4. merge 完了後、nested worktree を片付け:
   ```bash
   git worktree remove .worktrees/<task-id>
   ```
   (task ブランチ自体は monorepo の commit 履歴として残す = `git branch -D` はしない)
5. 結果サマリーをユーザーに報告 (= Step 6c と同じフォーマット、task 単位に分解)

### T8: Task Mode の制約

- task ID 群は全て同じ MS 配下でないとエラー (T1 で gating)
- 親 MS の worktree が無い場合は本 Skill は worktree を切らず、`beacon milestone start` への誘導で停止 (= worktree 確保は MS 活性化の責務 / e-1477)
- nested worktree 削除前に親 MS branch への merge を必ず通す (= 子の commit が孤立しないように)
- 親 MS worktree 自体は Task Mode 完了後も残る (= 親 worktree の状態は dispatch 後も継続作業可能)
- **並列 Wave のサブエージェント数は推奨 2 / 最大 3** (Step 0.4 の並列上限原則 + T4 の Wave 自動分割で構造的に強制、 ms-61 e-1825)。 4 件以上を 1 Wave に詰めて起動してはならない (= Mac dogfood で 8 並列 → Claude Code harness の permission prompt 大量化で破綻、約 8 分の dispatch 無駄になった事象に基づく構造防御)
