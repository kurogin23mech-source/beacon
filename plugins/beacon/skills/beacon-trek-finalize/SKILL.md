---
name: beacon-trek-finalize
description: Trek (= 缶詰の徹夜作業部屋、 事前承認スコープを持つ自律実行の作業空間) を終結する Skill。 含まれる全 PR の構造化 summary を user に提示し、 1 confirm で 全 PR 一括 merge + Trek archive まで完遂する。 「approve = AI / merge = Trek 単位 user / release = user」 の 3 段境界 (= ms-92 / e-2169 + CORE doc pr-review-autonomy-boundary) を 1 動線で具現化する。
version: 0.1.0
---

# Beacon Trek Finalize

> Trek (= 缶詰の徹夜作業部屋) の終結を 1 動線で完遂する Skill。 含まれる全 PR (= プルリクエスト) の構造化 summary を user に提示し、 1 confirm で 全 PR 一括 merge + Trek archive + 任意で release ceremony への動線を回す。

## 設計思想 (= 3 段境界の具現化)

ms-92 / e-2169 + CORE doc `pr-review-autonomy-boundary` で確立した役割分担:

| 境界 | actor | 本 Skill での扱い |
|---|---|---|
| approve / reject | AI (= leader role) | Trek 期間中に逐次実行済の前提 (= 個別 PR の approve は本 Skill 起動時にはすでに済) |
| **merge** | **user (= 1 confirm 集約)** | **本 Skill のメイン責務、 全 PR 一括 merge** |
| release | user | 本 Skill から `/beacon-push` (= release.yml dispatch) へ動線提示 |

個別 PR の self-merge は構造的に禁止 (= `beacon pr merge` が AI session 由来時に refuse、 e-2169 実装)。 本 Skill 経由でのみ `BEACON_TREK_FINALIZE_CONSENT=1` を export して merge 経路を開ける。

---

## 前提条件チェック

```bash
cd "$PROJECT_DIR" 2>/dev/null; beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```

`NO_BEACON` なら何もせず終了。 `gh auth status` も確認 (= `gh pr merge` 実行に必要)。

## Step 0: 対象 Trek の確定

引数 `/beacon-trek-finalize <trek-id>` で渡された場合はそれを使う。 引数無し の場合は `beacon trek list --joined --status active --json` で picker から選ぶ:

```bash
cd "$PROJECT_DIR" && beacon trek list --joined --status active --json
```

複数あれば user に picker を出す:

```
終結する Trek を選んでください:
  1. tk-aaaa1111 — Trek 自律性 phase 2 (= active、 leader: you)
  2. tk-bbbb2222 — ...
  cancel で中止
```

選んだ trek_id を `trek_id` に保持。

## Step 1: Trek 完遂条件のチェック

```bash
cd "$PROJECT_DIR" && beacon trek show "$trek_id" --json
```

返ってきた JSON で:

- `status == "active"` を確認 (= planning / archived は finalize 対象外)
- `members` から自分が `role: leader` であることを確認 (= leader role でない場合は warning で「leader transfer 後に再実行してください」 と返して終了)
- `task_states` 全てが terminal (= `done` / `archived`) か確認。 まだ working / leader_review が残っていれば warning で「未完了 task がある: <id 一覧>」 と表示し続行 / 中止を user に問う

leader role でない場合は中止 (= leader stance reminder と整合、 CORE doc `trek-leader-stance`)。

## Step 2: Trek 内 PR の収集

Trek scope の各 project から PR (= type=pr) entries を pull する。 scope が複数 project にまたがる場合は project ごとに繰り返す。

cwd の project の場合:
```bash
cd "$PROJECT_DIR" && beacon pr list --json
```

cross-project の場合 (= scope に別 project が含まれる):
```bash
# 各 project の PR を別途取得 (= e-2141 cross-project task add と同様、
# 別 worktree に切り替えるか server API 直叩きが必要)。 v0.48 系では
# 同 project のみで動かし、 cross-project finalize は follow-up task で
# 実装する (= 本 Skill body で skip 宣言する)。
```

集めた PR entries から以下を抽出:

- PR URL (= `meta.pr_url`)
- PR number (= URL から parse)
- intent (= AI が `/beacon-pr-create` で書いた「このPRで何を達成するか」)
- review_status (= pending / approved / rejected)
- linked commits (= `meta.linked_commits`)
- AC integrity (= 紐づく task の AC が満たされたかの AI 判断 = `done_reason`)

approved 以外の PR がある場合は warning で「未 approve PR: PR#N」 と提示。 user 判断で「approve 済のものだけ merge する」 か「中止して個別 PR の approve を先に済ませる」 を選ばせる。

## Step 3: gh 経由で各 PR の test / lint / cross-look 状況を取得

各 PR の状態を `gh` で確認:

```bash
gh pr view <pr_number> --json statusCheckRollup,reviewDecision,mergeable
```

集計:
- test pass: statusCheckRollup の全 status が SUCCESS
- lint pass: 同上 (= test と一括 status、 別 check は project 構成次第)
- cross-look: reviewDecision == APPROVED
- mergeable: MERGEABLE / CONFLICTING / UNKNOWN

CONFLICTING (= 事前 conflict 検知、 AC #7) がある場合は warning で「PR#N に conflict あり、 解消後に再 finalize」 と提示し中止する選択肢を提供。

## Step 4: 構造化 summary を user に提示 (= AC #4)

組み立てた情報を user に提示する。 1 confirm で 全 PR を merge する境界を **明示** する:

```
─── Trek finalize summary (= ms-92 / e-2169) ─────────────────────

trek_id: tk-aaaa1111 "Trek 自律性 phase 2"
含まれる PR (= 全 N 件):

  1. PR #184: feat(ms-92): /beacon-dm-send 確認 step を 1 prompt に集約 (e-2181)
     URL:     https://github.com/.../pull/184
     intent:  /beacon-dm-send の旧 Step 5/5b/5c/Step 6 を 1 prompt 集約経路に再構成…
     status:  approved / test ✓ / lint ✓ / mergeable ✓
     rationale: AI approve commit abc1234 で AC #1-6 全達成 confirmed

  2. PR #185: feat(ms-92): cross-project task add via Trek scope (e-2141)
     URL:     https://github.com/.../pull/185
     ...

  N. PR #N: ...

集約承認境界 (= e-2169 確立):
  - approve は AI labor として個別 PR で逐次済 (= 上記 rationale 参照)
  - merge は Trek 単位の user achievement、 1 confirm で N 件一括取り込み
  - release は別境界、 完了後に /beacon-push で release.yml dispatch を促す

──────────────────────────────────────────────

このまま N 件の PR を一括 merge + Trek archive まで進めますか?
  yes      — 全 PR を gh pr merge --merge で取り込み + Trek archive
  drilldown <N> — 個別 PR の詳細を表示 (= drill-down link 経路)
  partial  — merge する PR を個別選択 (= 一部だけ取り込み)
  cancel   — 中止
```

user が `drilldown <N>` を選んだ場合は `gh pr view <pr_number>` で詳細を表示し、 同じ 4 択 prompt に戻る。

## Step 5: 1 confirm 実行 (= yes / partial)

`yes` を受け取ったら、 全 PR を順に merge する。 本 Skill 経由で merge する場合のみ `BEACON_TREK_FINALIZE_CONSENT=1` を export し、 e-2169 で land した AI-session merge ban を bypass する:

```bash
export BEACON_TREK_FINALIZE_CONSENT=1
for pr_id in <approved_pr_entry_ids>; do
    # 1. gh で実際に merge (= hash 保持、 CORE doc 0KqFUbmJ7V0lmJZcW230 参照)
    pr_number=$(beacon pr show "$pr_id" --json | python3 -c "import json,sys,re; d=json.load(sys.stdin); url=d.get('meta',{}).get('pr_url',''); m=re.search(r'/pull/(\d+)', url); print(m.group(1) if m else '')")
    gh pr merge "$pr_number" --merge --auto || gh pr merge "$pr_number" --merge
    # 2. beacon entry を merged に状態遷移
    beacon pr merge "$pr_id" --json
done
unset BEACON_TREK_FINALIZE_CONSENT
```

`partial` の場合は user が選択した PR 群のみ同じ loop で処理。

## Step 6: Trek archive

全 merge 成功後、 Trek を archive する:

```bash
cd "$PROJECT_DIR" && beacon trek archive "$trek_id" --json
```

エラー (= 例: trek_doc が already archived) は warning で表示するが終了処理は続行 (= 「merge は通った、 archive は idempotent」)。

## Step 7: release ceremony 動線 (= 任意)

Trek 完遂後の release への動線を提示する (= 強制しない、 user 判断):

```
✓ Trek tk-aaaa1111 を finalize しました
  merged PRs:    N 件
  archived trek: tk-aaaa1111

次の動線 (= 任意):
  - リリースを切る場合:    /beacon-push (= release.yml dispatch、 5 配信チャネル整合)
  - リリースをまだ貯める:   そのまま、 次の Trek 起動で OK
  - retro (= 振り返り):    /beacon-retro で今 Trek の経緯を 1 ページにまとめる
```

## 制約

- **個別 PR の self-merge は本 Skill 経由 でしか通らない**: e-2169 で land した CLI 側 merge ban が `BEACON_TREK_FINALIZE_CONSENT=1` 以外 (= user override / human session kind) は refuse する。 user 明示 prompt 由来の単発 merge は `BEACON_PR_MERGE_USER_OVERRIDE=1` で escape hatch
- **本 Skill は leader role 限定**: executor role の session が起動した場合は warning で leader transfer を促して終了 (= ms-92 / e-2166 leader stance と整合)
- **cross-project Trek finalize は v0.48 系で同 project のみ**: 別 project の PR は別 worktree で個別 finalize する。 cross-project 集約 finalize は follow-up task (= AC #6 branch stacking とセット) で実装
- **conflict 事前検知 (= AC #7)**: Step 3 で `mergeable == CONFLICTING` の PR を検出したら warning + 中止オプションを user に提示

## 関連 doc / 関連 task

- CORE doc `pr-review-autonomy-boundary` (= 本 Skill の役割分担根拠)
- CORE doc `trek-leader-stance` (= leader role の 4 責務、 Trek 完遂は leader 意志)
- CORE doc `0KqFUbmJ7V0lmJZcW230` (= PR 取り込み戦略: hash 保持と beacon entry 整合、 merge --merge 規範)
- ms-92 / e-2169 (= 本 Skill の起票元)
- ms-92 / e-2164 (= leader-digest channel、 Trek 状態の continuous observation 源)
