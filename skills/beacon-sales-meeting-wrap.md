---
name: beacon-sales-meeting-wrap
description: 終わった面談について、Drive の議事録を取り込んで証跡(Communication)として残し、その内容からフェーズ遷移(次へ/やり直し/決着)の判定案と次にやるべき活動案を提示する。フェーズを進める確定は人が行う。終了検知(C)を起点に、または「面談の振り返り」等で起動。営業専用。
version: 1.0.0
triggers:
  - /beacon-sales-meeting-wrap
  - 面談の振り返り
  - 面談終了ワークフロー
  - 議事録を取り込み
---

# Beacon Sales Meeting Wrap (終了ワークフロー A)

> ms-107 e-3435。面談が終わった後の「議事録取得 → フェーズ遷移判定 → 次活動」が
> 手作業でバラバラなのを 1 ワークフローに束ねる。**実施した事実（議事録 = Communication）
> を根拠に**フェーズを進める判断を支援する。
>
> 検知は機械（終了検知 C）、**判断境界は人** — この Skill は議事録を証跡化し、遷移の
> 判定案・次活動案を**提示する**ところまで。フェーズを実際に進める確定は人が行う。

## 文章の書き方 (Beacon 全体の哲学)

要約・提案は非開発者が 1 行で「何が決まって、次に何をすべきか」を掴める自然な日本語で。

## 前提条件チェック

Bash ツールで営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" \
  && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` なら終了。内部コマンドは `python3 "$(beacon _lib-path)/commands.py" <cmd>` で呼ぶ。

## Step 1: 対象の終了面談を特定

終了検知 (C = `/beacon-sales-meeting-detect`) が終了に落とした面談 (status=ended) が対象。
引数で商談/面談が渡っていればそれを、無ければ「終了済みで未処理の面談」を提示して選ぶ:

```bash
beacon meeting list <opp-id> --json    # status=ended のものが A の入力キュー
```

対象商談を `$OPP`、面談を `$MTG` として保持。相手 (Account/Contact) も把握する。

## Step 2: 議事録を取り込む (Drive)

議事録の取得元は **Google Drive の議事録 doc**。面談後に Drive に置かれた議事録を拾う
（`/beacon-sales-drive` と同じ Drive MCP 経路、台帳解決でアカウントを取る）。面談の
識別子（`$MTG` / カレンダー event / 日付 + 相手名）で該当 doc を探し、本文を読む。

議事録が見つからない場合は、ユーザーに「議事録の Drive リンクを教えてください / 要点を
貼ってください」と促す（手動起点にフォールバック）。

## Step 3: 議事録を証跡 (Communication) として残す

議事録の要点を **1 行要約**にして、面談の実績として Communication に残す。対象は商談
（無ければ該当活動 act-）。source に議事録 doc の Drive リンクを入れて出典を辿れるように:

```bash
BEACON_COMM_TARGET="<$OPP または act-id>" \
  BEACON_COMM_SUMMARY="<議事録の1行要約: 何が決まり何が宿題か>" \
  BEACON_COMM_DIRECTION="inbound" BEACON_COMM_CHANNEL="meeting" \
  BEACON_COMM_SOURCE_URL="<議事録 doc の Drive リンク>" \
  BEACON_COMM_OCCURRED="<面談の日時>" \
  python3 "$(beacon _lib-path)/commands.py" communication_add
```

## Step 4: フェーズ遷移の判定案を出す (確定は人)

議事録の内容と現フェーズのゴール（`beacon phase list` の methodology）を照合し、
**3 択の判定案**を根拠付きで提示する（`beacon opportunity judge` の 3 択と同じ語彙）:

- **advance (次へ)**: このフェーズのゴールが達成された（例「先方が提案を受けて検討に入った」）
- **retry (やり直し)**: 遷移日は来たが未達、仕切り直し（次の遷移日を提案）
- **terminal (決着)**: 成約/失注/不成立が確定した

```
判定案: advance（商談準備 → 提案準備）
  根拠: 議事録『初回面談で課題が明確化、提案の方向性に合意』→ 現フェーズのゴール達成
  ※ 確定は人。よければ `beacon opportunity judge $OPP advance` を実行します。
```

**AI は judge を自動実行しない** — 案を出し、人が確定したら実行する（判断境界は人）。

## Step 5: 次にやるべき活動を提案

遷移後（or 現）フェーズの activity_template と議事録の宿題から、**次の一手の活動案**を
提示する。人が採用したら活動として起票し、日程が要るなら `/beacon-sales-schedule`、
送信が要るなら `/beacon-sales-email` に繋ぐ（送信・予約は人間承認を維持）。

## Step 6: 結果報告

```
📝 面談の振り返り ([MTG] / [相手])
  議事録: 証跡として記録（<1行要約>）
  フェーズ判定案: <advance/retry/terminal> — 根拠: <…>
  次の活動案: <…>（採用しますか？）
```

## 制約

- **フェーズは自動で進めない** — 判定案の提示まで。judge の確定は人（検知は機械・判断は人）。
- **議事録は必ず証跡 (Communication) として残す**（source に Drive リンク、後から辿れる）。
- **送信・予約は既存 Skill の人間承認経路に繋ぐ**（この Skill は送信・予約をしない）。
- **使う Drive アカウントは台帳解決から取る**（手書きしない、e-3365）。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。

## 関連

- `/beacon-sales-meeting-detect` (C) — 終了検知。A の起点（status=ended が入力キュー）
- `/beacon-sales-drive` — 議事録 doc の取得（Drive MCP 経路）
- `/beacon-sales-schedule` / `/beacon-sales-email` — 次活動の実行（送信・予約は人間承認）
- SPEC `o83GEljD8xeFMr95wLTh` §5 / A（議事録取込 e-3359 を内包）
