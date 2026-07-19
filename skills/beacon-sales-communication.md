---
name: beacon-sales-communication
profession: sales
description: メール/Slack 以外の場所（Facebook Messenger・LINE・電話・対面など）で起きた顧客とのやり取りを、ユーザーの口頭報告から証跡として商談・顧客・活動・ナーチャリングに記録する。「Messengerで話した」「電話で確認した」「対面で会った」「やり取りを記録して」等で起動。
version: 1.0.0
triggers:
  - /beacon-sales-communication
  - やり取りを記録
  - コミュニケーション記録
  - Messengerで
  - メッセンジャーで
  - LINEで
  - 電話で話した
  - 対面で会った
  - オフラインで
---

# Beacon Sales Communication (off-channel 報告記録)

> 営業 (profession=sales) プロジェクトで、**メール/Slack の自動取込に乗らないやり取り**
> (Facebook Messenger・LINE・電話・対面 など) を、ユーザーの口頭報告から証跡
> (Communication) として記録する。ms-107 e-3454。
>
> 役割分担: メール・Slack・カレンダーは morning-standup → 一括取込 (D) が自動で拾う。
> この Skill は **その経路に乗らない媒体を、人間が報告して手で残す** 補完経路。
> Communication は「実際に起きたやり取りを source を辿れる要約付きで残す証跡」
> (= 営業の Commit)。予定 (Activity/Nurturing) ではなく事実を残す層。

## 文章の書き方 (Beacon 全体の哲学)

記録する要約は、後で読む人 (非開発者を含む) が 1 行で状況を掴める自然な日本語で書く。
社内略語を持ち込まない。相手・用件・次にどうなったかが分かる 1 行にする。

## 前提条件チェック

Bash ツールで営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" \
  && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` の場合、「営業プロジェクトでのみ使えます」と伝えて終了する。cloud mode で
`project.json` を直接読めない場合は `beacon opportunity list` が動くかで代替判定してよい。

以降、`$ROOT` は `beacon-find-root` の出力。内部コマンド (`communication_add`) は
ユーザー向け CLI 動詞ではないので `python3 "$(beacon _lib-path)/commands.py" communication_add` で呼ぶ。

## Step 1: 対象の特定 (どの商談・顧客・予定のやり取りか)

証跡は必ず対象にひもづく。対象は 4 粒度から選ぶ:

- **商談 (opp-)** — 進行中の商談そのもの
- **顧客 (acc-)** — 商談が無い顧客 / 顧客全体レベルのやり取り
- **活動 (act-)** — その商談の特定の活動を「果たした」やり取り
- **ナーチャリング (nrt-)** — 継続顧客への特定の関係維持予定を「果たした」やり取り

ユーザーの報告に会社名・人名が含まれていれば、まず商談・顧客一覧から候補を照合する:

```bash
beacon opportunity list
beacon account list
```

- 報告から対象が一意に絞れる → その `opp-` / `acc-` を採用 (対象を `$TARGET` に保持)。
- 「この活動を果たした」「この打診の返事」等、**特定の予定に対する実績**だと分かる場合は、
  その活動 (`act-`) / ナーチャリング (`nrt-`) を `$TARGET` にする (= 予定→実績の紐付け)。
  活動 ID は `beacon opportunity list` 展開や商談モーダルで確認できる。
- 絞れない / 複数候補 → ユーザーに 1 問だけ確認:「どの商談 (または顧客) のやり取りですか？」

## Step 2: やり取りの内容を構造化

報告の文面から以下を埋める。**報告に書かれていない項目だけ** を 1 度にまとめて聞く
(既に分かる項目は再質問しない)。

| 項目 | 中身 | 既定 / 補完 |
|---|---|---|
| **direction (向き)** | `inbound` (相手→自分/受信) か `outbound` (自分→相手/送信) | 報告の動詞で推定 (「返事が来た」=inbound /「連絡した」=outbound)。両方向あるやり取りは **最後の向き** を採る (ボール導出の起点になるため) |
| **channel (媒体)** | 自由記述。`messenger` / `line` / `電話` / `対面` / `sms` 等そのまま | 報告から抽出。不明なら聞く |
| **summary (要約)** | 何が起きたかの 1 行 | AI が報告を 1 行に圧縮 (相手・用件・結果) |
| **occurred_at (発生日時)** | いつのやり取りか (ISO8601 目安) | 報告に日時があればそれ、無ければ「今日」でよいか確認 |
| **source (出典)** | 辿れるリンク / スレッド ID があれば | Messenger/LINE のスレッド URL 等。無ければ空でよい (対面/電話は出典なしが普通) |

複数の往復を 1 度に報告された場合は、**1 往復 = 1 Communication** を原則に分けて記録する
(ボールと証跡の粒度が保たれる)。ただしユーザーが「まとめて 1 件でいい」と言えば 1 件にする。

## Step 3: 確認 → 記録

組み立てた内容を 1 度提示し、記録してよいか確認する (1 prompt):

```
以下を証跡 (Communication) として記録します:
  対象:   [TARGET] ([商談/顧客/活動/ナーチャリング 名])
  向き:   [受信 / 送信]
  媒体:   [channel]
  要約:   [summary]
  日時:   [occurred_at]
  出典:   [source URL / なし]

記録しますか？ (記録する / 直す / やめる)
```

「記録する」なら内部コマンドで記録する:

```bash
BEACON_COMM_TARGET="$TARGET" \
  BEACON_COMM_SUMMARY="$SUMMARY" \
  BEACON_COMM_DIRECTION="$DIRECTION" \
  BEACON_COMM_CHANNEL="$CHANNEL" \
  BEACON_COMM_SOURCE_URL="$SOURCE_URL" \
  BEACON_COMM_SOURCE_REF="$SOURCE_REF" \
  BEACON_COMM_OCCURRED="$OCCURRED" \
  python3 "$(beacon _lib-path)/commands.py" communication_add
```

- `$TARGET` が `act-` / `nrt-` の場合、内部で親商談 / 親顧客に格納され、その予定を
  「果たした」実績として紐づく (linked_id)。ユーザーは意識しなくてよい。
- `$SOURCE_URL` / `$SOURCE_REF` / `$OCCURRED` は無ければ空文字で渡す。
- 「直す」なら Step 2 に戻り、「やめる」なら記録せず終了する。

複数往復を分けて記録する場合は、この Step を件数分繰り返す。

## Step 4: 結果報告

```
✓ やり取りを記録しました ([channel] / [対象名])
  [comm-N] [向き] [要約]
  ボール: [自分 / 相手]  ← 最新の向きから導出
```

`beacon communication list "$TARGET"` の導出ボールを 1 行添えると、「次に動くのは
どちらか」がその場で分かる。

## Step 5: 顧客ドキュメントへ知見を追記 (dossier 自動蓄積、ms-106 e-3550/e-3747)

このやり取りから **顧客について新しく分かった持続的な知見** (組織構造 / 意思決定プロセス /
課題 / 予算サイクル / キーパーソン / 嗜好 など) があれば、その顧客(Account)の顧客
ドキュメント (dossier) に日時付きで追記する。証跡(Communication)は「何が起きたか」、dossier
は「顧客について何が分かっているか」の蒸留 — 別物なので両方に残す。

一過性の連絡事項 (日程・宿題など) は Communication で足りるので dossier には足さない。
新しい知見があるときだけ、対象の `account_id` (= `$ACC`) を解決し (対象が acc- ならそれ、
opp- なら `beacon opportunity list --json` で account_id を引く)、`/beacon-sales-dossier`
の追記経路 (append-only、要点を 1〜数行に蒸留) に渡す。dossier が無ければ新規作成。

## 制約

- **記録は人間確認を経る** (Step 3 の 1 prompt)。勝手に多数の証跡を書き込まない。
- **channel は事実をそのまま残す** — 「Messenger」を「other」に丸めない (自由記述で保持)。
- メール送信・アポ調整そのものは別 Skill (`/beacon-sales-email` / `/beacon-sales-schedule`)。
  この Skill は **既に起きたやり取りの記録** に徹し、送信・予約はしない。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。
- 要約は非開発者が読める自然な日本語で (社内略語を持ち込まない)。

## 関連

- `/beacon-sales-email` — メール送信 + 証跡記録 (送信する側)
- `/beacon-sales-schedule` — アポ調整 + 面談記録 (予約する側)
- SPEC `o83GEljD8xeFMr95wLTh` (ms-107 実運用 engine)。D (一括取込) が自動で拾う
  メール/Slack と、この Skill が手で残す off-channel が、同じ Communication 層に集まる。
