---
name: beacon-init
description: Beaconプロジェクトを会話形式で初期化。既存リポジトリはProject Archaeologyでgit logから自動分析。beacon init --name/--objective フラグで非対話実行。
version: 1.0.0
triggers:
  - beacon init
  - プロジェクトをbeaconで管理したい
  - beacon を始めたい
---

# Beacon Init

> 会話でプロジェクト情報を収集し、`beacon init` を非対話で実行する。既存リポジトリはProject Archaeologyを提案する。

## Step 1: 環境スキャン + モード判定

以下を **並列に** Bash ツールで実行（CWD は Claude Code 起動時の cwd）:

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
ls -la 2>/dev/null | head -10
```
```bash
test -f .beacon/project.json && echo "BEACON_EXISTS"
```
```bash
cat README.md 2>/dev/null | head -5
```
```bash
cat package.json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('description',''))" 2>/dev/null
cat pyproject.toml 2>/dev/null | grep -m1 '^description' 2>/dev/null
cat Cargo.toml 2>/dev/null | grep -m1 '^description' 2>/dev/null
```

### モード判定（3モード）

| 判定材料 | モード |
|---|---|
| `.beacon/project.json` あり | **A: 既存Beacon** → 「すでに初期化済みです」と伝えて終了 |
| `.git` あり + コミット数 >= 10 + README/package.json description あり | **C: 既存リポBeacon化**（経験開発者の既存プロジェクト） |
| 上記以外（ホーム / 空dir / 軽量dir / git無し等） | **B: 新規プロジェクト**（推定駆動、subdir作成は必要時のみ） |

判定結果を内部で `$INIT_MODE = A/B/C` として保持。

**Note**: 旧設計の Mode B / D を統合。「ホームで起動した非開発者」と「mkdir済みの開発者」は subdir 作成有無が違うだけで UX は同じであるべき（質問最小化）。

## Step 2: モード別の準備

### モード A（既存Beacon）
```
このディレクトリにはすでに beacon が初期化されています。
状態を確認するには /beacon-session-start を実行してください。
```
→ 終了

### モード B（新規プロジェクト、推定駆動）

ユーザーの最初の発話から **プロジェクト名と目的を推定** する。

#### サブステップ B-1: subdir 必要性の判定

cwd が `$HOME` / `/` / `/tmp` / `$HOME/Desktop` / `$HOME/Documents` 等の **汎用ディレクトリ** か？

- **YES（汎用ディレクトリ）**: 専用 subdir を作る必要あり
  - ユーザーの発話から名前推定（「家計簿アプリ作りたい」→ `kakeibo-app`）
  - Bash(`mkdir -p ~/<name>`, cwd=~)
  - `$PROJECT_DIR = ~/<name>`
  - ユーザーへ告知: 「『○○』のスペースを ~/<name> に作りますね」

- **NO（既にディレクトリが指定されている、空でも軽量プロジェクトでも）**:
  - subdir 作成は不要
  - `$PROJECT_DIR = "$(pwd)"`
  - 名前推定はディレクトリ basename またはユーザー発話から

#### サブステップ B-2: 推定値の準備

- **name**: 推定済み（subdir 名 or basename）
- **objective**: ユーザーの最初の発言をそのまま、または整形
- **retro-day**: friday（デフォルト）
- **storage**: cloud（認証済み）/ local

質問は **0回**。ただし、ユーザーが明らかに違うものを期待してそうなら一言確認:
> 「ところで名前は kakeibo-app で大丈夫？」

### モード C（既存リポBeacon化）

```
$PROJECT_DIR = "$(pwd)"
```

推定値を準備:
- **name**: ディレクトリ basename
- **objective**: README 先頭 / package.json description / pyproject description のいずれか1文
- **retro-day**: friday（デフォルト）
- **storage**: cloud（デフォルト、認証済みなら）/ local

Step 3 でまとめて1画面確認（個別質問しない）。

## Step 3: モード別の情報収集

### モード B（subdir作成）

ユーザーの最初の発言（やりたいこと）を **そのまま objective に転用** する。  
追加で聞くことはなし。デフォルト値を適用:
- name: Step 2 で推定済み
- objective: ユーザーの最初の発言（必要に応じてAIが整形）
- retro_day: friday
- storage: cloud（認証済みなら）/ local（未認証）

→ Step 4 へ（確認も最小限）

### モード C（既存リポジトリBeacon化）

Step 2 で準備した推定値を **1画面で確認** する（個別質問しない）:

```
このリポジトリを Beacon で管理しますね。以下で進めます:

  プロジェクト名: [basename]
  大目的: [READMEから推定]
  Retro day: 金曜日
  Storage: [cloud or local]

OK ですか？変えたい項目があれば教えてください（例: 「nameは○○で」）
```

ユーザーが「OK」「いいよ」等で承認 → Step 5 へ。  
修正があれば該当項目だけ更新して再提示。

## Step 4: 確認

モード B: 確認スキップ（推定で進む、Step 3 で告知済み）  
モード C: Step 3 で確認済み。ユーザー修正があれば該当項目だけ更新

## Step 5: 実行（モード B / C 共通）

**重要**: Bash 呼び出しは必ず `cwd=$PROJECT_DIR` を指定する（Claude Code 起動時の cwd と異なる場合があるため）。

```bash
beacon init --name "[name]" --objective "[objective]" --retro-day [retro_day] --storage [storage]
```

Bash ツール呼び出し例:
```
Bash(command="beacon init --name ... --objective ... --retro-day ... --storage ...", cwd=$PROJECT_DIR)
```

### Step 5b: Web UI を自動オープン（cloud mode の場合）

Beacon の作業形態は「ターミナル + Web UI 並列表示」が前提。  
init 直後に Web UI を立ち上げて、以降は別ウィンドウで開いたままにする。

```bash
# Bash 呼び出し (cwd=$PROJECT_DIR)
if [ -f .beacon/cloud.json ]; then
  PROJECT_ID=$(python3 -c "import json; print(json.load(open('.beacon/cloud.json')).get('project_id',''))")
  if [ -n "$PROJECT_ID" ]; then
    WEBUI_URL="https://beacon-ai.dev/projects/$PROJECT_ID"
    # OS判別: mac/Linux/Win いずれかで動く
    (open "$WEBUI_URL" 2>/dev/null \
      || xdg-open "$WEBUI_URL" 2>/dev/null \
      || cmd.exe /c start "$WEBUI_URL" 2>/dev/null \
      || powershell.exe -Command "Start-Process '$WEBUI_URL'" 2>/dev/null) &
    echo "WEBUI_URL=$WEBUI_URL"
  fi
fi
```

local mode（cloud.json 無し）の場合は Web UI なし。  
代わりに案内（Tauri Desktop App or tmux ダッシュボード）。

成功したら、モード別のメッセージを出す:

**モード B**:
```
「[name]」のスペースを準備しました（場所: $PROJECT_DIR）。

📊 Web UI を別ウィンドウで開きました: $WEBUI_URL
   ターミナルの隣に並べておくと、これからの状態変化が常に見られます。
   （local mode の場合: beacon Tauri Desktop または `beacon` で tmux ダッシュボードが使えます）

続けてもう少しだけ話を聞かせてください、目指す形を整理してから始めましょう。
→ /beacon-vision に続きます
```

**モード C**:
```
このリポジトリを Beacon で管理する準備ができました。

📊 Web UI を別ウィンドウで開きました: $WEBUI_URL
   ターミナルの隣に並べておくと、これからの状態変化が常に見られます。

続けて、ターゲットや成功基準など、READMEには書かれていない部分を整理しましょう。
→ /beacon-vision (Existing モード) に続きます
```

## Step 6: 次フローへのチェイン（モード別）

### モード B → /beacon-vision (Fresh モード) に必ずチェイン

新規プロジェクトの本質はビジョン整理から。確認なしで `/beacon-vision` を起動。  
ユーザーの最初の発言がすでに「やりたいこと」なので、Skill 内で Fresh モードとして対話継続。

```
Bash(... or Skill invocation ...) で /beacon-vision を起動
渡す context: $PROJECT_DIR, ユーザーの初期発言
```

### モード C → /beacon-vision (Existing モード) に必ずチェイン

既存リポでも **ターゲット・成功基準・制約・やらないこと** の明文化は必要。  
README + package.json description で objective は推定済み、Existing モードが Skill 内で残り4セクションを直接質問で埋める。

```
Bash(... or Skill invocation ...) で /beacon-vision を起動
渡す context: $PROJECT_DIR, 既存推定値（name/objective）
```

完了後、roadmap はスキップして `/beacon-session-start` で Project Archaeology が走る。

## Step 7: session-start の起動

`/beacon-vision` が完了した後、Skill 内チェインで `/beacon-session-start` が `cwd=$PROJECT_DIR` で起動する。

モード B では: vision → roadmap → session-start のチェイン  
モード C では: vision (Existing) → session-start (Archaeology) のチェイン（roadmap スキップ）

## 制約

- **ユーザーをターミナルに戻さない**（CORE doc `ux-principle-no-terminal` 参照）
  - `beacon init` も `mkdir` もすべて Bash ツール経由で Skill が実行する
  - ユーザーに `cd` や `claude` 再起動を依頼しない
- すべての Bash 呼び出しで `cwd=$PROJECT_DIR` を指定する（Claude Code 起動時の cwd と異なる場合があるため）
- ファイル操作は絶対パスを使う（`$PROJECT_DIR/foo` の形式）
- `rm` は使わない
- ユーザーが明示的に答えた情報は再度聞かない
- モード B/C では質問を最小化（推定 + デフォルト適用、修正は受け付ける）
- モード D は従来通り 4 問順に聞く
- 専門用語（プロジェクト、ディレクトリ、リポジトリ等）は使ってOK。説明はしない（ユーザーがClaude Codeに聞ける環境を信頼）
