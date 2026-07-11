---
name: beacon-dm-send
description: 別セッションに対話的に DM を送る Skill (新規送信・返信両対応)。受信トレイから返信、または live + healthy filter で受信者選択 → live 検証 → ペイロード入力 → envelope auto-issue → 送信を 1 フローで完結。手書きで起きがちな受信者ミスや --to / --in-reply-to 忘れ・project跨ぎ忘れ・budget gate 不意打ち・stale session_id 再利用を構造的に排除する。
version: 0.2.0
---

# Beacon DM Send (unified send + reply)

> 別セッションに対話的に DM を送る Skill。新規送信と既存 DM への返信を **1 つのフローで** 扱う。
>
> 旧 `/beacon-dm-reply` は本 Skill に統合 (2026-06-10)。返信時の `--in-reply-to <event_id>` 自動付与、budget gate (= 自動応答の連発防止枠) 自動 handle はそのまま継承。
>
> dogfood で観測された 5 つの手書きミスを構造的に排除する:
> (1) 受信者の project_id 間違い / (2) `--to <session_id>` 忘れ / (3) `--in-reply-to` 付き送信時の budget gate 不意打ち / (4) `--payload` の JSON クオート崩壊 / (5) hook で context に inject された DM event を listen 経路が drain 済で見落とす (旧 dm-reply 病理) / (5b) 会話文脈で覚えた sid を使い回して dead session に送る (= e-1402 / LPS 観察 4 病理)。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

### 守ること

1. **読み手目線 1 行から始める**: 「何が嬉しいか」「何が困るか」をユーザー体験の言葉で。技術用語ではなく価値で書く
2. **横文字 3 段階**:
   - そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP` / `Tauri` / `WebSocket`)
   - 初出時に日本語注: 技術概念 (`allowlist (= 許可リスト)` / `opt-in (= 個別許可)` / `subcollection (= 子コレクション)`)
   - 日本語化が望ましい: 一般概念 (configure → 設定 / receiver → 受信側 / audit → 監査 / hit → 一致 / install → 設置 / merge → 取り込み / deploy → 配置)
3. **ID 参照には文脈**: `e-XXXX` / `UC?` / `ms-XX` は初出に必ず『何の話か』1 行添える
4. **尻切れトンボ禁止**: 主語と述語を省略しない、論理関係を明示

### 詳細

詳しい原則と例は CORE doc `entry-writing-principle` 参照。

---

## 前提条件チェック

Bash ツールで実行:
```bash
__BEACON_ROOT=$(beacon-find-root) && [ -f "$__BEACON_ROOT/.beacon/cloud.json" ] && echo "OK" || echo "NO_BEACON_OR_CLOUD"
```

- ファイル両方とも存在 (`OK`) → 続行
- どちらか欠落 (`NO_BEACON_OR_CLOUD`) → 以下の親切エラーを返して終了:

  - `.beacon/project.json` が無い → 「Beacon プロジェクトのルートで実行してください」
  - `.beacon/cloud.json` が無い (= local mode) → 以下を表示:
    ```
    このプロジェクトは local mode (cloud sync 無し) なので、bus DM は使えません。
      理由: bus は cloud project_id を必要とします (DM は cloud 経由で配信されるため)
      cloud mode に切り替えるには:
        1. beacon auth login            # beacon-ai.dev で認証
        2. beacon cloud setup           # cloud project を作成 / リンク
      local CLI / hook 等の操作は cloud mode 切替後も従来通り使えます。
    ```

  プログラム的に検出する場合は `beacon_cli.skills_helpers.dm_send.check_local_mode_error(cwd_has_cloud_json)` が同じテキストを返すので、これを使ってもよい。

自分の session_id を控えておく (cross-project 跨ぎ判定や、後で `--verify` するときに使う):
```bash
beacon session id
```

自分のプロジェクト ID を控える (Skill 内で `cwd_project_id` と呼ぶ):
```bash
python3 -c "import json,sys; print(json.load(open('.beacon/cloud.json')).get('project_id',''))"
```

---

## Step 0: 起動モード判定 (reply / send)

このセッションの context (= UserPromptSubmit hook が inject する additionalContext や `<channel>` 通知) を見て、DM event が既に届いているかを判定する。

### 判定ロジック

| 観測条件 | 判定 |
|---|---|
| context に `BEACON BUS INBOX` セクション + channel=dm の event が含まれる | `mode = "reply"` 候補。ユーザーに確認: 「<from> からの DM "<preview>" に返信しますか? 別の人に新規送信もできます」→ ユーザー回答で確定 |
| ユーザー入力に「返信」「reply」「reply to」が含まれる (hook 未検知でも) | `mode = "reply"` (= 受信トレイから picker、Step 1-reply の listen 経路へ) |
| 上記いずれでもない | `mode = "send"` (default、Step 1-send の discovery picker へ) |

判定結果を以降の分岐に使う。reply mode の場合、起点となる **parent event の `event_id` / `from_session` / `from_project` / `payload`** を控える (hook 検知時) か、Step 1-reply で取得する (明示起動時)。

### 重要 (= 旧 dm-reply 病理の構造解消)

UserPromptSubmit hook 経由で DM event が context に inject されているとき、`beacon bus listen --once` を呼ぶと **server-side cursor が hook で既に進められて** 同じ event が空に見える (2026-06-10 LPS 観察 1)。本 Skill では hook 検知時に **listen 経路を skip して直接 context から parse** することでこの盲点を構造的に消す。

---

## Step 1: 受信者候補の取得 (mode で入口だけ違う)

### Step 1-send (send mode のみ)

**ms-94 / e-2291 (2026-07-06 default 反転後)**: `beacon bus directory` の
default が全 project 横断 (= 自 user が member の全 project) になったので、
どの CLI 経路を通っても cross-project listing が拾える。 従来の `beacon
sessions` は alias 相当として存続し挙動同等。 「他 project の receiver
session が見えない」 「セッションがいません」 の footgun は構造的に解消。

Bash ツールで実行 (どちらでもよい、下の方が cwd 情報も一貫して見えるので推奨):
```bash
beacon bus directory --live --healthy --since-min 5 --json
```

または、下位互換の等価コマンド:
```bash
beacon sessions --live --healthy --since-min 5 --json
```

JSON 配列が返る。各 session row は `project_id` + `project_name` field 付き
(= 後段で `bus send --project <pid>` に流す)。 空 (`[]`) なら fallback へ。

#### Step 1-send-a: fallback A (旧マシン内 bridge スキャン、ネット障害用)

新 endpoint が API network 障害等で失敗した場合、 旧 v0.25.0+ 経路の同マシン
bridge スキャンにフォールバック (= server 不要、 でも同マシン限定):
```bash
PYTHONPATH="$(dirname $(dirname $(realpath $(which beacon))))" python3 -m beacon_cli.skills_helpers.dm_discover
```

JSON 配列が返る。各 session row は `project_id` field annotated 付き。

#### Step 1-send-b: fallback B (cwd 明示限定モード)

**ms-94 / e-2291**: default が cross-project になったので、 明示的に cwd
project 限定モードに絞りたい場合 (= 差分 audit / cwd 内 debug 用途) は
`--cwd-only` を追加:

```bash
beacon bus directory --live --healthy --since-min 5 --cwd-only --json
```

このモードは 「cwd project の session だけ 見たい」 という明示意図がある時のみ
使う (= 通常は default の cross-project 経路で足りる)。 filter の意味を Skill
起動 user に補足:
「(cwd project のみで listing、 他 project の session は含まれません)」

#### Step 1-send-c: 0 件のときの user-scoped catch-up 提案 (ms-54 / e-2973)

最終的に sessions 配列が **空** の場合、以前は「listening 中の受信先がありません」と即終了していたが、SPEC doc `wJZrmxZGmT7d5lRQvWnE` (= 「DM primitive の使い分け原則: session-scoped = 即時 wake / user-scoped = 次回 catch-up」) の 2 経路使い分け原則に基づき、**user-scoped catch-up 経路を提案する**。

##### Step 1-send-c-1: 送信先 user の候補提示

`beacon member list --json` の結果から、自分以外のプロジェクトメンバー一覧を提示:

```
現在 live なセッションが 1 件もありません。user-scoped で「次回起動時に届く」形で送りますか?

送信先を選んでください:
1. dolphin.orca@gmail.com (editor)
2. alice@example.com (viewer)
3. cancel で中止
```

##### Step 1-send-c-2: user-scoped mode に切替

選択された user の `user_id` を **`recipient_uid` と呼ぶ** (以下、session-scoped 経路の `recipient_sid` と対称に扱う)。**送信 mode を `user-scoped` に設定** し、Step 2 (live 検証) は user-scoped では意味がないので skip、Step 3 (cross-project) は同様に評価する。

##### Step 1-send-c-3: 経路の意味を user に明示

user-scoped 選択後、1 度だけユーザーに以下を提示 (Skill 進行を止めない、通知のみ):

```
Note: user-scoped は「時差配信」経路です (SPEC doc wJZrmxZGmT7d5lRQvWnE)。
  相手 bridge は今すぐこの DM を AI に流しません (= e-1209 filter で drop)。
  相手が次にセッションを起動すると /beacon-session-start の catch-up step が
  この DM を拾って AI に inject します。
  今すぐ届けたい場合は cancel し、相手が live になってから再送してください。
```

#### Step 1-send-d: メンバー情報の取得 (best-effort cross-reference)

Bash ツールで:
```bash
beacon member list --json
```

session.actor.email がメンバーの email と一致するなら、その人の email + role を picker 行に添える。一致しないなら machine / agent のみで表示する。member list が空でもエラーにせず無視する。

#### Step 1-send-e: 候補表示と選択 (identity 配線、ms-93 Phase 3)

**session_id (sid) は使い捨ての経路トークンであって identity ではない** (bridge 再起動 / Codex daemon の re-mint で sid は変わる)。picker で生 sid を選ばせると、選んでから送るまでの間に sid が churn して stale に飛ぶ事故が起きる (2026-07-07 に DNS 切替 DM と bug 報告 DM が 2 回 stale sid に消えた)。そこで **人間には「誰の / どの作業場か」を stable identity で見せ、送信時に現在生きている sid へ解決し直す**。

Step 1-send で取得した directory rows (JSON 配列) を identity helper に通してラベル化する:

```bash
# ROWS = beacon bus directory --live --healthy --since-min 5 --json の出力
PYTHONPATH="$(dirname $(dirname $(realpath $(which beacon))))" \
  python3 -m beacon_cli.skills_helpers.identity_resolve label <<< "$ROWS"
```

返る各要素は `{session_id, label, project_id, machine, cwd, agent_kind, last_poll_at}`。`label` は `describe_candidate` の「種別 on マシン in cwd (started …, pid …)」形式。これを軸に表示し、生 sid は補助情報 (括弧) に降格する:

```
1. claude-code on WORKMACHINE in D:\Projects\beacon (started 2026-07-07T21, pid 222) → dolphin.orca@gmail.com (editor)  [sid=workmachine-…8ff3c173, healthy age 1s]
2. codex on CFGW5D79LL in /Users/…/beacon (started 2026-07-07T05, pid 68732)  [sid=codex-…65dbdb5e, healthy age 5s]
3. claude-code on mac-mini in /Users/…/beacon  [sid=6d270a08…, stale age 12m] → (member unknown)
```

Step 1-send-d のメンバー email / role は従来通りラベル末尾に添える。member 一致は `machine` / actor.email で cross-reference する。

ユーザーに尋ねる:
```
どの受信者に送りますか?
- 番号 (1, 2, 3, …) でピック
- session_id を直接貼り付け (= identity を経由せず生 sid 指定、上級者向け)
- cancel で中止
```

選択された行の **identity key** (`machine` / `cwd` / `agent_kind` / `project_id`) と、その時点の `session_id` (= `recipient_sid`)・`project_id` (= `recipient_project_id`) を控える。identity key は Step 2 / Step 7 の送信時 re-resolve で使う。

### Step 1-reply (reply mode のみ)

hook で context に DM が inject されている場合 (Step 0 で判定済):
- context から `event_id`, `from_session`, `from_project`, `payload` を抽出
- 複数 DM event がある場合は picker に並べてユーザーに選ばせる:
  ```
  受信トレイに 3 件の DM があります。どれに返信しますか?
  1. [from=aa60cc21…] "バンドル届いてます…"
  2. [from=6d270a08…] "Win → Mac テスト送信 #2"
  3. cancel で中止
  ```

hook 未検知だがユーザーが明示的に reply mode を要求した場合のみ inbox listen を使う:
```bash
beacon bus listen --once --channel dm --json
```
**`--auto-ack` を渡さない** — peek のみで cursor を動かさない (= 後で hook や別 Skill が同じ event を処理可能)。

選択された event の全フィールドを以下に控える:
- `parent_event_id = event_id`
- `recipient_sid = from_session`
- `recipient_project_id = from_project`
- `parent_payload = payload` (Step 5 で引用に使う)

---

## Step 2: 受信者の live 検証 (両モード共通)

### Step 2-pre: identity 送信時 re-resolve (send mode で identity key を控えている場合、ms-93 Phase 3)

Step 1-send-e で **identity key** (machine / cwd / agent_kind / project_id) を控えている場合、picker 表示から送信までの間に sid が churn している可能性があるので、**送信直前に現在生きている sid へ解決し直す**。fresh な directory を取り直して helper に通す:

```bash
FRESH=$(beacon bus directory --live --healthy --since-min 5 --json)
PYTHONPATH="$(dirname $(dirname $(realpath $(which beacon))))" \
  python3 -m beacon_cli.skills_helpers.identity_resolve resolve \
    --machine "<machine>" --cwd "<cwd>" --agent-kind "<agent_kind>" \
    --project "<project_id>" <<< "$FRESH"
```

返る `current_sid` と **`current_sid_stale`** (= その sid が直近 `stale_threshold_s` 秒 (既定 30 秒) 以内に poll していなければ `true`、ms-93 / e-2519 AC 5) を使う。`current_sid_stale` は「解決はできたが、その session は 30 秒以上 heartbeat しておらず死んでいる疑いが濃い」を表す。`--now` を渡さなければ helper が現在 UTC で判定する:

| 観測結果 | 動作 |
|---|---|
| `current_sid` が非空・`current_sid_stale=false` で、控えていた `recipient_sid` と同じ | churn なし・生きている。そのまま続行 |
| `current_sid` が非空・`current_sid_stale=false` だが `recipient_sid` と違う | **sid が churn した**。`recipient_sid` を `current_sid` に更新し、ユーザーに 1 行通知:「相手の sid が更新されていたので最新の `<current_sid>` に送ります (identity: <label>)」。以降 Step 2 本体の live 検証は skip 可 (= 既に live directory から解決済) |
| `current_sid` が非空だが **`current_sid_stale=true`** | 解決はできたが相手は 30 秒以上 heartbeat しておらず死んでいる疑い。**そのまま送らず** soft-warn:「相手の最新 sid `<current_sid>` は約 `<poll_age_s>` 秒 heartbeat が途絶えています (死んでいる可能性)。送っても配送されないかもしれません。送る / 中止?」。ユーザーが送るを選んだ時だけ続行 (= dead sid への sailent 配送を構造的に止める、§8-G FINDING #3) |
| `current_sid` が空 (`candidates` も空) | その identity は現在 live でない。Step 2 本体の soft-warn 経路へ流す (= 生 sid のまま送るか中止) |
| `candidates` が 2 件以上 | 同 identity key の並走 session が複数。label で人間に選び直させる (= 通常は agent_kind + cwd で 1 件に絞れるはずなので稀)。各 candidate の `stale` フラグで死んでいる方を除外する |

生 sid を直接貼られた (= identity を経由しない) 場合、この Step は skip して Step 2 本体の live 検証へ進む。

**ここが旧 dm-send / dm-reply 両方に共通する live-check の責務**。CLI 側にも e-1402 (= 2026-06-10 LPS 観察 4 で起票された CLI-side live-check gate) で同じ防御が入っているが、Skill 側でも **送信前に明示的に** 検証することで「dead session に DM を投げる」を構造的に防ぐ (defense in depth)。

Bash ツールで実行:
```bash
PYTHONPATH="$(dirname $(dirname $(realpath $(which beacon))))" python3 -m beacon_cli.skills_helpers.dm_discover
```

返ってきた JSON 配列で `recipient_sid` を探す:

| 観測結果 | 動作 |
|---|---|
| `recipient_sid` が live+healthy で見つかる | そのまま続行 |
| `recipient_sid` は見つからないが、**同じ user (= 同 email / 同 machine) の別 live session** がある | 代替候補を提示: 「相手の `<sid>` は live じゃないようです。代わりに同じユーザーの live セッション `<alt_sid>` (machine=...) に送りますか? / そのまま元の sid に送る / 中止」 |
| 完全に live セッションが無い (= 相手が全部 down) | soft-warn: 「相手の session は現在 live ディレクトリに見えません。送信は通るかもしれませんが、配送されない可能性があります。続けますか? (yes / cancel)」 |

reply mode で「代替候補に送る」を選んだ場合、`recipient_sid` を新しい sid に置き換える。**`parent_event_id` (in-reply-to) は変更しない** — それは元の DM への返信という意味的紐付けで、宛先 sid とは独立。

### opt-out

ユーザーが `/beacon-dm-send --skip-live-check` で起動した場合は Step 2 全体を skip。CI / 自動運用想定。default は **検証あり**。

---

## Step 3: cross-project 判定 (両モード共通)

`recipient_project_id` と Step 0 で取得した `cwd_project_id` を比較する:

| 比較 | 動作 |
|---|---|
| `recipient_project_id == cwd_project_id` | 同プロジェクト、`--project` フラグ不要 |
| `recipient_project_id != cwd_project_id` (両方非空) | cross-project。**ユーザー確認**: 「相手は project_id=<recipient_project_id> です。そちらに <送信 / 返信> を投げます (cwd は <cwd_project_id>)」→ yes なら `cross_project_id = recipient_project_id` を保持 |
| `recipient_project_id` が空 | cwd を仮定、`--project` フラグ不要 |

cross-project ケースで no を選ばれたら中止。

---

## Step 4: budget gate の確認と自動 grant (reply mode のみ)

`--in-reply-to` 付きの送信は budget gate に当たる (e-1000 = AI による無限自動応答を防ぐ仕組み)。**ユーザーが「返信する」と明示的に Skill を起動した時点で「人間の意思 = 返信を 1 回許可する」と解釈** し、budget が 0 なら **silent に grant してから送信** する。

send mode (= `--in-reply-to` なし) ではこの Step は skip。

Bash ツールで:
```bash
beacon bus budget show --json
```

返ってきた JSON を見て:

| 状態 | 動作 |
|---|---|
| `{"armed": false}` (default) | `beacon bus budget grant --turns 3` を実行 → ユーザーに「自動応答 budget を 3 turn grant しました」と 1 行通知 |
| `armed: true` で `remaining > 0` | そのまま続行。「現在 budget: X/Y remaining」を 1 行表示 |
| `armed: true` で `remaining == 0` (使い切り) | `beacon bus budget grant --turns 3` を実行 → ユーザーに「budget を再 grant しました」と通知 |

grant コマンドが失敗した場合 (ネットワークエラー等) は、ユーザーにエラーをそのまま提示して中止。**budget が空のまま `bus send --in-reply-to` を打ってはならない** (exit 1 になって意味不明な状態になる)。

---

## Step 5: 本文入力 + 統合 draft + 1 prompt 確認 (両モード共通) — ms-92 e-2181 で旧 Step 5 / 5b / 5c / Step 6 を集約

**変更の意図 (= ms-92 e-2181)**: 旧設計は 「本文 prompt → action prompt → template prompt → draft + yes/edit/cancel prompt」 と user が同じ内容を 2 回以上見る冗長経路だった。 user の起動メッセージで recipient + content が既に明示されていれば追加対話を完全 skip し、 normal な DM は 「draft + 1 confirm」 の 1 prompt で送れるようにする。 安全境界 (= cross-project / sensitivity high / 外部 user 初回 等) は 1 prompt 内 inline の警告表示 + 危険経路では明示 force-yes 文言入力 で構造的に維持する (= 2 prompt 化しない、 安全性は犠牲にしない)。

**ms-68 / e-1643 補足 (= entry-writing principle の draft 表示)**: 本 Step は draft 提示型 (= 送信前に full argv + payload 本文をユーザーに見せて yes/edit/cancel を取る形)、 ms-68 SPEC の「書き込み直前の draft 表示」要件を満たす。 draft 表示前に payload 本文について self-review 4 原則 (読み手目線 1 行 / 横文字 3 段階 / ID 参照に文脈 / 尻切れトンボ禁止) を 1 度通す。 **特に DM は受信側 AI が「非開発者の代理として読む」可能性がある (= 受信側 AI は親プロジェクトの文脈を持たない)** ため、横文字濫用 / ID 参照に文脈なし は致命的。 違反があれば `edit` で Step 5a に戻して書き直す。

---

### Step 5a: 入力経路の判定 (= 起動メッセージから recipient + content を拾えるか)

user の Skill 起動メッセージ (= /beacon-dm-send の引数や直前 user 発話) を読み、 以下が全て揃っているか確認:

- **recipient** (= picker で選んだ session_id、 または起動メッセージで「<sid> に送って」 / 「@<machine名> に」 等で明示)
- **content** (= 送信本文、 起動メッセージで「<本文>」 のように明示)
- **action** / **template** 種別 (= 不要が default、 明示無ければ skip 扱い)

判定結果による分岐:

| 状態 | 分岐 |
|---|---|
| 全部揃っている | Step 5b (= 対話入力) を skip、 直接 Step 5c (= draft + 1 confirm) へ |
| 一部欠落 | Step 5b で **不足分のみ** 対話入力 (= 既に決まっている項目は再質問しない) |

これにより 「user が完全形式で叩いた DM 送信」 は 1 confirm round-trip、 「不完全な起動」 は対話 + 1 confirm の 2 path で同じ最終 confirm prompt を共有する (= AC #2 / #6)。

---

### Step 5b: 不足分のみ対話で取得 (= 必要時のみ実行)

Step 5a で欠落と判定された項目だけを聞く。 複数項目を 1 prompt に束ねて尋ねてよい (= 「本文 + action + template の有無 を一括で書いてください」 等)。

聞く可能性のある項目:

- **本文** (不足時): 「本文を入力してください (改行 OK、 空行 + Enter で送信)」
- **reply mode の引用** (任意、 default yes): 親 DM の先頭 3 行を引用形式で本文先頭に添えるか
- **send mode の action 指定** (任意、 default なし): 「受信側に auto-execute 権限を渡す action 名はありますか? 普通は空 Enter」 (reply mode では skip = 副作用権限付与は新規送信の役割)
- **テンプレート種別** (任意、 default skip): 「pr-review / op-result / skip」 の picker (詳細は Step 5b-ext)

入力された本文は **そのまま文字列として** 保持する。 **JSON エスケープは Skill が自動で行う** (= user が `--payload '{"text":"..."}'` 形式で書く必要は無い)。

reply mode で引用 yes を選んだ場合の引用フォーマット:
```
[親 DM 引用]
from: <from_session の頭8文字>
> <親 payload.text の先頭3行 or 全体>
```

#### Step 5b-ext: テンプレート骨格 (= 選択された場合のみ Step 5c の draft 組み立て前に適用)

繰り返し送る種類の DM (= PR レビュー依頼 / Operation 結果共有) は、 毎回ゼロから本文を書くと受信側が「何を見ればいいか」 を読み取るコストが上がる。 ms-80 e-1820 で導入した骨格テンプレートを Step 5a の起動メッセージ または Step 5b の picker で選択された場合のみ適用する。

##### テンプレート 1: pr-review

PR レビュー依頼。 受信側 reviewer は 「PR 番号 / 何を変えたか / どこを見てほしいか / 緊急度」 を 1 メッセージで掴める必要がある。 以下を順に聞いて埋める (= 既に Step 5b で本文を書いていれば「要点」 欄として再利用):

```
PR URL or 番号: (例: https://github.com/kurogin23mech-source/beacon/pull/157 or #157)
1 行サマリー: (= この PR で何ができるようになるか、 Step 5b 本文があれば流用)
注視ポイント: (例: lib/auth.py の profile resolver 周辺、 AWS profile 経路)
受入条件 / AC: (= 何が満たされれば merge OK か、 SPEC doc id / task id があれば添える)
緊急度: (= asap / today / this-week / 任意期日)
```

埋まったら以下の骨格で payload.text を組み立てる (= Step 5b 入力を上書き、 user には Step 5c draft で見せて confirm):

```
[PR レビュー依頼]
PR: <pr_url>
要点: <1 行サマリー>

見てほしいところ:
<注視ポイント>

merge 条件:
<受入条件 / AC>

緊急度: <urgency>

(受信側 AI へ: 上記の見てほしいところ + merge 条件 を起点に /review を起動し、 approve / request-changes / reject の判断材料を整理してください)
```

##### テンプレート 2: op-result

Operation 自律実行 (= ms-60 envelope auto-execute / ms-66 server-side scheduler) の結果共有。 送信側 AI が定期実行の record を user or 他セッションに通知する用途。

```
op-id: (例: op-1)
実行時刻: (= 自動補完可、 ISO8601)
結果: ok / warning / error
何を観測したか: (= 1-3 行、 incident があれば e-id を添える)
次のアクション: (= 自動 close / user 承認待ち / 別 Operation 起動 等)
```

骨格:
```
[Operation 実行結果]
op: <op-id>
ts: <timestamp>
status: <result>

観測:
<観測内容>

次のアクション: <next>
```

##### テンプレート不要なケース

- 1 行の問いかけ / 雑談 / 確認 → テンプレ skip、 Step 5b 本文をそのまま使う
- 既に Step 5a 起動メッセージでテンプレート骨格を入力済 → 二重適用しない、 skip
- 受信者が同一 user の並走セッション (= 自分↔自分 multi-machine) → 骨格は省略可、 要点だけで足りる

テンプレートはあくまで **読みやすさの骨格** であり、 Step 5c の draft 表示で user が自由に edit できる。 テンプレ適用後でも横文字 3 段階 / ID 参照に文脈 の self-review 4 原則は同じ強度で適用する (= 横文字濫用が骨格に隠れて見落とされやすい)。

---

### Step 5c: 統合 draft + 1 prompt 確認 (= 旧 Step 6 を本 Step に inline 化)

Step 5a / 5b までで揃った材料を draft で組み立てて、 **1 prompt で send/edit/cancel を取る**。 警告表示・budget 状態・template 適用は全て同じ 1 prompt 内に inline で並べる (= AC #3、 2 prompt 化しない)。

#### draft 表示フォーマット

```
─── DM 送信 draft (1 confirm 経路、 ms-92 e-2181) ───
mode:        send | reply
recipient:   <sid 頭 8 文字>… [machine=<machine>, project=<proj_label>]
in-reply-to: <parent_event_id>            ← reply mode のみ

[警告] (= 該当時のみ inline 表示、 不要なら省略)
  ⚠ live check: 相手は stale (age 12m)、 配送されない可能性 (= Step 2 で soft-warn の場合)
  ⚠ cross-project: 相手 project=<rid>、 cwd=<cid> (= Step 3 で cross-project の場合)
  ⚠ sensitivity high: 外部 user 初回 / 機密内容 (= 後述 Step 5d で明示入力 強制)
  budget:      armed=true, 3/3 grant 済               (= reply mode、 Step 4 で grant した場合)
  budget:      armed=true, 2/3 remaining              (= reply mode、 既存 budget があった場合)

本文 (送信されるもの、 self-review 4 原則を 1 度通す):
> <本文 line 1>
> <本文 line 2>
> ...

action (任意): <action_name 1>, <action_name 2>   ← 指定時のみ
template:      pr-review / op-result / なし

----
組み立て argv:
  beacon bus send --channel dm --to <sid> --payload '{"text":"..."}' [--project <id>] [--in-reply-to <eid>] [--action ...] --json

送信しますか? (yes / edit / cancel)
```

#### user 応答の解釈

- `yes` → Step 7 (= 実 execute) へ
- `edit` → Step 5b に戻る (= 本文 / action / template の修正したい項目だけ聞き直す、 完成済項目は引き継ぐ)
- `cancel` → 中止

---

### Step 5d: 危険経路の force-confirm (= AC #4 維持、 安全境界の構造的保持)

Step 5c draft 内警告が以下のいずれかを含む場合、 単一 `yes` ではなく **明示 force-yes 文言** を 1 prompt 内 inline で要求する (= 安全境界を犠牲にしない構造的歯止め):

| 条件 | 検知方法 |
|---|---|
| cross-project | `recipient_project_id != cwd_project_id` (= Step 3 で yes 既選択でも本 Step で再確認) |
| 外部 user 初回 | member list に email 不在の recipient への **初回** 送信 (= dm 履歴に該当 sid との往復ゼロ) |
| sensitivity high | 起動オプション `--sensitivity high` or 起動メッセージで「機密」 「sensitive」 「confidential」 を含む |

force-confirm の inline 文言 (= Step 5c の draft 末尾に直接添える):

```
⚠ 上記 draft は警告付きです (cross-project / 外部 user 初回 / sensitivity high のいずれか)。
   通常 yes ではなく、 「risk understood」 と入力してください (= cancel で中止)。
```

これにより danger 経路は 「draft 表示 + 警告閲覧 + 明示文言入力」 を同じ 1 prompt round-trip で完結し、 通常 DM の 1 prompt yes 経路と構造を共有する (= 安全性は 「prompt の数」 ではなく 「force 文言の入力」 で担保)。

normal な DM (= cross-project でも 外部 user 初回 でも sensitivity high でもない) は `yes` 1 文字で送れる。

---

## Step 7: 送信実行 (両モード共通)

Bash ツールで上記コマンドを実行する。JSON 出力 (`--json`) を有効化することで stdout に event_id + delivery + envelope + budget 情報が返るので、結果を読む。

### user-scoped mode の argv 差分 (= Step 1-send-c 経由の場合)

Step 1-send-c を通ってきた場合は session-scoped の `--to <recipient_sid>` の代わりに **`--to-user <recipient_uid>`** を使う (SPEC doc `wJZrmxZGmT7d5lRQvWnE` の 2 経路使い分け原則)。他のフラグ (`--payload`, `--in-reply-to` 等) は同一。

例:
```bash
beacon bus send --channel dm --to-user 113315684036322209061 \
  --payload "$(cat /tmp/payload.json)" [--project <id>] --json
```

user-scoped 送信では Step 8 の receipt 確認は **delivered 止まりが正しい** (opened stamp は次回相手起動時に立つ) ため、Step 8 の解釈ガイドに従い「これは時差配信なので opened ✗ は正常」と report する。

**注意**: `--payload` の値は Python の `json.dumps({"text": "<本文>"}, ensure_ascii=False, separators=(",", ":"))` 相当で組み立てる。`Bash` ツールの引数に渡すとき、シェルに渡る形にしてエスケープに注意 (改行は `\n` に変換される)。実装上は Python heredoc (= 必ず quoted EOF を使う、後述「heredoc 注意」参照) で payload JSON を構築 → 環境変数経由でコマンドに渡すか、シングルクォートで囲んでそのまま渡す。

### heredoc 注意 (= e-1401 で起票された病理回避)

Bash 内の `python3 -c "..."` の double-quote 内に backtick (`` ` ``) を含む文字列を書くと、zsh が command substitution として展開して本文が抜け落ちる。複雑な payload を書くときは必ず **quoted heredoc** を使う:

```bash
cat > /tmp/payload.json <<'EOF'
{"text": "...本文 (改行 / バッククォート OK) ..."}
EOF
```

`<<'EOF'` のシングルクォート付き形式が必須 (`<<EOF` だと展開される)。

---

## Step 8: 送信直後の receipt 確認 (両モード共通、ms-54 / e-1348)

`beacon bus send --json` の stdout から `event_id` を取り出し、送信から数秒待ってから `beacon bus status` で 3 段 (sent / delivered / opened) を確認する。これにより「送ったつもりが届いていない」を構造的に検知できる (200 OK と delivery 成立は別物)。

Bash ツールで実行:
```bash
sleep 4
beacon bus status <event_id> [--project <id>]
```

`sleep 4` の根拠: 受信側 bridge の poll 周期 default 2 秒 + 2 秒のマージンで、ack 経路を持つ受信者 (= bridge v0.26.0 以降) なら opened まで stamp されているケースが多い。

**この `sleep 4` が唯一の verification 待機** — `(not yet)` のままでも **追加で sleep してはならない**。以前は `sleep 8` の retry を入れていたが (e-1400)、ack 経路を持たない受信者 (= 古い bridge < v0.26.0 / 非 bridge subscriber / CI 等の PE-bridge スタイル) では常に空待ちになって 12 秒の死時間を生むだけだった。

`(not yet)` のままなら、状態を **そのまま** ユーザーに報告し、以下のヒントを添える:

```
delivered / opened がまだ stamp されていません。以下の可能性があります:
  - 受信側 bridge が古い (< v0.26.0、ack 経路非対応)
  - 受信側が non-bridge subscriber (= CI / 自動運用 / PE-bridge スタイル)
  - 単に届くのが遅い (= 数秒以内に手動で `beacon bus status <event_id>` で再確認可)

receipt 不要と分かっている相手なら次回から `/beacon-dm-send --no-verify` を推奨。
```

「もう一度待ってみる」を Skill 側で勝手にやらない。空待ちを増やすより、状況を honest に出す方が UX として正しい (e-1400)。

### Step 8.1: 結果解釈と報告

`beacon bus status` の出力から 3 段を読み取り、ユーザーに簡潔に報告:

```
✓ DM <送信 / 返信> 完了
  event_id:    <event_id>
  to:          <recipient_sid>
  in_reply_to: <parent_event_id>   ← reply mode のみ
  delivery:    <propose-to-ai / auto-execute / notify-user-only>
  envelope:    T1 (auto-issued) / なし
  budget:      <used>/<total>, <remaining> remaining   ← reply mode のみ

receipt (3 段):
  ✓ sent       <timestamp>
  <✓ or ✗> delivered  <timestamp or (not yet)>  [by <session_id>]
  <✓ or ✗> opened     <timestamp or (not yet)>  [by <session_id>]
```

### Step 8.2: 解釈ガイド (delivered / opened が立たない時)

| 状態 | 意味 | 次のアクション |
|---|---|---|
| sent ✓ / delivered ✗ / opened ✗ | 受信側 bridge が /unread を fetch していない | 相手の `bridge=True` を directory で確認、`channel install` 漏れの可能性 |
| sent ✓ / delivered ✓ / opened ✗ (**session-scoped**) | bridge は受け取ったが filter chain で drop or mcp.notification 失敗 | 相手の channel allowlist (`BEACON_CHANNEL_ALLOWLIST`) と DM の channel が一致しているか、受信側 session が allowlist に入っているか確認 |
| sent ✓ / delivered ✓ / opened ✗ (**user-scoped**) | **正常挙動** — user-scoped は時差配信、相手 bridge が e-1209 filter で意図的に drop、opened は次回起動時に立つ | 追加アクション不要。SPEC doc `wJZrmxZGmT7d5lRQvWnE` の 2 経路使い分け原則参照 |
| sent ✓ / delivered ✓ / opened ✓ | 完全到達 | 完了 |
| sent ✓ / delivered ✗ / opened ✗ かつ 8 秒待っても変化なし | 受信側 bridge が **古い beacon バージョン** で ack 経路を持たない可能性 | 相手の `actor.agent.version` を directory で確認 (v0.26.0 未満は receipt 非対応)、`pipx upgrade beacon-ai` (PyPI 名は beacon-ai、内部 CLI は beacon) または `brew upgrade beacon` を促す |

これにより送信者は「届いていない / 開封されていない」を **送信時に即時** に検知できる (e-1348 設計の本質的価値)。

### `--no-verify` オプション (= 受信側が ack 経路を持たないと分かっているとき推奨)

以下のいずれかが事前に分かっている場合、ユーザーに **`/beacon-dm-send --no-verify` の使用を推奨** する:

- 受信側が古い bridge (< v0.26.0、ack 非対応)
- 受信側が non-bridge subscriber (= CI / 自動運用 / PE-bridge スタイル)
- 大量送信 / 自動化フローで sleep の累積コストを避けたい
- 「届いたか」より「送れたか」だけ知りたいケース

`--no-verify` 指定時は Step 8 の `sleep 4 + bus status` を完全 skip し、Step 8.1 の `receipt (3 段)` セクションも省略する。送信完了 (event_id + delivery) だけ報告して終了する。

default は **verify あり** (UX 上、receipt 確認しないと「届いた」と思い込む病理を再生産するため)。ただし「受信側に ack が無い」と判明している局面で毎回 4 秒待つのは無駄なので、Skill 側もユーザーがそういう局面を述べたら積極的に `--no-verify` を提案すること。

---

## エラー時の挙動

| エラー | 対応 |
|---|---|
| `bus directory` / `dm_discover` が API エラーで失敗 | エラーメッセージをそのまま提示して終了。`beacon cloud status` の確認を促す |
| 候補 0 件 (live filter でも空) | 「相手の listen が立っていない可能性。MCP 接続 or `bus listen` 起動を案内してください」と返して終了 |
| `bus budget grant` が失敗 (reply mode) | エラーを提示して中止 (budget 無しで send しない) |
| `bus send` が exit 1 (envelope reject / network failure) | エラーをそのまま提示。`--no-envelope` を試すかどうか聞く |
| `bus send` が exit 1 with "exhausted" (reply mode) | budget consume race の可能性。1 回だけ再 grant して retry。それでも失敗なら中止 |
| `bus send` が exit 1 (server 404) | サーバが古い (envelope 未対応)。`--no-envelope` で再試行を提案 |

---

## 制約

- このSkill は受信者選択を **対話的に必ず通す**。`session_id` を直接渡しても候補表示は省略しない (= 「いま生きてる相手か」を必ず人間に見せる)。
- Step 2 の live 検証は両モード共通の必須 step。`--skip-live-check` で opt-out 可能だが default は検証あり。
- `--action` 付き送信は **send mode のみ、明示確認後**。デフォルトでは渡さない。reply mode では使わない。
- 自分自身の session_id への送信は無意味なので警告する (受信側が自分 = `beacon session id` の出力と一致する場合)。
- cross-project 送信は **常に明示確認**。silently 飛ばさない。
- reply mode の budget grant は `--turns 3` に固定 (一発返信 + 余裕 2 turns)。大きな数を grant したい場合は明示的に `/beacon-bus-armed` を使うべき。
- 返信本文の JSON エスケープは Skill 側で行う。ユーザーが `--payload '{"text":"..."}'` 形式で書く必要は無い。
- 同じ event に対する複数返信は禁止しない (続けて返信したいケースもあるため)。ただし budget は再 grant されない限り 1 回ずつ消費される。

---

## 関連 Skill

- `/beacon-bus-armed` — 自律 DM 応答モード (Monitor で listen を armed、N turn 自動返信)。
- `/beacon-bus-budget` (将来) — budget 管理専用 Skill (現在は CLI 直接)。
- (旧 `/beacon-dm-reply` は 2026-06-10 に本 Skill へ統合済)
