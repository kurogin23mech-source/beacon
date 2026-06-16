---
name: beacon-log
description: コミット後にbeaconへ記録し、進捗率をAI評価で自動更新する。prepare/finalizeの2段階ワークフロー。
version: 0.5.0
triggers:
  - /beacon-log
  - beacon に記録
  - コミットを記録
  - 進捗を更新
---

# Beacon Log

> コミット記録 + MS選定 + 進捗評価を1つのワークフローで完結させる。
>
> (サマリー更新は e-1040 で廃止 — 人間向けナラティブは `project-vision` CORE doc、セッション単位の経緯は session_logs subcollection が継承する)

## 責務分界 (= ms-79 / e-1818)

このSkillは「いま終えた commit」 を構造化する責務に閉じる。タスクキューの意図的な管理 (= 新規 task の add / cancel / 一覧) は `/beacon-task` の責務。「次の塊」 を 1 文で示唆するところまでが /beacon-log の範囲で、それを task として登録するかは user 判断に委ねる。

詳細は CORE doc `5qySQmOHa9sZhyJiOOjR` (= /beacon-log と /beacon-task の責務分界) 参照。Step 4 / Step 4.5 の挙動 (= done 判定 / MS 完了提案) もこの分界に従う。

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

## cwd 解決（最重要）

このSkillは **PostToolUse hook 経由で起動されることが多い**。hookは `additionalContext` に `(project: /abs/path/to/beacon/root)` 形式でプロジェクトパスを埋め込む。Claude Code をホームディレクトリで起動しているケースでは、hookが渡すpathが唯一の手がかりになる。

以下の優先順位で **作業ディレクトリ** を決定する（以降 `$PROJECT_DIR` と呼ぶ）:

1. **hook が渡した `(project: ...)` パス**を additionalContext から抽出
2. ユーザーが `/beacon-log <path>` のように引数で渡した場合はそれを使う
3. それも無ければ、Bash ツールで `pwd` を実行してホーム直下なら abort、それ以外ならカレントを使う

ホームディレクトリ (`$HOME` の値そのもの) を `$PROJECT_DIR` にしてはならない (誤動作の原因)。

**以降、すべての Bash 呼び出しは `cd "$PROJECT_DIR" && ...` 形式で実行する。** `cd` を経由せずに beacon コマンドを実行すると、Claude Code が起動した cwd に依存して動作がブレる。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
cd "$PROJECT_DIR" 2>/dev/null; beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 1: コンテキスト取得（読み取り専用）

Bash ツールで実行:
```bash
cd "$PROJECT_DIR" && beacon log --prepare
```

stdout に JSON が返る。2つのパターンがある:

### パターンA: 単一アクティブMS
```json
{
  "commit": {"hash": "...", "message": "...", "date": "...", "summary": "..."},
  "milestone": {"id": "...", "title": "...", "status": "...", "progress": N, "total_tasks": N, "done_tasks": N, "pending_tasks": [...], "recent_entries": [...]},
  "current_summary": "..."
}
```

### パターンB: 複数アクティブMS
```json
{
  "commit": {"hash": "...", "message": "...", "date": "...", "summary": "..."},
  "candidates": [
    {"id": "...", "title": "...", "status": "...", "progress": N, "total_tasks": N, "done_tasks": N, "pending_tasks": [...], "recent_entries": [...]},
    ...
  ],
  "current_summary": "..."
}
```

## Step 1.5: マイルストーン選定（candidatesがある場合のみ）

パターンBの場合、以下の基準で **メインMSを1つ選定** する:

### 選定基準
- `commit.message` の内容が、どのMSの `title`・`pending_tasks`・`recent_entries` に最も関連するかを判断する
- 直近のエントリの流れ（recent_entries）との連続性を重視する
- 迷う場合は、よりスコープが狭い（具体的な）MSを優先する

### 出力
選定した MS の `id`（例: `ms-9`）。以降の Step では選定した MS の情報を使う。

パターンAの場合はこの Step をスキップし、`milestone` をそのまま使う。

## Step 1.7: 副次MS記録（candidatesがあり、複数MSに関連する場合のみ）

パターンBで、コミットが **メインMS以外のMSにも関連する** 場合、そのMSに `beacon save` で記録を残す。

### 判断基準
- `commit.message` が複数MSの `title` や `pending_tasks` に関連するかを評価する
- 明確に関連するMSのみ対象とする。曖昧な場合は記録しない

### 対象がある場合
各副次MSに対して Bash ツールで実行:

```bash
cd "$PROJECT_DIR" && beacon save "<コミット内容の1行要約（副次MSの観点から）>" -m <副次ms-id> --hash <commit.hash> --source manual --json
```

- description はメインMSへの記録とは異なり、**副次MSの視点で** コミットの貢献を要約する
- `--hash` でコミットハッシュを紐づけることで、同じコミットがどのMSに影響したか追跡可能になる

### 対象がない場合
何もせず Step 2 へ進む。パターンAの場合もこの Step をスキップする。

## Step 1.8: コミット記録の補足情報生成

Step 1（+ Step 1.5）で特定した MS の情報と `commit.message` を読み、以下の2つを生成する。

### behavior（このコミット後にどう動くか）

**ユーザー視点で** 「このコミットをマージした後、システムはどのように動くか」を1〜2文で記述する。

文章の書き方は CORE doc **`entry-writing-principle`** (doc_id `F3ZkqT0pKS6JpR8dn70n`) に従う。Beacon のターゲットには非開発者が含まれるため、開発者の癖 (横文字濫用 / 別 task ID への click-through 前提 / 主語省略) は読み手を排除する。

- NG: 「○○クラスに□□メソッドを追加した」（実装の話）
- OK: 「○○画面で□□ボタンを押すと△△が表示されるようになる」（挙動の話）
- バグ修正の場合: 「以前は□□すると△△が起きたが、修正後は正しく▲▲する」
- 内部リファクタリングで外部挙動が変わらない場合: 「外部挙動の変化なし（内部実装の整理）」

横文字 3 段階に従う:
- 固有名詞 (`Firestore` / `pipx` / `MCP`) はそのまま
- 技術概念 (`allowlist` / `opt-in`) は初出時に日本語注 (例: `allowlist (= 許可リスト)`)
- 一般概念は日本語化 (configure → 設定 / receiver → 受信側 / audit → 監査)

### resolves（解決したタスクID）

Step 1.9 のタスク自動完了と **同じ高信頼度の基準** でマッチしたタスクIDを特定する（`e-XXX` 形式）。
マッチしない場合は空文字列。

### priority（このコミットの変更の優先度）

以下の定義を参照して、このコミットが解決する問題の優先度を判定する:

| 優先度 | 基準 |
|---|---|
| `highest` | サービスの価値が成立しないレベルの影響 |
| `high` | 大コンポーネントに致命的な影響 |
| `middle` | 大コンポーネントに使いにくいレベル、または小機能に致命的 |
| `low` | 小機能に使いにくいレベル |
| `lowest` | 軽微（誤字・表示系など、修正も軽微） |

新機能追加: そのコンポーネントの重要度で判断。バグ修正: バグの影響範囲で判断。リファクタリング/docs: `lowest`。

---

## Step 1.9: タスク自動完了（pending_tasksがある場合のみ） — AC ベース自己判断

メインMSの `pending_tasks` の中から、このコミットで解決されたタスクを **AI が AC を物理的に照合して** 判定する。
キーワード一致や entry-id 明示だけで done にしない。判断軌跡は `done_reason` に必ず残し、後から監査できるようにする。

> この振る舞いは CORE doc `task-done-judgment-principle`（タスク完了判定の AI 自律原則）に従う。違反する判定をしてはならない。

### 参照する材料

タスク側（pending_tasks の各エントリから）:
- `description` / `motivation` / `acceptance_criteria`
- 紐づく MS の SPEC ドキュメント（`beacon doc list --scope spec --ms <ms-id>` で関連 doc があれば `beacon doc show <doc_id>`）
- 紐づく親エントリや depends_on があればそれも参照

コミット側:
- `commit.message`（Step 1 で取得済）
- 必要に応じて `git show <hash> --stat`（どのファイルがどれだけ変わったか）
- AC が具体的でファイルパス言及がある場合は `git show <hash> -- <path>` で実 diff を確認

### 判定ロジック

**1. 候補抽出 → 信頼度評価**

各 pending_task について:
- **HIGH**: コミットメッセージに entry-id が明示（`e-XXX` / `(e-XXX)` / `e-XXX:`）かつ description キーワードと意味的に一致
- **MID**: description / AC のキーワードがコミットメッセージと意味的に一致（entry-id 明示はないが、内容で関連が確信できる）
- **LOW**: 関連はあるが弱い（同じファイル領域だが目的が違う、近い話題だが別実装、等）

**LOW のタスクは最初から skip**（DONE/PARTIAL/SKIP の判定にも進まない、Step 4 の報告にも載せない）。HIGH/MID のみ次の AC 照合に進む。

**2. AC 照合 → 3 通り判定**

AC が定義されている場合（acceptance_criteria が空でない）:
- 各 AC 項目を 1 つずつコミット（message + 必要なら diff）と照合
- **全項目達成 → ✓ DONE**
- **一部達成（AC の一部のみ満たす）→ △ PARTIAL**
- **未達（コミットは関連するが AC は満たさない）→ ✗ SKIP**

AC が空 かつ motivation が空 の場合（旧フォーマットのタスク）:
- `description` のキーワード照合のみで判断
- **HIGH なら ✓ DONE**、**MID なら ✗ SKIP**（保守的に）
- done_reason に「AC 未定義のため description で判断」と注記

AC が空だが motivation がある場合:
- motivation を「達成すべき目的」として扱い、上の AC 照合と同じロジックを適用

### 3 通り判定後のアクション

**✓ DONE**: Bash ツールで実行
```bash
cd "$PROJECT_DIR" && beacon task done <entry-id> --reason "<判断軌跡 1〜2 文>"
```

`--reason` の書き方:
- AC を明示参照する形式: 「AC『○○ができる』をコミット <hash:7> の <file:func> 改修で満たした」
- AC 未定義のとき: 「AC 未定義。description『○○』とコミット内容が一致、entry-id 明示で HIGH 確信」
- 複数 AC を 1 コミットで満たす場合: 「AC 全 N 項目（○○・△△・▲▲）をコミット <hash:7> で達成」

**△ PARTIAL**: done を実行 **しない**。代わりに follow-up task を起票:
```bash
cd "$PROJECT_DIR" && beacon task add "残: <未達 AC 項目を具体化>" -m <ms-id> \
  --motivation "<元タスク description> の AC のうち <未達項目> が未達" \
  --acceptance-criteria "<未達項目を測定可能な形で>"
```

元タスク（PARTIAL 判定されたほう）は `todo` のまま残す。Step 4 報告に「e-XXX: 部分達成、follow-up e-YYY 起票」と明示。

**✗ SKIP**: 何もしない。done も follow-up も書き込まない。Step 4 報告に「e-XXX: AC 未達のため done 保留（理由: <1 行>）」と明示。

### 守るべき原則

1. **AC を物理的に照合する**。キーワード一致や entry-id 明示だけで done にしてはならない。
2. **判断軌跡を done_reason に必ず残す**。「なぜこの判断で done になったか」が後から辿れる形にする。
3. **部分達成は隠さず follow-up task として可視化する**。サイレントな AC 未達を作らない。
4. **AC が定義されていないタスクは保守的に扱う**。HIGH のみ done、MID は SKIP。
5. **AI が自律判断する**。ユーザーに「このタスク done にしていいですか？」と対話介入しない。ユーザーは Step 4 報告と done_reason で事後監査する。

### AI 未検証の側面を done_reason に併記する

DONE する fix について、AI が物理確認できなかった側面（実行できない環境、spawn される subprocess の挙動、別 entry-point の影響、別 OS、別ブラウザ等）があれば、`done_reason` の末尾に「**(未検証: <側面>)**」と 1 行で添える。

これは Skill が AI に完璧を求めるためではなく、**ユーザーが監査時に「ここを見ればギャップが catch しやすい」というポインターを残すため**。「動かしながら考える」philosophy に従い、列挙できなくても DONE 自体は妨げない（フィルタでなくフラグ）。

例:
```
--reason "AC『stdout reconfigure』を main.py 追加で満たした。
          entry-id 明示 + 強一致で HIGH。
          (未検証: subprocess child process での効果、Windows 実機での動作)"
```

「全て」「すべての」「process-wide」「entry-point」「グローバル」「全 OS で動く」などの語が AC に含まれているとき、fix が触ったコード経路以外の発火点（subprocess / 別 entry-point / hook 経由 / fork 後 / 別 OS 等）を 1 つ思い浮かべて、未検証なら必ずこのタグで明示する。思い浮かばない場合は無理に書かなくてよい（過剰な adversarial thinking は philosophy 違反）。

## Step 2: 進捗率の評価

Step 1 (+ Step 1.5) で特定した MS の情報を読み、以下の基準で **進捗率（0-100の整数）** を決定する:

### 評価基準
- `milestone.title`（目標）に対して、現在どの程度到達しているかを **定性的に** 評価する
- `done_tasks / total_tasks` の比率は参考値であり、そのまま進捗率にしてはならない
- タスクの重さは均一ではない。大きなタスクの完了は進捗を大きく動かし、小さなタスクは小さく動かす
- `milestone.progress`（現在の進捗率）からの変化は、今回のコミットの貢献度に見合った幅にする
- 前回より下がることは通常ない（スコープ拡大時を除く）

### 出力形式
整数値のみ（例: `55`）。内部で使用するため、説明文は不要。

## Step 3: 書き込み（finalize）

**ms-68 / e-1641 補足 (= entry-writing principle の自律パス例外)**: `/beacon-log` は **PostToolUse hook 経由で自律起動される唯一の Skill**。post-commit hook はユーザー応答を待てないため、書き込み前の draft 提示 step は持たない。代わりに **Step 1.8 で behavior を生成した直後に self-review (4 原則) を必須化**: 読み手目線 1 行 / 横文字 3 段階 / ID 参照に文脈 / 尻切れトンボ禁止 をその場で 1 度自問し、違反があれば finalize の前に直す。これは ms-68 SPEC (`HQvN4Gimw3n1UKwrk6o1`、書き手の作法を仕組みで担保する MS) で明示された「自律パスは self-review のみで進める」の唯一の正当例。

Step 1.5〜2 の結果と Step 1.8 で生成した補足情報を使って、Bash ツールで実行:

```bash
cd "$PROJECT_DIR" && beacon log --finalize -m <選定したms-id> --progress <Step2の値> \
  --behavior "<Step1.8のbehavior>" --resolves "<Step1.8のresolves（なければ省略）>"
```

`resolves` が空の場合は `--resolves` フラグごと省略する。

> **e-1040 廃止項目**: かつての Step 3 (サマリー生成) + `--summary` 引数は廃止しました。
> 人間向けナラティブは `project-vision` CORE doc、セッション経緯は session_logs subcollection を使ってください。
> ユーザーが `/beacon-log` に引数で説明を渡した場合は finalize に直接渡さず、commit メッセージか behavior に反映してください。

## Step 4: 結果の提示

finalize の stdout と Step 1.9 の判定結果を組み合わせ、ユーザーに結果を簡潔に報告:

```
Beacon: [hash] → [ms-id] [紐づけ先] (progress%)
Behavior: [Step1.8で生成したbehaviorを引用]
by [actor]              ← ms-79 / e-1815 で追加。下の表示ルール参照。

タスク判定 (Step 1.9):              ← 判定対象が 1 件以上ある場合のみ
  ✓ DONE:    [e-id] <description 短縮> — <done_reason 短縮>
  △ PARTIAL: [e-id] <description 短縮> → follow-up [新規 e-id] <残作業>
  ✗ SKIP:    [e-id] <description 短縮> — <skip 理由>

AI が物理確認できなかった側面 (user 監査推奨):   ← done_reason に「(未検証: ...)」があれば
  - [e-id]: <未検証の側面>
```

### actor 表示ルール (= ms-79 / e-1815, UC3-F1)

- log entry は `meta.actor` (= ms-51 / e-934 で記録される `{machine, agent}` ペア) を運ぶ。Skill 側は finalize 直後にこの値を読み、以下の条件で **`by <actor>` 行を末尾に表示** する:
  - **マルチユーザー / マルチエージェント環境**: `agent` が当該プロジェクトで複数登場している場合 (= `beacon status --json` の commit entries で 2 種類以上の `meta.actor.agent` を観測) に表示
  - **fork / dispatch 子セッション**: `meta.actor.agent` が `parent.dispatch-N` 形式の場合は **必ず表示** (= 親と子のどちらの作業として残ったかが視認できないと混乱の元になる)
  - **solo dev**: 1 種類しか登場せず suffix も無い場合は省略 (= 冗長性回避、ms-79 e-1815 AC-2 通り)
- `agent` が `hostname.dispatch-1` 形式なら `by mac.local.dispatch-1`、純粋な hostname なら `by mac.local`、agent.json 設定済なら `by claude-mac` のように、そのまま末尾に置く
- `meta.actor` が欠落 (= 古い commit / actor 解決失敗) のときは行ごと省略

### 表示例

```
Beacon: abc1234 → ms-1 [feature-auth] (35%)
Behavior: ログイン画面でメール送信ボタンを押すと確認メールが届くようになる
by claude-mac.dispatch-2          ← サブエージェント子の commit、必ず表示

タスク判定 (Step 1.9):
  ✓ DONE: e-100 メール送信フロー実装 — AC『...』達成
```

```
Beacon: def5678 → ms-2 (40%)
Behavior: 外部挙動の変化なし（内部実装の整理）
                                  ← solo dev で actor 1 種類のみ、by 行は省略
```

### source 表示ルール (= ms-79 / e-1817, UC3-F3)

`meta.source` が `"auto-op"` の場合 (= Operation 自律実行 / envelope context での commit) は、`Behavior:` 行の直後に **`source: auto-op` を 1 行付け加える**。これにより `/beacon-retrospect` を待たずに、その場で「これは AI 自律 commit です」 と user に伝えられる。

`meta.source` が無い (= 通常の人間対話 commit) ときは何も表示しない (= human が default、冗長表示を避ける)。

```
Beacon: 999abcd → ms-1 (52%)
Behavior: 月次バッチが自動実行され、レポートが指定 S3 に保存される
source: auto-op                   ← Operation envelope 経由の commit
by claude-mac.dispatch-1
```

判定対象がなかった（pending_tasks が空、または LOW 信頼度しかなかった）場合は「タスク判定」セクションごと省略する。
「AI が物理確認できなかった側面」セクションは、DONE 判定したタスクの `done_reason` に「**(未検証: ...)**」が含まれている場合のみ表示する。ユーザーはここを見て監査ポイントを把握する。

## Step 4.5: MS完了判定（e-550 / UC3-G4）

Step 4 の結果から、以下の条件のいずれかを満たす場合、ユーザーに **MS閉じる提案** を行う:

1. **全タスク done**: メインMSの `total_tasks > 0` かつ `done_tasks == total_tasks`
2. **進捗が高水位**: 今回 finalize した進捗率 `>= 95` で、かつ前回より進捗が上がった

判定材料は Step 1 の JSON (`milestone.total_tasks` `milestone.done_tasks`) と Step 3 で finalize に渡した進捗率の値。追加の CLI 呼び出しは不要。

### 提案文（実行は不要、ユーザー判断に委ねる）

```
このコミットで ms-XX の進捗が NN% に到達しました。
- 全タスク done なら: `beacon milestone done ms-XX --reason "..."` で完了化
- まだ観察期間が必要なら: `beacon milestone observe ms-XX --reason "..."` で observing に
- 何もしない場合はこのまま継続

このセッションで完了させますか？
```

**重要**: この Skill は **提案だけ** 行う。`beacon milestone done` / `observe` は **直接実行しない** (e-549 規約: コミット前確認と同じく、状態変更は明示承認を経て初めて走る)。

## Step 5: ドキュメント評価

今回のコミットが **設計判断・方針変更・新しいルール** を含むかを評価する。

### スコープ定義
- **core**: 設計原則・アーキテクチャ方針。全セッションで常時参照される（session-startで自動読み込み）。変更は慎重に。
- **spec**: 仕様・技術的な詳細。特定の機能やAPIの仕様書。
- **memo**: 一時的な検討メモ・調査記録。揮発してもよい情報。

### 評価基準
以下のいずれかに該当する場合、ドキュメント対応が必要:
1. **新しい設計原則・方針が生まれた**（例: 「○○は△△で統一する」）→ core または spec の新規作成
2. **既存のCOREドキュメントと矛盾する変更をした**（例: アーキテクチャの方針転換）→ core の更新
3. **仕様として記録すべき技術的決定をした**（例: APIの認証方式を変更）→ spec の新規作成/更新

### 該当しない場合
何もせず Step 6 へ進む。大半のコミットはここで終わる。

### 該当する場合

1. 既存ドキュメント一覧を取得:
```bash
cd "$PROJECT_DIR" && beacon doc list --json
```

2. 更新すべき既存ドキュメントがあるか、新規作成が必要かを判断する

3. ユーザーに提案する:
```
Doc: [既存doc更新 or 新規作成] [scope] "[タイトル]"
  理由: [なぜドキュメント化が必要か]
```

4. ユーザーが承認したら実行:
   - 新規作成: `cd "$PROJECT_DIR" && beacon doc add --scope <scope> --title "<title>" --content "<content>"`
   - 更新: `cd "$PROJECT_DIR" && beacon doc update <doc_id> --content "<content>"`
   - stdinからコンテンツを渡す場合: `cd "$PROJECT_DIR" && echo '<content>' | beacon doc add --scope <scope> --title "<title>" --stdin`

5. ユーザーが却下したら何もしない。

## Step 6: リズム提案（e-585 / UC6-K8'）

このプロジェクトが既にサイクル中の場合、push / deploy / release のタイミングを **積極的に提案する**。サイクル外なら沈黙する (Claude Code 本体に教育を委ねる)。

### Step 6a: サイクル活性判定の取得

Bash ツールで実行:
```bash
cd "$PROJECT_DIR" && beacon cycle status --json
```

stdout に各サイクルの活性状態 + 直近アクション日が返る:
```json
{
  "push": {"active": true, "last_action_date": "2026-05-20"},
  "deploy": {"active": true, "last_action_date": "2026-05-25"},
  "retro": {"active": false, "last_action_date": null},
  "operation": {"active": false, "last_action_date": null},
  "release": {"active": true, "last_action_date": "2026-05-20"}
}
```

`beacon cycle status` コマンドが未実装または失敗した場合はこの Step をスキップ (リズム提案無し、サイレントに継続)。

### Step 6b: push 提案

`push.active == true` で、かつ Step 1 の `commit.hash` 以降の未 push commit が積み上がっている疑いがあれば提案。

未 push 数のラフな指標: `git log --oneline @{u}..HEAD 2>/dev/null | wc -l`（hookの cwd を尊重）。
0 件なら提案しない。1 件以上なら以下のように案内:

```
push サイクル中のプロジェクトです。未 push commit が N 件溜まっています。
- すぐ push: `git push` のあと `/beacon-push` で記録
- まだ寝かす: そのまま続行
```

`push.active == false` の場合は **沈黙**。

### Step 6c: deploy 提案

`deploy.active == true` で、かつ直近 push 以降に deploy 記録が無い場合に提案:

```
deploy サイクル中のプロジェクトです。前回 deploy: [last_action_date]。
- デプロイの頃合いなら: `/beacon-deploy` で記録
- まだなら: そのまま続行
```

`deploy.active == false` の場合は **沈黙**。

### Step 6d: 沈黙ルール

- 提案は **1コミットあたり最大1種類** (push 提案を出したら deploy 提案は次の機会に)。鬱陶しさ回避
- ユーザーが過去に dismiss した場合、その情報は project の `meta.cycle_hint_shown_at` 等に記録される (将来拡張)。本 Step では best-effort で出すだけ

## Step 7: トリガーチェック

Step 4〜6 の報告後、Bash ツールで実行:
```bash
cd "$PROJECT_DIR" && beacon trigger check
```

JSON 配列が返る。空でなければ、各トリガーの `message` をユーザーに提示する:
```
Beacon trigger: [message]
```

トリガーへの対応（例: `/beacon-retro` の実行）はユーザーの判断に委ねる。自動実行してはならない。

## 制約

- Step 1（prepare）は読み取り専用。書き込みは Step 3（finalize）のみ。
- 進捗率の生成は、Step 1 の JSON に含まれる情報のみで判断する。追加のファイル読み取りやコマンド実行は行わない。
- project.json を Read ツールで直接読んではならない。
- **すべての Bash 呼び出しに `cd "$PROJECT_DIR" && ...` を前置する**。hook 経由起動時は hook が渡した project path を使う (ホーム以外を起点に動作させる)。
- MS 完了化 (`beacon milestone done` / `observe`) は提案のみ。実行はユーザー承認後に別途。
