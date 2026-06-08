---
name: beacon-dm-reply
description: 受信した DM に対話的に返信する。受信トレイの最新 N 件を表示 → どれに返信するか選択 → budget gate を自動 handle (= ゼロなら自動 grant) → --in-reply-to + --to を自動付与して送信。返信時の「in-reply-to 忘れ」「親 event_id 手書き」「budget gate 不意打ち」を構造的に排除する。
version: 0.1.0
triggers:
  - /beacon-dm-reply
  - DMに返信
  - 返信する
  - reply dm
  - DM 返信
  - bus reply
---

# Beacon DM Reply

> 受信した DM に対話的に返信する。最近の受信を listing して、どれに返信するかを選び、本文を入力するだけ。
>
> 今日 (2026-06-09) の dogfood で観測された返信時の壊れやすさを構造的に排除する: (a) `--in-reply-to <event_id>` を手書きすると親 event_id を間違える / (b) `bus budget grant` を忘れて exit 1 で止まる / (c) cross-project 返信で `--project <id>` を忘れる。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章は、**非開発者を含む読み手** が読めるように書く。詳細は CORE doc `entry-writing-principle` 参照。

---

## 前提条件チェック

Bash ツールで実行:
```bash
test -f .beacon/project.json && test -f .beacon/cloud.json && echo "OK" || echo "NO_BEACON_OR_CLOUD"
```
- `NO_BEACON_OR_CLOUD` の場合、「Beacon プロジェクトのルートで実行してください (cloud mode 必須)」と返して終了。

自分の session_id を控える:
```bash
beacon session id
```

自分のプロジェクト ID を控える:
```bash
python3 -c "import json,sys; print(json.load(open('.beacon/cloud.json')).get('project_id',''))"
```

## Step 1: 受信トレイの取得

Bash ツールで実行 (一発取得、auto-ack なし = peek のみ):

```bash
beacon bus listen --once --channel dm --json
```

`listen` は stdout に **1 イベント = 1 JSON 行** を出す。出力を行単位で読み、JSON を parse する。
**`--auto-ack` を渡さない** ことが重要 — peek のみで cursor は動かさない (= 後で MCP hook や別 Skill が同じ event を処理できる)。

何も出ない場合 (空 stdout)、ユーザーに:
```
未読の DM はありません。返信する受信が無いため終了します。
- 受信を待ちたい: `beacon bus listen` で待機 (Monitor で armed 状態にしたいなら /beacon-bus-armed)
- 新規 DM を送信したい: /beacon-dm-send
```
と案内して終了。

## Step 2: 受信のリスト表示

各 event を以下のフォーマットで最新順に並べる (最大 10 件):

```
1. [2026-06-09T15:36:12] from=6d270a08… channel=dm: "バンドル届いてます。Mac 側から ack 入りで送ります"
2. [2026-06-09T15:22:01] from=aa60cc21… channel=dm: "Win → Mac テスト送信 #2、--in-reply-to つきの試験"
3. [2026-06-09T15:10:55] from=aa60cc21… channel=dm: "Win → Mac テスト送信 #1"
...
```

ユーザーに尋ねる:
```
どの DM に返信しますか?
- 番号 (1, 2, …) でピック
- cancel で中止
```

選択された event の全フィールド (`event_id` / `from_session` / `from_project` / `channel` / `payload`) を控える。

## Step 3: budget 状態の確認と自動 grant

`--in-reply-to` 付きの送信は budget gate に当たる (e-1000)。**ユーザーが「返信する」と明示的に Skill を起動した時点で「人間の意思 = 返信を 1 回許可する」と解釈** し、budget が 0 なら **silently に grant してから送信** する。

Bash ツールで:
```bash
beacon bus budget show --json
```

返ってきた JSON を見て:

| 状態 | 動作 |
|---|---|
| `{"armed": false}` (default) | `beacon bus budget grant --turns 3` を実行 → ユーザーに「自動応答 budget を 3 turn grant しました」と 1 行通知 |
| `armed: true` で `remaining > 0` | そのまま続行。`現在 budget: X/Y remaining` を 1 行表示 |
| `armed: true` で `remaining == 0` (使い切り) | `beacon bus budget grant --turns 3` を実行 → ユーザーに「budget を再 grant しました」と通知 |

grant コマンドが失敗した場合 (ネットワークエラー等) は、ユーザーにエラーをそのまま提示して中止。**budget が空のまま `bus send --in-reply-to` を打ってはならない** (exit 1 になって意味不明な状態になる)。

## Step 4: cross-project 判定

選択 event の `from_project` と Step 0 で取得した `cwd_project_id` を比較する。

| 比較 | 動作 |
|---|---|
| `from_project == cwd_project_id` | 同プロジェクト、`--project` フラグ不要 |
| `from_project != cwd_project_id` (両方非空) | cross-project 返信。**ユーザー確認** : 「相手は project_id=<from_project> です。そちらに返信を投げます (cwd は <cwd_project_id>)」 → yes なら `cross_project_id = from_project` を保持 |
| `from_project` が空 | cwd を仮定。`--project` フラグ不要 |

cross-project ケースで no を選ばれたら中止。

## Step 5: 返信本文の入力

ユーザーに本文を尋ねる:
```
返信本文を入力してください (改行 OK、空行 + Enter で送信):

[親 DM 引用]
from: <from_session の頭8文字>
> <親 payload.text の先頭3行 or 全体>
```

本文を文字列として保持する。JSON エスケープは Skill が行う (ユーザーが `{"text":"..."}` 形式で書く必要は無い)。

## Step 6: 送信確認

組み立てた argv をユーザーに見せて確認:
```
以下のコマンドで返信します:

  beacon bus send --channel dm --to <from_session> --in-reply-to <event_id> --payload '{"text":"<本文>"}' [--project <from_project>] --json

送信しますか? (yes / edit / cancel)
```

- `yes` → Step 7
- `edit` → Step 5 に戻る
- `cancel` → 中止

## Step 7: 送信実行

Bash ツールで上記コマンドを実行する。stdout の JSON を解析する。出力には `_budget` フィールドが含まれるので、送信後の残数を確認する。

## Step 8: 結果報告

```
✓ DM 返信送信完了
  event_id: <new_event_id>
  in_reply_to: <parent_event_id>
  to: <from_session> (<from_machine if known>)
  delivery: <propose-to-ai>
  budget: <used>/<total>, <remaining> remaining
```

`remaining` が 0 になった場合は補足:
「次の自動応答用 budget は 0 です。続けて返信する場合は `/beacon-dm-reply` を再起動するか、`beacon bus budget grant --turns N` を実行してください」

## エラー時の挙動

| エラー | 対応 |
|---|---|
| `bus listen --once` が空 stdout | 「未読 DM 無し」で終了 |
| `bus budget grant` が失敗 | エラーを提示して中止 (budget 無しで send しない) |
| `bus send` が exit 1 with "exhausted" | budget consume race? 1 回だけ再 grant して retry。それでも失敗なら中止 |
| `bus send` が exit 1 (envelope/network) | エラーをそのまま提示。`--no-envelope` での retry を提案 |

## 制約

- Skill は **必ず受信トレイから選ばせる**。event_id を直接渡しても listing を省略しない (= 「どれに返信しているのか」を必ず人間に見せる)。
- Budget grant は `--turns 3` に固定 (一発返信 + 余裕 2 turns)。大きな数を grant したい場合は明示的に `/beacon-bus-armed` を使うべき。
- cross-project 返信は **常に明示確認**。
- 返信本文の JSON エスケープは Skill 側で行う。ユーザーが `--payload '{"text":"..."}'` 形式で書く必要は無い。
- 同じ event に対する複数返信は禁止しない (ユーザーが続けて返信したいケースもあるため)。ただし budget は再 grant されない限り 1 回ずつ消費される。

## 関連 Skill

- `/beacon-dm-send` — 新規 DM 送信。
- `/beacon-bus-armed` — 自律 DM 応答モード (Monitor で listen を armed、N turn 自動返信)。
- `/beacon-bus-budget` (将来) — budget 管理専用 Skill (現在は CLI 直接)。
