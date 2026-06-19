---
name: beacon-trek-pulse
description: Trek autonomous loop の 1 tick に対する executor の自己申告 + 4 択 picker。/beacon-trek-execute と並列に存在する短時間 Skill で、 server に「Skill が実際 invoke された」 ground truth を渡す (= Layer 2 observability)。 trek-progress-check event 由来 でも user 引数 `/beacon-trek-pulse <trek-id>` 由来でも起動する。 1 tick = 1 pulse の運用。
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

## Step 1: trek_id の確定

- bus event 由来: TREK ACTION REQUIRED block の `trek_id:` 行から抽出
- user 引数: そのまま
- 不明 → `beacon trek list --joined --json` で 1 件しかなければそれ、 複数なら picker で user に選ばせて確定

確定したら以降は user に「この trek でいいですか?」 を聞かない。

## Step 2: 4 択 picker (= executor の 3 行動の選択肢を AI 自身に提示)

CORE doc `5nfTSmCDVUzD4SLzIhI5` の executor 3 行動 (= terminal / continue / dm-leader) に **「明示的 no-op」** を加えた 4 択。 排他選択ではなく、 1 つ以上を選ぶ。 「terminal + dm」 「continue + dm」 等の組合せ可、 「terminal + continue」 のみ禁止 (= 1 task の state を 1 つに collapse できない)。

| picked_choice | 意味 | server に渡す値 | 続きの action |
|---|---|---|---|
| `terminal` | 自分の task を `done` / `leader_review` / `user_review` のいずれかに遷移 | `terminal` | `beacon trek task-state` を打つ |
| `continue` | 自走継続、 state は `working` 維持、 stamp 更新で TTL リセット | `continue` | `beacon trek task-state <trek> <task> working --note "..."` で stamp 更新 |
| `dm-leader` | working のまま leader に judgment 要請 (= 「相談」 行動) | `dm-leader` | `/beacon-dm-send` Skill で leader 宛 DM、 channel=`dm` |
| `no-op` | tick を見たが今回は何もしない (= 明示的に観測したことだけ記録) | `no-op` | 何もしない |

AI 自己判断で 1 つ以上を選ぶ。 user に picker を見せない (= 自律実行の意味)。

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

### no-op
何もしない (= pulse-ack 記録だけ残る)。

組合せ (= terminal + dm-leader 等) は順次実行で OK。

## Step 5: user に結果報告 (= 1 行で簡潔に)

実行した内容を 1 行で報告:

```
✓ pulse-ack 記録: choice=<choice>, total_acks=<N>, last_at=<timestamp>
  action 実行: <terminal / continue / dm-leader / no-op の要約>
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

詳細は SPEC ms-88 (= 「Trek における自律性を担保するハーネス設計」) と CORE doc `5nfTSmCDVUzD4SLzIhI5`。
