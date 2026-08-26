---
name: beacon-task
description: beaconのタスク操作（追加・完了・更新・削除・一覧）をCLI経由で実行する。
version: 0.2.0
triggers:
  - /beacon-task
  - beacon にタスクを追加
  - タスクを起票
  - タスクを完了
  - タスクを削除
---

# Beacon Task

> beacon のタスク操作を CLI 経由で行う。

## 責務分界 (= ms-79 / e-1818)

このSkillは「これから取り組む作業のキュー」を意図的に管理する責務に閉じる。コミットの記録 / 進捗率の AI 評価 / commit を起点とした task 自動 done は `/beacon-log` の責務。task の優先順位を推奨する際は、/beacon-log が直前 commit を元に出す「次の塊」 示唆と競合しないように priority highest を除いて log 側を優先する。

詳細は CORE doc `5qySQmOHa9sZhyJiOOjR` (= /beacon-log と /beacon-task の責務分界) 参照。両 Skill の冒頭でこの doc を参照誘導し、新しい finding (= UC3 の追加要望) が来たとき先にこの doc に追記してから実装に入る (= 後付けで境界が動く drift を防ぐ forcing function)。

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

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## 操作の判定

ユーザーの指示から、以下のどの操作かを判定する:

| 操作 | キーワード例 |
|------|-------------|
| add | 追加、起票、作る、入れて |
| done | 完了、終わった、done |
| update | 更新、変更、修正 |
| delete | 削除、消す、キャンセル |
| list | 一覧、確認、見せて |
| show | 詳細、中身 |

## Step 1: 対象マイルストーンの確認

操作に対象MSの指定がない場合、Bash ツールで実行:
```bash
beacon status --json
```
- `status == "in_progress"` のマイルストーンをデフォルトの対象とする
- 複数ある場合はユーザーに確認する

## Step 2: 操作の実行

判定した操作に応じて、Bash ツールで対応するコマンドを実行する:

### add

タスク追加前に、以下の3つを会話文脈から生成する。**文章の書き方は CORE doc `entry-writing-principle` (doc_id F3ZkqT0pKS6JpR8dn70n) の 3 層構造 + 横文字 3 段階 + ID 参照ルールに従う**。Beacon のターゲットには非開発者が含まれるため、開発者の癖 (横文字濫用 / 別 task ID への click-through 前提 / 主語省略) は読み手を排除する。原則の要点:

1. **description は読み手目線で 1 行**: 「何が嬉しいか」をユーザー体験の言葉で。横文字は最小、技術用語ではなく価値で書く
2. **motivation は背景 2-4 文**: 現状の症状 → 放置できない理由 → どう改善されると嬉しいか。開発者でなくても『何が嬉しいか』が分かる粒度
3. **acceptance_criteria は bullet で技術詳細**: ここだけは技術用語 OK、ただし横文字には初出時に日本語注 (`allowlist (= 許可リスト)`)、別 task ID 参照には『何の話か』1 行を添える

**motivation（なぜ必要か）**: 「なぜ今このタスクが必要か」を 2-4 文で。ユーザーの発言・作業文脈・紐づくMSのタイトルから推論。

**acceptance_criteria（完了条件）**: 「これを満たしたらdoneにできる」を bullet で具体的に1〜3項目。曖昧な場合は「〜が動作すること」「〜が確認できること」形式。**横文字 3 段階に従う**: 固有名詞 (Firestore / pipx) はそのまま、技術概念 (allowlist / opt-in) は初出時に日本語注、一般概念 (configure / receiver / audit) は日本語化。

**priority（優先度）**: 以下の定義を参照して判定する:

| 優先度 | 基準 |
|---|---|
| `highest` | サービスの価値が成立しないレベルの影響 |
| `high` | 大コンポーネントに致命的な影響 |
| `middle` | 大コンポーネントに使いにくいレベル、または小機能に致命的 |
| `low` | 小機能に使いにくいレベル |
| `lowest` | 軽微（誤字・表示系など、修正も軽微） |

タスクの性質（バグ修正・新機能・改善・ドキュメント等）と影響範囲から判断する。

**priority は必須 (ms-126)**: `beacon task add` は優先度未指定 (= 空) を拒否する (= 5 段階のいずれかを必ず付ける forcing function)。人が優先度を判断する経路ではこの Skill が常に `--priority` を付けるので問題にならない。人が判断していない機械生成経路 (issue import / roadmap 一括 / dispatch 由来など) だけが `--untriaged` フラグで「未 triage」sentinel を明示的に立てられる。この Skill (= 人の判断経路) では `--untriaged` を使わない。

#### Step 2.5 (add): draft 表示 → ユーザー確認 → 実行

`beacon task add` を実行する **前** に、生成した description / motivation / acceptance_criteria / priority を **平文で 1 度ユーザーに見せて確認** する。silent な書き込みは Web UI で読み手が見るまで違反に気付けないため、書き込み前に touchpoint を 1 つ挟む。

ユーザーに以下の形式で提示:

```
タスクを以下の内容で起票します:

  description: <生成した 1 行>
  motivation:  <生成した 2-4 文>
  AC:
    - <項目 1>
    - <項目 2>
  priority:    <highest/high/middle/low/lowest>
  対象 MS:     <ms-id> <ms-title>

このまま起票しますか? (= OK / 書き直し)
```

- ユーザー応答が `OK` / `はい` / `そのまま` 等 → 下記コマンド実行
- ユーザー応答が書き直し指示 (= 表現の修正 / AC の追加 / priority の変更等) → 該当箇所を直して再度 draft 表示。OK が出るまで繰り返す
- **例外 (= 応答待ち不可)**: post-* hook 経由で起動した自律パスでは応答を待てない。その場合のみ self-review (= 上記 4 原則チェック) のみで `beacon task add` を直接実行する。`/beacon-task` 通常起動はこの例外に該当しない (= 必ず draft 表示する)

```bash
beacon task add "<description>" --ms <ms-id> \
  --motivation "<生成したmotivation>" \
  --acceptance-criteria "<生成したacceptance_criteria>" \
  --priority <生成したpriority>
```
- detail がある場合は `--detail "<text>"` も付加

### done

完了前に `--reason`（完了根拠）を会話文脈から生成する:
- コミットハッシュがあれば「コミット XXXXXXX で実装済み、動作確認済み」
- ユーザーの発言から「〜を確認したため」「〜が不要となったため」等
- acceptance_criteria が記録されている場合はそれを参照して満足度を記述する

```bash
beacon task done <entry-id> --reason "<生成したreason>" --decided-by human-delegated
```
- `--decided-by human-delegated`（ms-154 e-5650 — 決定の監査価値を実捕獲する）: この Skill の done は「ユーザーがキューを操作する意図」＝人間が完了を指示する経路なので、決定主体は人間 = `human-delegated`。これで AI が自律照合して下す `/beacon-log` の done（`autonomous-AI`）と監査上で弁別できる。
- 完了根拠にコミットが絡む場合は実 link を `--evidence "commit:<hash:7>"` で渡す（自己参照は積まない、繰り返し可）。無ければ省略してよい（evidence 空 = 裏付け無しとして正直に残る）。
- entry-id はユーザーが指定するか、description からの照合で特定する
- 照合する場合は先に `beacon task list --json --ms <ms-id>` で一覧を取得し、未完了タスクから一致するものを探す
- **タスク完了後、Step 3（進捗率の自動評価）に進む**

### update
```bash
beacon task update <entry-id> [--description "<text>"] [--status <status>] [--detail "<text>"]
```

### delete
```bash
beacon task delete <entry-id>
```

### list
```bash
beacon task list --ms <ms-id>
```
- デフォルトでは cancelled を非表示。ユーザーが「全部見せて」等の場合は `--all` を付加

### show
```bash
beacon task show <entry-id>
```

## Step 3: 進捗率の自動評価（done 操作時のみ）

done 操作の場合、タスク完了後にマイルストーンの進捗率を定性評価して更新する。

### 3a. コンテキスト取得

Bash ツールで実行:
```bash
beacon log --prepare
```

### 3b. 進捗率の評価

prepare の JSON を読み、以下の基準で進捗率（0-100の整数）を決定する:

- `milestone.title`（目標）に対して、現在どの程度到達しているかを **定性的に** 評価する
- `done_tasks / total_tasks` の比率は参考値であり、そのまま進捗率にしてはならない
- タスクの重さは均一ではない
- `milestone.progress`（現在の進捗率）からの変化は、完了したタスクの貢献度に見合った幅にする
- 前回より下がることは通常ない（スコープ拡大時を除く）

### 3c. 進捗率の書き込み

```bash
beacon milestone update <ms-id> --progress <進捗率>
```

## Step 4: 結果の報告

コマンドの stdout をユーザーに簡潔に報告する。
done 操作時は進捗率の更新結果も含める:
```
Done: [entry-id] description
Progress: ms-id (N% → M%)
```

## Step 5: トリガーチェック（done 操作時のみ）

done 操作の報告後、Bash ツールで実行:
```bash
beacon trigger check
```

タスク done 操作は auto-fire 条件 (retro-day / release-due 等) に影響しないため、 明示的な `tick` は不要 (auto-throttle gate が 5 分に 1 度自動更新する、 ms-98 / e-2764)。 `check` のみで local read。

JSON 配列が返る。空でなければ、各トリガーの `message` をユーザーに提示する:
```
Beacon trigger: [message]
```

トリガーへの対応（例: `/beacon-retro` の実行）はユーザーの判断に委ねる。自動実行してはならない。

## 制約

- データ取得・操作は必ず `beacon` CLI 経由。project.json を直接読み書きしない。
- ID は `e-{連番}` 形式。CLI が自動採番するため、Skill で ID を生成しない。
- 進捗率の評価は prepare の JSON に含まれる情報のみで判断する。
