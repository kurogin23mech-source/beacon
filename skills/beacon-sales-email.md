---
name: beacon-sales-email
profession: sales
description: 営業の商談に紐づくメールを、下書き→送信アカウント(identity)の照合→人間承認→送信まで Beacon のコンソール上で完結させる。送信は必ず商談に活動記録(証跡)として残し、複数 Google アカウントの取り違えを送信前の照合で構造的に防ぐ。「営業メール送って」「商談のメール」「初回連絡のメール」等で起動。
version: 1.0.0
triggers:
  - /beacon-sales-email
  - 営業メール
  - 商談メール
  - 初回連絡メール
  - この商談のメール
---

# Beacon Sales Email

> 営業 (profession=sales) プロジェクトで、商談 (Opportunity) に紐づくメールを送る。
> ms-107 e-3354。送信の自律度は **下書きまで**、送信は人間承認を経る (SPEC §3)。
> 複数 Google アカウントの取り違えは送信前の照合で止める (SPEC §2、土台は e-3353)。

## 文章の書き方 (Beacon 全体の哲学)

顧客に出すメール本文は、相手 (非開発者・社外の意思決定者) が 1 度読んで意味が取れる自然な日本語で書く。社内の略語・横文字を持ち込まない。件名は用件が 1 行で分かる形にする。

## 前提条件チェック

Bash ツールで実行し、営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" account_list >/dev/null 2>&1 && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` の場合 (= 営業テンプレートでないプロジェクト)、この Skill は「営業プロジェクトでのみ使えます」と伝えて終了する。cloud mode で `project.json` を直接読めない場合は `beacon opportunity list` が動くかで代替判定してよい。

以降、`$ROOT` は `beacon-find-root` の出力。内部コマンド (`sales_identity_*`) は
ユーザー向け CLI 動詞ではないので `python3 "$(beacon _lib-path)/commands.py" <cmd>` で呼ぶ。

## Step 1: 対象商談の特定

Bash ツールで商談一覧を取得し、ユーザーにどの商談のメールかを確認する:

```bash
beacon opportunity list
```

引数で `/beacon-sales-email opp-3` のように商談 ID が渡されていればそれを採用。
無ければ一覧を提示し「どの商談ですか？」と 1 問だけ聞く。対象を `$OPP` として保持。

商談の相手 (顧客 Account とその担当者 Contact) を把握するため account 一覧も引く:

```bash
beacon account list
```

## Step 2: どのアカウントで送るか — 送信アカウント台帳から解決 (この送信ごとに切替可)

送信元は **送信アカウント台帳** (label→email+MCP route、e-3365) から引く。台帳を通さず
namespace を手書きしない (= 取り違えが起きる経路を残さない)。まず台帳を確認:

```bash
BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" sales_account_list
```

- **台帳が空** の場合、ユーザーに「どの Google アカウント (メールアドレス) で送りますか？
  会社用/個人用など呼び名 (label) も教えてください」と確認し、登録する:

```bash
BEACON_SEND_LABEL="会社" BEACON_SEND_EMAIL="<アドレス>" python3 "$(beacon _lib-path)/commands.py" sales_account_add
BEACON_SEND_LABEL="会社" BEACON_SEND_SERVICE="gmail" BEACON_SEND_NAMESPACE="mcp__gmail" \
  python3 "$(beacon _lib-path)/commands.py" sales_account_route
# 既定の送信元にするなら default label を pin (次回から $LABEL 省略で使える):
BEACON_SEND_IDENTITY="会社" python3 "$(beacon _lib-path)/commands.py" sales_identity_set
```

- **どの label で送るか**を決める。既定 (default label) でよければ `$LABEL` は空のまま。
  この 1 通だけ別アカウントにしたい場合は、ユーザーが選んだ label を `$LABEL` に入れる
  (= real-time 切替。プロジェクト全体を pin し直す必要はない)。

送信に使う Gmail の route を台帳から解決する。**これが送信先アカウントの唯一の決定経路**:

```bash
BEACON_SEND_SERVICE="gmail" BEACON_SEND_LABEL="$LABEL" \
  python3 "$(beacon _lib-path)/commands.py" sales_account_resolve
echo "RESOLVE_EXIT=$?"
```

- `RESOLVE_EXIT=0` → JSON `{label,email,namespace,alias}` が返る。`email` を `$FROM`、
  `namespace` を `$NS` (使う Gmail MCP ツール群。現状 `mcp__gmail`) として保持。
- `RESOLVE_EXIT=1` (BLOCK) → その label の gmail route が台帳に無い。**送信しない**。
  上の `sales_account_add` / `sales_account_route` で登録してから再開する。

> Gmail は `send_email` に account 引数が無いため、アカウント切替 = **namespace 切替**
> (別サーバ)。現状この環境は `mcp__gmail` 単一サーバなので実切替先は 1 つ。2 つ目の
> Gmail アカウントを使うには別 namespace のサーバ接続が要る (台帳は複数 namespace を
> 持てる形なので、繋げば `sales_account_route` で足すだけ)。

## Step 3: 下書きの生成 (自律はここまで)

商談の文脈 (フェーズ・相手・これまでの活動) と、ユーザーが伝えた用件から、
**件名と本文の下書き**を生成する。宛先 (To) は対象商談の担当者 Contact の
メールアドレス (account list に出る) を使う。ユーザーが宛先や用件を指定して
いればそれに従う。

生成したら self-review: (a) 相手が 1 度で意味を取れるか (b) 用件が件名 1 行で
分かるか (c) 社内略語が残っていないか。違反があれば直す。

### 営業メールのお作法 (soft guidance / e-3498)

お作法は「お願い」であって絶対の強制ではない (顧客により例外あり)。だが既定は必ず守る:

- **ボールはこちらが握って返す**: 相手に丸投げして判断待ちにしない。次アクションと期限をこちらから提示する。
- **日程を打診するメールは、こちらから候補 (既定 3 枠、曜日・日付・時間帯を明記) を出す**。「ご都合はいかがですか？」と相手に候補日を出させるのは営業の鉄則違反。
  - **重要**: 日程がらみのメールは、この Skill で手書きせず **`/beacon-sales-schedule` 経由で組む**。schedule が自分の空きから 3 枠を算出し、それをカレンダーに『仮押さえ』した上で候補入りの本文を用意する (仮押さえ→提示→確定枠を本予定化→残り解放、の一連は日程ドメインのお作法)。この Skill が日程メールを単発で書くと、候補算出も仮押さえも欠けて鉄則が漏れる。
  - もしユーザーが明示的にこの Skill で日程メールを書けと指示した場合でも、本文が「候補をこちらから 3 枠出す」形になっているかを self-review で確認し、なっていなければ直す。

## Step 4: 送信前ゲート (identity 照合) — 必須

送信に使う from が、選んだ label の identity と一致するかを、送信の **直前** に照合する。
Step 2 と同じ `$LABEL` を渡す (= 解決した route と同じアカウントで gate する)。exit code で gate:

```bash
BEACON_SEND_FROM="$FROM" BEACON_SEND_LABEL="$LABEL" \
  python3 "$(beacon _lib-path)/commands.py" sales_identity_check
echo "GATE_EXIT=$?"
```

- `GATE_EXIT=0` (OK) → Step 5 へ進んでよい。
- `GATE_EXIT=1` (BLOCK) → **送信しない**。BLOCK メッセージをユーザーに転記し、
  台帳の email を直すか label を選び直すかをユーザーに委ねる。ここは止めるのが正しい挙動。

## Step 5: 人間承認 → 送信

下書き (件名 / 本文 / 宛先 / from) をユーザーに提示し、**送信してよいか明示的に確認**する。
AI が自律で送信してはならない (SPEC §3: 送信は人間承認)。

```
以下のメールを送信します:
  from:    [FROM]
  to:      [宛先]
  件名:    [件名]
  本文:
    [本文]

送信しますか？ (送信する / 直す / やめる)
```

ユーザーが「送信」と答えたら、**Step 2 で解決した `$NS` の送信ツール**で送る
(例 `$NS` = `mcp__gmail` → `mcp__gmail__send_email`)。namespace は台帳解決の値をそのまま
使い、別のものに差し替えない (取り違え防止)。ユーザーが「直す」なら Step 3 に戻る。
「やめる」なら中止。

> 送信先アカウントは Step 2 の台帳解決 (`$NS`/`$FROM`) と Step 4 のゲートの二重で担保
> される。将来 Gmail 側が account 引数対応 or 複数 namespace になれば、台帳に route を
> 足すだけで同じ動線で切り替わる。

## Step 6: 活動記録 (証跡) を必ず残す

送信の**事実の記録は Step 6.5 の Communication (証跡) が担う**。ここでは
**新しい活動を作らない** — 送信 1 回につき「予定 (todo) 活動」と「証跡」の 2 レコードが
できて内容が重複し、しかも活動が todo のまま残って未消化に見える問題を避けるため
(e-3505)。

代わりに、**この送信が『計画していた活動』を果たした場合は、その活動を done にする**。
対象商談の未消化活動 (フェーズ起票時に seed された `act-` 等) を `beacon opportunity list`
等で確認し、今回の送信がどれを満たしたかを判断する:

```bash
# この送信が満たした計画活動 (例: 「初回面談を打診」) を done にする
BEACON_ACT_ID="<満たした act-id>" \
  python3 "$(beacon _lib-path)/commands.py" activity_done
```

満たした計画活動が無い単発の送信 (突発の連絡 等) なら、活動の done 化は不要 —
Step 6.5 の Communication だけが記録になる。**いずれにせよ新規 todo は作らない。**

## Step 6.5: 証跡 (Communication) + 返信待ちなら watch を立てる (e-3432 / e-3437)

送信は「実際に起きたやり取り」なので、事実の証跡 (Communication) としても残す。対象は
その活動 (act-) にすると「どの予定を果たした送信か」まで辿れる (無ければ商談 opp-)。
`--source-ref` に送信メールの message-id / thread-id を入れて出典を辿れるようにする:

**スレッド集約の原則 (e-3535)**: 紐づけ先の活動は、この送信が属する会話スレッドの**起点と
なる既存の活動**を選ぶ。返信の往復ごとに新しい活動を作って散らさない — 1 スレッドの証跡は
1 活動配下に集約する (打診と確定が別活動に割れると fold・履歴が読めなくなる)。

```bash
BEACON_COMM_TARGET="<act-id または $OPP>" \
  BEACON_COMM_SUMMARY="<送信内容の1行要約>" \
  BEACON_COMM_DIRECTION="outbound" BEACON_COMM_CHANNEL="email" \
  BEACON_COMM_SOURCE_REF="<message-id / thread-id>" \
  python3 "$(beacon _lib-path)/commands.py" communication_add
```

**このメールが返信を必要とする** (日程打診・確認依頼・見積送付後の返答待ち 等) なら、
その活動に **watch を立てる** — 返信ウォッチャー (E, `/beacon-sales-reply-watch`) が
hourly にこのスレッドを確認し、返信が来たら ball を自分に戻して通知する:

```bash
BEACON_WATCH_TARGET="<act-id>" BEACON_WATCH_CHANNEL="email" \
  BEACON_WATCH_THREAD="<thread-id / message-id>" \
  python3 "$(beacon _lib-path)/commands.py" watch_set
```

**watch を立てたら、それを hourly に回す Operation を必ず ensure する** (e-3504)。watch は
「箱」で、それを叩く「時計」が Operation。ensure を飛ばすと watch が立つのに一度も
チェックが回らない (dogfood 報告④の実害):

```bash
python3 "$(beacon _lib-path)/commands.py" sales_reply_watch_op_ensure
```

これは冪等 (返信ウォッチャー Operation が無ければ作り、あれば再利用)。出力に
`beacon operation approve <op-id> --spec <doc-id>` の案内が出たら、**自動発火 (server tick
が hourly に `/beacon-sales-reply-watch` を起こす) を有効にするには一度この承認が必要**である
旨をユーザーに伝える。承認は人間が行う (自律実行の standing authorization を人が発行する境界)。

返信不要の連絡 (お礼・案内のみ 等) では watch を立てない (無駄打ち防止)。返信が必要か
どうかは送信内容から判断する (時間に敏感な打診・依頼 = watch 対象)。

## Step 7: 結果報告

ユーザーに簡潔に報告:

```
✉ 送信しました (from [FROM] → [宛先])
  商談 [OPP] に活動記録を残しました。
```

送信を止めた場合 (Step 4 BLOCK / ユーザーが「やめる」) は、送信しなかった旨と理由を報告する。

## 制約

- **送信は人間承認を経る** (AI 自律は下書きまで、SPEC §3)。
- **送信先アカウント (namespace/from) は必ず台帳解決 (Step 2) から取る**。namespace を
  手書きしない — 台帳を通らない送信経路を残さないのが取り違え防止の本質 (e-3365)。
- **Step 4 のゲートを飛ばさない** (identity 照合前に送信しない)。Step 2 と Step 4 は
  同じ `$LABEL` で揃える。
- **送信できたら必ず Step 6 の活動記録を残す** (証跡を欠かさない)。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。
- 顧客宛て本文は非開発者が読める自然な日本語で (社内略語を持ち込まない)。
