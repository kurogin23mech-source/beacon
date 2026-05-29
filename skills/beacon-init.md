---
name: beacon-init
description: Beaconプロジェクトを会話形式で初期化。プロジェクト名と大目的を明示的に聞いてから場所を決める。既存リポはProject Archaeologyへ自動チェイン。
version: 2.2.0
triggers:
  - beacon init
  - プロジェクトをbeaconで管理したい
  - beacon を始めたい
---

# Beacon Init

> 名前と大目的を明示的に聞いて、場所決め → 確認 → init を 1 本道で進める。環境による事前分岐は最小化する (CORE doc `PU9HG2IVQdW3tLiAJvix` バイブコーダー Philosophy に準拠)。

## Step 1: 軽い環境スキャン (分岐はしない、情報収集だけ)

以下を **並列に** Bash ツールで実行 (CWD は Claude Code 起動時の cwd):

```bash
test -f .beacon/project.json && echo "BEACON_EXISTS"
```
```bash
pwd
```
```bash
basename "$(pwd)"
```
```bash
git log --oneline 2>/dev/null | wc -l | tr -d ' '
```
```bash
cat README.md 2>/dev/null | head -10
```
```bash
cat package.json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('description',''))" 2>/dev/null
cat pyproject.toml 2>/dev/null | grep -m1 '^description'
cat Cargo.toml 2>/dev/null | grep -m1 '^description'
```

### 早期 escape: 既存 Beacon

`BEACON_EXISTS` を検知したら即:

```
このディレクトリにはすでに beacon が初期化されています。
状態を確認するには /beacon-session-start を実行してください。
```
→ 終了

それ以外は続行。以下を内部で保持:
- `$CWD`, `$CWD_BASENAME`
- `$GIT_COMMITS` (コミット数、0 含む)
- `$README_HEAD` (README の最初の数行)
- `$PKG_DESC` (package.json / pyproject.toml / Cargo.toml の description、見つかれば)

**この段階ではホーム判定や Mode 分岐をしない**。環境情報は Step 2 の draft 組み立てと Step 3 の場所決めに使うだけ。

## Step 2: name + 大目的を明示的に聞く

ユーザーに対して **1 メッセージで以下のフォーム** を提示する:

```
Beacon プロジェクトとして始めますね。以下を教えてください:

  📛 プロジェクト名:   [name-draft があれば挿入、なければ「(例: kakeibo-app)」]
  🎯 大目的:           [objective-draft があれば挿入、なければ「1〜2 行で
                       (このプロジェクトが完成したら何ができるようになるか)」]

  以下は任意 (空でも OK、後で /beacon-vision で深掘りできます):
  👥 ターゲット:        誰のためのプロジェクトか
  📝 その他補足:        制約・成功基準・やらないこと等
```

### draft の組み立て

- **name-draft**:
  - ユーザーの初期発話に明示的な名前があればそれ
  - そうでなく `$GIT_COMMITS >= 10` (既存リポ) なら `$CWD_BASENAME`
  - それ以外は draft なし (フォームは例文だけ)

- **objective-draft** (信頼順に上から拾う):
  - ユーザーの初期発話 (「家計簿アプリ作りたい」等) があればそれ
  - `$README_HEAD` の最初の意味ある 1〜2 行 (HTML/Markdown 装飾は除去)
  - `$PKG_DESC`
  - 何もなければ draft なし

draft を出すときは「README から拾いました、違ったら修正してください」のように **出どころを 1 行添える**。

### このステップでは聞かないもの

- retro-day → デフォルト friday を Step 4 の確認画面で見せる
- storage → デフォルト local。発話に「cloud」「クラウド」「team」「チーム」「複数人」「同期」が含まれていたら cloud にして確認画面に出す
- 場所 → Step 3 で AI が判定、確認画面で見せる

## Step 3: 場所決め (name/objective が固まった後)

ユーザーから name と objective が返ってきた後、cwd を見て subdir 作成要否を判定:

| `$CWD` の種類 | 判定 | `$PROJECT_DIR` |
|---|---|---|
| `$HOME` / `/` / `/tmp` / `$HOME/Desktop` / `$HOME/Documents` / `$HOME/Downloads` | **汎用** → subdir 必要 | `$CWD/<name-as-dirname>` |
| それ以外 (空 dir, 軽量 dir, 既存リポ等) | **専用** → そのまま使う | `$CWD` |

`<name-as-dirname>` は name から英数字+ハイフン化:
- 例: 「家計簿アプリ」→ `kakeibo-app` (ユーザーが name に英字系を書いてくれていればそれを使う、漢字だけなら romaji 化、それも難しければ `project-YYYYMMDD` フォールバック)

**この時点ではまだ mkdir しない**。Step 4 で確認してから Step 5 で実行する。

## Step 4: 確認 (不可逆操作の前、1 回必須)

CORE doc `AeN9aPpjvh6URTQlFmb6` の例外規定: 不可逆操作は事前確認必須。`mkdir` と `beacon init` (cloud project_id 発行を含む) はここに該当する。

### 確認テンプレ

```
以下で進めます:

  📁 場所:              $PROJECT_DIR  [汎用cwdの場合は末尾に「(新規作成)」]
  📛 プロジェクト名:    [name]
  🎯 大目的:            [objective]
  [target が非空なら] 👥 ターゲット:   [target]
  [notes が非空なら]  📝 補足:        [notes]
  🔁 振り返り日:        金曜日                  (後で変更可)
  💾 保存先:            [ローカル or クラウド]  (cloud sync したい場合は「cloud で」と言ってください)

このまま進めて大丈夫ですか？ (「OK」or 修正点を指示)
```

任意項目 (target / notes) が空のまま提出された場合は **その行自体を出さない** (空欄を見せない)。

### 修正の受付

- 個別項目修正 (「name は household-budget で」「cloud で」等) → 該当項目だけ更新して **再提示**
- 「OK」「進めて」「いいよ」等 → Step 5 へ

## Step 5: 実行

**重要**: Bash 呼び出しは必ず `cwd` を明示する (Claude Code 起動時の cwd と `$PROJECT_DIR` は異なる場合がある)。

### Step 5a: mkdir (汎用 cwd の場合のみ)

```
Bash(command="mkdir -p $PROJECT_DIR", cwd="$CWD")
```

### Step 5b: beacon init

```
Bash(
  command="beacon init --name '[name]' --objective '[objective]' --retro-day [retro_day] --storage [storage]",
  cwd="$PROJECT_DIR"
)
```

### Step 5c: 監査 UI 起動

Beacon の作業形態は「ターミナル + 監査 UI 並列表示」が前提。init 直後に UI を立ち上げる。

#### local mode (デフォルト、cloud.json 無し)

Tauri Desktop App の自動起動を試みる。**プロジェクトパスを必ず引数で渡す** (Tauri 側 `find_project_dir()` が `args[1]` を見るため):

```bash
# Bash 呼び出し (cwd=$PROJECT_DIR)
TAURI_OPENED=""
if [ -d "/Applications/Beacon.app" ]; then
  # macOS: --args でプロジェクトパスを渡す。warm start (既存インスタンスあり) の時は
  # tauri-plugin-single-instance が args を running instance に転送し、
  # project-changed イベント経由でフロントが対象プロジェクトに切り替わる (F27)。
  open -a Beacon --args "$PROJECT_DIR" 2>/dev/null && TAURI_OPENED="1"
elif command -v beacon-desktop >/dev/null 2>&1; then
  # Linux: バイナリは args[1] を直接受け取れる
  beacon-desktop "$PROJECT_DIR" &>/dev/null & TAURI_OPENED="1"
elif [ -x "$HOME/AppData/Local/Programs/Beacon/Beacon.exe" ]; then
  # Windows: 同上
  "$HOME/AppData/Local/Programs/Beacon/Beacon.exe" "$PROJECT_DIR" &>/dev/null & TAURI_OPENED="1"
fi

if [ -z "$TAURI_OPENED" ]; then
  echo "TAURI_NOT_INSTALLED"
fi
```

**前提**: Beacon.app v0.1+ (tauri-plugin-single-instance 同梱、F27 で導入)。古い Beacon.app では warm start 時に args が再処理されず、引数なし起動と同等の挙動になる。その場合 Beacon.app を最新ビルドに差し替える。

#### cloud mode (明示的 opt-in、cloud.json あり)

```bash
# Bash 呼び出し (cwd=$PROJECT_DIR)
if [ -f .beacon/cloud.json ]; then
  PROJECT_ID=$(python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id',''))")
  if [ -n "$PROJECT_ID" ]; then
    WEBUI_URL="https://beacon-ai.dev/?project=$PROJECT_ID"
    (open "$WEBUI_URL" 2>/dev/null \
      || xdg-open "$WEBUI_URL" 2>/dev/null \
      || cmd.exe /c start "$WEBUI_URL" 2>/dev/null \
      || powershell.exe -Command "Start-Process '$WEBUI_URL'" 2>/dev/null) &
    echo "WEBUI_URL=$WEBUI_URL"
  fi
fi
```

### Step 5d: 完了報告

mkdir 有無 (= Step 5a が走ったか) と storage で内容を変える。target/notes が入力されていれば末尾に `/beacon-vision` 誘導を 1 行添える。

**brand-new dir (Step 5a で mkdir, local)**:
```
「[name]」のスペースを準備しました (場所: $PROJECT_DIR、local mode)。

[Tauri 起動成功時]
🖥  Beacon Desktop App を開きました。ターミナルの隣に並べておくと、これからの状態変化が常に見られます。

[Tauri 未インストール時]
ℹ️  確認方法:
  - `beacon` で tmux ダッシュボード (要 tmux)
  - Beacon Desktop App をインストール (配布パイプライン整備中、ms-44 参照)
  - cloud sync が欲しくなったら `beacon cloud setup` で opt-in

[target/notes が入力されていた場合]
書いてくれたターゲット・補足は `/beacon-vision` で正式なビジョン doc として整形できます。

→ Step 6 (マイルストーン着手) に進みます
```

**brand-new dir (Step 5a で mkdir, cloud)**:
```
「[name]」のスペース (cloud sync 有効) を準備しました (場所: $PROJECT_DIR)。

📊 Web UI を別ウィンドウで開きました: $WEBUI_URL
   ターミナルの隣に並べておくと、これからの状態変化が常に見られます。

[target/notes が入力されていた場合の /beacon-vision 誘導 1 行]

→ Step 6 (マイルストーン着手) に進みます
```

**既存 dir (Step 5a で mkdir せず、`$PROJECT_DIR == $CWD`)**:
```
このリポジトリを Beacon で管理する準備ができました。

[Tauri or Web UI 起動結果]

→ /beacon-session-start に進みます (現状の git/コードを読み込んで、マイルストーン提案を出します)。
```

## Step 6: マイルストーン着手の入り口

判定軸は **「Step 5a で mkdir したか否か」** だけ。`$PROJECT_DIR != $CWD` (Step 3 で汎用 cwd → subdir 必要と判定) なら mkdir 経由 = brand-new empty dir。それ以外は既存 dir で /beacon-init が叩かれたケース。**git commits 数のような閾値判定はしない** (session-start に委ねる)。

### A. Step 5a で mkdir した場合 (brand-new empty dir)

dir に何もないので Archaeology / code reading の素材がない。**いきなり `/beacon-roadmap` を起動しない**。ユーザーに既にイメージがあるケースが多いので、まず聞く:

```
最初のマイルストーンを決めましょう。

  すでにイメージがあれば教えてください (例: 「領収書の取り込みから始めたい」)。
  まだぼんやりしてるなら、こちらから簡単に提案します。
```

ユーザーの返答で分岐:

| ユーザー応答 | 動作 |
|---|---|
| 「提案して」「お任せ」「分からない」「ぼんやり」等 | → `/beacon-roadmap` (ミニマム提案モード) を起動 |
| 具体的な MS イメージ (「○○から始めたい」「△△を作る」等) | → 即 `beacon milestone add "[ユーザー発話を MS タイトル化]"` を実行。SPEC・vision チェインはしない (ユーザーが明示的に欲しがれば後で呼ぶ) |
| 「ちょっと考える」「まだ決めてない」等 | → 「決まったら `beacon milestone add` で起票できます。`/beacon-roadmap` を呼べばこちらから提案もできます」と案内だけして待機 |

### B. Step 5a で mkdir しなかった場合 (既存 dir で /beacon-init を叩いた)

`$PROJECT_DIR == $CWD`。dir に何かしらコンテキストがある可能性が高い (README / source / git history など)。**`/beacon-session-start` に直接チェイン**:

```
このリポジトリの現状を読み込んで、マイルストーン提案を出します。
→ /beacon-session-start
```

session-start 側が consultant mode で内部判定する (init 側で git 履歴量を見ない、判定責務は session-start に集約):
- `git_commits >= 10` → Archaeology (git log clustering) + code reading
- それ未満 → code reading だけ (B フロー)
- 真の空 dir → 結果が薄くて Q&A 相当に自然に落ちる

### 共通: vision には自動チェインしない

CORE doc `4AS5ehyJc8mGU1gsiFvz` (最速アウトプット) と `PU9HG2IVQdW3tLiAJvix` (バイブコーダー Philosophy) に従い、**`/beacon-vision` には自動チェインしない**。深掘りビジョン整理が必要になったら、ユーザーが明示的に `/beacon-vision` を呼ぶ。

## 制約

- **ユーザーをターミナルに戻さない** (CORE doc `ux-principle-no-terminal`)
  - `beacon init` も `mkdir` もすべて Bash ツール経由で Skill が実行する
  - ユーザーに `cd` や `claude` 再起動を依頼しない
- すべての Bash 呼び出しで `cwd` を明示する
- ファイル操作は絶対パスを使う (`$PROJECT_DIR/foo` の形式)
- `rm` は使わない
- name と objective は **必ず明示的に聞く** (推定だけで進めない)。draft の pre-fill はしてよい
- target/notes は **任意の補足欄として案内するだけ**、空で進んで OK。空なら確認画面にも出さない
- mkdir は **確認 (Step 4) の後で実行** する。確認前には絶対に作らない
- 旧 Mode A/B/C 分岐は廃止。早期 escape (既存 Beacon) と Step 6 のルーティング (mkdir 有無) だけ残す
- **Step 6 で git_commits 数による分岐をしない**。「mkdir した = brand-new」「mkdir しなかった = 既存 dir → session-start に委ねる」だけ。閾値判定は session-start の責務 (F28)
- 新規プロジェクト (brand-new dir) で `/beacon-roadmap` を自動チェインしない。**Step 6 A でユーザーにイメージの有無を聞いてから分岐** する (バイブコーダーが既に手を動かしたい状態を尊重)
- Tauri Desktop の起動コマンドには **必ずプロジェクトパスを引数で渡す** (`open -a Beacon --args "$PROJECT_DIR"` 等)。引数なしだとアプリは起動するがプロジェクト未指定状態になる
- warm start (Tauri 既起動) でも引数が再処理される (F27: tauri-plugin-single-instance 同梱)。pkill 不要
- 専門用語 (プロジェクト、ディレクトリ、リポジトリ等) は使ってよい。説明はしない (ユーザーが Claude Code に聞ける環境を信頼)
