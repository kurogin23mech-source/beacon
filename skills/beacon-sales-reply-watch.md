---
name: beacon-sales-reply-watch
description: 時間に敏感なやり取り（日程打診など）で「相手にボールがあり watch 中」のスレッドを高頻度で確認し、返信が来ていたら証跡(Communication)を残してボールを自分に戻し、人に通知する。検知に徹し、返信自体はしない。定期発火(hourly Operation)で自律実行、または「返信チェック」で手動起動。営業専用。
version: 1.0.0
triggers:
  - /beacon-sales-reply-watch
  - 返信チェック
  - 返信ウォッチ
  - 返信来てないか確認
---

# Beacon Sales Reply Watch (返信ウォッチャー E)

> ms-107 e-3437。日次取込 (D) は遅いので、日程打診など時間に敏感なやり取りだけを
> **高頻度 (hourly)** で拾う運用層。「相手にボールがあり (ball=相手) watch を立てた」
> スレッドだけを確認し、返信が来ていたら **証跡 (Communication) を1件残して ball を
> 自分に戻し、人に通知**する。
>
> **検知に徹する — 返信自体はしない** (SPEC §3 / この Skill の AC)。次にどう返すかの
> 判断と送信は人 (または `/beacon-sales-email`)。検知は機械・判断境界は人。自律送信を
> しないので cross-user consent (ms-110) の境界も踏まない。
>
> 実行形態: 実際の確認は client セッションの Skill (MCP でメール/Slack を見るため)。
> 定期発火は E を Operation として立て、server tick が hourly に起こす (tick は
> target 非依存の primitive、Operation はその利用者)。手動起動でも同じ動作。

## 文章の書き方 (Beacon 全体の哲学)

通知は非開発者が 1 行で「誰から返信が来て、次に何をすべきか」を掴める自然な日本語で。

## 前提条件チェック

Bash ツールで営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" \
  && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` なら「営業プロジェクトでのみ使えます」と伝えて終了。内部コマンドは
`python3 "$ROOT/lib/commands.py" <cmd>` で呼ぶ (watch は user 向け CLI 動詞ではない)。

## Step 0: quiet hours の尊重

まず現在時刻を取得 (カレンダー MCP の `get-current-time`、無ければシステム時刻)。
**quiet hours (既定 20:00–08:00) はメール確認・通知をスキップ**し、「quiet hours の
ためスキップ」とだけ記録して終了する。相手の生活時間に無配慮な深夜チェックを避ける
(SPEC AC: quiet hours を尊重)。quiet hours はプロジェクト設定で上書き可能。

## Step 1: 確認対象スレッドの取得

「watch あり かつ ball=相手 (= まだ返信待ち)」のスレッドだけを取る:

```bash
BEACON_WATCH_AWAITING=1 BEACON_JSON=1 python3 "$ROOT/lib/commands.py" watch_list
```

`watches[]` が空なら「返信待ちのスレッドはありません」と記録して終了。各要素は
`work_item_id` (act-/nrt-) / `target_id` (opp-/acc-) / `channel` / `thread_ref` /
`last_checked_at` を持つ。これが確認の対象。

## Step 2: 各スレッドの返信確認 (MCP)

各スレッドについて、`channel` に応じた MCP で `thread_ref` のスレッドに **last_checked_at
以降の相手からの新着**があるか確認する:

- **email**: 送信アカウント台帳から gmail route を解決 (`/beacon-sales-email` と同じ経路、
  手書きしない)。`thread_ref` (thread-id / message-id) のスレッドを検索し、こちらの最後の
  送信より後に相手からの返信があるかを見る。
- **slack**: 該当スレッドの新着返信を見る。
- 判定できない (thread_ref 無し等) スレッドは skip し、報告に「thread 情報不足」で残す。

相手の生活時間を跨いだ連投等の誤検知を避けるため、**新着があったスレッドだけ** Step 3 へ。

## Step 3: 返信があったら証跡を残して ball を戻す (検知のみ)

新着があったスレッドは、その内容を **1 行に要約**して inbound の Communication として
残す。work item (act-/nrt-) を対象にすると、その予定に紐づく実績として記録され、
**ball が自動で自分に戻る** (derive_ball が inbound で flip):

```bash
BEACON_COMM_TARGET="<work_item_id>" \
  BEACON_COMM_SUMMARY="<相手の返信の1行要約>" \
  BEACON_COMM_DIRECTION="inbound" \
  BEACON_COMM_CHANNEL="<channel>" \
  BEACON_COMM_SOURCE_REF="<message-id>" BEACON_COMM_SOURCE_URL="<permalink>" \
  BEACON_COMM_OCCURRED="<返信の時刻>" \
  python3 "$ROOT/lib/commands.py" communication_add
```

ball が自分に戻ると、そのスレッドは次回 Step 1 の「返信待ち」から外れる (= 二重に拾わ
ない)。**ここで返信はしない** — 次にどう動くかは人の判断。

話題が完結した (例: 面談日程が確定した) と読み取れる場合は、watch を落として見張りを
終える:

```bash
BEACON_WATCH_TARGET="<work_item_id>" python3 "$ROOT/lib/commands.py" watch_clear
```

完結かどうか機械で決めきれない時は watch を残し、報告で人に委ねる。

## Step 4: 通知と記録

返信が来たスレッドを人に通知する (これが E の主目的 — 時間に敏感な返信を素早く可視化):

```
📬 返信が届いています (あなたのボールに戻りました)
  [work_item_id] <相手/用件>: <返信の1行要約>
    → 次の一手: <日程確定なら watch 終了 / 追加打診なら /beacon-sales-email>
  … 他 N 件
  確認したが返信なし: M 件 (次の tick で再確認)
```

自律実行 (server tick 由来) の場合、run record として残す配線は E の Operation 側が
担う。異常 (メール MCP 認証切れ等) は incident として起票する余地を残す。

## 制約

- **検知に徹する — 返信/送信はしない** (SPEC §3)。ball を戻して人に渡すところまで。
- **quiet hours を尊重** (既定 20:00–08:00 はスキップ)。
- **使うメール/Slack アカウントは台帳解決から取る** (手書きしない、取り違え防止 e-3365)。
- **watch あり かつ ball=相手 のスレッドだけ確認** (SPEC §3、無駄打ち防止)。ball が
  自分に戻ったスレッドは自動で対象外 (二重検知しない)。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。
- 日次の網羅取込は D (`/beacon-sales-standup-intake`)。E は時間敏感な返信待ちだけの
  高頻度確認で、別 cadence・別役割。

## 関連

- `/beacon-sales-email` — 返信必須メール送信時に watch を arm する側 (E の入口)
- D 一括取込 (e-3436) — 日次網羅。E は hourly の返信待ちだけ
- tick primitive (`lib/tick_scheduler`) — E を hourly 発火させる target 非依存の定期起動
- SPEC `o83GEljD8xeFMr95wLTh` §3 (ms-107 実運用 engine)
