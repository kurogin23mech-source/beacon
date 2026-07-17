---
name: beacon-sales-standup-intake
profession: sales
description: その日のメール/Slack の営業関連のやり取りを、該当する商談・顧客の証跡(Communication)として恒久台帳に落とし、活動の消化・ボール更新・新規リード候補の提示を行う日次の取込。「日次取込」「今日のやり取り取り込み」「standup 取込」等で起動。営業専用。
version: 1.0.0
triggers:
  - /beacon-sales-standup-intake
  - 日次取込
  - 今日のやり取りを取り込み
  - standup 取込
---

# Beacon Sales Standup Intake (一括取込 D)

> ms-107 e-3436。morning-standup（メール・Slack・カレンダーを日次で一括取得する既存
> Skill）で集めた「その日の現実」を、Beacon の**恒久台帳**に systematic に落とす日次
> 取込。該当する商談/顧客に**証跡 (Communication) を残し**、消化された活動を done に、
> ボールを更新し、既知でない相手は**新規リード候補として提示**する（自動起票しない）。
>
> 役割分担: D は**記録層（何が起きたかを網羅的に刻む、日次・重い）**。時間に敏感な
> 返信待ちの高頻度確認は E (`/beacon-sales-reply-watch`、hourly・軽い)。混ぜない。

## 文章の書き方 (Beacon 全体の哲学)

要約・提示は非開発者が 1 行で「誰と何のやり取りがあったか」を掴める自然な日本語で。

## 前提条件チェック

Bash ツールで営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" \
  && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` なら終了。内部コマンドは `python3 "$(beacon _lib-path)/commands.py" <cmd>` で呼ぶ。

## Step 1: その日のやり取りを集める

`/morning-standup` が集める当日のメール・Slack を取込源にする。standup を先に走らせて
その結果を使うか、同じ MCP 経路（会社/個人 Gmail・Slack）で当日分を取得する。**営業に
関係するもの**（顧客・商談の相手とのやり取り）だけに絞る。台帳解決でアカウントを取り、
手書きしない。

## Step 2: 既存の商談・顧客の一覧を用意

突合の突き合わせ先を取る:

```bash
beacon opportunity list --json
beacon account list --json
```

各メッセージの相手（送信者/宛先のメールアドレス）を、**顧客(Account) の Contact の
メールアドレス**と照合する。

## Step 3: 各メッセージを突合して恒久台帳に落とす

メッセージごとに、以下の判定で処理する:

| 判定 | アクション |
|---|---|
| Contact→Account が一意に決まり、その Account の open 商談が **1 つ** | その商談 (opp-) or 該当活動 (act-) に Communication を残す |
| Account は決まるが open 商談が **複数** | **人に確認**:「この相手は ○○社、open 商談が A/B あります。どちらの話ですか？」→ 選んだ商談に紐づけ (誤紐付け防止) |
| 送信者が既知の Contact に**紐づかない** | **新規リード候補として提示**（自動起票しない）:「○○ から連絡。新規リードとして起票しますか？」→ 人が確定したら `beacon account add` |

Communication の記録（対象は商談 opp- / 顧客 acc- / 活動 act- / ナーチャリング nrt-）:

```bash
BEACON_COMM_TARGET="<target-id>" \
  BEACON_COMM_SUMMARY="<やり取りの1行要約>" \
  BEACON_COMM_DIRECTION="<inbound: 相手発 / outbound: 自分発>" \
  BEACON_COMM_CHANNEL="<email / slack>" \
  BEACON_COMM_SOURCE_REF="<message-id / thread-id>" BEACON_COMM_SOURCE_URL="<permalink>" \
  BEACON_COMM_OCCURRED="<やり取りの時刻>" \
  python3 "$(beacon _lib-path)/commands.py" communication_add
```

## Step 4: 活動の消化・ボール更新

記録した Communication を根拠に:

- **活動の done 化**: 予定していた活動（例「提案書を送る」）が実際に行われたと読めるなら、
  その活動を done にする（`/beacon-task` 経由 or 内部コマンド）。予定と実績の対応を取る。
- **ボール**: ball は最新 Communication の向きから自動導出されるので、明示更新は不要
  （inbound を残せば自分に、outbound を残せば相手に自動で移る）。
- **取消済は対象外 (e-3587)**: `status == "cancelled"` の活動・証跡は「消化すべき予定」でも
  「ボール導出の材料」でもない。done 化の対象からも、突合の対象からも除外する（取消済を
  拾うと、消したはずの活動が実績待ちとして復活する）。

## Step 5: 結果報告

```
📥 日次取込 (基準 <日付>)
  商談に紐づけ: N 件
    - [opp-x] ○○社: <要約>
  複数商談で確認待ち: M 件（下記から選んでください）
  新規リード候補: K 件（起票しますか？）
    - <相手> <要約>
  消化した活動: <act-x> ○○ を done
```

複数商談の確認・新規リード起票は、この報告を受けてユーザーが確定する。

## 制約

- **記録層に徹する**（何が起きたかを刻む）。時間敏感な返信待ちの高頻度確認は E の役割。
- **新規リードは自動起票しない**（提案のみ、人が確定）。台帳をノイズで汚さない。
- **突合が曖昧（同一顧客に open 商談複数）な時は人に確認**してから紐づける（誤紐付け防止）。
- **使うメール/Slack アカウントは台帳解決から取る**（手書きしない、e-3365）。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。

## 関連

- `/morning-standup` — 当日のメール/Slack/カレンダーを集める取込源
- `/beacon-sales-reply-watch` (E) — 時間敏感な返信待ちの hourly 確認（D とは別役割・別 cadence）
- SPEC `o83GEljD8xeFMr95wLTh` §2 / スコープ (ms-107 実運用 engine)
