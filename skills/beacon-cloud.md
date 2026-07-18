---
name: beacon-cloud
description: Cloud sync (= ローカル Beacon データの cloud 同期) を AI が対話で操作する Skill。日常運用は `open` / `status` (= web UI を開く / read-only 確認) のみ、`upload-initial` は新規プロジェクトの 1 回きり migration、`off` は sandbox 用途。ms-84 Phase 4 (e-2038) で `push` / `pull` / `force-pull` は CLI から削除された (= cloud が唯一の真値、戻り経路は構造的に不可能)。
version: 0.3.0
triggers:
  - /beacon-cloud
  - cloud open
  - cloud status
  - cloud upload-initial
  - cloud off
---

# Beacon Cloud

> Cloud sync (= ローカルの `.beacon/` を cloud Firestore と同期) の運用は、**ms-36 Cloud-first 設計が機能している今、日常 op は `open` (= web UI を開く) と `status` (= read-only 確認) のみ**。CLI / Skill / Web UI のすべてが直接 cloud を読み書きするため、push / pull は本来不要な操作になっている (UC16-F2 / e-1862 で確認)。
>
> **ms-84 Phase 4 (e-2038) で `push` / `pull` / `force-pull` は CLI から削除済**: cloud が唯一の真値になり (= ローカル cache を持たない)、 cloud → local の戻り経路は構造的に不可能になった。 SPEC 受入条件 3 / 8。
>
> 残る subaction:
> - `open` / `status`: 日常運用
> - `upload-initial`: 新規 cloud project への 1 回きり bootstrap (旧 `push` の本来用途のみが残った)
> - `off`: sandbox / 一時オフライン作業
>
> 過去病理: `beacon cloud push` を別マシン作業中に走らせて上書きする事例が複数件あった。 Phase 4 削除によりこの病理は構造的に発生不可能になった (= そもそも push CLI が存在しない)。
>
> 整理後の subaction 運用区分は CORE doc `YqsGgUhqEe0fHGDfXr3Q` (= 「Cloud sync subaction の運用区分」) を参照。 ms-84 land 後に書き換え予定。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

### 守ること

1. **読み手目線 1 行から始める**: 「何が嬉しいか」「何が困るか」をユーザー体験の言葉で。技術用語ではなく価値で書く
2. **横文字 3 段階**:
   - そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP`)
   - 初出時に日本語注: 技術概念 (`sync (= 同期)` / `overwrite (= 上書き)` / `cloud project_id`)
   - 日本語化が望ましい: 一般概念 (push → 送信 / pull → 取り込み / merge → 取り込み)
3. **ID 参照には文脈**: `e-XXXX` / `UC?` / `ms-XX` / `cloud project_id` は初出に必ず『何の話か』1 行添える
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

## Cloud Mode の構図 (= 1 段落導入)

Beacon は Claude Code から呼ばれる前提で動く。Claude Code は internet 接続必須なので、Beacon は **常時 cloud mode で動くのが本来の姿** (e-1861 / ms-61 で「Beacon = cloud-only」 を構造的に確定)。`.beacon/cloud.json` の存在が cloud mode かどうかの単一の判定源 (= silent drift 防止のため二重持ちを撤廃)。

本 Skill が扱うのは **cloud 運用の通常操作** (= push / pull / open) と、**sandbox / 検証用途の `off`** の 4 つ。`off` は default の picker には出さない (= 誤発火防止)。初回 setup は `/beacon-init` または直 CLI に委ねる。

---

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` の場合、この Skill は何もせず終了する。

cloud mode 状態を取得:
```bash
beacon cloud status --json
```

返ってきた JSON の `enabled` / `project_id` / `last_synced_at` を控える。

subaction 別の前提:
- `push` / `pull` / `off`: cloud mode が enabled (= `.beacon/cloud.json` 存在) であること
- `open`: 上記に加え、ブラウザで開ける環境 (= GUI 環境推奨、SSH 環境は URL 表示のみ)
- `off` は **sandbox / 検証用途のみ** (e-1861 / ms-61) — Beacon は Claude Code から呼ばれ常時 cloud 動作する前提のため、production では使わない。明示的に `/beacon-cloud off` と subaction 名指定された時だけ届く (= default picker から外している)。

---

## Step 0: subaction の判定

ユーザーが `/beacon-cloud <subaction>` で起動した場合、第 1 引数を subaction として採用。引数なし or 不明 subaction の場合は **default picker** を出す。

### Default picker (= 日常運用)

```
どの操作を実行しますか?
  1. open    cloud project を web UI でブラウザに開く (= 日常運用)
  2. status  cloud mode 状態を確認 (= read-only)

選択 (番号 or subaction 名, cancel で中止):

特殊用途 (= 通常は呼ばない、明示入力時のみ受理):
  - upload-initial          新規 cloud project への 1 回きり bootstrap
  - off                     cloud mode 解除、sandbox / 一時オフライン作業のみ
```

### 受理ルール

- **default 動線**: ユーザーが picker から `open` / `status` を選んだ場合は通常フロー
- **特殊用途**: `upload-initial` / `off` は user が **subaction 名を明示入力** した場合のみ受理する (= picker の番号には出さない、 誤発火防止)
- **退役 subaction**: `push` / `pull` / `force-pull` は ms-84 Phase 4 (e-2038) で CLI から削除済。これらの名前で呼ばれた場合は「ms-84 Phase 4 で削除されました。 `upload-initial` が新規 bootstrap の代替です」 と説明して終了
- **`off` 特例 (e-1861 / ms-61)**: Beacon は Claude Code から呼ばれる前提で cloud-only 運用が標準。`off` を出すと「local mode に戻す」誤選択を誘発し、2026-06-15 incident (= sub-agent silent drift) の再現経路になる。default picker から外し、明示的に `/beacon-cloud off` と subaction 名指定された時のみ進む
- **未認識文字列**: 上記いずれにもマッチしないなら「未認識 subaction、`/beacon-cloud` 単独で起動すると default picker が出ます」と返して終了

判定された subaction に対応する Step へ分岐する。

---

## Step 1: upload-initial (= ローカル → cloud、新規 bootstrap 専用、強警告 + 二段確認)

> **位置付け** (ms-84 Phase 4 / e-2038): この操作は **新規 cloud project への 1 回きりの bootstrap (= 初回 upload)** 専用。`upload-initial` 名でのみ起動できる (= 旧名の `push` は CLI / Skill から削除済)。
>
> 日常運用では upload-initial は不要 — CLI / Skill / Web UI のすべてが直接 cloud を読み書きするため (ms-36 Cloud-first 設計)。「アップロードしたい」と思った時点で「新規プロジェクトを cloud に持ち上げる場面か?」と一度立ち止まる。

### 1-0: 退役名で呼ばれた場合の応答

ユーザーが `push` で起動した場合 (= ms-84 Phase 4 削除済の旧名)、以下を提示して終了:

```
注意: `beacon cloud push` は ms-84 Phase 4 (e-2038) で CLI から削除されました。
  - cloud-first 設計のため、CLI / Skill / Web UI は直接 cloud を読み書きしています
  - 「push すべきローカル変更」が見えること自体、何か乖離が起きている可能性が高い (= 構造的には起こらないはず)

代わりに以下を検討してください:
  - 新規 cloud project への初回 upload を実行したい → /beacon-cloud upload-initial
  - cloud 側の状態を確認したい               → /beacon-cloud status
  - cloud 側が壊れている疑いがある           → user の手動介入が必要 (= サポート起票)

このまま終了します。
```

`upload-initial` 名で起動された場合はこのステップをスキップ (= 意図が明確)。

### 1-a: cloud project が既に在るかの確認

```bash
beacon cloud status
```

ms-84 Phase 4 (e-2038) で push / pull / diff は廃止されたので、「他マシンの未 push 変更を
検知する」概念は無い (= ローカルキャッシュを持たない設計に cut over 済)。upload-initial の
唯一の危険は **既に在る cloud project をローカル状態で上書きする** ことなので、見るのは
「この repo が既に cloud project に紐づいているか」だけ。出力を以下で読む:
- `Cloud: not configured` → まだ cloud project に紐づいていない (= 新規 bootstrap で安全)
- `Cloud: <project_id>` → 既に cloud project に紐づき済 (= upload-initial は上書き。1-c で中止 default)

### 1-b: 衝突可能性の表示 (= 必ず提示)

#### 過去病理例 (= 提示前に必ず読み上げて user の体感に橋渡しする、e-1778)

`upload-initial` 系の操作 (= 旧 `cloud push` 含む) で実際に起きた事故を具体的に提示する。
「これは自分にも起きうる」 と user に体感させるための forcing function。
e-1777 (= 過去 incident 共通パターン memo doc、 並行作業中) の完成後に詳細 cross-link 予定。

`````
過去にこの操作で起きた事故 (= 抽象論ではなく実例):

  [病理 A] 2026-05-19 cloud 上書き data loss (= ms-24 / commit fe8b7ffd で構造修復)
    Mac で `beacon cloud push` を実行 → cloud 側に他マシンから入っていた docs / retros が
    丸ごと上書き消滅。 ローカルが「古いまま」 だったのに気付かず whole-PUT が走った。
    その後 cloud mode では `--force` 必須 + docs / retros は skip する保護を CLI 側で追加。

  [病理 B] 2026-06-15 silent mode flip による「データ消えた」 体感 (= e-1776 / e-1861 で構造修復)
    サブエージェントが `.beacon/config.json` を `{"mode": "local"}` に黙って書き換えた。
    以降の全 CLI が cloud を参照しなくなり、 user 体感では「cloud のデータが消えた」。
    実際は cloud 側に残っていたが、 silent な mode flip で local 空 cache を見ていただけ。
    対策: cloud.json 存在を唯一の真値にして `mode` 二重持ちを撤廃 (ms-61 / e-1861)。

  [病理 C] 2026-06-07 Firestore 1 MiB cap で write 全停止 (= ms-59 / PR #80 で構造修復)
    project doc が 1 MiB を超え、 「増やす write」 (= log / task add / milestone add) が
    全て HTTP 500 で reject される状態に陥り 4-5 時間 blocked。 monolithic な
    `1 project = 1 doc` 設計が原因。 upload-initial は新規 bootstrap で whole-PUT を走らせる
    操作なので、 同じ payload size 由来の事故を踏みやすい (= subcollection 化済の今でも、
    bootstrap タイミングで一気にサイズが乗ると同型の事故が起きうる)。

これらは全て事後に構造修復済だが、 「whole-PUT で他者の状態が消える」 「silent な切り替わり」
「payload 肥大化」 の 3 軸は upload-initial が本質的に踏みうる経路。 cancel が default の理由。
`````

#### cloud project の存在確認 (= status の結果)

```
─────────────────────────────────────────────
  cloud 接続状況 (beacon cloud status):
    Cloud: <project_id  or  "not configured">
    Auth:  <logged in / not logged in>

  判定:
    - not configured   → 新規 bootstrap (= 上書き対象なし、比較的安全)
    - <project_id> あり → 既に cloud project に紐づき済 (= upload-initial は
                          ローカル状態で丸ごと上書き。他マシン / 他メンバーの変更を破壊しうる)
─────────────────────────────────────────────
```

`not configured` なら新規 bootstrap なので単段確認 (1-c) へ進む。既に `project_id` が在る場合は
1-c の中止 default + force-overwrite 二段確認へ。

ただし `not configured` 表示でも上記病理 B (= silent mode flip で local が空 cache 化し、
cloud 側が「無い」ように見える) と病理 C (= bootstrap 時の payload size 肥大化) は残るため、
迷ったら cancel が安全。

### 1-c: 警告と二段確認

cloud に既にプロジェクトが存在するときは原則中止する (= upload-initial は新規 bootstrap 専用):

```
警告: cloud project_id が既に存在します (= 別マシンで先に upload 済み)。
  - upload-initial は新規 cloud project への 1 回きり bootstrap です
  - 既存 cloud project に対する re-upload は **ローカル状態で上書き** するため、 他マシンの変更を破壊します
  - ms-84 Phase 4 (e-2038) で pull / force-pull は削除されたので戻し経路はありません

進める場合の確認:
  - cancel (推奨): 中止して状況を確認 (= 既存 project にすでに join 済の可能性)
  - force-overwrite: 警告を承知の上で上書き (= 失われる変更があれば手動復旧が必要)
```

`force-overwrite` を選んだ場合、追加で `project_id` の完全一致入力を要求 (= 誤発火防止):

```
最終確認: cloud project_id を完全一致で入力してください (= 上書き upload の意思表示):
  期待値: <project_id>
  入力:
```

完全一致しない場合は中止。

新規 project (= cloud に未登録) の場合は単段確認:

```
ローカル変更 12 件を cloud に upload します (= 新規 project bootstrap)。

upload-initial を実行しますか? (yes / cancel)
```

### 1-d: 実行

```bash
beacon cloud upload-initial
```

`--force` フラグは force-overwrite を選んだ場合のみ付与する。

### 1-e: 結果報告

```
✓ cloud に upload しました (= 新規 bootstrap)
  upload 数:      12 件
  cloud 最終更新: <new timestamp>
  cloud URL:      <project URL>
  local cache:    .beacon/project.json → .beacon/project.json.before-cloud-YYYYMMDD (= ms-84 Phase 3 cut-over)

他マシン / 他メンバー側で同期するには:
  - そのマシンで `beacon cloud join <project-id>` を実行 (= cloud.json を書き込んで cloud-first に切り替え)
```

---

## Step 2: pull / force-pull (= ms-84 Phase 4 で削除済、tombstone)

ms-84 Phase 4 (e-2038) で `beacon cloud pull` / `force-pull` は CLI から削除された。 cloud → local の戻り経路は構造的に不可能 (= ローカル cache を持たない設計に cut over 済)。

ユーザーが `pull` / `force-pull` で起動した場合は以下を返して即終了:

```
注意: `beacon cloud pull` / `force-pull` は ms-84 Phase 4 (e-2038) で CLI から削除されました。
  - cloud が唯一の真値になり、ローカル cache そのものを持たなくなったため、 戻り経路は構造的に不可能になりました
  - cloud 側の状態を確認したい → /beacon-cloud status または web UI (= /beacon-cloud open)
  - cloud 側が壊れている疑いがある → user の手動介入が必要 (= サポート起票)
  - 移行前のローカル cache (= `.beacon/project.json.before-cloud-YYYYMMDD`) はオフラインで参照できます

このまま終了します。
```

---

## Step 3: off (= cloud mode 解除、特殊用途、sandbox / 検証・オフライン作業のみ)

> **位置付け** (e-1861 + e-1862 / UC16-F1): cloud mode off は **sandbox / 検証 / 一時オフライン作業** のみが想定用途。Beacon は Claude Code から呼ばれる前提で常時 cloud 動作するのが正しい姿、production の cloud project から sync を外す正当な理由はほぼ無い (= 別マシンで作業継続できなくなる、メンバーへの変更が届かなくなる、bus DM が止まる)。
>
> 誤起動を防ぐため CLI 側でも `--confirm <project_id>` 完全一致を必須化している (= 二段防御、e-1861)。さらに UC16-F1 (= local mode 撤去) で議論中のため、将来的には `off` 自体が削除される可能性あり。現状は sandbox 用途で残置 — 「常用するもの」ではなく「特定理由で一時的に切るもの」と扱う。

### 3-a: 影響範囲の表示

```
警告: cloud mode を off にすると以下が起きます:
  - .beacon/cloud.json が削除されます (= 再 setup には beacon cloud setup が必要)
  - 以降の操作 (= task done / log 等) は cloud に同期されません
  - 既に cloud にある履歴は cloud 側に残ります (= 削除はしません)
  - 他メンバーの操作は cloud に流れ続けますが、あなたのローカルには反映されません
  - DM 受信 (= bus listen) も無効になります (= cloud project_id が必要なため)

主な用途 (= e-1862 整理後の運用区分):
  - sandbox / 検証専用: テスト的に local mode で動作確認したい
  - 一時オフライン: ネットワークが取れない環境で作業したい
  - 別 cloud project に切り替える前段階 (= off → setup --project <別id>)

⚠️ 日常運用では off にしません。UC16-F1 (= local mode 撤去) の議論次第で
   将来この subaction 自体が削除される可能性があります。
```

### 3-b: 二段確認

```
cloud project_id を完全一致で入力してください (= 誤発火防止):
  期待値: <project_id>
  入力:
```

完全一致した場合のみ次に進む:

```
最終確認: cloud mode を off にします。

  - 再度 cloud mode に戻すには: beacon auth login + beacon cloud setup
  - 既存 cloud 履歴は cloud 側に残ります

実行しますか? (yes / cancel)
```

### 3-c: 実行

```bash
beacon cloud off --confirm "<project_id>" --json
```

`--confirm` flag に project_id を渡し、CLI 側でも一致検証 (= 二重防御)。

### 3-d: 結果報告

```
✓ cloud mode を off にしました
  state: local mode
  .beacon/cloud.json: 削除済

再度 cloud mode にするには:
  1. beacon auth login
  2. beacon cloud setup (= 同じ project_id を指定で再リンク可)
```

---

## Step 4: open (= web UI をブラウザで開く、日常運用)

> **位置付け** (e-1862): これが **日常運用の主動線**。cloud project の状態を確認・編集するには web UI を開くのが標準。CLI から直接見たい場合は Step 5 (status) で代用可。

### 4-a: project URL の取得

```bash
beacon cloud status --json
```

返ってきた `project_url` (= web UI の URL) を取得。

### 4-b: 環境判定

GUI 環境かどうか判定:
```bash
[ -n "$DISPLAY" ] || [ "$(uname)" = "Darwin" ] && echo "GUI" || echo "HEADLESS"
```

GUI 環境:
```
project を web UI で開きます: <URL>

  open (Mac) / xdg-open (Linux) を実行しますか? (yes / 自分でコピー / cancel)
```

HEADLESS 環境 (= SSH 接続 / CI 等):
```
GUI 環境ではないため、URL を表示します:
  <URL>

このリンクをブラウザにコピーしてください。
```

### 4-c: 実行 (GUI 環境のみ)

ユーザーが yes を選んだ場合:
```bash
# Mac
open "<URL>"
# Linux
xdg-open "<URL>"
```

read-only な操作なので二段確認は不要。

---

## Step 5: status (= cloud mode 状態を read-only で確認、日常運用)

> **位置付け** (e-1862): web UI を開かずに CLI で軽く確認したい場合の主動線。read-only。

### 5-a: 状態取得 + 提示

```bash
beacon cloud status --json
```

返る JSON から以下を提示:
- `enabled`: cloud mode の on/off (= `.beacon/cloud.json` の有無)
- `project_id`: cloud project_id
- `project_url`: web UI URL
- `last_synced_at`: 最終同期時刻

提示例:
```
cloud 状態:
  mode:        cloud (= ON)
  project_id:  <id>
  web UI:      <URL>
  最終同期:    <timestamp>
```

`enabled == false` の場合:
```
cloud 状態: 未設定 (= local mode)
  cloud mode に切り替えるには: beacon auth login + beacon cloud setup
```

read-only なので確認不要、即時実行して終了。

---

## 共通: エラーハンドリング

| エラー | 対処 |
|---|---|
| cloud 未認証 (= upload-initial / off 共通) | 「`beacon auth login` が必要です」と返して終了 |
| network エラー | 「cloud に到達できません: <error>。ネットワーク接続を確認してください」 |
| upload-initial 中の "already in cloud mode" | 「既に cloud mode です。 既存 project に対する re-upload は警告付きの `--force` のみ許可されます」 |
| `--confirm` 不一致 | 「project_id が一致しません。誤発火防止のため中止しました」と返して中止 |

---

## 制約

- **default picker には `open` と `status` (= 日常運用 read-only ops) のみを並べる** (e-1862)。upload-initial / off は特殊用途として明示入力時のみ受理 (= 番号選択動線には出さない)。 退役名 (`push` / `pull` / `force-pull`) は tombstone 応答のみ。
- **upload-initial / off は二段確認 (= project_id 完全一致入力 + 最終 yes) を必須** とする。誤発火防止の構造的ガード。
- **退役名 (`push` / `pull` / `force-pull`) で起動された場合は tombstone メッセージで終了**。ms-84 Phase 4 (e-2038) で CLI から削除済 — 何かを実行する経路は存在しない。
- 衝突可能性がある場合、AI は **cancel を必ず推奨** する。force-overwrite を選ばせるのは user の明示的判断としてのみ。
- `--confirm` / 長文オプションは **single quote または quoted heredoc** で渡す (= zsh の double-quote backtick 展開を避ける、過去経験から)。
- open / status は read-only なので確認不要だが、open は URL を勝手にブラウザに流さず GUI / HEADLESS 環境を判定してから動く。
- 本 Skill は **既に cloud setup 済の運用** を扱う。初回 setup は `/beacon-init` または直 CLI (= `beacon auth login` + `beacon cloud setup`) に委ねる (= scope 分離)。

---

## 関連 Skill (= 役割分担)

- `/beacon-init` — Beacon プロジェクトの初期化 (= local mode / cloud mode 両対応の初回 setup)。本 Skill は cloud mode 切り替え後の継続運用に focus。
- `/beacon-member` — メンバー操作。cloud mode 必須なので、本 Skill で cloud mode が有効化されてから使う。
- `/beacon-trek` — Trek 操作。同様に cloud mode 必須。
- `/beacon-dm-send` / `/beacon-dm-respond` — DM 送受信。cloud project_id を経由するため cloud mode 必須。

cloud mode の **on/off 切り替え** だけが本 Skill の責務。他の cloud 依存 Skill とは重ならない。
