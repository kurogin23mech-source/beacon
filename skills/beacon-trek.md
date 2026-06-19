---
name: beacon-trek
description: Trek (= 分散協奏のための作業領域) を AI が対話で操作する Skill。create / join / leave / scope-add / scope-remove / member-invite / status / archive の 8 subaction を 1 Skill に統合し、subaction ごとに draft 表示 + ユーザー確認を挟んで beacon trek <verb> を実行する。
version: 0.1.0
triggers:
  - /beacon-trek
  - trek を作る
  - trek 参加
  - trek 操作
  - trek status
  - trek scope
  - trek archive
---

# Beacon Trek

> Trek (= 分散協奏のための作業領域) は ms-69 で導入された「複数プロジェクト / 複数メンバーが共通の作業文脈を共有する箱」。CLI / Web UI / API には trek 操作が揃っているが、AI が trek を操作するときは裸の `beacon trek <verb>` を叩いており、verb 取り違え / scope 範囲ミス / archive 誤発火 のリスクがあった。
>
> 本 Skill は 8 subaction (create / join / leave / scope-add / scope-remove / member-invite / status / archive) を統合 entry とし、各 subaction で draft 表示と確認を必ず通す。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

### 守ること

1. **読み手目線 1 行から始める**: 「何が嬉しいか」「何が困るか」をユーザー体験の言葉で。技術用語ではなく価値で書く
2. **横文字 3 段階**:
   - そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP` / `Tauri` / `WebSocket`)
   - 初出時に日本語注: 技術概念 (`scope (= 共有範囲)` / `archive (= 凍結)` / `invitation (= 招待状)`)
   - 日本語化が望ましい: 一般概念 (configure → 設定 / receiver → 受信側 / audit → 監査)
3. **ID 参照には文脈**: `e-XXXX` / `UC?` / `ms-XX` / `trek-XX` は初出に必ず『何の話か』1 行添える
4. **尻切れトンボ禁止**: 主語と述語を省略しない、論理関係を明示

### Self-review (生成直後に必ず)

文章を書いた直後、自分で読み返して以下を自問:
- (a) 読み手 (非開発者を含む) は 1 度読んで意味が取れるか?
- (b) 一般概念の横文字 (configure / receiver / audit 等) が残ってないか?
- (c) ID 参照に『何の話か』1 行添えたか?
- 違反していたら書き直し。enforce ではないが必須の self-check。

### 詳細

詳しい原則と例は CORE doc `entry-writing-principle` (doc_id `F3ZkqT0pKS6JpR8dn70n`) 参照。

---

## Trek とは (= 1 段落導入)

Trek は「複数プロジェクトを横断する作業領域」。例えば「Beacon 本体プロジェクト」と「Beacon を実運用している社内プロジェクト」を 1 つの trek に紐づけると、両プロジェクトの milestone / task / doc / commit が trek 単位で集約され、参加メンバー全員が同じ進捗ビューを見る。プロジェクト自体は独立のまま、人とコンテキストだけを共有する仕組み (= ms-69 が定義)。

---

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` の場合、この Skill は何もせず終了する。
- 一部 subaction (`join` / `member-invite` / `scope-add` 等) は cloud mode が必須 (= `.beacon/cloud.json` 必須)。subaction 解釈後に必要なら個別チェック。

自分の cwd の project_id を控える (= subaction の多くで使う):
```bash
python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id','')) if __import__('os').path.exists('.beacon/cloud.json') else print('')"
```

---

## Step 0: subaction の判定

ユーザーが `/beacon-trek <subaction> [...args]` で起動した場合、第 1 引数を subaction として採用。引数なし or 不明 subaction の場合は picker を出す:

```
どの操作を実行しますか?
  1. create          新しい trek を作る (= 共有作業領域を立ち上げる)
  2. join            既存 trek に参加する (= 招待コードから join)
  3. leave           trek から離脱する (= 自分だけ抜ける、trek 自体は残る)
  4. scope-add       trek にプロジェクト / MS / doc を含める (= 共有範囲を広げる)
  5. scope-remove    trek から含まれているものを外す
  6. member-invite   trek にメンバーを招待する
  7. status          trek の現状を見る (= メンバー / scope / 直近活動)
  8. archive         trek を凍結する (= 完了プロジェクト、読み取り専用化)

選択 (番号 or subaction 名):
```

`cancel` で中止。

判定された subaction に対応する Step (1〜8) へ分岐する。

---

## Step 1: create (= 新しい trek を作る)

### 1-a: 入力の引き出し

ユーザーに以下を尋ねる (= 1 ターン 1 質問、または並列で 1 メッセージ内で全部):

```
新しい trek を作ります。以下を教えてください:
  - trek 名 (例: "Beacon × 社内運用統合", 30 字以内):
  - 一言で何を共有するか (= description、読み手目線 1 行):
  - 含めるプロジェクト (= 自プロジェクトのみ / 複数指定可、後で scope-add で追加もできます):
```

`--description` が空のまま続行を強要された場合、warning で「description 無しの trek は member-invite の文面が薄くなります」と通知して続行。

### 1-b: draft 表示 (= ms-68 e-1641 と同じパターン)

組み立てた argv をユーザーに見せて確認:

```
以下のコマンドで trek を作成します:

  beacon trek create --name "<name>" --description "<description>" [--include-project <pid>]...

このまま作成しますか? (yes / edit / cancel)
```

### 1-c: 実行 と結果報告

```bash
beacon trek create --name "<name>" --description "<description>" [--include-project <pid>]... --json
```

成功すると `trek_id` が返る。ユーザーに次のステップを提示:

```
✓ trek を作成しました
  trek_id:    trek-XXXX
  name:       <name>

次のステップ:
  - メンバー招待: /beacon-trek member-invite trek-XXXX
  - scope 追加:   /beacon-trek scope-add trek-XXXX
  - 現状確認:     /beacon-trek status trek-XXXX
```

---

## Step 2: join (= 既存 trek に参加)

### 2-a: 招待コードの取得

引数 `/beacon-trek join <invitation-code>` で渡された場合はそれを使う。無ければユーザーに尋ねる:

```
join したい trek の招待コードを教えてください (= 招待状の URL or トークン):
```

### 2-b: 招待内容の preview

```bash
beacon trek invitation peek <invitation-code> --json
```

返ってきた trek の詳細 (= trek 名 / description / 既存メンバー / scope) を表示してユーザーに確認を取る (= silent join 禁止):

```
─────────────────────────────────────────────
  trek:        <name>
  description: <description>
  既存メンバー: alice@example.com (owner), bob@example.com (editor)
  scope:       project=foo, project=bar
  招待者:      alice@example.com
─────────────────────────────────────────────

この trek に join しますか? (yes / cancel)
```

### 2-c: 実行

```bash
beacon trek join <invitation-code> --json
```

成功時の結果報告:

```
✓ trek <trek_id> に join しました
  既存メンバー全員に通知が届きます。
  確認: /beacon-trek status <trek_id>
```

---

## Step 3: leave (= 自分だけ抜ける、trek は残す)

### 3-a: 対象 trek の選択

引数で指定がなければ自分が参加している trek 一覧から picker:

```bash
beacon trek list --member-me --json
```

```
参加中の trek:
  1. trek-abcd "Beacon × 社内運用統合" (member: 4 名)
  2. trek-efgh "TrailNode β 検証" (member: 2 名)

どの trek から leave しますか? (番号 or trek_id, cancel で中止):
```

### 3-b: owner 判定と warning

抜けようとしている trek で自分が **owner だけ** だった場合、強警告:

```
警告: あなたがこの trek の唯一の owner です。
leave すると trek は orphan (= 管理者不在) になります。

選択:
  - 別メンバーに owner を譲ってから leave (= /beacon-member role-change で owner 移譲、その後再度 /beacon-trek leave)
  - そのまま leave して trek を orphan 状態にする (非推奨)
  - cancel
```

### 3-c: 二段確認

```
trek <trek_id> "<name>" から leave します。
  - trek 内の自分の発言 / 編集履歴は残ります (= audit 上の透明性)
  - 自分の view からは見えなくなります
  - 再 join するには新しい招待が必要です

本当に leave しますか? (yes / cancel)
```

### 3-d: 実行

```bash
beacon trek leave <trek_id> --json
```

---

## Step 4: scope-add (= 共有範囲を広げる)

### 4-a: 対象と追加要素の指定

```
どの trek に何を追加しますか?
  trek_id: trek-XXXX
  追加要素のタイプ:
    project   自プロジェクトを共有する
    milestone 特定 MS のみ共有 (例: ms-12)
    doc       特定 doc のみ共有 (例: doc_id)

選択:
```

milestone / doc を指定する場合は ID 引き出し:

```
ms-id (例: ms-12) または doc_id を教えてください:
```

### 4-b: draft 表示

```
以下を trek <trek_id> "<trek 名>" の scope に追加します:

  - project: <自プロジェクト名>
    (= project 全体の milestone / doc / commit が trek に流れます)
  または
  - milestone: ms-XX "<MS タイトル>"
    (= この MS とその配下 task のみが共有されます)

このまま追加しますか? (yes / cancel)
```

### 4-c: 実行

```bash
beacon trek scope add <trek_id> --type <project|milestone|doc> --id <target-id> --json
```

---

## Step 5: scope-remove (= 含めたものを外す)

### 5-a: 対象 scope の picker

```bash
beacon trek scope list <trek_id> --json
```

```
trek <trek_id> "<name>" の現在の scope:
  1. project: beacon-b95643 "Beacon 本体" (含めた人: alice@..., 含めた日: 2026-06-01)
  2. milestone: ms-69 "Trek 機能" (含めた人: bob@..., 含めた日: 2026-06-05)

どれを外しますか? (番号 or scope-id, cancel):
```

### 5-b: 影響範囲の説明 + 確認

```
警告: scope を外すと以下が起きます:
  - trek 内のメンバーは <project / MS / doc> が見えなくなります
  - 既に行われた commit / レビュー / DM は audit 上は残ります (= 削除はしない)
  - 後で再度 scope-add すれば復帰できます

本当に外しますか? (yes / cancel)
```

### 5-c: 実行

```bash
beacon trek scope remove <trek_id> --scope-id <scope-id> --json
```

---

## Step 6: member-invite (= メンバー招待)

### 6-a: 招待先と role の指定

```
trek <trek_id> "<name>" にメンバーを招待します。

  招待先 (email or member_id):
  role (owner / editor / viewer, 詳細は /beacon-member role-change の説明参照):
  招待メッセージ (= 受信者に届く 1〜3 行、空 Enter で default):
```

role の意味を 1 行ずつ補足表示 (= 招待者が間違えないように):
- `owner`: trek の管理権 (= scope 変更 / archive / member 削除が可能)
- `editor`: trek 内の commit / task 操作が可能 (= 普通の作業者)
- `viewer`: 読み取りのみ (= 監査 / 報告閲覧)

### 6-b-prime: scope sensitivity 確認 (= ms-75 / e-1863、構造的に挟む step)

**この step は必ず draft 表示より前に実行する。** AI が候補提示する前に scope に含まれる各 project の data class (= データ機密度 / sensitivity) を user に提示し、 外部 user (= scope 内 project の member ではない人) 初回招待時には明示確認を構造的に強制する。 2026-06-16 LPS dogfood で「同 project の live セッション」 を根拠に外部 user を silent inclusion した事故 (= event g3rhokmRrTF9KoRarA3w) が顕在化したため、 この確認を AI 自身も飛ばせない動線にする。

#### Step 6-b-prime-1: trek scope の各 project の sensitivity を取得

trek の `scope` 配列を 1 つずつ歩いて、 各 `project` フィールドの sensitivity を取得する:

```bash
# 各 project root に cd して disclosure_policy を取得
for pid in <project-ids>; do
  cd $PROJECT_ROOT_FOR_PID && \
    python3 -c "import json; d = json.load(open('.beacon/project.json')); \
      pol = d.get('disclosure_policy', {}); \
      print(pid, pol.get('sensitivity', 'unknown'))"
done
```

cloud mode では `beacon project show <pid> --json` 相当が使える (= 同 user が読める project に限る)。 自分が member ではない project の sensitivity は取得不可なので **その時点で「unknown」 として扱い user に確認する**。

#### Step 6-b-prime-2: data class の集約 + 表示

scope 内 project の sensitivity を 1 つでも `high` (= 機密度 high 以上) を含むか判定する:

```
以下の Trek scope に含まれる project の機密度 (data class) を確認してください:

  - beacon-b95643         sensitivity = high     (= 機密度 high)
  - lps-customer-profile  sensitivity = high     (= 機密度 high)
  - trailnode-public      sensitivity = low      (= 機密度 low)

⚠ scope に sensitivity=high の project が含まれます。
   招待先 <invitee> は外部 user (= 上記 project のいずれにも未参加) です。
   それでも招待を進めますか? (yes / cancel)
```

判定ロジック:
- **外部 user 判定**: 招待先 email が scope 内 project の members[] に **1 つも含まれない** 場合 → 外部 user
- **high sensitivity 判定**: scope 内 project の `disclosure_policy.sensitivity == "high"` が **1 つでもあれば** high

**外部 user + high sensitivity の組合せ** のみが yes/cancel 強制確認を発火する (= LPS 事故の再現条件)。 それ以外 (= 内部 user / すべて low / unknown のみ) は表示するが 強制確認 step を挟まずに 6-b の draft 表示に進む (= 過剰な動線を避ける、 Beacon Philosophy「過剰なサポートはそれ自体が摩擦」)。

#### Step 6-b-prime-3: AI 自身も飛ばせない

**重要**: この step は user 確認の前段ではなく、 **AI が picker に候補を並べる時点でも同確認を自動付与する** (= AI 自身がこの事故を起こさない)。 `beacon trek invite` を AI 自律で呼ぶ Skill (= `/beacon-trek-execute` 等) も Step 5/7 の境界判定に「外部 user 初回招待 + high sensitivity」 を含めて escalation 対象とする。

具体的には: `/beacon-trek-execute` Step 5 (= デプロイ / リリースの境界 detection) に「Trek member 招待 (= 外部 user 初回招待 + high sensitivity scope の組合せ)」 を境界アクションとして列挙する。 これにより autonomous run でも本 step が構造的に通る。

### 6-b: draft 表示

```
以下の招待を送ります:

  trek:    <trek_id> "<name>"
  to:      <invitee>
  role:    <role>
  message: "<message>"
  expires: <default 7 日後>

送信しますか? (yes / edit / cancel)
```

### 6-c: 実行

```bash
beacon trek invite <trek_id> --to <invitee> --role <role> --message "<message>" --json
```

結果報告では招待 URL (= 招待コードを含む) をユーザーに渡し、「直接 DM / Slack 等で共有してください」と促す (= 招待 URL は機密扱い)。

---

## Step 7: status (= trek の現状確認、読み取りのみ)

### 7-a: 対象選択

引数なしなら自分が参加 / owner の trek 一覧から picker (Step 3-a と同じ)。

### 7-b: 表示

```bash
beacon trek status <trek_id> --json
```

返ってきた JSON を読み手目線で整形:

```
─────────────────────────────────────────────
  trek:    trek-XXXX "<name>"
  state:   open / archived
  作成:    2026-06-01 by alice@example.com

  メンバー (4 名):
    - alice@example.com (owner, 参加: 2026-06-01)
    - bob@example.com   (editor, 参加: 2026-06-03)
    - ...

  scope (= 共有範囲):
    - project: beacon-b95643 "Beacon 本体"
    - milestone: ms-69 "Trek 機能"

  直近 7 日の活動:
    - commit: 12 件 (内 alice 8 / bob 4)
    - task done: 5 件
    - doc 追加: 2 件
    - DM: 8 件

  未消化 task (= 期限内かつ未完了): 3 件
─────────────────────────────────────────────
```

read-only なので確認 step なし。

---

## Step 8: archive (= trek を凍結、読み取り専用化)

### 8-a: 対象選択と権限確認

archive は owner のみ可能。自分が owner でない場合は明示エラー:

```
trek <trek_id> の archive は owner のみ実行できます。
あなたの role: <role>。owner に依頼するか、まず role 変更を相談してください。
```

### 8-b: 影響範囲の表示 と二段確認 (= 重操作)

```
警告: trek <trek_id> "<name>" を archive します。

これにより以下が起きます:
  - trek 内の commit / task 操作が **読み取り専用** になります
  - メンバーは引き続き閲覧可、ただし新規発言 / 編集はできません
  - archive を解除して再開できますが、解除には全 owner の合意が必要です

trek 名を入力して確認してください (= 誤操作防止):
```

ユーザーが trek 名を **完全一致** で入力した場合のみ次の確認に進む:

```
最終確認: trek "<name>" を archive します。よろしいですか? (yes / cancel)
```

### 8-c: 実行

```bash
beacon trek archive <trek_id> --confirm "<trek-name>" --json
```

`--confirm` flag は CLI 側でも名前一致を検証 (= Skill と CLI で二重防御)。

### 8-d: 結果報告

```
✓ trek <trek_id> "<name>" を archive しました
  - 全メンバーに通知が届きます
  - 解除するには: beacon trek unarchive <trek_id> (全 owner の合意が必要)
```

---

## 共通: エラーハンドリング

| エラー | 対処 |
|---|---|
| `trek_id` が存在しない | 「指定した trek が見つかりません。`beacon trek list` で一覧を確認してください」と提示 |
| 権限不足 (= role 不足で操作拒否) | 「この操作は <required-role> 以上のロールが必要です」と返し、依頼可能な owner を提示 |
| cloud 未認証 (= cloud mode 必須 subaction) | 「`beacon auth login` + `beacon cloud setup` を実行してから再試行してください」と案内 |
| 招待コード expired | 「招待が期限切れです。招待者に再発行を依頼してください」 |
| invitation peek 失敗 | 「招待コードが不正、または既に使われている可能性があります」 |

`--reason` / `--description` に長文を渡す際は **single quote または quoted heredoc** を使う (= double quote + backtick は zsh が command substitution として展開してしまうため)。

---

## 制約

- 全ての破壊的操作 (= leave / scope-remove / archive) は **draft 表示 + 二段確認** を通す。silent execute 禁止。
- archive は **trek 名の完全一致入力** を要求 (= 誤発火防止の構造的ガード)。
- AI は subaction 選択を勝手にしない。引数で明示されない限り picker でユーザーに選ばせる。
- cross-project な trek 操作 (= 別 project の MS を scope に追加等) は、追加前に「この MS は別 project (project=<pid>) のものです、含めますか?」と明示確認する。

---

## 関連 Skill

- `/beacon-member` — member 操作 (= invite / role-change / remove)。trek の member-invite と CLI レイヤは別 (= trek メンバー vs project メンバー)、用途で使い分け。
- `/beacon-cloud` — cloud sync 操作 (= push / pull / off / open)。trek 操作の前提となる cloud mode の入口。
- `/beacon-spec` — trek 配下で SPEC を書く時はこちらを併用。
