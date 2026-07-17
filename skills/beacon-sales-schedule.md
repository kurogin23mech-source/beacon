---
name: beacon-sales-schedule
profession: sales
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
> ms-107 e-3355 / e-3433。相手の都合を聞く前提で候補日時を出し、確定したら Google
> カレンダーに予定を作り、**同じ日時で Beacon の遷移日も更新し、予定に Beacon 識別 ID を
> 埋め込んだ Meeting レコードを残す** (遷移日とカレンダーの二重管理・ズレを構造的に防ぐ /
> 後段の終了検知が識別 ID で商談を突き合わせられる)。商談に活動記録 (証跡) も残す。
> 予定作成の自律度は **候補提示まで**、カレンダーへの登録は人間承認を経る (制約参照)。

## 文章の書き方 (Beacon 全体の哲学)

相手に見せる候補日時や、メール連携時の文面は、社外の相手が 1 度読んで分かる自然な
日本語で書く。社内の略語・横文字を持ち込まない。日時は「7/15 (火) 14:00〜15:00」の
ように曜日込みで、取り違えの起きない形にする。

## 前提条件チェック

Bash ツールで実行し、営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" account_list >/dev/null 2>&1 && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` の場合 (= 営業テンプレートでないプロジェクト)、この Skill は「営業プロジェクトでのみ使えます」と伝えて終了する。cloud mode で `project.json` を直接読めない場合は `beacon opportunity list` が動くかで代替判定してよい。

以降、`$ROOT` は `beacon-find-root` の出力。内部コマンド (`opportunity_activity`) は
ユーザー向け CLI 動詞ではないので `python3 "$(beacon _lib-path)/commands.py" <cmd>` で呼ぶ。

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

## Step 2: どのアカウントのカレンダーか — 送信アカウント台帳から解決 (この調整ごとに切替可)

どのカレンダーに入れるかは **送信アカウント台帳** (label→MCP route、e-3365) から引く。
台帳を通さず namespace を手書きしない (= 取り違え防止)。まず台帳を確認:

```bash
BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" sales_account_list
```

- **台帳が空 / calendar route 未設定** の場合、ユーザーに「どの Google アカウントの
  カレンダーで調整しますか？」と確認して登録する (label が既にあれば route だけ足す):

```bash
BEACON_SEND_LABEL="会社" BEACON_SEND_EMAIL="<アドレス>" python3 "$(beacon _lib-path)/commands.py" sales_account_add
BEACON_SEND_LABEL="会社" BEACON_SEND_SERVICE="calendar" \
  BEACON_SEND_NAMESPACE="mcp__google-calendar" BEACON_SEND_ALIAS="work" \
  python3 "$(beacon _lib-path)/commands.py" sales_account_route
```

- 既定 (default label) でよければ `$LABEL` は空のまま。この 1 件だけ別アカウントの
  カレンダーにしたい場合は、ユーザーが選んだ label を `$LABEL` に入れる (real-time 切替)。

calendar の route を台帳から解決する。**これが使うカレンダーの唯一の決定経路**:

```bash
BEACON_SEND_SERVICE="calendar" BEACON_SEND_LABEL="$LABEL" \
  python3 "$(beacon _lib-path)/commands.py" sales_account_resolve
echo "RESOLVE_EXIT=$?"
```

- `RESOLVE_EXIT=0` → JSON `{label,email,namespace,alias}`。`namespace` を `$CALNS`
  (使うカレンダー MCP ツール群。例 `mcp__google-calendar` / `mcp__google-calendar-personal`)、
  `alias` を `$CALACCT` (各ツールの `account` 引数。null なら省略) として保持。
- `RESOLVE_EXIT=1` (BLOCK) → その label の calendar route が台帳に無い。**予定を作らない**。
  上の登録をしてから再開する。

以降、カレンダー MCP を呼ぶときは必ず `$CALNS` のツールを使い、`account: $CALACCT` を渡す
(alias が null のときは `account` 省略)。namespace を別のものに差し替えない。

## Step 3: 空き時間の確認

まず今日時点の日時を取得し、いつ以降で探すかの基準にする:

`$CALNS` の `get-current-time` (例 `mcp__google-calendar__get-current-time`) で現在日時を取得する。

続いて `$CALNS` の `get-freebusy` で自分の空きを取得する (`account: $CALACCT`)。探索する期間は
ユーザーの希望 (例:「来週」) に合わせ、無指定なら今日以降の直近 1〜2 週間を見る。

> 参考: `briefing-manager` Skill が同じ Google カレンダー MCP (freebusy / create) を
> 使っており、そのカレンダー操作パターンを流用できる。ただし本 Skill は自己完結で書く。

## Step 4: 候補の提示 → 相手都合の確認

freebusy の空きから、面談に使えそうな時間帯を **既定 3 枠**、曜日込みで提示する
(例:「7/15 (火) 14:00〜15:00 / 7/16 (水) 10:00〜11:00 / 7/17 (木) 16:00〜17:00」)。
枠数は顧客により 2〜4 もあり得る (お作法は「お願い」であって固定ではない) が、**既定は 3 枠**。

### カレンダーのお作法 (soft guidance / e-3498)

**顧客に提示する枠は、提示と同時にカレンダーへ『仮』で押さえる** (ダブルブッキング防止
＋相手が選んだ枠を即確定できる)。相手が 1 枠を選んで確定したら、その枠を本予定化し
(Step 6)、**残りの仮押さえは解放する**。この「3 枠算出→仮押さえ→提示→確定枠を本予定・
残り解放」の一連が日程調整のお作法。3 枠を出す指示 (メール本文) の作法は email 側
(`/beacon-sales-email` の Step 3) にも書かれており、日程メールは必ず schedule 経由で
組む (email 単発で日程を書くと候補算出も仮押さえも欠ける)。

この候補はあくまで**相手に投げる叩き台**。ユーザーに見せ、次のどれかに進む:

- ユーザーが相手にこのまま確認する → 相手の返事を待って Step 5 へ。
- 相手にメールで候補を送りたい → **自動送信はしない**。「メールで候補を送りますか？
  その場合は `/beacon-sales-email` で送れます」と `/beacon-sales-email` への連携を促す。
- ユーザーがその場で日時を確定できる → Step 5 へ。

## Step 5: 日時の確定 (人間が決める)

相手都合が分かり、ユーザーが 1 つの日時を確定したら、その日時 (開始・終了・場所/
オンライン別) を `$WHEN` として整理する。AI が勝手に確定しない — 確定はユーザーの言葉で。

## Step 6: 予定作成 (人間承認後) — 遷移日 + カレンダー + 識別 ID を束ねる

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

ユーザーが「登録する」と答えたら、以下を **この順で** 行う。ここが e-3433 (B) の核心 —
面談を Beacon の Meeting レコードにも刻み、それを **その商談の前進ゲート (= 次のフェーズへ
進めてよいかを判定する関門) の発火源** に据える。前進ゲートの遷移日 (= フェーズ達成を判定する
予定日) が面談日に揃い、Google カレンダーとは **1 つの識別 ID で束ねる**。後段の終了検知 (C) が
この識別 ID でカレンダー予定 → 商談を突き合わせ、この面談の終了がそのフェーズの判定を促す。

1. **カレンダー予定を作る**: Step 2 で解決した `$CALNS` の `create-event`
   (例 `mcp__google-calendar__create-event`) で予定を作る。`account: $CALACCT` を渡す
   (alias が null なら省略)。タイトルは相手と用件が分かる形 (例:「[商談] ○○社 △△様 面談」)。
   返ってきた **event id** を `$EVENT_ID` として保持する。

2. **Meeting を確定し前進ゲートの遷移日を同時更新**: `--set-transition` 相当
   (`BEACON_MTG_SET_TRANSITION=1`) で **その商談の前進ゲートの遷移日が面談日に同時更新される**
   (遷移日はゲートが持つ = 二重管理を無くす、e-3580)。

   フェーズ入場時に面談アンカー (例「初回面談を実施」) から **予定未定 (status=unscheduled)
   の Meeting が seed 済み**で、かつ **その予定未定 Meeting がその商談の前進ゲートの発火源
   (anchor) として既に紐づいている**ことがある (e-3548 / e-3583)。**新規作成の前に既存を探し、
   あればそれを確定する** — こうすると「確定した面談」と「アポ確定を丸写しした活動」が二重に
   残る事故が構造的に起きず、確定した面談がそのまま前進ゲートの発火源になる。まず対象商談の
   Meeting を引く:

   ```bash
   BEACON_MTG_OPP="$OPP" BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" meeting_list
   ```

   出力の meetings に `status == "unscheduled"` の `mtg-N` があるか確認する。

   - **予定未定 mtg- がある → その同じレコードに日時とカレンダー情報を入れて確定** (新規作成
     しない = 重複が構造的に不可能):

     ```bash
     BEACON_MTG_ID="$MTG_ID" BEACON_MTG_AT="$WHEN_START" BEACON_MTG_END="$WHEN_END" \
       BEACON_MTG_EVENT_ID="$EVENT_ID" BEACON_MTG_CAL_NS="$CALNS" BEACON_MTG_CAL_ACCT="$CALACCT" \
       BEACON_MTG_SET_TRANSITION=1 \
       python3 "$(beacon _lib-path)/commands.py" meeting_reschedule
     ```

   - **予定未定 mtg- が無い (seed されていない商談) → 従来どおり新規に予約**:

     ```bash
     BEACON_MTG_OPP="$OPP" BEACON_MTG_AT="$WHEN_START" BEACON_MTG_END="$WHEN_END" \
       BEACON_MTG_LOCATION="$LOC" BEACON_MTG_EVENT_ID="$EVENT_ID" \
       BEACON_MTG_CAL_NS="$CALNS" BEACON_MTG_CAL_ACCT="$CALACCT" \
       BEACON_MTG_SET_TRANSITION=1 \
       python3 "$(beacon _lib-path)/commands.py" meeting_schedule
     ```

   確定した `mtg-N` が Beacon 識別 ID。埋め込む **識別 ID タグ** は
   `beacon-meeting-id: mtg-N` の形 (`$TAG`)。

3. **カレンダー予定の説明にタグを埋め込む**: `$CALNS` の `update-event` で、手順 1 の
   `$EVENT_ID` の予定の **説明 (description) に `$TAG` を 1 行加える** (`account: $CALACCT`)。
   これで終了検知 (C) がこの予定を商談に紐付けられる。既存の説明文がある場合は末尾に改行 +
   `$TAG` を足す (上書きしない)。

「直す」なら Step 4 に戻り、「やめる」なら中止する (Meeting も作らない)。

> 使うカレンダー (namespace/account) は Step 2 の台帳解決の値をそのまま使い、別のものに
> 差し替えない。会社用/個人用の取り違えは台帳解決で構造的に閉じる (e-3365)。

### 予定変更のとき (再調整)

すでに Meeting のある商談で日時が変わったら、**新規に作り直さず** 既存 `mtg-N` を動かす
(識別 ID を保って追従させる)。カレンダー側は `update-event` で `$EVENT_ID` の時間を直し、
Beacon 側は:

```bash
BEACON_MTG_ID="$MTG_ID" BEACON_MTG_AT="$NEW_WHEN_START" BEACON_MTG_END="$NEW_WHEN_END" \
  BEACON_MTG_SET_TRANSITION=1 \
  python3 "$(beacon _lib-path)/commands.py" meeting_reschedule
```

これで遷移日もカレンダーも新しい日時に揃う (AC: 予定変更時も両者が追従)。対象の
`mtg-N` は `BEACON_MTG_OPP="$OPP" BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" meeting_list`
で引ける。

## Step 7: 別途の「アポ確定」活動は作らない (e-3548 / e-3536)

**確定した面談は Step 6-2 の Meeting レコードそのものが記録**になる。以前はここで
`[アポ確定] …` という活動を新規作成していたが、それだと確定 Meeting と丸写し活動が
二重に残る (opp-6 で実際に起きた)。**もう活動は作らない** — 面談アンカーから seed した
Meeting を確定するだけで、「いつ次に会うか」は Meeting から辿れる。

(予定未定 mtg- が無く新規予約した場合も同じ。Meeting が唯一の面談記録。)

## Step 8: 結果報告

ユーザーに簡潔に報告:

```
📅 予定を登録しました ([WHEN] / [相手])
  Meeting [mtg-N] を確定し、商談 [OPP] の遷移日を [面談日] に更新しました。
  (seed 済みの予定未定 Meeting を確定 / 無ければ新規作成)
```

登録を止めた場合 (ユーザーが「やめる」／相手都合が付かず保留) は、登録しなかった旨と
理由を報告する。

## 制約

- **予定の作成は人間承認を経る** (AI 自律は候補提示まで、勝手にカレンダーへ入れない)。
- 相手へのメール送信は自動でしない (`/beacon-sales-email` に連携、送信はそちらの承認経路)。
- **使うカレンダー (namespace/account) は必ず台帳解決 (Step 2) から取る**。namespace や
  account を手書きしない — 台帳を通らない経路を残さないのが取り違え防止の本質 (e-3365)。
- **予定を作れたら必ず Meeting を刻み (Step 6-2)、カレンダー予定に識別 ID タグを埋め込む
  (Step 6-3)**。この handshake を欠かすと後段の終了検知 (C) が商談を突き合わせられない。
- **確定した面談の記録は Meeting レコードそのもの。別途「アポ確定」活動を作らない** (e-3548
  / e-3536 — 確定 Meeting と丸写し活動の二重化を構造的に断つ)。
- **予定未定 (unscheduled) の seed 済み Meeting があれば新規作成せず必ずそれを確定する**
  (`meeting_reschedule`)。予定変更のときも既存 `mtg-N` を動かす (新規作成しない = 識別 ID を保つ)。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。
- 相手に見せる文面・日時は非開発者が読める自然な日本語で (社内略語を持ち込まない)。
