---
name: beacon-sales-cockpit
profession: sales
description: 全商談を横断して「今日やるべきこと・各商談の次の一手」を提示し、実行を既存の営業 Skill（メール送信・日程調整・議事録保管）に繋ぐ。遷移日の未設定・超過、未消化の準備活動、相手ボールの放置などを拾って優先度付きで並べる。「今日やること」「営業コックピット」「次の一手」等で起動。営業専用。
version: 1.0.0
triggers:
  - /beacon-sales-cockpit
  - 営業コックピット
  - 今日やること
  - 次の一手
  - 商談の状況
---

# Beacon Sales Cockpit (横断コックピット F)

> ms-107 e-3373。engine の価値が人に届く動線。全商談のフェーズ・ゴール・遷移日・
> 未消化活動・ボールを読んで、「**今日やるべきこと / 各商談の次の一手**」を優先度付きで
> 提示し、実行を既存 Skill（送信・予約・保管）に繋ぐ。これが無いと engine はデータの
> ままで手動運用になる。
>
> 提示までが自律、**送信・予約・フェーズ確定は人間承認**を維持（既存 Skill の境界を継承）。

## 文章の書き方 (Beacon 全体の哲学)

提示は非開発者が 1 行で「どの商談で・何を・なぜ今やるべきか」を掴める自然な日本語で。

## 前提条件チェック

Bash ツールで営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" \
  && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` なら終了。

## Step 1: 盤面を読む

全商談と、判定待ち・返信待ちの状態を取る:

```bash
beacon opportunity list --json                 # 全商談・フェーズ・遷移日・活動
beacon opportunity due --json                  # 遷移日が due/overdue の商談
beacon phase list --json                       # フェーズごとのゴール・活動テンプレ
BEACON_WATCH_AWAITING=1 BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" watch_list  # 返信待ち
```

引数で商談 ID が渡っていれば、その 1 件に絞って深掘りする。無ければ全商談を横断する。

## Step 2: 各商談の「次の一手」を導く

各商談には、いま進行中のフェーズにつき **前進ゲート (= 次のフェーズへ進めてよいかを判定する
関門)** が 1 つ開いている。商談ごとに、以下の優先順位で「次の一手」を 1 つ決める（上から評価し
最初にヒットしたもの）:

1. **前進ゲートが空（発火源 未紐づけ／遷移日 未設定）** → 最優先で「発火源を確保せよ」。
   ゲートに判定のきっかけ（面談 or 日程付きの活動）が紐づいていないと、準備も追い込みも
   駆動できない（SPEC §2、空ゲート = 確保が目的）。→ `/beacon-sales-schedule` で面談を確定
   すると、その面談がゲートの発火源になり遷移日も同時に入る。
2. **前進ゲートが判定どき（紐づけた面談/活動が完了、または遷移日 超過）** → 「判定せよ」。
   紐づけた面談が終わっていれば `/beacon-sales-meeting-wrap`（議事録→判定案）、無ければ
   `beacon opportunity judge` の 3 択（前進 / やり直し / 決着）に繋ぐ。
3. **相手にボールがあり返信待ち超過** → 「催促 or 待ち」。watch 中なら E が拾う、そうで
   なければ `/beacon-sales-email` でフォロー。
4. **現フェーズの準備活動（activity_template）が未消化** → 「その活動をやれ」。送信なら
   `/beacon-sales-email`、日程なら `/beacon-sales-schedule`。
5. **期日超過の活動 / ナーチャリング** → 拾って促す。
6. 上記どれも無ければ「待ち（相手ボール・予定通り）」。

各商談の現フェーズのゴール（methodology）を添えて、「なぜ今それか」を 1 行で示す。

## Step 3: 横断で「今日やるべき」を優先度順に提示

全商談の次の一手を、緊急度（遷移日超過 > 遷移日 due > 返信待ち超過 > 準備活動 > その他）で
並べて提示する。担当（assignee）で絞れる場合は自分の分に絞ってよい:

```
🎯 今日やること (営業コックピット)
  ⚠ 判定 [opp-3] ○○社（提案準備 / 前進ゲート 判定どき: 提案面談 実施済 or 遷移日 07-14 超過）
     → 議事録あれば /beacon-sales-meeting-wrap、無ければ judge の3択
  ⏰ 発火源を確保 [opp-7] △△社（商談準備 / 前進ゲート 空: 発火源 未紐づけ）
     → /beacon-sales-schedule で初回面談を確定（面談がゲートの発火源になり遷移日も入る）
  📬 返信待ち超過 [opp-5] □□社（3日 相手ボール）
     → /beacon-sales-email でフォロー
  ○ 準備活動 [opp-2] ◇◇社（提案準備: 提案書を送る が未消化）
     → /beacon-sales-email
  … 待ち: N 件（予定通り・相手ボール）
```

## Step 4: 実行に繋ぐ（人間承認は既存 Skill が担保）

ユーザーが「これをやる」と選んだら、対応する既存 Skill を起動する。**送信・予約・フェーズ
確定はその Skill の人間承認経路**を通る（この Skill は提示と接続に徹し、勝手に送らない）:

- 面談の予約 → `/beacon-sales-schedule`
- メール送信 → `/beacon-sales-email`
- 議事録→判定 → `/beacon-sales-meeting-wrap`
- 資料保管 → `/beacon-sales-drive`

## 制約

- **提示までが自律**。送信・予約・フェーズ確定は既存 Skill の人間承認を経る（勝手にやらない）。
- **空の前進ゲート（発火源 未紐づけ）を最優先で促す**（engine 駆動の起点、SPEC §2 =
  空ゲート = 発火源を確保せよ）。
- 読み取り専用で導出する（盤面を読むだけ、`project.json` を直接書き換えない）。
- 実行は既存 Skill に繋ぐ。この Skill 内で送信 API を直接叩かない。

## 関連

- `/beacon-sales-schedule` / `/beacon-sales-email` / `/beacon-sales-meeting-wrap` /
  `/beacon-sales-drive` — 実行を担う既存 Skill 群
- `/beacon-sales-reply-watch` (E) — 返信待ちの自動確認（コックピットは超過を可視化）
- SPEC `o83GEljD8xeFMr95wLTh` 設計方針 2 / 6・スコープ（ms-107 実運用 engine）
