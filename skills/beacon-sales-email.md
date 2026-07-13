---
name: beacon-sales-email
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
ROOT=$(beacon-find-root) && BEACON_JSON=1 python3 "$ROOT/lib/commands.py" account_list >/dev/null 2>&1 && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` の場合 (= 営業テンプレートでないプロジェクト)、この Skill は「営業プロジェクトでのみ使えます」と伝えて終了する。cloud mode で `project.json` を直接読めない場合は `beacon opportunity list` が動くかで代替判定してよい。

以降、`$ROOT` は `beacon-find-root` の出力。内部コマンド (`sales_identity_*`) は
ユーザー向け CLI 動詞ではないので `python3 "$ROOT/lib/commands.py" <cmd>` で呼ぶ。

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

## Step 2: 送信 identity (どのアカウントで送るか) の確認

Bash ツールで pin 済みの送信 identity を確認:

```bash
python3 "$ROOT/lib/commands.py" sales_identity_show
```

- 値が返れば `$FROM` として保持。
- `(未設定)` の場合、ユーザーに「どの Google アカウント (メールアドレス) で送りますか？」と確認し、pin する:

```bash
BEACON_SEND_IDENTITY="<ユーザーが答えたアドレス>" python3 "$ROOT/lib/commands.py" sales_identity_set
```

pin は取り違え防止の土台。会社用/個人用を取り違えて顧客に送る事故を防ぐため、
以降このプロジェクトの送信はここで固定した identity に揃える。

## Step 3: 下書きの生成 (自律はここまで)

商談の文脈 (フェーズ・相手・これまでの活動) と、ユーザーが伝えた用件から、
**件名と本文の下書き**を生成する。宛先 (To) は対象商談の担当者 Contact の
メールアドレス (account list に出る) を使う。ユーザーが宛先や用件を指定して
いればそれに従う。

生成したら self-review: (a) 相手が 1 度で意味を取れるか (b) 用件が件名 1 行で
分かるか (c) 社内略語が残っていないか。違反があれば直す。

## Step 4: 送信前ゲート (identity 照合) — 必須

送信に使う from が pin と一致するかを、送信の **直前** に照合する。exit code で gate:

```bash
BEACON_SEND_FROM="$FROM" python3 "$ROOT/lib/commands.py" sales_identity_check
echo "GATE_EXIT=$?"
```

- `GATE_EXIT=0` (OK) → Step 5 へ進んでよい。
- `GATE_EXIT=1` (BLOCK) → **送信しない**。BLOCK メッセージをユーザーに転記し、
  identity を pin し直すか from を直すかをユーザーに委ねる。ここは止めるのが正しい挙動。

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

ユーザーが「送信」と答えたら、Gmail MCP で送信する (ツールは環境の Gmail 送信ツール
= `mcp__gmail__send_email` 等)。ユーザーが「直す」なら Step 3 に戻る。「やめる」なら中止。

> 補足: MCP 側に複数の Gmail アカウントがある場合、send が使うアカウントが pin と
> 一致するかは Step 4 のゲートで担保する。将来、送信ツール側のアカウント選択 (from の
> 明示指定) を詰める (= 回しながら精緻化)。

## Step 6: 活動記録 (証跡) を必ず残す

送信できたら、対象商談にメール送信を活動記録として残す (SPEC 受入条件 4)。
これを飛ばすと「何をしたか」を後で辿れなくなるため必須:

```bash
BEACON_OPP_ID="$OPP" BEACON_ACTIVITY_DESC="[メール送信済] 件名『<件名>』→ <宛先>" \
  python3 "$ROOT/lib/commands.py" opportunity_activity
```

> v1 補足: 現状 activity は「予定 (todo)」型で記録される。送信済みを表す
> 「起きた事実 (event)」型の記録は今後の精緻化対象 (description に [送信済] を明記して代替)。

## Step 7: 結果報告

ユーザーに簡潔に報告:

```
✉ 送信しました (from [FROM] → [宛先])
  商談 [OPP] に活動記録を残しました。
```

送信を止めた場合 (Step 4 BLOCK / ユーザーが「やめる」) は、送信しなかった旨と理由を報告する。

## 制約

- **送信は人間承認を経る** (AI 自律は下書きまで、SPEC §3)。
- **Step 4 のゲートを飛ばさない** (identity 照合前に送信しない、取り違え防止)。
- **送信できたら必ず Step 6 の活動記録を残す** (証跡を欠かさない)。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。
- 顧客宛て本文は非開発者が読める自然な日本語で (社内略語を持ち込まない)。
