---
name: beacon-trek-review
description: Trek scope 内の task が review 対象 state (= done / user_review / leader_review) に遷移した時、 leader が forced 3-択 (approve / re-work / forward-to-user) で review する Skill。 ms-75 / e-2048 の leader 強制 review 経路 (ms-88 5-state 語彙: todo / working / leader_review / user_review / done)。
version: 0.2.0
triggers:
  - /beacon-trek-review
  - trek-task-review
  - Trek task review
  - Trek レビュー
---

# Beacon Trek Review (leader 強制 review Skill)

> ms-75 / e-2048 で導入された Trek task state machine の **leader 側強制経路**。 executor が task を review 対象 state (= `done` / `user_review` / `leader_review`) に遷移させた瞬間、 または server-side TTL safety net が `working → leader_review` を強制した瞬間に、 server が leader へ `trek-task-review` channel の DM を送る。 leader はこの Skill を起動して **3 択を必ず選ぶ** ことで、 「review を後回しにして scope が滞留する」 病理を構造的に防ぐ。

## 5-state 語彙 (= ms-88 e-2107、旧 2-state からの移行)

Trek task は 5 状態を取る (`lib/trek.py: VALID_TASK_STATES`):

| state | 意味 | 種別 |
|-------|------|------|
| `todo` | 未着手 | 非 terminal |
| `working` | 実行中 (executor が claim して作業) | 非 terminal |
| `leader_review` | **leader 判断要請** — executor 自発 or server 強制 (= TTL / pulse 不発火罰則) | review 対象 |
| `user_review` | **user 判断要請** — 嗜好 / 不可逆 / cross-Trek 副作用で human へ escalation | review 対象 / terminal |
| `done` | 完遂 | review 対象 / terminal |

> 旧 2-state (`done` / `waiting-review`) は撤去済。 `waiting-review` は `leader_review` (leader 判断) と `user_review` (user 判断) の 2 つに分割された (= conflate 解消、 e-2107)。 `set_task_state` は新コードに対して `waiting-review` を拒否する。 review が発火する state は `REVIEW_TRIGGER_STATES = (done, user_review, leader_review)`。

## いつ起動するか

以下のいずれか:

1. **bus inbox に `channel=trek-task-review`, `delivery=auto-execute` の event が届いた**: payload に `trek_id` + `task_id` + `state` (= `done` / `user_review` / `leader_review`) + `note` + `updated_by_session_id` が入っている。 `auto_stalled=true` なら server-side TTL 罰則由来 (= executor が pulse を怠って `working → leader_review` に強制遷移した) の可能性が高い。
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
beacon task list --json --ms <ms_id>   # task の所属 MS を trek scope から推定
git log --oneline -10                    # executor の作業履歴
```

executor の note + 最近 commit + task description を読み、 3 つの判断材料を組み立てる:
- 何を完了 (`done`) / leader へ渡そう (`leader_review`) / user へ渡そう (`user_review`) としているか
- 自律完結可能か (= AC が機械的に検証できるか)
- user 判断が要るか (= 嗜好 / 不可逆 / cross-Trek 副作用)

`auto_stalled=true` の場合は特に注意: executor が黙って止まった (= pulse 不発火) 可能性があるので、 その task が本当に完了したのか / 途中で止まっただけなのかを commit と note で見極める (= false-positive なら re-work で `working` に戻す)。

## Step 2: 3 択を必ず提示 (= forced 3-choice、 後回し禁止)

ユーザーには **次の 3 つの選択肢を明示し、 review action を 1 つ選んで実行する**。 「後で見る」 は許可しない (= state machine 強制の核心):

### Option A: approve (= 受領、 完了確定)

executor の宣言を妥当と認める。 task を `done` に確定する (= review 対象が `leader_review` / `user_review` でも、 leader が完遂と認めたら `done` へ)。 全 scope task が `done` になったら Trek 自体を archive 提案する。

```bash
beacon trek task-state <trek_id> <task_id> done --note "<approve 理由>"
```

### Option B: re-work (= working に戻す + 理由 DM)

executor の宣言を retract、 task を `working` に戻して追加作業を要求する。 executor に DM で 「re-work 理由 + 期待する追加 deliverable」 を送る。

```bash
beacon trek task-state <trek_id> <task_id> working --note "<re-work 理由>"
# DM で executor (= updated_by_session_id) に re-work 要請
beacon bus send --channel dm --to <updated_by_session_id> --payload '{"text": "[Re-work 要請] task <task_id> を working に戻しました。 理由: <...>。 追加で <...> を満たしてから再度 done / leader_review 宣言してください。"}' --json
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
  state: <new state = done / working / user_review>
  action: <approve / re-work / forward-to-user>
  根拠: <leader 判断の 1 行要約>
```

## Step 4: 全 task review 完了時の Trek archive 提案

trek の他 task を集計し、 状態に応じて提案する (= CLAUDE.md 規約に従い、 archive 自体は user 確認境界):

```bash
beacon trek show <trek_id> --json で task_states を集計
全 done なら:
  「Trek <trek_id> の全 task が done です。 archive しますか? (= beacon trek archive <trek_id>)」
全 terminal (= done / user_review) だが user_review を含むなら:
  「Trek <trek_id> は全 task terminal だが user_review が残っています。 user 判断完了まで保留」
leader_review / working / todo が残るなら:
  「review 続行、 残り <N> task」
```

## 制約

- **必ず 3 択を 1 つ選ぶ** (= 「後で」 を出さない、 leader bottleneck 病理の構造解消)
- `task-state` 書き換えは leader 権限 (= server-side で leader role + leader_session_id 一致を検証)
- forward-to-user 時、 user の response を待つ間 task は `user_review` のまま、 scheduler は当該 Trek への progress-check 配信停止
- 新 state 語彙は `todo / working / leader_review / user_review / done` の 5 つのみ。 旧 `waiting-review` は使わない (= `set_task_state` が拒否する)
- Trek archive は user 確認境界、 自動実行しない (= CLAUDE.md 規約)
