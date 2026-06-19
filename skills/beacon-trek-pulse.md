---
name: beacon-trek-pulse
description: Trek autonomous loop の 1 tick に対する executor の自己申告 + 5 択 picker (terminal / continue / dm-leader / dm-peer / no-op)。/beacon-trek-execute と並列に存在する短時間 Skill で、 server に「Skill が実際 invoke された」 ground truth を渡す (= Layer 2 observability)。 trek-progress-check event 由来 でも user 引数 `/beacon-trek-pulse <trek-id>` 由来でも起動する。 1 tick = 1 pulse の運用。
version: 0.1.0
triggers:
  - /beacon-trek-pulse
  - trek-progress-check
  - trek pulse
  - Trek 自己申告
---

# Beacon Trek Pulse (= autonomous loop の 1 tick observable ack)

> Trek の自律実行は 3 層で実質強制を達成する設計 (CORE doc `5nfTSmCDVUzD4SLzIhI5`):
> Layer 1 (= narrative) — bus.mjs / Skill body の言い回しで AI に行動を提示する
> Layer 2 (= observability) — server が「Skill が実際 invoke されたか」 を ground truth で知る ← **本 Skill の責務**
> Layer 3 (= enforcement) — TTL 罰則で AI 不協力に server-side で対応
>
> 単一層は弱い、 3 層揃って初めて「AI compliance 依存しない実用設計」 になる。 本 Skill は Layer 2 を担う最小単位。

## soft narrative 自覚化 (= 重要、 SPEC ms-88 受入条件 7)

**この Skill body は self-suggestion であって server / hook 側 enforcement ではない**。 narrative の強さは AI が自発的に行動を選ぶための材料を提供しているだけで、 「protocol が強制した」 framing で説明責任を narrative に転嫁する psychological pattern は破棄する。 行動を選ぶのは AI 自身、 narrative はあくまで材料。

Layer 3 (= server-side TTL 罰則) が動作している前提でこの Layer 2 を運用する。 narrative を信じすぎず、 行動の責任を返す方向で読む。

## いつ起動するか

以下のいずれか:

1. **bus inbox に `channel=trek-progress-check`, `delivery=auto-execute` の event が届いた** (= 通常経路): bus.mjs hardcode 経由で「pulse Skill 推奨」 narrative が context に入っている。 ms-88 / e-2105 で narrative が「12 分以内に pulse / execute / task-state いずれか必須」 と consequence 明示になった。
2. **user が `/beacon-trek-pulse tk-XXXX` を直接呼ぶ**: dogfood / 動作確認用。
3. **`/beacon-trek-execute` Skill の途中で「pulse だけ打って継続」 判断**: executor が working 継続を server に伝えたいだけの場合、 本 Skill を inline 呼び出しせず直接 `beacon trek task-state` で working stamp + 後述 `beacon trek pulse-ack` を直接叩いてよい (= 同等)。

## 引数

`/beacon-trek-pulse <trek-id> [--note "..."]`

- `<trek-id>` (= tk-XXXX): bus event 由来なら payload から、 user 起動なら引数から取得
- `--note` 任意: 後で見返す時のための短い注記 (= 200 文字 cap、 server で truncate)

## cwd 解決

`(project: ...)` パスを additionalContext から優先抽出。 なければ pwd。 ホーム直下なら abort。 **すべての Bash 呼び出しに `cd "$PROJECT_DIR" && ...` を前置する。**

## 前提条件チェック

```bash
cd "$PROJECT_DIR" 2>/dev/null; beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```

`NO_BEACON` なら何もせず終了。

## Step 0: Kickoff Ritual (= 初回 invoke 時のみ、 ms-88 / e-2138)

本 session が当該 Trek で **初めて pulse-ack を打つ前**、 または既存 `pulse-ack` が HTTP 400 `kickoff_required` を返してきた時に走る。 peer (= leader + 他 executor) に対し自分の plan を 4 セクションで宣言する **kickoff DM** を 1 件送り、 完了 endpoint で stamp する。

### なぜ要るか (= 設計背景)

2026-06-20 tk-3045b8d1 dogfood で観測された race (= 3 session が同 cwd を共有して unstaged 編集が 5 分間消失) を構造的に塞ぐ。 同 user の複数 session が peer の状況 (= 「いま誰が何を触っているか」) を共有できていなかったため、 leader / executor が独立に同 file を触り、 git stash 経路で他 session の WIP が一時消滅する事故が起きた。 narrative reinforcement では弱いので:

- **Layer 2 (= server validation)**: `POST /api/treks/<id>/pulse-ack` が `kickoff_pending=true` の session を HTTP 400 `kickoff_required` で reject (= server side が物理 gate、 ms-88 / e-2138 server side)
- **Layer 1 (= Skill body 強制)**: 本 Step 0 が kickoff DM 生成 → 送信 → endpoint stamp の 3 段を順に走らせる。 narrative ではなく順序で強制

この 2 層で「kickoff 未送信のままでは progress 不能」 を構造的に閉じる。

### Step 0.1: kickoff_pending 判定 (= lazy 推奨)

判定経路は 2 通り。 **lazy パターンを推奨** (= overhead 最小):

- **lazy (推奨)**: Step 3 の `pulse-ack` を最初に叩いて、 HTTP 400 `kickoff_required` が返ってきた場合のみ Step 0 に戻る。 GET 不要、 RTT (= round trip 時間) 1 回。
- **eager**: 先に `beacon trek show --json` で `kickoff_status[<自 session id>].pending` を確認。 ただし trek doc 全体を取るので payload が大きい、 cadence 高い session では負荷が積む。

判定材料:

```bash
cd "$PROJECT_DIR" && beacon session id
# → 自セッションの sv-XXX 取得
```

`beacon trek show --json` で取得した `kickoff_status` map に自セッション ID が居ない、 または `pending=true` なら Step 0.2 へ。 居て `pending=false` なら Step 0 はスキップして Step 1 へ。

### Step 0.2: kickoff DM 生成 (= 4 セクション必須)

leader 宛に送る本文を以下 4 セクションで構成する。 1 つでも欠けていたら **送らずに retry** (= race の根本原因「peer 状況の不可視」 を再現するため)。

1. **自分の session_id / agent**: `beacon session id` で取得した sv-XXX、 agent 名 (= Claude Code / Cursor 等)、 担当 user (= email)
2. **担当予定 (= MS / task / scope)**: 取り組む MS と task ID (= `ms-XX` / `e-XXXX`)、 1 行で完了定義 (= 何が land すれば終わりか)
3. **使う worktree path**: `.worktrees/<slug>/` の絶対 path、 branch 名。 同 cwd race 再発を構造的に防ぐため worktree 隔離は必須前提
4. **触らない範囲 (= 明示宣言)**: 他 executor の担当領域 (= 「lib/* は私担当じゃない」 「server/* には触らない」 等) を予め言葉にする。 leader が peer snapshot で reply に使う材料、 また自身のガード role を兼ねる

本文は Markdown、 各セクション 1-3 行で簡潔に。 「読み手は非開発者を含む」 の Beacon 文章原則 (= CORE doc `entry-writing-principle`) を守る。

### Step 0.3: DM を leader 宛に送信

leader の session id は `beacon trek show --json` の `leader_session_id` field から取得:

```bash
cd "$PROJECT_DIR" && LEADER_SID=$(beacon trek show "<trek-id>" --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('leader_session_id',''))")
```

`/beacon-dm-send` Skill 経由で送るのが推奨 (= live-check + budget gate + envelope 自動付与):

```bash
/beacon-dm-send  # 引数: --to "$LEADER_SID" --channel dm --payload '{"text":"<kickoff DM 本文>"}'
```

Trek scope 内 DM なので server 側 `dm_gate.py` が `shared_trek_member` 判定で blanket bypass (= 受信側 user の都度承認なし、 cross-user 承認 ms-70 の例外、 ms-75 / e-1854)。 送信に成功したら返ってきた `event_id` (= bus.send レスポンスの `event_id` field) を控える。 次の Step 0.4 で stamp に使う。

直接 CLI で送る場合 (= Skill 経由が難しい dogfood 等):

```bash
cd "$PROJECT_DIR" && KICKOFF_EVENT_ID=$(beacon bus send --channel dm \
  --to "$LEADER_SID" \
  --payload "$(jq -n --arg text "$KICKOFF_BODY" '{text:$text}')" \
  --json | jq -r .event_id)
```

### Step 0.4: kickoff completion を server に stamp

`POST /api/treks/<trek-id>/kickoff` を叩いて、 server に「DM 送信した」 と stamp する。 これを叩くまで `pulse-ack` は 400 で reject され続けるので、 **送信成功直後に必ず叩く**。

```bash
cd "$PROJECT_DIR" && beacon trek kickoff "<trek-id>" \
  --session-id "$(beacon session id)" \
  --kickoff-dm-event-id "$KICKOFF_EVENT_ID"
```

(= 内部的に `POST /api/treks/<trek-id>/kickoff` body=`{session_id, kickoff_dm_event_id}` を叩く)

成功時 stdout に `{session_id, user_id, pending:false, sent_at, kickoff_dm_event_id}` が返る。 `pending:false` を確認したら Step 1 に進む。 失敗 (= 403 / 401 / network) なら 1 度だけ retry、 それでも失敗するなら note 残しに降格 (= `/beacon-trek-execute` Step 5.5 と同等の graceful stop) して停止。

### Step 0.5: take-over 経路の自動 reset

`POST /api/treks/<id>/take-over` を直前に叩いた場合、 server が新 session に対して `kickoff_pending=true` を強制 reset している (= `trek.reset_kickoff_pending` 経由、 ms-88 / e-2138 server side 仕様)。 つまり take-over 直後の最初の pulse-ack は必ず 400 で reject される。 take-over 後は本 Step 0 を **再度** 経由する想定で動く。 1 度 stamp 済でも take-over で reset されたら kickoff を再送する (= 新 leader / 新 executor の plan は前 session の plan と別物の前提)。

### Step 0.6: 完了確認

`pending:false` を確認したら以降の Step 1 / 2 / 3 / 4 を通常通り進める。 同 Trek 内では二度目以降の pulse-ack で Step 0 は skip される (= server 側 `kickoff_status[sid].pending == false` のため pulse-ack が 200 通る)。

### Step 0 補足: deploy timing と template 例

- **endpoint の live timing**: `POST /api/treks/<id>/kickoff` 経路は **v0.48.0 deploy 以降に live**。 v0.47.0 までは未 deploy のため、 移行期は Skill 側で endpoint 呼び出しを skip して DM 送信だけ行ってもよい (= 暫定運用)。 deploy 後の最初の dogfood で endpoint 経路を含む正常 flow を検証する想定。
- **template 例 (= 4 セクション宣言の良い具体例)**: ms-88 dogfood で executor B (= sv-77e81553-1781883249698-de65dadb) が手作業で送信した kickoff DM (= bus event_id `IYtm6kMKOoCZPQ1CiVya`、 2026-06-20 dogfood) が本機能の **completed shadow dogfood** に該当する。 同 DM の本文 4 セクション構成 (= 自分 / 担当 / worktree / 触らない範囲) をそのまま生成形 template として参照可。 audit trace で event を引けば、 「現に効いた kickoff DM はこの形」 を後段で確認できる。

## Step 1: trek_id の確定

- bus event 由来: TREK ACTION REQUIRED block の `trek_id:` 行から抽出
- user 引数: そのまま
- 不明 → `beacon trek list --joined --json` で 1 件しかなければそれ、 複数なら picker で user に選ばせて確定

確定したら以降は user に「この trek でいいですか?」 を聞かない。

## Step 2: 5 択 picker (= executor の行動の選択肢を AI 自身に提示)

CORE doc `5nfTSmCDVUzD4SLzIhI5` の executor 行動の 5 択 (= terminal / continue / dm-leader / dm-peer / no-op)。 排他選択ではなく、 1 つ以上を選ぶ。 「terminal + dm-leader」 「continue + dm-peer」 等の組合せ可、 「terminal + continue」 と「dm-leader + dm-peer」 のみ禁止 (= 前者は 1 task state を 1 つに collapse できない / 後者は同じ判断要請を 2 経路同送するのは noise)。

| picked_choice | 意味 | server に渡す値 | 続きの action |
|---|---|---|---|
| `terminal` | 自分の task を `done` / `leader_review` / `user_review` のいずれかに遷移 | `terminal` | `beacon trek task-state` を打つ |
| `continue` | 自走継続、 state は `working` 維持、 stamp 更新で TTL リセット | `continue` | `beacon trek task-state <trek> <task> working --note "..."` で stamp 更新 |
| `dm-leader` | working のまま leader に judgment 要請 (= 「上向き相談」 行動) | `dm-leader` | `/beacon-dm-send` Skill で leader 宛 DM、 channel=`dm` |
| `dm-peer` | working のまま peer (= 別 executor) に judgment 要請 (= 「横向き相談」 行動、 ms-88 / e-2140 peer-first culture) | `dm-peer` | `/beacon-dm-send` Skill で peer 宛 DM、 channel=`dm` |
| `no-op` | tick を見たが今回は何もしない (= 明示的に観測したことだけ記録) | `no-op` | 何もしない |

AI 自己判断で 1 つ以上を選ぶ。 user に picker を見せない (= 自律実行の意味)。

### stuck 時の default は「user に問う」 ではなく「peer に DM 相談」 (= ms-88 / e-2140)

「設計判断で詰まった」 「scope 解釈が曖昧」 「次の slice をどう切るか迷う」 等で身動きが取れなくなった時、 **第一選択は user prompt ではなく `dm-peer`** (= 別 executor に相談)。 user は Trek scope に「peer-first culture」 を opt-in 済 (= /beacon-trek-execute の起動承認時点で blanket bypass されている、 ms-70 / e-1854)。 user 起床まで Trek が autonomous に走り続けるためには、 peer 同士で AI 間決定を回す経路を default にする必要がある。

判断境界の優先順 (= /beacon-trek-execute Skill body と整合):

1. **自己判断 (= 即実行)**: SPEC / CORE doc / 既存 task が方向を語っており答えが構造的に明確なら、 DM 不要で即実行
2. **`dm-peer` (= 横向き相談、 第一の DM 経路)**: peer (= 同 Trek 内の別 executor session) に判断を持ちかける。 leader が忙しい / coordination overhead 過多な状況で詰まらせない。 peer 同士で同等知識を期待できる場合に default
3. **`dm-leader` (= 上向き相談、 第二の DM 経路)**: leader 固有判断 (= deploy 順序 / cross-trek 影響 / scope 境界の最終決定) が必要な時のみ
4. **`continue` (= 自走継続)**: 上の 1-3 で答えが出ないなら、 試行的に進めて結果で学ぶ (= 安全な範囲なら fail forward)
5. **user 戻り次第判断待ち (= 真の停止、 例外経路)**: 不可逆 / 嗜好 / cross-Trek 副作用 / 権限 secret のみ。 上の 1-4 で済む判断を user に振らない

「念のため user に聞く」 は 1 ターンの無駄を 30 分単位で積み上げる病理。 Trek 自律権限の存在意義は、 こうした AI 間決定経路を user 介入なしで通すこと。 詳細は /beacon-trek-execute Skill body の「判断境界の優先順」 section も併読。

## Step 3: pulse-ack を server に投げる (= Layer 2 observability の核)

choice を決めたら、 **必ず最初に**以下を実行する。 これが Layer 2 の中心で、 「Skill が actually invoked された」 ground truth を server に渡す。

```bash
cd "$PROJECT_DIR" && beacon trek pulse-ack "<trek-id>" --picked-choice "<choice>" --note "<note>"
```

(= 内部的に POST /api/treks/<trek-id>/pulse-ack に投げる、 ms-88 / e-2106)

成功時 stdout に `{"session_id": "...", "total_acks": N, "last_pulse_ack_at": "...", "last_picked_choice": "...", "history": [...]}` が返る。 これを Step 4 で user 通知に使う。

**重要**: pulse-ack を打たないまま Step 4 以降の action を実行する経路は禁止。 「先に observability を埋め、 行動はそのあと」 の順序が Layer 2 の意味。

## Step 4: 選んだ action を実行

Step 2 で選んだ choice に応じて:

### terminal
```bash
cd "$PROJECT_DIR" && beacon trek task-state "<trek-id>" "<task-id>" "<state>" --note "<reason>"
```
state は `done` / `leader_review` / `user_review` のいずれか。 server が validation + leader DM 発火を担当 (= ms-75 / e-2048 + ms-88 / e-2107)。

### continue
```bash
cd "$PROJECT_DIR" && beacon trek task-state "<trek-id>" "<task-id>" working --note "<short progress note>"
```
state は変えない (= working → working は idempotent、 server 側で last_activity_at が refresh される)。 これで TTL リセット。

### dm-leader
```bash
/beacon-dm-send  # 通常 DM Skill を invoke
# 引数: trek の leader_session_id 宛、 channel="dm"、 本文 = judgment 要請 1 行
```

### dm-peer (= ms-88 / e-2140 peer-first culture)

peer (= 同 Trek 内の別 executor session) に判断要請する。 leader を介さず横向きに相談することで、 user 起床まで Trek が autonomous に走り続ける。

```bash
cd "$PROJECT_DIR" && PEER_SIDS=$(beacon trek show "<trek-id>" --json | python3 -c "
import json, sys, os
d = json.load(sys.stdin)
me = os.environ.get('BEACON_SESSION_ID', '')
leader = d.get('leader_session_id', '')
peers = [m.get('session_id','') for m in (d.get('members') or [])
         if m.get('session_id') and m.get('session_id') != me and m.get('session_id') != leader]
print('\n'.join(peers))
")
# 1 件しかなければそれに送る、 複数なら relevant な担当の peer を AI 自己判断で 1 件選ぶ
/beacon-dm-send  # 引数: --to "<peer-sid>" --channel dm --payload '{"text":"<judgment 要請 1 行>"}'
```

peer 選定のヒント:
- **担当領域で選ぶ**: 相談したい file / MS の担当 peer を kickoff DM (= Step 0) で宣言済の場合はそこから
- **手が空いてそうな peer**: 直近 pulse-ack history で `no-op` が多い peer は手が空いている可能性

peer が複数 + 誰に振ればいいか分からない時は、 leader に「peer assignment の help を要求」 形で `dm-leader` に倒す (= peer-first が default だが leader が peer 選定の指導役にも回れる)。

### no-op
何もしない (= pulse-ack 記録だけ残る)。

組合せ (= terminal + dm-leader 等) は順次実行で OK。 但し `dm-leader + dm-peer` の同送は noise なので避ける (= 同じ判断要請を 2 経路に同送するなら、 まず peer に投げて返事を待ってから leader に escalate する直列順を取る)。

## Step 5: user に結果報告 (= 1 行で簡潔に)

実行した内容を 1 行で報告:

```
✓ pulse-ack 記録: choice=<choice>, total_acks=<N>, last_at=<timestamp>
  action 実行: <terminal / continue / dm-leader / dm-peer / no-op の要約>
```

長い report は不要。 「観測された」 ことが伝わればよい。

## 制約

- **pulse-ack を最初に打つ**: Step 4 action 失敗時でも observability は残る (= "Skill が invoke された" 事実は揺るがない)
- **同 tick で 2 度起動しない**: 1 tick = 1 pulse の運用。 並列 invoke で history が水増しされても server は素直に記録するが、 自身で multiple-fire を避ける
- **`/beacon-trek-execute` と排他ではない**: pulse は短時間 ack、 execute は実装ループ。 同 session が両方持っていてよい
- **soft narrative 自覚**: 本 Skill body の言い回しが強くても、 行動を選ぶのは AI 自身。 narrative に説明責任を転嫁しない

## なぜ存在するか (= 設計背景)

2026-06-19 dogfood (tk-40b0b27c) で判明: 旧 e-2069 (bus.mjs CHANNEL_TO_SKILL hardcode) + e-2068 (forced 3-択 picker) は narrative inject に過ぎず、 「Skill marker が executor 端末に出ていない」 という観察事実を server は知らなかった。 ms-84 executor の正直な内省 (= 「私が initial load の narrative を self-hypnosis 的に保持していただけ」) で核心が判明。

→ 「Skill が実際 invoke された」 ground truth を server に渡す経路として本 Skill が生まれた。 self-report の form は最小: 1 ack = 1 POST。 これで「N tick 中 M 回 pulse fire (= M/N compliance 率)」 が measurable な指標になる。 dogfood 後の retrospect が indirect signal だけだった問題を構造的に解消する。

2026-06-20 tk-3045b8d1 dogfood で更に追加 (= ms-88 / e-2139 / e-2140): 旧 4 択 (terminal / continue / dm-leader / no-op) に **dm-peer** を加えた 5 択に拡張。 詰まった時の default action を「user に問う」 から「peer に DM 相談」 に移すことで、 user 起床まで Trek が autonomous に走り続ける経路を作る。 既存の `dm-leader` が「上向き相談」、 新規 `dm-peer` が「横向き相談」 で responsibility が分担される (= leader 1 名集中ではなく peer-first culture)。

詳細は SPEC ms-88 (= 「Trek における自律性を担保するハーネス設計」) と CORE doc `5nfTSmCDVUzD4SLzIhI5`。
