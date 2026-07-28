---
name: beacon-sales-meeting-wrap
profession: sales
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

## Step 2: 議事録を取り込む (在処は顧客ごとに一度決めれば以後は自動で探す, e-3552)

議事録 (文字起こし) の在処は**顧客ごとに違う**。Meet + Google Workspace なら自動生成物が
カレンダーの予定の添付や Drive の Meet 録画フォルダに入るが、無料の Google アカウントや
Zoom / tl;dv / Otter 等の外部ツールは置き場も名前もまちまちになる。だから「いつも同じ場所に
ある」を前提にしない。**議事録の在処を顧客ごとに一度決めておけば、次回以降は自動で探す**。
以下の順に上から試す:

### 探し方1: Meet の予定に添付された議事録を辿る

面談 `$MTG` に紐づくカレンダーの予定があれば、まずその**予定に添付された議事録 (Meet の録画・
文字起こし)** を辿る。Meet を使っている面談ではこれが最も確実な第一候補。使う Google アカウントは
台帳(送信アカウント一覧)から解決する (手書きしない, e-3365)。[^wrap-cal]

### 探し方2: 顧客ごとに決めておいた在処を見る

探し方1 で取れない、またはこの顧客が Meet でない場合、その顧客について**あらかじめ決めておいた
議事録の在処**を読む: [^wrap-source-get]

在処には次の種類がある:

| 在処の種類 | 探し方 |
|---|---|
| Meet の予定添付 | 探し方1 と同じ。すでに拾えていればここは飛ばす |
| Drive の特定フォルダ | 決めておいた Drive フォルダを見る。ファイル名の手掛かりがあれば該当議事録を絞る |
| 外部ツール (Zoom / tl;dv / Otter 等) | 道具名を手掛かりに探す。連携が無ければ探し方3 (人に聞く) に落ちる |
| 手動 | 自動生成物なし。探し方3 (人に聞く) に直行する |

在処が未設定の顧客は、無理に Drive を総当たり検索しない (取り違えの元) — 探し方3 に落ちる。
**この顧客の議事録の在処が毎回同じなら、一度決めておけば次回から自動で辿れる**ことをユーザーに
促してよい。ユーザーが「この顧客の議事録は◯◯にある」と教えてくれれば、この Skill が
その在処を顧客に覚えさせる (在処の種類と、必要ならフォルダの場所やファイル名の手掛かり、
道具名)。[^wrap-source-set]

### 探し方3: 人に議事録リンク / 要点を聞く

探し方1・2 で議事録が取れない (無料アカウント等で自動生成物が無い、外部ツール未連携) 場合は、
ユーザーに「議事録の Drive リンクを教えてください / 要点を貼ってください」と促す。ここで得た
リンクは Step 3 の出典として残す。

## Step 3: 議事録を証跡 (Communication) として残す

議事録の要点を **1 行要約** (`--summary`) にして、面談の実績として Communication に残す。
対象は商談（無ければ該当活動 act-）。source に議事録 doc の Drive リンクを入れて出典を辿れる
ように。加えて、議事録の**骨子 (誰が何を言ったか・決まったこと・宿題)** を数行にまとめて
`--body` (`BEACON_COMM_BODY`, 任意・複数行) に入れる — これは 1 行要約より厚い『どういう
内容だったか』の要約で、Web UI のやり取り行『詳細』を開くと最上部に表示される (e-3544)。
1 行要約は一覧の見出し、body は展開時に読む中身、と役割を分ける。

**スレッド集約の原則 (e-3535)**: 活動に紐づける時は、この面談を生んだ**起点の既存活動**
(例「初回面談を実施」) を選ぶ。振り返り用に新しい活動を作らない — 1 スレッド (この面談) の
証跡は 1 活動配下に集約する (fold・履歴が読めなくなるのを防ぐ)。

```bash
BEACON_COMM_TARGET="<$OPP または act-id>" \
  BEACON_COMM_SUMMARY="<議事録の1行要約: 何が決まり何が宿題か>" \
  BEACON_COMM_DIRECTION="inbound" BEACON_COMM_CHANNEL="meeting" \
  BEACON_COMM_SOURCE_URL="<議事録 doc の Drive リンク>" \
  BEACON_COMM_BODY="<議事録の骨子を数行で: 論点・合意事項・宿題>" \
  BEACON_COMM_OCCURRED="<面談の日時>" \
  python3 "$(beacon _lib-path)/commands.py" communication_add
```

## Step 3.5: 顧客ドキュメントへ知見を追記 (dossier 自動蓄積、ms-106 e-3550/e-3747)

議事録から **顧客について新しく分かった持続的な知見** があれば、その顧客(Account)の
顧客ドキュメント (dossier) に日時付きで追記する。証跡(Communication)が「何が起きたか」の
イベントログなのに対し、dossier は「顧客について何が分かっているか」の蒸留・積み上げ。

判定基準 — **追記する**のは顧客の課題 / 勝ち筋 (なぜ買ってくれるか) / キーパーソン・
意思決定 / 予算サイクル・決裁の勘所 など、**次の商談にも効く持続的な知見**。**追記しない**
のは日程・宿題など一過性の連絡事項 (それは Communication で足りる)。新しい知見が無ければ
この Step は飛ばす。

追記する場合、対象商談 `$OPP` の `account_id` (= `$ACC`) を `beacon opportunity list --json`
で解決し、`/beacon-sales-dossier` の追記経路に渡す。ポイントは **議事録から出た情報を
dossier の見出し (顧客の課題[経営/部署/現場] / 勝ち筋 / キーパーソン / 購買勘所) に AI が
振り分けて置く**こと。「◯◯を聞いてこい」と宿題を出すのではなく、**出た情報を自然に
マッピングして『この課題は現場の話として書きました』と残す**。要点だけを蒸留 (議事録の
丸写しはしない)。dossier が無ければ既定テンプレで新規作成。

> 自律実行 (面談終了検知 C 由来) でここまで自動で走る場合も、dossier への追記は記帳系
> (合意事実の蓄積) なので人間確認は挟まない。外向き送信だけが承認境界。

## Step 4: 前進ゲートの判定案を出す (確定は人)

この面談は、その商談の **前進ゲート (= 次のフェーズへ進めてよいかを判定する関門)** に
紐づいた発火源。面談が終わったので、いまがそのゲートの判定どき。議事録の内容と現フェーズの
ゴール（`beacon phase list` の methodology + 前進の枠組み）を照合し、**3 択の判定案**を
根拠付きで提示する（`beacon opportunity judge` の 3 択と同じ語彙）:

- **advance (次へ)**: このフェーズのゴールが達成された（例「先方が提案を受けて検討に入った」）。
  ゲートを advance で締め、次フェーズの空ゲートが開く。
- **retry (やり直し)**: 判定どきだが未達、仕切り直し。ゲートを retry で締め、**同じフェーズで
  新しいゲート**が開く（次の遷移日を提案）。同フェーズを何回叩いたかが履歴に残る。
- **terminal (決着)**: 成約/失注/不成立が確定。ゲートを terminal で締め、以降ゲートは開かない。

いずれも判定の結果と根拠（誰・いつ・なぜ）はゲートの判断証跡として残る。

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

[^wrap-cal]: 実装上は面談 `$MTG` の `calendar_event_id` (確定時 e-3374 で保持済) から
カレンダーの予定を辿り、`beacon-meeting-id` タグと突合する。Drive / カレンダー MCP 経路は
`/beacon-sales-drive` と同じ。

[^wrap-source-get]: 実装上は顧客 (Account) に宣言された取得元を読む:
`BEACON_ACCOUNT_ID="$ACC" python3 "$(beacon _lib-path)/commands.py" sales_account_transcript_source_get`。
返る JSON (`null` なら未宣言) の `type` (`meet_calendar` / `drive_folder` / `external` / `manual`) で
上表の分岐に対応する。

[^wrap-source-set]: 実装上は在処を顧客に書き込む (種類と、必要なら folder_id / naming / tool を
まとめて渡す。宣言は 1 単位で書き替わる。`drive_folder` は folder_id 必須):
`BEACON_ACCOUNT_ID="$ACC" BEACON_TS_TYPE="drive_folder" BEACON_TS_FOLDER_ID="<Drive フォルダ ID>" BEACON_TS_NAMING="<命名の手掛かり (任意)>" python3 "$(beacon _lib-path)/commands.py" sales_account_transcript_source_set`。
宣言を消すのは明示 clear (`BEACON_TS_CLEAR=1`) のときだけ (#498 review: 空 `BEACON_TS_TYPE` は
エラーになるだけで既存宣言は消えない — 渡し忘れ/typo で宣言が飛ぶ事故を防ぐ)。
