---
name: beacon-dm-respond
description: 別セッションから届いた DM action 付き envelope を user に提示し、y/n 確認を取った上で beacon dm respond approve|deny <envelope-id> を叩く対話 Skill。ms-70 (= cross-user DM 承認) における「terminal Claude Code 内での user 直接判断」の動線を構造化する。
version: 0.1.0
triggers:
  - /beacon-dm-respond
  - /beacon-dm-approve
  - /beacon-dm-deny
  - DMを承認
  - DMに応答
  - envelope 承認
  - approve dm
  - respond dm
---

# Beacon DM Respond

> 別セッションから届いた DM action 付き envelope を **user の直接判断で** approve / deny する Skill。
>
> 背景: ms-70 (= cross-user DM 承認の SPEC) で「approval は terminal Claude Code 内での user 直接判断のみ」と決めた。AI が独断で approve してしまうと「他人の AI が自分のプロジェクトを操作する」入口になりかねないため、人間の y/n を必ず構造的に挟む。本 Skill が無いと、user は素の `beacon dm respond approve <envelope-id>` を手で打つことになり、envelope id 取り違え / channel 取り違え / approve すべきでないものを誤承認する病理が再生産される。

### Trek 参加中の例外 (ms-75 / e-1856)

送信者と受信者が同じ Trek (= 缶詰の徹夜作業部屋 / 事前承認スコープ) の member の場合、server 側 `dm_gate.py` が `shared_trek_member` 判定で **gate を bypass** する。受信側 AI は本 Skill を起動せず、envelope を直接受信して自律的に処理する (= Trek scope 内の事前承認が成立しているため)。

その結果、本 Skill の picker (Step 1) に並ぶのは以下のいずれか:
- Trek 外の cross-user DM (= 従来通り user 判断必須)
- Trek member だが Trek scope 外の action を要求する DM (= 安全側 gate trigger、user 判断必須)
- envelope が壊れていて Trek 関係を判定できなかった DM (= 安全側 gate trigger)

user が picker で見る envelope は「Trek 経由ではない他人の AI からの要求」 が原則。「Trek 内の member から来たのに何故 picker に並ぶの?」 と質問されたら、上記いずれかに該当している可能性を案内する。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

### 守ること

1. **読み手目線 1 行から始める**: 「何が嬉しいか」「何が困るか」をユーザー体験の言葉で。技術用語ではなく価値で書く
2. **横文字 3 段階**:
   - そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP` / `Tauri` / `WebSocket`)
   - 初出時に日本語注: 技術概念 (`allowlist (= 許可リスト)` / `opt-in (= 個別許可)` / `envelope (= 行使権チケット)`)
   - 日本語化が望ましい: 一般概念 (configure → 設定 / receiver → 受信側 / audit → 監査 / hit → 一致 / install → 設置)
3. **ID 参照には文脈**: `e-XXXX` / `UC?` / `ms-XX` は初出に必ず『何の話か』1 行添える。例 ✗「ms-70 の承認」→ ✓「ms-70 (= cross-user DM 承認の SPEC) の承認」
4. **尻切れトンボ禁止**: 主語と述語を省略しない、論理関係を明示

### Self-review (生成直後に必ず)

文章を書いた直後、自分で読み返して以下を自問:
- (a) 読み手 (非開発者を含む) は 1 度読んで意味が取れるか?
- (b) 一般概念の横文字 (configure / receiver / audit / hit / install / merge / deploy 等) が残ってないか?
- (c) ID 参照に『何の話か』1 行添えたか?
- 違反していたら書き直し。enforce ではないが必須の self-check。

### 詳細

詳しい原則と例は CORE doc `entry-writing-principle` (doc_id `F3ZkqT0pKS6JpR8dn70n`) 参照。本 Skill では上記 4 項目を **常に top of mind** で適用する。

---

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` の場合、この Skill は何もせず終了する。
- `.beacon/cloud.json` が無い (= local mode) 場合は以下を表示して終了:
  ```
  このプロジェクトは local mode (= cloud sync 無し) なので、DM envelope 承認は使えません。
  envelope は cloud bus 経由で配信されるため、cloud mode の認証 (= beacon auth login + beacon cloud setup) が必要です。
  ```

自分の session_id を控える (後段で「自分宛 envelope か」判定に使う):
```bash
beacon session id
```

---

## Step 0: 起動引数の解釈 (envelope id 指定 / 受信トレイ picker)

ユーザーが `/beacon-dm-respond <envelope-id>` のように直接 envelope id (= 行使権チケットの ID) 指定で呼んだ場合、その envelope を対象として **Step 2 (詳細取得)** に直行する。

引数なしの場合は **Step 1 (受信トレイ picker)** に進む。

サブコマンド hint (`/beacon-dm-approve` / `/beacon-dm-deny`) で起動された場合、後段の確認ステップでは defaultverb をそれに合わせて提示する (= ただし最終確認はあくまで人間の y/n)。

---

## Step 1: 受信トレイから pending envelope の picker

UserPromptSubmit hook が context に injectした `BEACON BUS INBOX` セクション、もしくは現在のセッションが受け取った `<channel>` event をまず確認する。inject されていれば listen 経路を skip して直接 parse する (= e-1401 の旧 dm-reply 病理を避ける、hook と listen のダブルドレイン回避)。

context に DM envelope 候補がなければ、CLI から pending 一覧を取る:

```bash
beacon dm pending --json
```

返ってきた JSON 配列を以下の形で表示:

```
未応答の envelope:
  1. [env-abc12345] from=dolphin.orca@gmail.com (machine=mac-mini)
       action: ops/log-fetch
       channel: operation-trigger
       received: 2026-06-15 14:32 (3 分前)
       preview: "Beacon API の error rate 確認お願いします"
  2. [env-def67890] from=DESKTOP-CHG6PAT (agent=auto-bus-armed)
       action: notify-only
       channel: dm
       received: 2026-06-15 14:18 (17 分前)
       preview: "PR #142 のレビュー終わりました"

  どの envelope を判断しますか?
    - 番号 (1, 2, ...) でピック
    - envelope id を直接貼り付け
    - skip で次回起動まで保留
    - cancel で中止
```

候補 0 件なら以下を返して終了:
```
現在、未応答の DM envelope はありません。
受信したら hook 経由で通知されるか、`beacon dm pending` で再確認してください。
```

選択された envelope の id を `envelope_id` として控える。

---

## Step 2: envelope 詳細の取得と user 提示 (= 判断材料を漏れなく)

選ばれた envelope の中身をフェッチする:

```bash
beacon dm envelope show <envelope_id> --json
```

返る JSON から以下を抽出し、user に **整形して** 提示する (= raw JSON を投げない、非開発者の読み手を排除しない):

| 表示項目 | 由来 fieldname |
|---|---|
| 送り主 (email / machine / agent) | `from.actor` |
| 送り主の project | `from.project_id` + `from.project_name` |
| 要求アクション | `action` (例: `ops/log-fetch`, `notify-only`) |
| ペイロード本文 | `payload.text` (改行を保ち、長文なら最初の 20 行 + 「... (続きは N 行)」) |
| envelope 期限 (= 自動失効時刻) | `expires_at` |
| 過去の同じ送り主からの履歴 (= 何回 approve / deny したか) | `from_actor_history` (= API 拡張で取れれば。無ければ skip) |
| 受信時刻 / channel | `received_at` / `channel` |

ms-68 / e-1641 補足 (= entry-writing principle の draft 表示) と同じ要請で、**承認 / 拒否を打つ前に必ず 1 度全文を見せる**。silent な approve / deny は audit (= 後から追跡可能性) を破壊する。

提示フォーマット例:

```
─────────────────────────────────────────────
  envelope: env-abc12345
  from:     dolphin.orca@gmail.com
            (machine=mac-mini, project=beacon-b95643)
  action:   ops/log-fetch
  channel:  operation-trigger
  received: 2026-06-15 14:32 (3 分前)
  expires:  2026-06-15 15:32 (=受信から 1 時間)

  ペイロード:
    > Beacon API の error rate 確認お願いします。
    > 直近 30 分のログをまとめて返してください。

  過去の応答履歴 (この送り主から):
    - approve: 3 回 / deny: 0 回 / 直近: 2026-06-14 (= 昨日)
─────────────────────────────────────────────
```

履歴が取れない場合は「過去の応答履歴: 取得不可 (= 初回 or API 未対応)」と明示。silent に欠落させない。

### action 種別 × tier 要件の参考表示 (= ms-76 framework)

提示の最後に、要求 action の **tier 要件** を 1 行で添える (= user が「これは自律でやって良かったか? AI 経由で良かったか?」 を判断する材料)。判定は CORE doc `QvyVwRU8otQEn5iMfP36` (= AI 自律 action の envelope tier framework) の action × tier matrix を起点にする。

| 受信 action 種別 | 必要 tier | 表示文言例 |
|---|---|---|
| 計画系 (= 議論 / 提案 / 確認応答) | T3 で軽量自律可 | 「計画系応答 — armed mode なら自律可、本 Skill では user 判断」 |
| コード変更指示 | T1 / T2 envelope 必須 | 「コード変更 — user 承認必須 (T1/T2 envelope 待ち)」 |
| 外部送信 (= 別 project / Slack / Discord) | T1 必須 (T2 でも Operation scope 明示時のみ) | 「外部送信 — user 承認必須 (T1 envelope 必須)」 |
| Bus Budget 増額 | T1 のみ (= 構造的禁止帯) | 「Budget 増額 — 必ず user 判断 (構造的に AI 自律不可)」 |

この表示は **AI の推奨判断ではない** (= ms-70 SPEC 要請に違反しない)。tier 要件は CORE doc に書かれた **客観的な分類** であり、user が「envelope の tier 要件と実際の要求が整合しているか」 を素早く確認するための材料。

---

## Step 3: user の判断を仰ぐ (= 必ず人間の y/n、AI 独断禁止)

提示の直後、**AI は推奨判断を一切添えない** で純粋に y/n を取る。これは ms-70 SPEC の本質的要請であり、「AI が推奨 → 人間が rubber-stamp」を構造的に防ぐため。

```
判断してください:
  approve   = この action の実行を許可する
  deny      = 拒否する (= 送り主に rejection が返る)
  detail    = 詳細をもう一度全文で見たい
  skip      = 今回は判断保留 (= envelope は pending のまま残る)
  cancel    = この Skill を中断する

選択 (approve / deny / detail / skip / cancel):
```

各選択肢の取り扱い:

| user 入力 | 動作 |
|---|---|
| `approve` / `a` / `y` | Step 4 へ (= approve コマンド組み立て) |
| `deny` / `d` / `n` | Step 4 へ (= deny コマンド組み立て、deny は理由 1 行を任意で取る) |
| `detail` | Step 2 の提示をもう 1 度全部出して Step 3 に戻る |
| `skip` | envelope を pending のまま残し、user に「次回起動か `beacon dm pending` で再確認できます」と伝えて終了 |
| `cancel` | 何もせず終了 |

`yes` だけだと「全文読んだか不明」のリスクが残るため、Skill 側は **detail の選択肢を必ず提示** する。

---

## Step 4: 実行コマンドの draft 表示と最終確認

ms-68 / e-1641 補足 (= entry-writing principle の draft 表示) と同パターンで、**実行する argv を一度 user に見せて** から流す。silent execute 禁止。

### approve の場合

```
以下のコマンドを実行します:

  beacon dm respond approve <envelope_id> [--note "<任意のメモ>"]

これでよろしいですか? (yes / cancel)
```

任意で承認メモを取りたい場合は user に 1 行尋ねる (空 Enter で省略可):
```
承認メモを残しますか? (audit log に残ります、空 Enter で省略)
```

メモが入力されたら `--note` を付与する。

### deny の場合

deny は **理由 1 行を必ず取る** (= 送り主が「なぜ却下されたか」を知るのは Beacon の透明性原則)。

```
deny 理由を 1 行で教えてください (= 送り主に届きます):
```

```
以下のコマンドを実行します:

  beacon dm respond deny <envelope_id> --reason "<入力された理由>"

これでよろしいですか? (yes / cancel)
```

---

## Step 5: 実行 と receipt 確認

ユーザー承認後、Bash ツールで実行:

```bash
beacon dm respond <approve|deny> <envelope_id> [--note "..."] [--reason "..."] --json
```

`--reason` / `--note` に長文を渡す場合の注意 (= /beacon-task で蓄積された経験):
double quote + backtick の組み合わせは zsh が command substitution として展開してしまう。長文や特殊文字を含む場合は **必ず single quote** または **quoted heredoc** で渡す:

```bash
beacon dm respond deny env-abc12345 --reason '実行対象のログ範囲が広すぎます。日付を絞ってから再依頼してください' --json
```

heredoc 版:
```bash
REASON=$(cat <<'EOF'
実行対象のログ範囲が広すぎます。
日付を絞ってから再依頼してください。
EOF
)
beacon dm respond deny env-abc12345 --reason "$REASON" --json
```

exit code 0 + JSON 出力に `status: "approved"` または `status: "denied"` が立てば成功。

---

## Step 6: 結果報告

user に簡潔に報告:

```
✓ envelope <env-id> を <approve / deny> しました

  action:    ops/log-fetch
  to:        dolphin.orca@gmail.com (machine=mac-mini)
  note:      "<note があれば>"
  reason:    "<deny 時の理由>"

送り主に応答 (= ack) が配信されました。
audit log を見るには: beacon dm log show <env-id>
```

approve だった場合は補足として、受信側 (= 送り主) が action を自動実行 (= Operation auto-execute) するかどうかを明示:

```
この approve により、送り主側で <action> が auto-execute されます。
受信側の Operation 実行ログを確認するには: beacon run list -o <op-id>
```

---

## 制約 / 例外処理

- **AI は推奨判断を添えない**。Step 3 で「approve がよさそうです」「これは拒否すべきでしょう」のような prefix を出してはならない (= ms-70 SPEC の本質)。user の純粋な判断を尊重する。
- envelope が既に **expired** または **resolved** (= 既に approve / deny 済) の場合、Step 2 で検知して以下を返して終了:
  ```
  envelope <env-id> は既に <expired / approved / denied> 済です。
  確認するには: beacon dm envelope show <env-id>
  ```
- 引数で指定された envelope が **自分宛でない** (= 受信者 session_id が `beacon session id` と一致しない) 場合、明示警告して中止 (= 他人宛 envelope に応答する権限はない)。
- `beacon dm respond` 自体が失敗した場合 (= ネットワークエラー / サーバ 4xx) はエラーをそのまま提示し、envelope の状態を再 fetch (`beacon dm envelope show`) して整合性を確認するよう促す。
- skip 選択時は envelope を **触らない**。受信トレイに残したまま user の次回操作を待つ。
- 1 つの envelope は 1 回しか approve / deny できない。Skill を 2 回起動して同じ envelope に対し違う判断を流すのは構造的に防げない (= サーバ側で reject される) ので、user 側で取り消したい場合は「送り主にもう一度送信を依頼する」案内を添える。

---

## 関連 Skill (= 役割分担)

- `/beacon-dm-send` — DM **送信側** の Skill。新規 DM 送信および返信を扱う (= 送り手 / approve はここでは扱わない)。
- `/beacon-bus-armed` — **自律 DM 応答モード**。Monitor で listen を armed しっぱなしにし、prompt 無しでも別セッションからの DM に AI が起動して返答する状態を作る (= ms-70 が禁じる「AI 独断 approve」とは別の経路: 単なる listen 維持であり、approve には本 Skill が必須)。
- `/beacon-dm-respond` (本 Skill) — DM **受信側で envelope の判断** を扱う。**AI 独断 approve を構造的に禁止** し、必ず人間の y/n を挟む。

この 3 つの Skill は重ならない役割分担:
- 送信 (生成) → `/beacon-dm-send`
- 受信 (listen 維持) → `/beacon-bus-armed`
- 受信 (判断) → `/beacon-dm-respond` (本 Skill)
