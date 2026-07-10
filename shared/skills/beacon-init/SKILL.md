---
name: beacon-init
description: Beaconプロジェクトを会話形式で初期化。プロジェクト名と大目的を明示的に聞いてから場所を決める。既存リポはProject Archaeologyへ自動チェイン。
version: 2.3.0
---

# Beacon Init

> 名前と大目的を明示的に聞いて、場所決め → 確認 → init を 1 本道で進める。環境による事前分岐は最小化する (CORE doc `PU9HG2IVQdW3tLiAJvix` バイブコーダー Philosophy に準拠)。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

### 守ること

1. **読み手目線 1 行から始める**: 「何が嬉しいか」「何が困るか」をユーザー体験の言葉で。技術用語ではなく価値で書く
2. **横文字 3 段階**:
   - そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP` / `Tauri` / `WebSocket`)
   - 初出時に日本語注: 技術概念 (`allowlist (= 許可リスト)` / `opt-in (= 個別許可)` / `subcollection (= 子コレクション)`)
   - 日本語化が望ましい: 一般概念 (configure → 設定 / receiver → 受信側 / audit → 監査 / hit → 一致 / install → 設置 / merge → 取り込み / deploy → 配置)
3. **ID 参照には文脈**: `e-XXXX` / `UC?` / `ms-XX` は初出に必ず『何の話か』1 行添える。例 ✗「e-1140 の AC のうち」→ ✓「e-1140 (自動応答の受信側挙動を hook で扱う) の受入条件のうち」
4. **尻切れトンボ禁止**: 主語と述語を省略しない、論理関係を明示

### Self-review (生成直後に必ず)

文章を書いた直後、自分で読み返して以下を自問:
- (a) 読み手 (非開発者を含む) は 1 度読んで意味が取れるか?
- (b) 一般概念の横文字 (configure / receiver / audit / hit / install / merge / deploy 等) が残ってないか?
- (c) ID 参照に『何の話か』1 行添えたか?
- 違反していたら書き直し。enforce ではないが必須の self-check。

### 詳細

詳しい原則と例 (病理の typology / 例外ケース / 良い例・悪い例) は CORE doc `entry-writing-principle` (doc_id `F3ZkqT0pKS6JpR8dn70n`) 参照。ただし本 Skill では上記 4 項目を **常に top of mind** で適用する (CORE 参照は補足、principal は本文埋め込み)。

---

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

ユーザーに対して **1 メッセージで以下のフォーム** を提示する。
**name と objective のどちらにも draft をできる限り埋めて出す** (Step 1 で取れた環境情報を最大限活用):

```
Beacon プロジェクトとして始めますね。以下を教えてください:

  📛 プロジェクト名:   [name-draft を必ず挿入。デフォルトは現在のディレクトリ名 "$CWD_BASENAME"]
                       このまま使う場合は Enter (空入力)、別の名前にする場合はその名前を入力してください。
  🎯 大目的:           [objective-draft があれば挿入し、出どころ ("README から" / "package.json から" 等) を 1 行添える。
                       何も取れなければ「1〜2 行で (このプロジェクトが完成したら何ができるようになるか)」]

  以下は任意 (空でも OK、後で /beacon-vision で深掘りできます):
  👥 ターゲット:        誰のためのプロジェクトか
  📝 その他補足:        制約・成功基準・やらないこと等
```

### draft の組み立て

- **name-draft** (常に必ず draft を出す):
  - ユーザーの初期発話に明示的な名前があればそれ
  - それがなければ常に `$CWD_BASENAME` をデフォルトとして提示する (git commits 数や cwd 種別による出し分けは **しない** / e-539)
  - 提示する際は「デフォルトとして現在のディレクトリ名 "$CWD_BASENAME" を提案します」と明示し、
    「このまま使う場合は Enter (空入力)、別の名前にする場合はその名前を入力してください」と添える
  - 空入力 (= Enter のみ) なら `$CWD_BASENAME` で確定。何か入力されればそれを採用

- **objective-draft** (信頼順に上から拾い、見つかった時点で確定 / e-540):
  - ユーザーの初期発話 (「家計簿アプリ作りたい」等) があればそれ
  - `$README_HEAD` の最初の意味ある 1〜2 行 (見出し直後の段落。HTML/Markdown 装飾、バッジ、画像リンクは除去)
  - `$PKG_DESC` (package.json `description` / pyproject.toml `description` / Cargo.toml `description` のいずれか)
  - どれも取れなければ draft なし → ユーザーに自由記述してもらう

draft を出すときは「README から抽出: "<text>"。これで OK ですか? (修正があれば書き換え版を入力)」のように
**出どころを 1 行添える**。ユーザーが「OK」「これで」と返したら採用、書き換え版を返したらそちらを採用。

### Step 1 への補強 (README / package description 抽出)

Step 1 の並列 Bash 実行で `$README_HEAD` と `$PKG_DESC` を取得済み。draft 抽出時は以下を意識する:

- README の最初の見出し (`# Project Name`) **直後** の段落 1〜2 文を採用する。見出し自身や badge 行
  (`![...](...)`, `[![...]](...)`) はスキップ
- 複数のソース (README と package.json) が両方ヒットしたら README を優先 (人間が書いた説明文の方が
  「大目的」に近いことが多い)
- 出どころを必ずユーザーに見せる (「README から抽出: ...」「package.json description から: ...」)

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

#### TAURI_NOT_INSTALLED の場合の案内 (e-779)

Tauri Desktop App が見つからなかった場合、ユーザーの OS を `uname -s` で判定して **その OS 向けの install コマンドを 1〜2 行で提示** する。コマンドを書いて見せるだけで、Skill 側からは自動 install しない (ユーザーが意思を持って install する流れを尊重する)。

```bash
# OS 別 install 案内 (TAURI_NOT_INSTALLED 検出時のみ表示)
OS_NAME=$(uname -s 2>/dev/null || echo "Unknown")
case "$OS_NAME" in
  Darwin)
    echo "  Beacon Desktop は未インストールです。以下で入ります (macOS):"
    echo "    brew install --cask beacon-desktop"
    echo "  または: GitHub Releases から .dmg を直接ダウンロード"
    ;;
  Linux)
    echo "  Beacon Desktop は未インストールです。以下で入ります (Linux):"
    echo "    curl -LO https://github.com/r-kida2/beacon/releases/latest/download/Beacon-x86_64.AppImage"
    echo "    chmod +x Beacon-x86_64.AppImage && ./Beacon-x86_64.AppImage"
    ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    echo "  Beacon Desktop は未インストールです。以下で入ります (Windows):"
    echo "    winget install BeaconAI.BeaconDesktop"
    echo "  または: GitHub Releases から .msi を直接ダウンロード"
    echo "  (SmartScreen 警告が出たら『詳細情報』→『実行』)"
    ;;
  *)
    echo "  Beacon Desktop は未インストールです。INSTALL.md を参照:"
    echo "    https://github.com/r-kida2/beacon/blob/main/INSTALL.md"
    ;;
esac

echo ""
echo "  install 後にもう一度 /beacon-init を叩けば、自動で起動します。"
echo "  (今すぐ Web UI で見るなら https://beacon-ai.dev/?project=... を開く"
echo "   ※ cloud mode で初期化した場合のみ)"
```

この案内はユーザーが自分で install する選択肢を残しつつ、必要なコマンドを目の前に出すことで「次に何をすれば動くか」を明確にする。

**前提**: Beacon.app v0.1+ (tauri-plugin-single-instance 同梱、F27 で導入)。古い Beacon.app では warm start 時に args が再処理されず、引数なし起動と同等の挙動になる。その場合 Beacon.app を最新ビルドに差し替える。

#### cloud mode (明示的 opt-in、cloud.json あり)

```bash
# Bash 呼び出し (cwd=$PROJECT_DIR)
if [ -f .beacon/cloud.json ]; then
  PROJECT_ID=$(python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id',''))")
  if [ -n "$PROJECT_ID" ]; then
    WEBUI_URL="https://beacon-ai.dev/?project=$PROJECT_ID"
    # ms-46 e-737: macOS が Beacon.app を URL handler に登録するケースを避け、
    # ブラウザを明示的に指定 (Tauri 起動防止)。
    DEFAULT_BROWSER=$(python3 -c "
import subprocess, plistlib, os, sys
p = os.path.expanduser('~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist')
try:
    with open(p, 'rb') as f: d = plistlib.load(f)
    for h in d.get('LSHandlers', []):
        if h.get('LSHandlerURLScheme') == 'https':
            r = h.get('LSHandlerRoleAll', '')
            if r and 'beacon' not in r.lower():
                print(r); sys.exit(0)
except Exception: pass
print('com.apple.Safari')
" 2>/dev/null || echo 'com.apple.Safari')
    (open -b "$DEFAULT_BROWSER" "$WEBUI_URL" 2>/dev/null \
      || open -a Safari "$WEBUI_URL" 2>/dev/null \
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

## Step 7: DM 機能 (multi-session messaging) の有効化提案 (ms-54 e-1238)

Step 6 で MS 着手の入り口を案内したあと、**最後に 1 回だけ** DM 機能の有効化を提案する。フロー A/B どちらでも同じ扱い。

### Step 7a: opt-out 判定

まず Bash ツールで以下を実行し、ユーザーが既に DM 機能の opt-out を表明していないか確認する:

```bash
cd "$PROJECT_DIR" && python3 -c "
import os, json, sys
# env
if os.environ.get('BEACON_NO_BUS', '').strip() in ('1','true','yes','on'):
    print('opted_out'); sys.exit(0)
# project
p = os.path.join('.beacon','project.json')
if os.path.exists(p):
    try:
        with open(p) as f: d = json.load(f)
        if (d.get('bus') or {}).get('disabled'):
            print('opted_out'); sys.exit(0)
    except Exception: pass
# global
g = os.path.expanduser('~/.beacon/config.json')
if os.path.exists(g):
    try:
        with open(g) as f: d = json.load(f)
        if (d.get('bus') or {}).get('disabled'):
            print('opted_out'); sys.exit(0)
    except Exception: pass
print('ok')
"
```

stdout が `opted_out` の場合は **沈黙**。`beacon channel install` を呼ばない、提案メッセージも出さない。「ユーザーが要らないと宣言した状態を尊重する」のが構造的に正しい振る舞い。

stdout が `ok` の場合のみ Step 7b に進む。

### Step 7b: 提案メッセージ

```
最後に 1 つ。Beacon には DM 機能 (= 別マシン・別 worktree の Claude Code セッションと
リアルタイムにメッセージ交換できる仕組み) があります。Mac と Win で同じプロジェクトを
開いた時、両セッションが互いに会話できます。

有効化しますか？ (Node が要ります)
  - はい → `beacon channel install` で .mcp.json に登録、bclaude で起動できるようになる
  - あとで → そのまま続行 (好きな時に `beacon channel install` で有効化)
  - 要らない → `beacon channel opt-out` で将来の auto-install も止める
```

### Step 7c: 実行

ユーザーが「はい」と答えたら Bash で `cd "$PROJECT_DIR" && beacon channel install` を実行し、出力をそのまま提示する。失敗時 (Node 未インストール等) は出力に含まれる brew/winget/nvm のヒントを尊重し、追加の弁明はしない。

「あとで」「要らない」を選んだら何もしない。ユーザーは後で明示的に opt-in / install できる。

## GitHub owner / repo の取り扱い (= 推測禁止 / e-2370)

install 手順 / clone コマンド / repository URL 等を生成・提示するとき、 GitHub の **owner (= 所有者) と repository 名は AI が推測してはならない**。 過去の dogfood (= 2026-06-24) で、 install prompt 生成時に AI が owner 名を勝手に推測 (= hallucination) して wrong owner の clone URL を流す事故が観察された。 user が手元で気付かないと、 wrong owner の clone url が install 手順に流れ、 install 失敗 / 別 user の repo を clone する事故 (= silently wrong url) になる。

### 構造的ルール

1. **取得は構造化 source から**: GitHub owner / repository 名が必要になったら、 必ず以下の順序で **コマンド実行による取得** を行う:
   ```bash
   # 第 1 候補: gh CLI (= 認証済 / fast)
   gh repo view --json owner,name -q '.owner.login + "/" + .name' 2>/dev/null

   # 第 2 候補: git remote (= gh が無い時)
   git remote get-url origin 2>/dev/null \
     | sed -E 's|^.*github\.com[:/]([^/]+)/([^/.]+)(\.git)?$|\1/\2|'
   ```

2. **取得失敗時は placeholder を残す**: 両方失敗 (= git remote が無い / GitHub 以外の remote / コマンド未インストール) したら、 AI 推測で埋めずに **明確な placeholder** をそのまま残す:
   - owner: `<your-github-owner>`
   - repo:  `<your-repo-name>`
   - URL 例: `https://github.com/<your-github-owner>/<your-repo-name>`

3. **絶対禁止**:
   - cwd ディレクトリ名 / プロジェクト名から owner を類推する
   - 過去の文脈 / training data から既知の owner 名を流用する
   - 「たぶん」「推定」 で owner を埋める

4. **user への提示**: placeholder を残した時は「GitHub owner / repo は取得できませんでした。 install 前にこの placeholder を user の値に置き換えてください」 と明示的に伝える。

この forcing function は本 Skill だけでなく、 `/beacon-onboard` Skill および server/static/join.html `buildSetupPrompt` (= 招待 install prompt) でも適用される (= 全 install prompt 経路に共通)。

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
