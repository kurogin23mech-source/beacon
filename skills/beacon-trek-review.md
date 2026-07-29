---
name: beacon-trek-review
description: Trek scope 内の task が review 対象 state (= leader_review / user_review) に遷移した時、 leader が forced 3-択 (approve / re-work / forward-to-user) で review する Skill。 approve は思想/目的達成レビューを課して leader_review→user_review へ倒すターミナル化境界 (ms-128 e-4370)。 ms-75 / e-2048 の leader 強制 review 経路 (状態集合 = block / todo / working / leader_review / user_review、 done は Trek 外)。
version: 0.2.0
triggers:
  - /beacon-trek-review
  - trek-task-review
  - Trek task review
  - Trek レビュー
---

# Beacon Trek Review (leader 強制 review Skill)

> ms-75 / e-2048 で導入された Trek task state machine の **leader 側強制経路**。 executor が task を review 対象 state (= `leader_review` / `user_review`) に遷移させた瞬間、 または server-side TTL safety net が `working → leader_review` を強制した瞬間に、 server が leader へ `trek-task-review` channel の DM を送る。 leader はこの Skill を起動して **3 択を必ず選ぶ** ことで、 「review を後回しにして scope が滞留する」 病理を構造的に防ぐ。

## 状態語彙 (= ms-128 方針5、 done は Trek 外)

Trek task の状態集合 = `{block, todo, working, leader_review, user_review}` (`lib/trek.py: VALID_TASK_STATES`)。**done は状態集合に無い** — done = 配置 = 顧客到達 = 人間のデプロイ判断境界で、 Trek の外の 1 レイヤー上。Trek は user_review で打ち止める (「手前まで運ぶ」、 方針5 / e-4366)。

| state | 意味 | 種別 |
|-------|------|------|
| `block` | 依存先 (blocker) 未解決で着手不能。全 blocker が leader_review 到達で server が自動 todo 復帰 | 非 terminal |
| `todo` | 未着手 | 非 terminal |
| `working` | 実行中 (executor が claim して作業) | 非 terminal |
| `leader_review` | **leader 判断要請** — executor 自発 (完成 PR 化) or server 強制 (halt 検知) | review 対象 |
| `user_review` | **Trek 完遂の打ち止め** — leader が思想/目的達成レビュー合格で自律 stamp。user の最終確認を待つ受動キュー | review 対象 / **唯一の terminal** |

> `set_task_state` は旧 `waiting-review` を `leader_review` に、 `done` を `user_review` に read-time migrate する (= 「Trek 内で done にしようとする」試みは user_review に吸収、 過去 stamp は遡行変更しない)。 review が発火する state は `REVIEW_TRIGGER_STATES = (user_review, leader_review)`。leader が取れる遷移は `leader_review → {user_review, working}` のみ (done へは書けない)。

## いつ起動するか

以下のいずれか:

1. **bus inbox に `channel=trek-task-review`, `delivery=auto-execute` の event が届いた**: payload に `trek_id` + `task_id` + `state` (= `leader_review` / `user_review`) + `note` + `updated_by_session_id` が入っている。 `auto_stalled=true` なら server-side TTL 罰則由来 (= executor が pulse を怠って `working → leader_review` に強制遷移した) の可能性が高い。
2. **user が `/beacon-trek-review <trek-id> <task-id>` を直接呼ぶ**: 動作確認 / 過去 transition の追跡用。

armed leader は (1) で自動的に Skill を起動する (= /beacon-bus-armed Skill 4.2 表の trek-task-review channel 分岐に対応)。

## 前提条件チェック

```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
`NO_BEACON` なら終了。

## Step 0: コンテキスト確定

inbox hook の additionalContext または引数から `trek_id`, `task_id`, `state`, `note`, `updated_by_session_id`, `auto_stalled` を抽出。 引数経由なら必要に応じて `beacon trek show <trek_id>` で task_state を再確認。

## Step 1: review 対象の文脈収集

Bash で並列実行:

```bash
beacon trek show <trek_id> --json
beacon task list --json -m <ms_id>     # task の所属 MS を trek scope から推定 (task 系は -m。--ms は milestone 専用で silent 無視される)
git log --oneline -10                    # executor の作業履歴
```

executor の note + 最近 commit + task description を読み、 3 つの判断材料を組み立てる:
- 何を完成させて leader へ渡そう (`leader_review`) / user へ渡そう (`user_review`) としているか
- 自律完結可能か (= AC が機械的に検証できるか)
- user 判断が要るか (= 嗜好 / 不可逆 / cross-Trek 副作用)

`auto_stalled=true` の場合は特に注意: executor が黙って止まった (= pulse 不発火) 可能性があるので、 その task が本当に完了したのか / 途中で止まっただけなのかを commit と note で見極める (= false-positive なら re-work で `working` に戻す)。

## Step 2: 3 択を必ず提示 (= forced 3-choice、 後回し禁止)

ユーザーには **次の 3 つの選択肢を明示し、 review action を 1 つ選んで実行する**。 「後で見る」 は許可しない (= state machine 強制の核心):

### Option A: approve (= ターミナル化境界、 leader_review → user_review、 e-4370 / AC6)

executor の完成宣言を妥当と認め、 task を **`user_review` に stamp** する (done ではない、 方針5)。この leader_review → user_review 遷移が **ターミナル化境界** で、 leader は stamp 前に **思想レビューと目的達成レビューを課す** (= AX / 保守性は PR 作成時に実行セッションが実施済み。leader は取り込み境界でなく配置手前の境界を守る、 ms-80 の役割分担と一致):

```bash
# 1. 思想レビュー — 実装が SPEC / vision の思想通りか、 文脈ゼロの独立 judge に問う
/beacon-review-run  # 引数: --type philosophy --pr <n> --origin-doc <対象 MS の SPEC doc-id>
# 2. 目的達成レビュー — target の受入条件が満たされた証拠を独立生成 (verdict=達成かは leader 判断)
/beacon-review-run  # 引数: --type attainment --target <ms-XX>
```

両レビューの findings を読み、 思想 drift 無し + 目的達成の証拠十分と leader が判断したら user_review に stamp:

```bash
beacon trek task-state <trek_id> <task_id> user_review --note "<思想 OK / 目的達成 OK の根拠 1 行>"
```

思想 drift / 未達成が見つかったら Option B (re-work) に倒す。 レビューを skip して user_review に stamp してはならない (= ターミナル化境界の意味が消える)。

### Option B: re-work (= working に戻す + 理由 DM)

executor の宣言を retract、 task を `working` に戻して追加作業を要求する。 executor に DM で 「re-work 理由 + 期待する追加 deliverable」 を送る。

```bash
beacon trek task-state <trek_id> <task_id> working --note "<re-work 理由>"
# DM で executor (= updated_by_session_id) に re-work 要請
beacon bus send --channel dm --to <updated_by_session_id> --payload '{"text": "[Re-work 要請] task <task_id> を working に戻しました。 理由: <...>。 追加で <...> を満たしてから再度 leader_review 宣言してください。"}' --json
```

### Option C: forward-to-user (= user 介入要請、 leader → user escalation)

leader 判断としても user 嗜好 / 不可逆 / cross-Trek 副作用に該当する場合、 user に判断を投げる。 task を `user_review` に遷移させ、 user 応答待ちで保留 (= `user_review` は terminal 扱いなので scheduler は当該 Trek への progress-check 配信を止める)。

```bash
beacon trek task-state <trek_id> <task_id> user_review --note "<user escalation 理由>"
# user に提示 (= 通常対話 turn で 3 択を再構成して聞く)
# 「task <task_id> が user_review 状態になりました。 内容: <executor note>。 続行可否を判断してください: re-work / accept / cross-MS 切り替え」
```

### 「後で見る」 を許可しない

leader は review DM 受信から **次の action 開始までに 1 つを選ぶ義務**。 後回しは Trek scope 内の他 task に波及して全体停滞の原因になる。 budget gate が引っかかる場合は user に 「budget grant + 即 review 実行」 を提案する (= ただし Trek 内 DM は e-4116 で budget bypass 対象なので、 通常は枯渇しない)。

## Step 3: 実行 + 結果報告

選んだ option を実行し、 結果を user に 1-2 行で報告:

```
Trek <trek_id> task <task_id> review 完了:
  state: <new state = user_review / working>
  action: <approve / re-work / forward-to-user>
  根拠: <leader 判断の 1 行要約>
```

## Step 4: 完遂 handoff (= 全 Target が user_review、 leader 単独が撃つ、 e-4370 / AC6)

review 後に trek の全 Target を集計し、 状態に応じて分岐する。**完遂 handoff (= リーダー→ユーザー「手前まで完了、あなたの番」通知) はリーダー単独が撃つ** (= 台帳所有者、 方針9)。実行セッションは完遂を判定しない — これで「あるセッションが完遂を撃った瞬間、別セッションが未 claim Target を claim して完遂が偽になる」race を防ぐ。

```bash
beacon trek show <trek_id> --json で task_states を集計
```

- **全 Target が `user_review` (= 完遂)** → **完遂 handoff を撃つ**:
  1. user へ通知 (= 非ブロッキング、 AskUserQuestion で止めない):
     ```bash
     beacon bus send --channel notify --delivery notify-user-only \
       --payload '{"text": "Trek <trek_id> 完遂: 全 Target が user_review (= 手前まで完了) に達しました。あなたの番です — 各 Target の最終確認 / デプロイをご判断ください。archive は /beacon-trek-finalize で。"}'
     ```
  2. 二重発火を防ぐため stamp (= completion_notified と組で leader-digest tick を止める、 ms-97 AC21):
     ```bash
     beacon trek summary-sent <trek_id>
     ```
  3. archive は user 確認境界 (= 自律 archive 禁止)。`/beacon-trek-finalize` を案内する。
- **全 terminal でないが leader_review / working / todo / block が残る** → 「review 続行、 残り <N> Target」と報告し、 handoff は撃たない。

## 制約

- **必ず 3 択を 1 つ選ぶ** (= 「後で」 を出さない、 leader bottleneck 病理の構造解消)
- `task-state` 書き換えは leader 権限 (= server-side で leader role + leader_session_id 一致を検証)
- forward-to-user 時、 user の response を待つ間 task は `user_review` のまま、 scheduler は当該 Trek への progress-check 配信停止
- 状態集合は `block / todo / working / leader_review / user_review` の 5 つのみ。 `done` は Trek 外 (= 書けない、 read-time に user_review へ migrate) / 旧 `waiting-review` も使わない (= `set_task_state` が拒否 / migrate する)
- **approve は思想/目的達成レビューを課してから user_review に stamp する (= ターミナル化境界、 e-4370)。レビュー skip 禁止**
- **完遂 handoff (全 Target user_review → user 通知) はリーダー単独が撃つ。 executor は完遂を判定しない (= race 防止、 方針9)**
- Trek archive は user 確認境界、 自動実行しない (= CLAUDE.md 規約、 `/beacon-trek-finalize` 経由)
