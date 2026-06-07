---
name: beacon-bus-armed
description: 現在のセッションを「自律 DM 応答モード」に切り替える。Monitor で bus listen を armed しっぱなしにし、prompt 無しでも別セッションからの DM に AI が起動して返答できる状態を作る。budget gate で必ず N 回で止まる安全形。
version: 1.0.0
triggers:
  - /beacon-bus-armed
  - armed mode
  - autonomous mode
  - 自律モード
  - 自動返信モード
  - 自動応答
  - bus を armed
  - listen を armed
---

# Beacon Bus Armed (autonomous DM mode)

> 「会話してなくても、別セッションから DM が来たら気付いて、N 往復まで自動返答する」状態にこのセッションを切り替える。

このスキルは **3 段ロケット** で動く:

1. **Budget grant** — 何ターンまで自動返答を許可するか人間に確認・付与
2. **inbox hook 確認** — `~/.claude/settings.json` に hook が install されているか確認 (してなければ案内)
3. **Monitor 起動** — `beacon bus listen --auto-ack` を Monitor ツールで armed する。これで event 1 件ごとに harness が起動 = prompt 無しでも反応するようになる

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && test -f .beacon/cloud.json && echo "OK" || echo "NO_BEACON_OR_CLOUD"
```
- `NO_BEACON_OR_CLOUD` の場合、「beacon project が cloud mode で初期化されていません」と伝えて終了。

## Step 1: 現在の budget 状態を確認

Bash ツールで:
```bash
beacon bus budget show --json
```

返ってきた JSON:
- `{"armed": false}` → budget 未付与 (default 状態)
- `{"total": N, "used": M, ...}` → 既に grant 済

### Budget 未付与の場合

ユーザーに **明示的に確認** する:

```
Bus event の自律返答モードを起動します。最大何往復まで AI が自動で返信していい？

例:
- 3 → 短い往復確認だけ
- 10 → 普通の dogfood
- 50 → 長時間の会話実験
- 0 / skip → 起動キャンセル

返事の数字で `beacon bus budget grant --turns <N>` を実行します。
```

ユーザーが数字を返したら:
```bash
beacon bus budget grant --turns <N>
```

ユーザーが skip / 拒否したら何もせず終了 (Monitor も armed しない)。

### Budget 既に grant 済の場合

そのまま続行。`remaining` が表示されていれば「現在の残数: X / Y」と添えて Step 2 へ。

## Step 2: inbox hook の install 確認

Bash ツールで:
```bash
test -f ~/.claude/settings.json && grep -q "beacon-bus-inbox-hook" ~/.claude/settings.json && echo "INSTALLED" || echo "NOT_INSTALLED"
```

### `NOT_INSTALLED` の場合

inbox hook が無いと「DM 来ても AI コンテキストに inject されない」ので autonomous mode が機能しない。**Monitor も armed しないで** ユーザーに案内:

```
inbox hook が未 install です。Monitor を armed しても DM が AI コンテキストに inject されないので、armed mode の意味がありません。

~/.claude/settings.json に以下を追加してください (`update-config` スキルでも可):

  "SessionStart":     [{"hooks": [{"type":"command","command":"<repo>/bin/beacon-bus-inbox-hook.py","timeout":10}]}],
  "UserPromptSubmit": [{"hooks": [{"type":"command","command":"<repo>/bin/beacon-bus-inbox-hook.py","timeout":10}]}]

install したら再度 /beacon-bus-armed を実行してください。
```

そして終了。

### `INSTALLED` の場合

Step 3 に進む。

## Step 3: Monitor を armed する

AI ツール `Monitor` を以下のように起動する:

```
Monitor(
  command: "beacon bus listen --auto-ack",
  description: "bus inbox listener — incoming DM をリアルタイムで pick up",
  persistent: true,
  timeout_ms: 3600000
)
```

`persistent: true` でセッション lifetime いっぱい armed しっぱなし。`--auto-ack` で受信後自動 cursor advance、重複配信が起きない。

armed 後にユーザーへ報告:

```
✓ autonomous DM mode armed
  budget: X / Y 残
  Monitor: bus listen --auto-ack (persistent, session lifetime)

別セッションから --in-reply-to 指定で DM が来ると、この AI が prompt 無しでも起動して、budget 残数を見ながら返答します。

停止したい時:
  - `beacon bus budget clear` で budget を 0 にする
  - もしくは Monitor を TaskStop で止める (この session は普通の状態に戻る)
```

## Step 4: 返答ループの取り扱い指示 (Skill 内ガイダンス)

Monitor が armed されている間、AI は次のように振る舞う:

### event 受信時

Monitor の stdout 行 (= 1 event = JSON) を notification として受け取ったら:

1. event を読む
2. payload の `text` / `to` / `in_reply_to` を確認、payload に明示的指示があれば従う
3. **返答が必要と判断したら**:
   - `beacon bus send --channel <ch> --sender <my_session_id> --in-reply-to <event_id> --payload '<json>'` を発行
   - `--in-reply-to` を **必ず** 付ける (これが budget gate の trigger; 付けないと manual mode 扱いになり gate が効かない = 暴走のリスク)
   - send 成功時に CLI が表示する「budget: M/N, X remaining」を読んで、残数が 0 ならそれ以上送らない
4. **返答が不要と判断したら**: 何もせず次の event を待つ (= sleep)

### budget exhausted 時

`bus send --in-reply-to` が exit code 1 + stderr に「exhausted」を返してきたら:

1. 残りの未送信応答は諦める
2. PushNotification でユーザーに「budget exhausted、N 件未対応の DM あり」を通知 (任意)
3. Monitor は armed のまま放置 (再 grant されれば再び動き出す)

### 既存の Monitor との関係

このセッションが他の autonomous loop (例: ScheduleWakeup ベースの cron loop) を持っている場合、Monitor は **並走** する。Monitor からの notification と user prompt と Schedule wakeup tick が混ざる可能性がある — 順番は harness が serialize するので競合は起きないが、文脈の切り替わりが多くなる点は注意。

## 制約

- Monitor 起動は **ユーザー確認の後**。Skill が無断で armed するのは Monitor が token を食う性質上やめておく。
- Budget が grant 済でも、ユーザーが明示的に「やめて」と言ったら Monitor を TaskStop してから終了する。
- 同じプロジェクトで複数の Monitor を多重 armed しない (1 セッション 1 Monitor)。重複してると同じ event が複数回 inject される無駄が出る。
- Monitor の `persistent: true` は session 終了時に自動で消える。長時間放置するなら Cloud Run の cost も意識する。
