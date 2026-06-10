---
name: beacon-dm-send
description: 別セッションに対話的に DM を送る Skill (新規送信・返信両対応)。受信トレイから返信、または live + healthy filter で受信者選択 → live 検証 → ペイロード入力 → envelope auto-issue → 送信を 1 フローで完結。手書きで起きがちな受信者ミスや --to / --in-reply-to 忘れ・project跨ぎ忘れ・budget gate 不意打ち・stale session_id 再利用を構造的に排除する。
version: 0.2.0
triggers:
  - /beacon-dm-send
  - /beacon-dm
  - DMを送る
  - DM 送信
  - DM 送って
  - DMで連絡
  - bus send
  - 別セッションに連絡
  - send dm
  - DMに返信
  - 返信する
  - reply dm
  - DM 返信
  - bus reply
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
test -f .beacon/project.json && test -f .beacon/cloud.json && echo "OK" || echo "NO_BEACON_OR_CLOUD"
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

**v0.25.0 以降**: 同マシン上で動いている全 bridge を ps + lsof で列挙、各 cwd の `.beacon/cloud.json` から project_id を読み、全 project の bus directory を集約する。これで cwd 以外のプロジェクトに居る session も候補に出る。

Bash ツールで実行 (`PYTHONPATH` を beacon repo root に固定する — raw-source install で site-packages に beacon_cli が無い場合の `ModuleNotFoundError` を防ぐ):
```bash
PYTHONPATH="$(dirname $(dirname $(realpath $(which beacon))))" python3 -m beacon_cli.skills_helpers.dm_discover
```

JSON 配列が返る。各 session row は `project_id` field annotated 付き。空 (`[]`) の場合は同マシン上に cloud-mode の bridge が動いてないことを意味する → 後段の fallback に進む。

#### Step 1-send-a: fallback (discover module 不在 or 0 件)

`PYTHONPATH=... python3 -m beacon_cli.skills_helpers.dm_discover` が `ModuleNotFoundError` 等で失敗、または 0 件で返った場合は、cwd の project だけ query する従来動作にフォールバック:

```bash
beacon bus directory --live --healthy --since-min 5 --json
```

`--healthy` 未対応 CLI の場合 (stderr に "unrecognized" 等):
```bash
beacon bus directory --live --since-min 5 --json
```
このフォールバック発生時、ユーザーに 1 行だけ補足:
「(true-heartbeat filter 未対応、live filter のみで listing)」

#### Step 1-send-b: 0 件のときの fallback

最終的に sessions 配列が **空** の場合、ユーザーに明示警告:
「現在 listening 中の受信先がありません。相手側で `beacon bus listen` または MCP 接続が必要です」と伝えて終了。

#### Step 1-send-c: メンバー情報の取得 (best-effort cross-reference)

Bash ツールで:
```bash
beacon member list --json
```

session.actor.email がメンバーの email と一致するなら、その人の email + role を picker 行に添える。一致しないなら machine / agent のみで表示する。member list が空でもエラーにせず無視する。

#### Step 1-send-d: 候補表示と選択

各 session を以下のフォーマットで表示する (helpers の `render_candidate_line` と同じ規則):

```
1. [machine=DESKTOP-CHG6PAT, agent=DESKTOP-CHG6PAT] session=aa60cc21… healthy (age 3s) → dolphin.orca@gmail.com (editor)
2. [machine=WORKMACHINE] session=dc526151… healthy (age 1s)
3. [machine=mac-mini] session=6d270a08… stale (age 12m) → (member unknown)
```

ユーザーに尋ねる:
```
どの受信者に送りますか?
- 番号 (1, 2, 3, …) でピック
- session_id を直接貼り付け
- cancel で中止
```

選択された session の `session_id` と `project_id` を控える (以下 `recipient_sid`, `recipient_project_id` と呼ぶ)。

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

## Step 5: 本文の入力 (両モード共通)

ユーザーに本文を尋ねる:

```
本文を入力してください (改行 OK、空行 + Enter で送信):
```

reply mode のときは親 DM を引用形式で添える:
```
[親 DM 引用]
from: <from_session の頭8文字>
> <親 payload.text の先頭3行 or 全体>
```

本文を **そのまま文字列として** 保持する。**JSON エスケープは Skill が自動で行う** — ユーザーが `--payload '{"text":"..."}'` 形式で書く必要は無い。

### Step 5b: action 指定 (send mode、任意)

send mode のみ:
```
受信側に auto-execute 権限を与える action 名はありますか? (普通は空でEnter)
```

reply mode では `--action` は使わない (= 返信は payload を運ぶだけ、副作用権限の付与は新規送信の役割)。

---

## Step 6: 送信確認 (mode で表示が変わる)

組み立てた argv をユーザーに見せて確認。reply mode と send mode で `--in-reply-to` の有無が変わる:

### send mode
```
以下のコマンドで送信します:

  beacon bus send --channel dm --to <recipient_sid> --payload '{"text":"<本文>"}' [--project <id>] [--action <name>]... --json

送信しますか? (yes / edit / cancel)
```

### reply mode
```
以下のコマンドで返信します:

  beacon bus send --channel dm --to <recipient_sid> --in-reply-to <parent_event_id> --payload '{"text":"<本文>"}' [--project <id>] --json

送信しますか? (yes / edit / cancel)
```

- `yes` → Step 7
- `edit` → Step 5 に戻る
- `cancel` → 中止

---

## Step 7: 送信実行 (両モード共通)

Bash ツールで上記コマンドを実行する。JSON 出力 (`--json`) を有効化することで stdout に event_id + delivery + envelope + budget 情報が返るので、結果を読む。

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
| sent ✓ / delivered ✓ / opened ✗ | bridge は受け取ったが filter chain で drop or mcp.notification 失敗 | 相手の channel allowlist (`BEACON_CHANNEL_ALLOWLIST`) と DM の channel が一致しているか、受信側 session が allowlist に入っているか確認 |
| sent ✓ / delivered ✓ / opened ✓ | 完全到達 | 完了 |
| sent ✓ / delivered ✗ / opened ✗ かつ 8 秒待っても変化なし | 受信側 bridge が **古い beacon バージョン** で ack 経路を持たない可能性 | 相手の `actor.agent.version` を directory で確認 (v0.26.0 未満は receipt 非対応)、`pip install --upgrade beacon-cli` を促す |

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
