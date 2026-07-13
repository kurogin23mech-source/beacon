---
name: beacon-sales-schedule
description: 商談の次のアポイントを、空いている時間から候補を出して調整し、確定したら Google カレンダーに予定を作成、商談に活動記録として残す。「アポ取って」「日程調整」「次の面談」等で起動。
version: 1.0.0
triggers:
  - /beacon-sales-schedule
  - アポ
  - 日程調整
  - 次の面談
  - 次のアポ
---

# Beacon Sales Schedule

> 営業 (profession=sales) プロジェクトで、商談 (Opportunity) の次のアポ (面談) を取る。
> ms-107 e-3355。相手の都合を聞く前提で候補日時を出し、確定したら Google カレンダーに
> 予定を作り、商談に活動記録 (証跡) を残す。
> 予定作成の自律度は **候補提示まで**、カレンダーへの登録は人間承認を経る (制約参照)。

## 文章の書き方 (Beacon 全体の哲学)

相手に見せる候補日時や、メール連携時の文面は、社外の相手が 1 度読んで分かる自然な
日本語で書く。社内の略語・横文字を持ち込まない。日時は「7/15 (火) 14:00〜15:00」の
ように曜日込みで、取り違えの起きない形にする。

## 前提条件チェック

Bash ツールで実行し、営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && BEACON_JSON=1 python3 "$ROOT/lib/commands.py" account_list >/dev/null 2>&1 && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` の場合 (= 営業テンプレートでないプロジェクト)、この Skill は「営業プロジェクトでのみ使えます」と伝えて終了する。cloud mode で `project.json` を直接読めない場合は `beacon opportunity list` が動くかで代替判定してよい。

以降、`$ROOT` は `beacon-find-root` の出力。内部コマンド (`opportunity_activity`) は
ユーザー向け CLI 動詞ではないので `python3 "$ROOT/lib/commands.py" <cmd>` で呼ぶ。

## Step 1: 対象商談の特定

Bash ツールで商談一覧を取得し、ユーザーにどの商談のアポかを確認する:

```bash
beacon opportunity list
```

引数で `/beacon-sales-schedule opp-3` のように商談 ID が渡されていればそれを採用。
無ければ一覧を提示し「どの商談のアポですか？」と 1 問だけ聞く。対象を `$OPP` として保持。

商談の相手 (顧客 Account とその担当者 Contact) を把握するため account 一覧も引く。
相手の氏名・連絡先は候補送付やカレンダー予定のタイトルに使う:

```bash
beacon account list
```

## Step 2: 空き時間の確認

まず今日時点の日時を取得し、いつ以降で探すかの基準にする:

Google カレンダー MCP の `mcp__google-calendar__get-current-time` で現在日時を取得する。

続いて `mcp__google-calendar__get-freebusy` で自分の空きを取得する。探索する期間は
ユーザーの希望 (例:「来週」) に合わせ、無指定なら今日以降の直近 1〜2 週間を見る。

> 参考: `briefing-manager` Skill が同じ Google カレンダー MCP (freebusy / create) を
> 使っており、そのカレンダー操作パターンを流用できる。ただし本 Skill は自己完結で書く。

## Step 3: 候補の提示 → 相手都合の確認

freebusy の空きから、面談に使えそうな時間帯を **2〜3 個**、曜日込みで提示する
(例:「7/15 (火) 14:00〜15:00 / 7/16 (水) 10:00〜11:00 / 7/17 (木) 16:00〜17:00」)。

この候補はあくまで**相手に投げる叩き台**。ユーザーに見せ、次のどれかに進む:

- ユーザーが相手にこのまま確認する → 相手の返事を待って Step 5 へ。
- 相手にメールで候補を送りたい → **自動送信はしない**。「メールで候補を送りますか？
  その場合は `/beacon-sales-email` で送れます」と `/beacon-sales-email` への連携を促す。
- ユーザーがその場で日時を確定できる → Step 5 へ。

## Step 4: 日時の確定 (人間が決める)

相手都合が分かり、ユーザーが 1 つの日時を確定したら、その日時 (開始・終了・場所/
オンライン別) を `$WHEN` として整理する。AI が勝手に確定しない — 確定はユーザーの言葉で。

## Step 5: 予定作成 (人間承認後)

確定した日時をユーザーに提示し、**カレンダーに登録してよいか明示的に確認**する。
AI が自律でカレンダーに入れてはならない (制約参照)。

```
以下の予定をカレンダーに登録します:
  商談:  [OPP]
  相手:  [Account / Contact]
  日時:  [WHEN]
  場所:  [場所 / オンライン]

登録しますか？ (登録する / 直す / やめる)
```

ユーザーが「登録する」と答えたら、Google カレンダー MCP の
`mcp__google-calendar__create-event` で予定を作る。タイトルは相手と用件が分かる形
(例:「[商談] ○○社 △△様 面談」)、説明に商談 ID を残す。「直す」なら Step 3 に戻り、
「やめる」なら中止する。

> 補足: 仕事用/個人用など複数の Google アカウントがある場合、営業用のカレンダーに
> 入れる。どのアカウント/カレンダーに入れるかの精緻化 (identity 照合) は今後の課題。

## Step 6: 活動記録 (証跡) を必ず残す

予定を作れたら、対象商談にアポ確定を活動記録として残す。これを飛ばすと「いつ次に
会うか」を後で辿れなくなるため必須 (beacon-sales-email と同じ内部コマンド経路):

```bash
BEACON_OPP_ID="$OPP" BEACON_ACTIVITY_DESC="[アポ確定] <日時> <相手/場所>" \
  python3 "$ROOT/lib/commands.py" opportunity_activity
```

## Step 7: 結果報告

ユーザーに簡潔に報告:

```
📅 予定を登録しました ([WHEN] / [相手])
  商談 [OPP] に活動記録を残しました。
```

登録を止めた場合 (ユーザーが「やめる」／相手都合が付かず保留) は、登録しなかった旨と
理由を報告する。

## 制約

- **予定の作成は人間承認を経る** (AI 自律は候補提示まで、勝手にカレンダーへ入れない)。
- 相手へのメール送信は自動でしない (`/beacon-sales-email` に連携、送信はそちらの承認経路)。
- **予定を作れたら必ず Step 6 の活動記録を残す** (証跡を欠かさない)。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。
- 複数 Google アカウント (仕事用/個人用) に注意し、営業用のカレンダーに入れる
  (アカウント選択の精緻化は今後の課題)。
- 相手に見せる文面・日時は非開発者が読める自然な日本語で (社内略語を持ち込まない)。
