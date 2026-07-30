---
name: beacon-sales-reply-watch
description: 時間に敏感なやり取り（日程打診など）で「相手にボールがあり watch 中」のスレッドを高頻度で確認し、返信が来ていたら証跡(Communication)を残してボールを自分に戻し、人に通知する。検知に徹し、返信自体はしない。定期発火(hourly Operation)で自律実行、または「返信チェック」で手動起動。営業専用。
version: 1.0.0
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
`python3 "$(beacon _lib-path)/commands.py" <cmd>` で呼ぶ (watch は user 向け CLI 動詞ではない)。

## Step 1: 確認対象スレッドの取得

「watch あり かつ ball=相手 (= まだ返信待ち)」のスレッドだけを取る:

```bash
BEACON_WATCH_AWAITING=1 BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" watch_list
```

`watches[]` が空なら「返信待ちのスレッドはありません」と記録して終了。各要素は
`work_item_id` (act-/nrt-) / `target_id` (opp-/acc-) / `channel` / `thread_ref` /
`last_checked_at` を持つ。これが確認の対象。

## Step 1.5: アタックリスト打診先も対象に含める (ms-132 e-4505)

一括連絡 (`/beacon-sales-outreach`) で打診した先も返信待ちの対象。施策 (Acquisition) 配下の
各アタックリストについて、**連絡済 (= 送信したがまだ返信が無い) 行**を worklist として取る:

```bash
# 施策一覧 → 各施策のリスト doc-id → 返信待ち行
beacon acquisition attack-lists <acq-id> --json
BEACON_DOC_ID=<doc-id> BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" acquisition_attack_list_awaiting_reply
```

`awaiting[]` の各要素は `acc_id` / `email` / `message_id` (打診時に送ったメールの id) を
持つ。これらも Step 2 の「返信確認」対象に加える (email チャネルで、その `message_id` の
スレッドに新着相手メッセージがあるか)。

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

**スレッド集約の原則 (e-3535)**: 対象は **watch を立てた元の work item (act-/nrt-)** を
そのまま使う (= このスレッドの起点)。返信のたびに新しい活動を作らない — 1 スレッドの往復
証跡は 1 活動配下に集約する。watch は元々スレッド起点に立っているので、通常はこれを守れば
自然に集約される。

```bash
BEACON_COMM_TARGET="<work_item_id>" \
  BEACON_COMM_SUMMARY="<相手の返信の1行要約>" \
  BEACON_COMM_DIRECTION="inbound" \
  BEACON_COMM_CHANNEL="<channel>" \
  BEACON_COMM_SOURCE_REF="<message-id>" BEACON_COMM_SOURCE_URL="<permalink>" \
  BEACON_COMM_OCCURRED="<返信の時刻>" \
  python3 "$(beacon _lib-path)/commands.py" communication_add
```

ball が自分に戻ると、そのスレッドは次回 Step 1 の「返信待ち」から外れる (= 二重に拾わ
ない)。**ここで返信はしない** — 次にどう動くかは人の判断。

**アタックリスト打診先の返信 (Step 1.5 由来、ms-132 e-4505)**: `communication_add` を直に
叩く代わりに、専用の記録動詞を使う。これは inbound 証跡を Account に残すのと同時に、対応する
アタックリストの行を **連絡済 → 返信あり** に進め、人間に通知 (trigger) する:

```bash
beacon acquisition attack-list-reply-record <doc-id> <acc-id> \
  --message-id "<返信の message-id>" --url "<permalink>" --summary "<返信の1行要約>"
```

これも **検知のみ** — 返信自体はしない。行が返信ありに進むと次回 Step 1.5 の「連絡済」から
外れる。リード化 (商談への引き上げ) はこの後、人の判断で `/beacon-...` により行う。

話題が完結した (例: 面談日程が確定した) と読み取れる場合は、watch を落として見張りを
終える:

```bash
BEACON_WATCH_TARGET="<work_item_id>" python3 "$(beacon _lib-path)/commands.py" watch_clear
```

完結かどうか機械で決めきれない時は watch を残し、報告で人に委ねる。

## Step 3.5: 返信意図の分類 (bounded、 ms-106 e-3534)

返信を検知したら、返信意図を **小さな閉集合 (4 値) のどれか** に分類する。分類までが
E の役割で、**返信・実行はしない** (次の動作は人 or 専用スキル)。日程打診スレッドの返信は
特にこの 4 値に収まる:

- **枠承諾** — こちらが提示した面談枠のどれかを相手が承諾した。
- **再提案** — 相手が別日時を希望 / 枠の再提示を求めている。
- **辞退** — 相手が見送り / 断り。
- **質問** — 内容確認・条件の問い合わせ。

分類は Step 4 の通知に添えるだけ (soft な routing 提案)。分類が bounded かつ、後続の
外向き送信がすべて人間承認なので、誤分類しても相手に誤爆は届かない (backstop)。

## Step 4: 通知と記録

返信が来たスレッドを人に通知する (これが E の主目的 — 時間に敏感な返信を素早く可視化)。
Step 3.5 の分類に応じて **次の一手** を添える:

```
📬 返信が届いています (あなたのボールに戻りました)
  [work_item_id] <相手/用件> [<分類: 枠承諾/再提案/辞退/質問>]: <返信の1行要約>
    → 次の一手:
        枠承諾 → /beacon-sales-meeting-confirm (面談確定。記帳は自動・確定返信は承認付き)
        再提案 → /beacon-sales-schedule (候補を出し直す)
        辞退   → 商談フェーズの見直し (決着 or 保留を人が判断)
        質問   → /beacon-sales-email (回答を下書き→承認→送信)
  … 他 N 件
  確認したが返信なし: M 件 (次の tick で再確認)
```

ルーティングは AI 判断の **提案** であり、実行 (面談確定 / 返信送信) は各スキル側の
人間承認を通る。E 自身は検知と分類に徹し、送信はしない。

自律実行 (server tick 由来) の場合、run record として残す配線は E の Operation 側が
担う。異常 (メール MCP 認証切れ等) は incident として起票する余地を残す。

## 制約

- **検知に徹する — 返信/送信はしない** (SPEC §3)。ball を戻して人に渡すところまで。
- **使うメール/Slack アカウントは台帳解決から取る** (手書きしない、取り違え防止 e-3365)。
- **watch あり かつ ball=相手 のスレッドだけ確認** (SPEC §3、無駄打ち防止)。ball が
  自分に戻ったスレッドは自動で対象外 (二重検知しない)。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。
- 日次の網羅取込は D (`/beacon-sales-standup-intake`)。E は時間敏感な返信待ちだけの
  高頻度確認で、別 cadence・別役割。

## 関連

- `/beacon-sales-meeting-confirm` — 枠承諾を受けて面談を確定する側 (E → 面談確定の受け渡し先)
- `/beacon-sales-email` — 返信必須メール送信時に watch を arm する側 (E の入口)
- D 一括取込 (e-3436) — 日次網羅。E は hourly の返信待ちだけ
- tick primitive (`lib/tick_scheduler`) — E を hourly 発火させる target 非依存の定期起動
- SPEC `o83GEljD8xeFMr95wLTh` §3 (ms-107 実運用 engine)
