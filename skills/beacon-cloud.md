---
name: beacon-cloud
description: Cloud sync (= ローカル Beacon データの cloud 同期) を AI が対話で操作する Skill。日常運用は `open` (= web UI を開く) のみで、push / pull / off は特殊用途 (= 初回 upload / 緊急 force pull / mode 解除) として明示位置付け。`/beacon-cloud` 単独起動では `open` を default surface し、push / pull は明示的な subaction 名指定でのみ届く誤発火防止構造。
version: 0.2.0
triggers:
  - /beacon-cloud
  - cloud open
  - cloud status
  - cloud upload-initial
  - cloud force-pull
  - cloud off
  - cloud push
  - cloud pull
---

# Beacon Cloud

> Cloud sync (= ローカルの `.beacon/` を cloud Firestore と同期) の運用は、**ms-36 Cloud-first 設計が機能している今、日常 op は `open` (= web UI を開く) と `status` (= read-only 確認) のみ**。CLI / Skill / Web UI のすべてが直接 cloud を読み書きするため、push / pull は本来不要な操作になっている (UC16-F2 / e-1862 で確認)。
>
> したがって push / pull / off は **特殊用途のみ** に位置付ける:
> - `push` (= 初回 upload / `upload-initial`): 新規 cloud project への 1 回きりの bootstrap
> - `pull` (= 緊急 force pull / `force-pull`): cloud と local が大幅乖離した時の emergency recovery
> - `off`: sandbox / 一時オフライン作業 (= UC16-F1 / e-1861 で「local mode 撤去」議論中、現状は sandbox 用途のみ)
>
> 過去病理 (= 過去経験から): `beacon cloud push` を別マシン作業中に走らせて、もう片方の machine で書いた task / commit log を上書きしてしまった事例が複数件。本 Skill では特殊用途 subaction を **picker の default に出さない** + push 前に必ず「相手側の最終更新時刻」と「自分のローカル最終変更時刻」を提示し conflict 可能性を user に判断させる二重ガード。
>
> 整理後の subaction 運用区分は CORE doc `YqsGgUhqEe0fHGDfXr3Q` (= 「Cloud sync subaction の運用区分」) を参照。3 階層 (= 日常運用 / 特殊用途 / sandbox) で位置付け済。

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

Beacon は default で local mode (= `.beacon/` ローカルのみ) で動く。`beacon auth login` + `beacon cloud setup` で cloud mode に切り替えると、`.beacon/cloud.json` に cloud project_id が書かれ、`beacon log` / `beacon task done` 等が自動で cloud Firestore に上り、複数マシン / 複数メンバー間で同期される。

本 Skill が扱うのは **切り替え後の運用** (= push / pull / off / open) であり、初回 setup は `/beacon-init` または直 CLI に委ねる。

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
  - upload-initial / push   初回 upload 専用、別マシン作業を上書きするリスク
  - force-pull / pull       緊急 force pull 専用、cloud-first 設計と矛盾するため通常不要
  - off                     cloud mode 解除、sandbox / 一時オフライン作業のみ
```

### 受理ルール

- **default 動線**: ユーザーが picker から `open` / `status` を選んだ場合は通常フロー
- **特殊用途**: `push` / `upload-initial` / `pull` / `force-pull` / `off` は user が **subaction 名を明示入力** (= `/beacon-cloud upload-initial` 等、または picker の数字 1/2 ではなく subaction 名タイプ) した場合のみ受理する。picker の番号には出さない (= 誤発火防止)
- **未認識文字列**: 上記いずれにもマッチしないなら「未認識 subaction、`/beacon-cloud` 単独で起動すると default picker が出ます」と返して終了

判定された subaction に対応する Step (1〜5) へ分岐する。

---

## Step 1: push / upload-initial (= ローカル → cloud、特殊用途、強警告 + 二段確認)

> **位置付け** (e-1862): この操作は **新規 cloud project への 1 回きりの bootstrap (= 初回 upload)** が本来の用途。`/beacon-cloud upload-initial` 名で呼ばれた場合は意図が明確、`push` 名で呼ばれた場合は「日常運用と勘違いされている可能性あり」と仮定して追加警告を出す。
>
> 日常運用では push は不要 — CLI / Skill / Web UI のすべてが直接 cloud を読み書きするため (ms-36 Cloud-first 設計)。「push したい」と思った時点で何か別の問題 (= cloud との乖離、stale local cache) を疑うべき。

### 1-0: 意図確認 (= `push` 名で呼ばれた場合のみ)

ユーザーが `push` (alias `upload-initial` ではなく素の `push`) で起動した場合、以下を最初に提示:

```
注意: cloud push は日常運用では使いません (e-1862)。
  - cloud-first 設計のため、CLI / Skill / Web UI は直接 cloud を読み書きしています
  - 「push すべきローカル変更」が見えること自体、何か乖離が起きている可能性が高い
  - 本来 push が必要なのは: (a) 新規 cloud project への初回 upload (b) 緊急の force overwrite

何をしようとしていますか?
  1. 初回 upload (新規 cloud project に bootstrap) — そのまま続行
  2. 緊急の force overwrite (cloud 側が壊れている等) — そのまま続行 (= 強警告あり)
  3. 単に最新を同期したい — それは pull の用途なので /beacon-cloud pull に切り替え推奨
  4. 何かおかしい気がする — cancel して状況を確認

選択 (cancel で中止):
```

`upload-initial` 名で起動された場合はこのステップをスキップ (= 意図が明確)。

### 1-a: cloud 側の最新状態取得

```bash
beacon cloud diff --json
```

返る JSON から以下を抽出:
- `cloud_last_modified_at`: cloud 側 (= 他マシン / 他メンバーの直近 push 時刻)
- `cloud_last_modified_by`: 直近 push したマシン / メンバー
- `local_pending_changes`: ローカルで未 push の変更 (= task / log / doc / config 等の差分)
- `cloud_changes_since_local_pull`: 自分が最後に pull してから cloud 側で起きた変更

### 1-b: 衝突可能性の表示 (= 必ず提示)

```
─────────────────────────────────────────────
  cloud 側の状況:
    最終更新:   2026-06-15 13:42 by mac-mini.local (= 別マシン)
    自分の最終 pull: 2026-06-15 10:00 (= 3 時間 42 分前)

    自分の最終 pull 以降、cloud 側で起きた変更:
      - task done: 4 件
      - log 追加: 7 件
      - doc 追加: 1 件

  ローカル側 (= これから push する内容):
    未 push 変更: 12 件
      - commit log: 6 件
      - task done: 3 件
      - doc 追加: 1 件
      - config 変更: 2 件

  衝突可能性: あり (= 別マシンが先に push しています)
─────────────────────────────────────────────
```

衝突可能性なしの場合 (= cloud_changes_since_local_pull が 0) は「衝突可能性: なし (= 別マシンからの push は検知されません)」と明示。

### 1-c: 警告と二段確認

衝突可能性ありの場合:

```
警告: cloud push は cloud 側の状態を **ローカル状態で上書き** します。
  - 別マシン (= mac-mini.local) が直近 3 時間 42 分前に push しています
  - そのマシンが書いた task done / log / doc は **失われる可能性** があります
  - 推奨: 先に /beacon-cloud pull で cloud 側を取り込み、conflict を merge してから push

進める場合の確認:
  - pull-first (推奨): 先に pull に切り替える
  - push-anyway: 警告を承知の上で上書き push (= 失われる変更があれば手動復旧が必要)
  - cancel: 中止
```

`push-anyway` を選んだ場合、追加で `project_id` の完全一致入力を要求 (= 誤発火防止):

```
最終確認: cloud project_id を完全一致で入力してください (= 上書き push の意思表示):
  期待値: <project_id>
  入力:
```

完全一致しない場合は中止。

衝突可能性なしの場合は単段確認:

```
ローカル変更 12 件を cloud に push します。
  - 上書きされる cloud 変更: なし (= 自分が最終 push 者)

push しますか? (yes / cancel)
```

### 1-d: 実行

```bash
beacon cloud push --json
```

長文 message が必要なオプション (= 将来追加された場合) は **single quote または quoted heredoc** で渡す (= zsh の double-quote backtick 展開を避ける、過去経験から)。

### 1-e: 結果報告

```
✓ cloud に push しました
  push 数:        12 件
  cloud 最終更新: <new timestamp>
  cloud URL:      <project URL>

他マシン / 他メンバー側で同期するには:
  - そのマシンで beacon cloud pull を実行
  - または自動 sync (= hook 経由) が動いていれば数分以内に反映
```

---

## Step 2: pull / force-pull (= cloud → ローカル、特殊用途、ローカル変更との衝突確認)

> **位置付け** (e-1862): この操作は **cloud と local が大幅乖離した時の emergency recovery (= 緊急 force pull)** が本来の用途。`/beacon-cloud force-pull` 名で呼ばれた場合は意図が明確、`pull` 名で呼ばれた場合は「日常運用と勘違いされている可能性あり」と仮定して追加質問を出す。
>
> 日常運用では pull は不要 — cloud-first 設計で常に cloud を直接読んでいるため、「pull すべき変更」という概念自体が薄い。ローカル `.beacon/project.json` は cloud キャッシュとしては stale でも、CLI は cloud から都度取得して動作する。

### 2-0: 意図確認 (= `pull` 名で呼ばれた場合のみ)

ユーザーが `pull` (alias `force-pull` ではなく素の `pull`) で起動した場合、以下を最初に提示:

```
注意: cloud pull は日常運用では使いません (e-1862)。
  - cloud-first 設計のため、CLI は常に cloud から直接読んでいます
  - ローカル `.beacon/project.json` が古くても CLI 動作には影響しません (web UI も cloud 直読)
  - 本来 pull が必要なのは: (a) cloud と local が大幅乖離した emergency recovery (b) オフライン作業を想定して local cache を最新化したい

何をしようとしていますか?
  1. emergency recovery (cloud 側を真値源として local を上書き) — そのまま続行 (= 強警告あり)
  2. オフライン作業前の cache 最新化 — そのまま続行
  3. 何かおかしい気がする (= push の代わりに pull と入れた等) — cancel して状況を確認

選択 (cancel で中止):
```

`force-pull` 名で起動された場合はこのステップをスキップ (= 意図が明確)。

### 2-a: ローカル未 push 変更の確認

```bash
beacon cloud diff --json
```

`local_pending_changes` を控える。

### 2-b: 衝突可能性の表示

```
─────────────────────────────────────────────
  cloud 側の最新:
    最終更新:    2026-06-15 13:42 by mac-mini.local
    新規変更:    cloud 側で起きた、ローカルに無い変更
      - task done: 4 件
      - log: 7 件

  ローカル側:
    未 push 変更: <件数> 件
      - <内容>

  衝突可能性: <あり / なし>
─────────────────────────────────────────────
```

### 2-c: 警告と確認

ローカル未 push 変更がある場合:

```
警告: ローカルに未 push 変更が <N> 件あります。
  pull は cloud 状態を取り込みますが、同じ entry (= 同 task / 同 log) を両側で変更している場合、conflict resolution が走ります。

選択:
  - push-first (推奨): 先にローカル変更を push する (= /beacon-cloud push に切り替え)
  - pull-anyway: そのまま pull (= サーバ側の conflict resolution に委ねる)
  - cancel: 中止
```

ローカル変更なしの場合は単段確認:

```
cloud から <N> 件の変更を取り込みます (= ローカル変更なし、安全)。
pull しますか? (yes / cancel)
```

### 2-d: 実行

```bash
beacon cloud pull --json
```

### 2-e: 結果報告

```
✓ cloud から pull しました
  取り込み数:   <N> 件
  conflict 解決: <件数> 件 (= 自動 merge 済)
  詳細:        beacon cloud log show
```

---

## Step 3: off (= cloud mode 解除、特殊用途、sandbox / オフライン作業のみ)

> **位置付け** (e-1862 / UC16-F1): cloud mode off (= local mode 化) は **sandbox / 一時的なオフライン作業** のみが想定用途。日常運用は cloud mode で固定。
>
> UC16-F1 (= local mode 撤去) で議論中のため、将来的には `off` 自体が削除される可能性あり。現状は sandbox 用途で残置 — 「常用するもの」ではなく「特定理由で一時的に切るもの」と扱う。

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
| cloud 未認証 (= push / pull / off 共通) | 「`beacon auth login` が必要です」と返して終了 |
| network エラー | 「cloud に到達できません: <error>。ネットワーク接続を確認してください」 |
| push 中の merge conflict (= サーバ拒否) | 「cloud 側が更新されています。先に pull してください: /beacon-cloud pull」と提示 |
| pull 中の auto-merge 失敗 | サーバが返した conflict 詳細を表示し、手動 resolve の手順を CORE doc 参照で提示 |
| `--confirm` 不一致 | 「project_id が一致しません。誤発火防止のため中止しました」と返して中止 |

---

## 制約

- **default picker には `open` と `status` (= 日常運用 read-only ops) のみを並べる** (e-1862)。push / pull / off は特殊用途として明示入力時のみ受理 (= 番号選択動線には出さない)。
- **push / off は二段確認 (= project_id 完全一致入力 + 最終 yes) を必須** とする。誤発火防止の構造的ガード。
- **`push` / `pull` 名で起動された場合は意図確認ステップ (Step 1-0 / 2-0) を先頭に挟む**。日常運用と勘違いされている可能性を排除し、本来の特殊用途 (= 初回 upload / 緊急 force pull) に正しく誘導する。
- 衝突可能性がある場合、AI は **pull-first を必ず推奨** する。push-anyway を選ばせるのは user の明示的判断としてのみ。
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
