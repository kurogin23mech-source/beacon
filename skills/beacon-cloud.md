---
name: beacon-cloud
description: Cloud sync (= ローカル Beacon データの cloud 同期) を AI が対話で操作する Skill。push / pull / off / open の 4 subaction を 1 Skill に統合。cloud push は別マシン作業との衝突 (= overwrite) を避けるため強警告 + 二段確認で構造的にガードする。
version: 0.1.0
triggers:
  - /beacon-cloud
  - cloud push
  - cloud pull
  - cloud sync
  - cloud off
  - cloud open
---

# Beacon Cloud

> Cloud sync (= ローカルの `.beacon/` を cloud Firestore と同期) は高度ユーザー向け機能。push / pull / off / open の 4 操作があり、特に **push は別マシンで進められた作業を上書きする** リスクがあるため、Skill 側で強警告 + 二段確認を構造的に挟む。
>
> 過去病理 (= 過去経験から): `beacon cloud push` を別マシン作業中に走らせて、もう片方の machine で書いた task / commit log を上書きしてしまった事例が複数件。本 Skill では push 前に必ず「相手側の最終更新時刻」と「自分のローカル最終変更時刻」を提示し、conflict 可能性を user に判断させる。

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

ユーザーが `/beacon-cloud <subaction>` で起動した場合、第 1 引数を subaction として採用。引数なし or 不明 subaction の場合は picker:

```
どの操作を実行しますか?
  1. push    ローカル変更を cloud に送信 (= 注意: 他マシンの作業を上書きする可能性)
  2. pull    cloud の最新状態を取り込む (= ローカルの未 push 変更があれば確認)
  3. off     cloud mode を解除して local mode に戻す
  4. open    cloud project を web UI でブラウザに開く

選択 (番号 or subaction 名, cancel で中止):
```

判定された subaction に対応する Step (1〜4) へ分岐する。

---

## Step 1: push (= ローカル → cloud、強警告 + 二段確認)

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

## Step 2: pull (= cloud → ローカル、ローカル変更との衝突確認)

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

## Step 3: off (= cloud mode 解除、local mode に戻す)

### 3-a: 影響範囲の表示

```
警告: cloud mode を off にすると以下が起きます:
  - .beacon/cloud.json が削除されます (= 再 setup には beacon cloud setup が必要)
  - 以降の操作 (= task done / log 等) は cloud に同期されません
  - 既に cloud にある履歴は cloud 側に残ります (= 削除はしません)
  - 他メンバーの操作は cloud に流れ続けますが、あなたのローカルには反映されません
  - DM 受信 (= bus listen) も無効になります (= cloud project_id が必要なため)

主な用途:
  - 一時的にオフラインで作業したい
  - 別 cloud project に切り替える前段階 (= off → setup --project <別id>)
  - cloud 同期を恒久的にやめたい (= 通常 1 人プロジェクトに戻す)
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

## Step 4: open (= web UI をブラウザで開く)

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

- **push と off は二段確認 (= project_id 完全一致入力 + 最終 yes) を必須** とする。誤発火防止の構造的ガード。
- 衝突可能性がある場合、AI は **pull-first を必ず推奨** する。push-anyway を選ばせるのは user の明示的判断としてのみ。
- `--confirm` / 長文オプションは **single quote または quoted heredoc** で渡す (= zsh の double-quote backtick 展開を避ける、過去経験から)。
- open は read-only なので確認不要だが、URL を勝手にブラウザに流さず、GUI / HEADLESS 環境を判定してから動く。
- 本 Skill は **既に cloud setup 済の運用** を扱う。初回 setup は `/beacon-init` または直 CLI (= `beacon auth login` + `beacon cloud setup`) に委ねる (= scope 分離)。

---

## 関連 Skill (= 役割分担)

- `/beacon-init` — Beacon プロジェクトの初期化 (= local mode / cloud mode 両対応の初回 setup)。本 Skill は cloud mode 切り替え後の継続運用に focus。
- `/beacon-member` — メンバー操作。cloud mode 必須なので、本 Skill で cloud mode が有効化されてから使う。
- `/beacon-trek` — Trek 操作。同様に cloud mode 必須。
- `/beacon-dm-send` / `/beacon-dm-respond` — DM 送受信。cloud project_id を経由するため cloud mode 必須。

cloud mode の **on/off 切り替え** だけが本 Skill の責務。他の cloud 依存 Skill とは重ならない。
