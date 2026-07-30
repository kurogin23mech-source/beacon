---
name: beacon-sales-meeting-confirm
description: 相手が提示した面談枠を承諾した後の一連（遷移日設定→面談確定→識別タグ→仮押さえ解放→Meet URL取得→確定返信の提案）を1本の決定論的パイプラインで通す。承認境界を『記帳=自動 / 外向き送信=人間承認』の細粒度で正す。合意事実の記帳は自動、相手への確定返信だけ人間承認。「面談確定」「枠承諾」「日程確定」等で起動、または返信検知(reply-watch)の枠承諾から。
version: 1.0.0
---

# Beacon Sales Meeting Confirm (面談確定 = 枠承諾後の決定論パイプライン)

> 営業 (profession=sales) プロジェクトで、こちらが提示した面談枠を相手が承諾した後の
> 一連を 1 本のパイプラインで通す。ms-106 e-3534。
>
> **このスキルの核心 = 承認境界を正す**: 日程調整 (`/beacon-sales-schedule`) は候補を
> 提示するまでが仕事で、カレンダー登録**全体**を人間承認で囲っていた (= 合意済み事実の
> 記帳にまで過剰に人手を挟む)。一方で AI は勝手に外向きの確定連絡を送ろうとしがちだった
> (= 外向きが無承認)。両側にズレていた境界を、このスキルは細粒度で正す:
>
> - **記帳系は自動** (= 合意した事実を残すだけ): 遷移日設定 / Meeting 確定 / 識別タグ /
>   仮押さえ解放。相手が枠を承諾した時点でこれらは既に合意事項なので、人間確認を挟まない。
> - **外向き送信だけ人間承認** (= 送信承認): 相手への確定返信 (Meet URL 入り) の直前のみ。
>   誤分類でこのスキルが誤発火しても、記帳は自動でも**確定返信は人間承認で止まる**ので
>   誤爆が相手に届かない (backstop)。

## 文章の書き方 (Beacon 全体の哲学)

相手に送る確定返信の文面は、社外の相手が 1 度読んで分かる自然な日本語で書く。日時は
「7/15 (火) 14:00〜15:00」のように曜日込みで取り違えの起きない形にする。

## 前提条件チェック

Bash ツールで営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" \
  && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` の場合、「営業プロジェクトでのみ使えます」と伝えて終了する。

## Step 1: 入力の特定 (どの商談の・どの枠が承諾されたか)

このスキルは 2 経路で起動する:

- **返信検知 (reply-watch) 由来**: `/beacon-sales-reply-watch` が相手の返信を検知し、
  返信意図を小さな閉集合 (枠承諾 / 再提案 / 辞退 / 質問) に分類する。そのうち **枠承諾**
  と判定された返信がこのスキルに渡る。渡される情報は「どの商談 (opp-)」「相手が選んだ枠
  (日時)」。ルーティングは AI 判断だが、分類が bounded かつ外向き送信が人間承認なので
  誤分類は相手に届く前に止まる。
- **ユーザーの口頭報告 由来**: 「○○社が △日 の枠で OK と返事きた、確定して」等。

いずれの経路でも、**対象商談 `$OPP`** と **相手が承諾した日時 `$WHEN`** を確定する。
複数の仮押さえ枠のうちどれが承諾されたかが曖昧なら、ユーザーに確認する (誤った枠を
本予定にしない)。相手・連絡先は商談から辿る。

## Step 2: 記帳系を自動で通す (合意事実の記帳、人間確認なし)

相手が枠を承諾した = 以下は既に合意事項。**確認を挟まず順に実行**する。使うカレンダーの
namespace/account は商談・台帳から解決した値をそのまま使う (会社用/個人用の取り違えは
台帳解決で閉じる)。

1. **承諾枠を本予定に確定 + 遷移日を同時更新**: `/beacon-sales-schedule` Step 6 と同じ
   Meeting 確定プリミティブを使う。まず対象商談の予定未定 Meeting を引く:

   ```bash
   BEACON_MTG_OPP="$OPP" BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" meeting_list
   ```

   `status == "unscheduled"` の `mtg-N` があれば、それを承諾枠の日時で確定する (新規作成
   しない = 重複防止)。`BEACON_MTG_SET_TRANSITION=1` で **その商談の前進ゲートの遷移日が
   面談日に同時更新**される (遷移日はゲートが持つ、二重管理を無くす):

   ```bash
   BEACON_MTG_ID="$MTG_ID" BEACON_MTG_AT="$WHEN_START" BEACON_MTG_END="$WHEN_END" \
     BEACON_MTG_EVENT_ID="$EVENT_ID" BEACON_MTG_CAL_NS="$CALNS" BEACON_MTG_CAL_ACCT="$CALACCT" \
     BEACON_MTG_SET_TRANSITION=1 \
     python3 "$(beacon _lib-path)/commands.py" meeting_reschedule
   ```

   予定未定 Meeting が無ければ `meeting_schedule` で新規に確定する (同じく SET_TRANSITION)。
   承諾枠が既に仮押さえイベントとして存在する場合は、その event を本予定 (承諾/confirmed)
   に更新し、その `event id` を `$EVENT_ID` にする。

2. **識別 ID タグを埋め込む**: 確定した `mtg-N` を Beacon 識別 ID とし、カレンダー予定の
   説明 (description) に `beacon-meeting-id: mtg-N` を 1 行足す (`$CALNS` の `update-event`、
   既存説明は上書きせず末尾に追加)。後段の終了検知がこの予定を商談に突き合わせられる。

3. **仮押さえの解放**: 承諾されなかった残りの仮押さえ枠 (カレンダーの tentative イベント)
   を `$CALNS` の `delete-event` で解放する。これで相手が選ばなかった枠が予定表に残らない。

4. **証跡の記帳**: 「相手が △日 の枠を承諾」を Communication として残す (証跡)。
   `/beacon-sales-communication` の記帳プリミティブ、または schedule/wrap と同じ経路。

ここまでは合意事実の記帳なので**すべて自動**。ユーザーに「登録しますか？」とは訊かない。

## Step 3: Meet URL を取得する

確定したカレンダー予定 `$EVENT_ID` から、オンライン会議 URL を取る。Google カレンダー
予定の `conferenceData` (Meet リンク) を `$CALNS` の `get-event` で読む:

```
mcp__google-calendar__get-event (account: $CALACCT, eventId: $EVENT_ID)
```

返った `conferenceData` / `hangoutLink` から Meet URL を `$MEET_URL` として保持する。
オンライン枠でなく URL が無ければ、確定返信では対面の場所を書く (URL は省く)。

## Step 4: 相手への確定返信を提案する (ここだけ人間承認 = 送信承認)

確定返信の**下書きを作ってユーザーに提示**する。日時 + (あれば) Meet URL + 場所を含める。
Meet で打診したなら確定連絡に必ず Meet URL を載せる。

```
以下の確定返信を送ります (送信前に確認してください):
  宛先:  [相手 / Contact]
  件名:  [面談日程 確定のご連絡]
  本文:
    ○○様
    ご都合を承りました。下記にて確定いたします。
    日時: 7/15 (火) 14:00〜15:00
    会議URL: $MEET_URL
    ...

送信しますか？ (送信する / 直す / やめる)
```

**送信は必ず人間承認を経る**。承認されたら `/beacon-sales-email` に橋渡しし、そこで
送信アカウント (identity) 照合 → 送信 → 商談への活動記録 (証跡) までを通す (複数
Google アカウントの取り違えを送信前照合で防ぐ)。「直す」なら文面を直して再提示、
「やめる」なら送信しない (記帳系は既に完了しているのでそのまま)。

## Step 5: 完了報告

記帳系の結果 (確定した mtg-N / 遷移日 / 解放した仮押さえ数) と、確定返信の送信有無を
1〜2 行で報告する。次の一手 (前進ゲートの発火源 = この面談。終了後にフェーズ判定が促される)
を添える (target-advancement)。

## 制約

- **記帳系=自動 / 外向き送信=人間承認** の細粒度境界を厳守する。記帳 (遷移日/Meeting/
  タグ/仮押さえ解放/証跡) に人間確認を挟まない。外向き確定返信だけ送信承認で止める。
- **誤爆の backstop**: 誤分類でこのスキルが誤発火しても、外向き確定返信の人間承認が
  最後の砦。記帳が自動でも相手には届かない。
- カレンダーの namespace/account は台帳解決値を使い差し替えない (会社用/個人用の取り違え防止)。
- Meeting は新規乱造せず既存の予定未定 mtg- を確定する (二重計上を構造的に防ぐ)。
