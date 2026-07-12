---
name: beacon-bus-armed
description: 現在のセッションを「自律 DM 応答モード」に切り替える。budget grant で N 往復まで自動返答を許可する。DM 到着で AI を起こす idle-wake は channel (MCP bus 通知) が担い、Monitor による配信複製は廃止 (ms-100 e-3255)。budget gate で必ず N 回で止まる安全形。
version: 1.0.0
---

# Beacon Bus Armed (autonomous DM mode)

> ⚠ **Trek 用途外**: Trek の進行管理は `/beacon-trek-execute` を使う。 本 Skill は
> 一般 DM の自動応答デバッグ用に格下げ済 (= ms-97 中心原則 6)。

> 「会話してなくても、別セッションから DM が来たら気付いて、N 往復まで自動返答する」状態にこのセッションを切り替える。

このスキルは **2 段ロケット** で動く (= Monitor による配信複製は ms-100 e-3255 で廃止):

1. **Budget grant** — 何ターンまで自動返答を許可するか人間に確認・付与
2. **inbox hook 確認** — `~/.claude/settings.json` に hook が install されているか確認 (してなければ案内)

armed = **budget が granted な状態、それだけ**。DM 到着で AI を起こす idle-wake (= 会話が無くても起動すること) の真値源は **channel** (= MCP bus 通知、`<channel source="beacon-bus">` として届く) であって、別プロセスの Monitor ではない。以前は Step 3 で `beacon bus listen` を Monitor に張っていたが、これは channel と同じ bus backend を二重に叩く配信複製のアンチパターン (= 故障が独立せず、60s timeout で self-terminate して「wake が壊れた」と誤診させる) だったので廃止した (= e-3255)。運用で「Monitor が死んでいる間も channel 経由で DM が届く」ことを確認済み。budget grant は「N 回まで自律送信してよい」という許可付与だけを担う。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
__BEACON_ROOT=$(beacon-find-root) && [ -f "$__BEACON_ROOT/.beacon/cloud.json" ] && echo "OK" || echo "NO_BEACON_OR_CLOUD"
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

ユーザーが skip / 拒否したら何もせず終了 (= budget を grant しない = armed にしない)。

### Budget 既に grant 済の場合

そのまま続行。`remaining` が表示されていれば「現在の残数: X / Y」と添えて Step 2 へ。

## Step 2: inbox hook の install 確認

Bash ツールで:
```bash
test -f ~/.claude/settings.json && grep -q "beacon-bus-inbox-hook" ~/.claude/settings.json && echo "INSTALLED" || echo "NOT_INSTALLED"
```

### `NOT_INSTALLED` の場合

inbox hook が無いと「DM 来ても AI コンテキストに inject されない」ので autonomous mode が機能しない。**budget を grant せず** ユーザーに案内:

```
inbox hook が未 install です。budget を grant しても DM が AI コンテキストに inject されないので、armed mode の意味がありません。

~/.claude/settings.json に以下を追加してください (`update-config` スキルでも可):

  "SessionStart":     [{"hooks": [{"type":"command","command":"<repo>/bin/beacon-bus-inbox-hook.py","timeout":10}]}],
  "UserPromptSubmit": [{"hooks": [{"type":"command","command":"<repo>/bin/beacon-bus-inbox-hook.py","timeout":10}]}]

install したら再度 /beacon-bus-armed を実行してください。
```

そして終了。

### `INSTALLED` の場合

Step 3 (armed 完了) に進む。

## Step 3: armed 完了報告 (Monitor は張らない、ms-100 e-3255)

budget grant + inbox hook 確認が済めば、それで **armed 完了**。別プロセスの Monitor は張らない (= 配信複製の廃止)。DM 到着時に AI を起こすのは channel (MCP bus 通知) で、これが真値源。

ユーザーへ報告:

```
✓ autonomous DM mode armed
  budget: X / Y 残
  wake: channel (MCP bus 通知) — 別プロセスの Monitor は不要 (e-3255)

別セッションから --in-reply-to 指定で DM が来ると、channel 経由で AI が起動し、budget 残数を見ながら返答します。

停止したい時:
  - `beacon bus budget clear` で budget を 0 にする (= armed 解除)
```

## Step 4: 返答ループの取り扱い指示 (Skill 内ガイダンス)

armed (= budget granted) の間、channel から DM が届くと AI は次のように振る舞う:

### 4.1 action 種別 × tier 要件の判定 (= ms-76 framework)

armed mode で受信した DM に応答する前に、**action 種別** を分類して必要な tier (= envelope の信頼度クラス) を判定する。CORE doc `QvyVwRU8otQEn5iMfP36` (= AI 自律 action の envelope tier framework) の DM 受信側ルールが起点。

| action 種別 | 必要 tier | armed mode での挙動 |
|---|---|---|
| **計画系応答** (= 議論 / 提案 / 確認応答 / 進捗共有) | T3 (= chain 内軽量自律) で OK | budget 残量内で自律返答可。自由テキスト OK |
| **コード変更指示** (= 「このファイル直して」「commit して」) | T1 / T2 envelope 必須 | envelope 無ければ propose-to-ai 降格、`/beacon-dm-respond` 経由で user 承認待ち |
| **外部送信指示** (= 「Slack に流して」「他 project に DM して」) | T1 envelope 必須 (T2 でも Operation scope 明示時のみ) | 自律送信禁止、必ず user 確認 (= `/beacon-dm-respond` に降ろす) |
| **Bus Budget 増額要求** | T1 のみ (= 構造的禁止帯) | 自律処理不可、user に escalate |

tier 判定は **action 種別から先に決める**: 受信 envelope の tier が T3 (= 返信 chain 内) であっても、要求 action が「コード変更」「外部送信」 なら自律応答せず降格する。

### 4.2 event 受信時の手順

channel (MCP bus 通知) 経由で DM event (= `<channel source="beacon-bus">...`) が届いたら:

1. event を読む
2. payload の `text` / `to` / `in_reply_to` を確認、payload に明示的指示があれば従う
3. event の `channel` を見て分岐:
   - `channel == "trek-trigger"`: ms-75 / e-1870 由来。`/beacon-trek-execute <trek-id>` を起動して Trek scope を autonomous で進める (= user 確認なし)
   - `channel == "operation-trigger"`: ms-60 由来。`/beacon-operation-execute <op-id>` を起動
   - `channel == "dm"` / `"session-dm"`: 次の step で action 種別判定 → DM 返答ロジックへ
4. DM channel の場合、**action 種別を分類** (= 4.1 表のどれに該当するか):
   - 計画系応答のみ → 続行 (= 自律返答可)
   - コード変更 / 外部送信 / Budget 増額のいずれかを含む → 自律実行せず、`/beacon-dm-respond <envelope-id>` を user に提示して降格 (= silent execute 禁止)
5. **DM 返答が必要 (= 計画系) と判断したら**:
   - `beacon bus send --channel <ch> --sender <my_session_id> --in-reply-to <event_id> --payload '<json>'` を発行
   - `--in-reply-to` を **必ず** 付ける (これが budget gate の trigger; 付けないと manual mode 扱いになり gate が効かない = 暴走のリスク)
   - send 成功時に CLI が表示する「budget: M/N, X remaining」を読んで、残数が 0 ならそれ以上送らない
6. **返答が不要と判断したら**: 何もせず次の event を待つ (= sleep)

### 4.3 B芯: 通常会話は確認を挟まず自律返信する (= 構造で担保、ms-100 e-3309)

armed の価値は「AI が席にいなくても自律的に返信する」こと。**通常会話** (= 計画系 / 議論 / 進捗共有、4.1 表の「計画系応答」) の返信では、`/beacon-dm-send` の draft 提示 + y/n 確認を **挟まず**、reply tool / `beacon bus send --in-reply-to` で直接返信する。確認を都度求めるのはプロンプト時代の名残で、armed の意図 (= 無人自律) と矛盾する。

危険カテゴリ (= 外部宛 [別プロジェクト宛の plain DM] / 機密 / action 付き) は、AI が「聞くのを忘れる」かどうかに依存しない: reply 送信経路の **質的ゲート** (`channel/bus-qualgate.mjs`、ms-100 e-3308) が armed 中に構造的に hold し、`isError` の「reply held」メッセージを返す。つまり本 Skill 4.1 / 4.2 の分類ガイダンスは human 向けの動線であって、**最終的な安全は code gate が担保する** (= 二重防御。AI の disposition が揺れても危険カテゴリは物理的に送れない)。

- **通常会話返信** → 確認なしで直接送信 (budget 残量内、B芯)
- **危険カテゴリ** → 質的ゲートが構造的に hold → held が返ってきたら `/beacon-dm-respond` 経由で user に判断を仰ぐ (A gate)

### Trek 参加中の DM 自律応答 (= ms-75 / e-1856 blanket exception)

送信者と受信者が同じ Trek の member の場合、server 側 `dm_gate.py` が `shared_trek_member` 判定で gate を bypass している (= ms-70 例外)。AI は届いた envelope に対し以下のように振る舞う:

- **計画系 / 議論 / 進捗共有 / 設計判断 / 実装計画** に該当する DM → **user 確認なしで自律返信してよい** (= Trek scope 内の事前承認に基づく、CORE doc `b1XOKXQeC0JXaKkO0CRt` の権限委譲ルール)
- **デプロイ / リリース / 外部送信 (Slack / Discord / 外部 email)** を要求する DM → user 確認必須 (= scope 外、安全側 default)
- 判断に迷う場合は安全側 (= user 確認) に倒す。Trek 自律は「明らかに scope 内の計画系」 に限定する

`/beacon-trek-execute` を起動済の場合、本 Skill (armed mode) は同 Trek scope の DM については重ねて処理しないこと (= 二重応答の防止)。

### budget exhausted 時

`bus send --in-reply-to` が exit code 1 + stderr に「exhausted」を返してきたら:

1. 残りの未送信応答は諦める
2. PushNotification でユーザーに「budget exhausted、N 件未対応の DM あり」を通知 (任意)
3. armed (= budget) 状態はそのまま。再 grant すれば channel wake で再び動き出す

### 他の autonomous loop との関係

このセッションが他の autonomous loop (例: ScheduleWakeup ベースの cron loop) を持っている場合、channel からの DM wake と user prompt と Schedule wakeup tick が混ざる可能性がある — 順番は harness が serialize するので競合は起きないが、文脈の切り替わりが多くなる点は注意。

## 制約

- budget grant は **ユーザー確認の後**。Skill が無断で armed (= grant) しない (= 自律送信の許可は human の明示同意が要る)。
- Budget が grant 済でも、ユーザーが明示的に「やめて」と言ったら `beacon bus budget clear` で解除して終了する。
- 別プロセスの Monitor (`beacon bus listen`) は **張らない** (= e-3255、channel と同じ bus backend を叩く配信複製。故障が独立せず、60s timeout で self-terminate して「wake が壊れた」と誤診させるため廃止した)。DM wake は channel が担う。
- 離席フォールバックが将来要るなら、配信複製でなく「未読 DM 検知 → PushNotification」の独立検知役で足す (= e-3255 方針、実装は別途)。
