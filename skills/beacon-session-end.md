---
name: beacon-session-end
description: Beaconプロジェクトのセッション終了時にサマリーを更新し、未完了タスクを整理する。
version: 0.1.0
triggers:
  - /beacon-end
  - /beacon-session-end
  - セッション終了
  - 今日はここまで
  - 作業を終わる
  - 終わろう
  - おしまい
  - また明日
  - 今日は終わり
  - ここで切ろう
  - 一旦終了
  - 切り上げ
---

# Beacon Session End

> セッション終了時にサマリーを更新し、次セッションへの引き継ぎを整備する。

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

## Step 1: 現状の収集（並列実行可）

以下を **Bash ツール** で **並列に** 実行:

### 1a. プロジェクト状態
```bash
beacon status --json
```

**職種分岐 (ms-108 e-3269 / SPEC 方針6)**: session-end は③共有フレーム Skill。1a の結果の `profession` を見て分岐する:
- **`profession == "dev"` (または未設定)**: 以降の 1b / Step 2.5 (MS・タスク棚卸し) をそのまま実行 (現行動作)。
- **`profession == "sales"` 等 (非 dev)**: `milestones[]` は空なので 1b (アクティブ MS のタスク一覧) と Step 2.5 の **MS 棚卸し (milestone observe / close) はスキップ** する。代わりに `targets[]` (= 商談) と直近の活動 (activity) / 証跡 (communication) を読み、Step 3 のサマリーを「このセッションで動かした商談・活動」で構成する。商談のフェーズ確定 (advance / retry / terminal) は営業の別フロー (`/beacon-sales-cockpit` / `beacon opportunity judge`) の責務で、session-end では触らない (= session-start が『次の一手』を cockpit に委譲するのと対称)。session log への機械集約は職種非依存化済み (`collect_project_entries` が商談 Target も歩く、e-3269 増分C)。

### 1b. アクティブMSのタスク一覧 (dev のみ)
```bash
beacon task list --json --ms <active-ms-id>
```
（active-ms-id が不明な場合は 1a の結果を待ってから実行。営業では milestones が無いのでスキップ）

### 1c. git 状態
```bash
git status --short
git log --oneline -3
```

## Step 2: 未コミット変更の確認

Step 1c で uncommitted changes がある場合、ユーザーに通知:
```
未コミットの変更があります:
  M file1.py
  M file2.py
コミットしてから終了しますか？
```
ユーザーの判断を待つ。

## Step 2.5: MS・タスク棚卸し（自動整理）

Step 1a の結果を読み、以下を自動整理する。

### A. 完了済みMSのステータス修正 (ms-43 e-567: 柔軟化)

これまでは `progress == 100` **かつ** `done_tasks == total_tasks` の AND 条件のみで検出していたが、現場では片方しか満たさないが「実質完了」のケースが多く、過半数の MS が in_progress に留まる問題があった。以下の **3 つのシグナルのうち 2 つ以上** が成立する MS を「完了候補」とする。

**シグナル**:
1. `progress == 100`
2. `done_tasks + cancelled_tasks == total_tasks` (cancel は完了とみなす)
3. 直近 14 日以内に **新規 task が追加されていない** かつ 直近 7 日以内にコミットがある (= 静かに収束した)

`status == "in_progress"` の MS のうち上記を満たすものを **「完了候補」** として **ユーザーに確認** する:

```
完了候補と思われる MS:
  - [ms-XX] [title]
    シグナル: progress=100 / 全タスク消化 / 直近 7 日でコミット 5 件
    → まだ作業継続ですか？ 区切りが付いていれば別途 close/observe してください。
```

**observe への遷移は session-end では行わない (ms-119)**。`observing` は「基本目的は達成済み・運用に回してよい」という**完了主張**であり、目的達成 / 思想レビューのゲート対象になった。「セッションが終わること」と「MS を完了主張として閉じること」の間に相関はないので、session-end が observe を実行するのは miswiring。session-end はあくまで **完了候補を surface するだけ** に留め、実際の遷移は本人が `beacon milestone observe <ms-id> --review`（= 人間承認ゲート経由）を意図的に走らせる。

done にする場合 (進行中マイルストーン状態を「閉じる」) は、区切りが明確なら session-end からでも実行してよい:

```bash
beacon milestone close <ms-id>
```

**強制実行はしない**。AND 条件を緩めた分、誤判定リスクが上がるので、必ず人間に確認を求める。

なお、シグナル 2 で言う `cancelled_tasks` は `beacon task list --json --ms <ms> | jq '[.entries[] | select(.status=="cancelled" and .type=="task")] | length'` 相当で取得する。CLI が直接フィールドを返していない場合は `entries[]` を AI が走査する。

### B. 実装済みタスクの自動done候補提示

アクティブMSの `pending_tasks` と Step 1c の `git log` を照合し、「最近のコミットで解決されたと思われるが done になっていないタスク」を特定:

- コミットメッセージにタスクIDが含まれる
- コミットメッセージがタスクの description と明確に対応する

候補がある場合はユーザーに提示:
```
タスク完了の候補:
  - [e-256] beacon setup コマンド実装
  → done にしますか？
```

ユーザーが承認したら:
```bash
beacon task done <entry-id>
```

候補がない場合はこのステップをスキップする。

## Step 3: サマリーの生成

Step 1 の情報を元に、以下の基準でサマリーを生成:

### 記載すること
- このセッションで何に取り組み、何が決まったか
- なぜ今の方針に至ったか（背景・判断の経緯）
- 次セッションで最初に知るべきこと
- ブロッカーや懸念点があれば

### 記載しないこと
- 進捗率やタスク消化数（beacon status で見える）
- コミット一覧（git log で見える）

### 出力形式
2-4文の日本語テキスト。

## Step 4: サマリーの書き込み (session log として永続化)

**ms-68 / e-1643 補足 (= entry-writing principle の draft 表示)**: `beacon session end --summary` を実行する **前** に、Step 3 で生成したサマリー本文を 1 度ユーザーに提示し、self-review 4 原則 (読み手目線 1 行 / 横文字 3 段階 / ID 参照に文脈 / 尻切れトンボ禁止) を 1 度通す。session log は次セッションの AI / 人間が **唯一の引き継ぎ手段** として読むため、横文字濫用 / ID 参照に文脈なし / 尻切れトンボ は次セッションの初動を確実に劣化させる。違反があれば書き直してから書き込む。

```
session log を以下の summary で永続化します:

  <Step3 で生成したサマリー本文>

このまま記録しますか? (= OK / 書き直し)
```

`beacon summary "<text>"` は ms-57 e-1040 で **廃止** (CLI が ignored を返す)。後継は `beacon session end --summary "<text>"`。これは session log を upsert し、`summary` フィールドに引き継ぎ narrative を書き込む。次回の `/beacon-session-start` Step 1j がこの session log を読み「次セッション最優先 / top of queue / 次にやること」セクションを抽出する経路。

Bash ツールで実行:
```bash
beacon session end --summary "<Step3のテキスト>"
```

このコマンドは同時に session 中の notes / commits / PRs も session log に集約する。`beacon session log list` で確認可能。

**作業した target への帰属 (ms-153 e-5550 → ms-164 e-5942 / SPEC 方針3)**: session log は「project 一本のまとめ」ではなく **実際に作業した target (= 対象)** に帰属付けて記録される (read=root / write=作業 target の非対称)。帰属先は CLI が自動で決める — **fork worktree では `.beacon/fork.json` の `target_ms_id` を土台に** (この fork がその target を進めるために立てられたため)、session 中の commit が載った target 群と **合わせて** 帰属する。ms-164 で帰属は **複数化 (multi-attribution)**: 1 つの session が 1 日で複数 target を横断して触れても、その **全ての作業 target に帰属** する (旧挙動は複数横断を「曖昧」として未帰属に畳んでいた)。これにより 1 つの session log を root からも各子 target からも辿れる。commit も fork も無いときだけ active target にフォールバックし、それも無ければ未帰属 (project 全体) のまま。Skill 側で target を指定する必要はない (payload の `target_ids` (複数) / 後方互換の先頭 `target_id` / `target_source` に自動記録)。判定規則そのものは occupation 側の単一ヘルパー `occupation.resolve_worked_targets` に集約されており、note / push / deploy / incident も同じ規則を共有する (ms-164 実装順序1)。

> **帰属の realize 方法 (スコープ境界)**: 現時点では「まとめ本文の保存先そのもの (= `session_logs` サブコレクション) を作業 target のレコード配下へ物理移動する」ことはしていない。保存先の移動は schema 移行に当たるため、代わりに session log レコードに `target_ids` / `target_source` を付けて「どの target 群を進めた session か」を構造的に辿れるようにしている。`read=root / write=作業 target` の非対称のうち write 側は「作業 target 群への帰属付け」まで realize 済み (本文の物理再配置と read 側の対称移行は後続スライス)。

**重要**: Step 3 で生成したサマリーには、引き継ぎを次セッションが拾えるよう **「次セッション最優先」「次にやること」「top of queue」等の見出し** を含めること (session-start Step 1j の抽出ロジックがこのキーワードでセクションを切り出す)。

## Step 4.5: セッションメモのレビューと統合昇格

Bash ツールで実行:
```bash
beacon note list --json
```

メモが存在しない場合はこのステップをスキップする。

メモが存在する場合、以下の流れで処理する。

### A. メモの解釈

各メモについて、**会話の流れと現在のコンテキストから** 次の3点を整理する:

1. **経緯**: そのメモはどのような議論・作業・気づきの中で生まれたか（1〜2文）
2. **残す価値**: 将来のセッションで参照する意義があるか
   - 価値あり: 設計判断・嗜好・ハマったポイント・再現が難しい状況・暗黙の前提
   - 価値なし: そのセッション内で消化済み、コードやドキュメントに既に反映済み、一時的な作業メモ
3. **残す理由**: 価値ありと判定した場合、どの場面で参照されると想定するか（1文）

ただコピペするだけのメモ昇格は **してはならない**。文脈の無いメモは将来のセッションでノイズになる。

### B. 統合ドキュメントの構成

価値ありと判定したメモを **1つのドキュメント** にまとめる。

- タイトル: `セッションメモ YYYY-MM-DD`（今日の日付）
- scope: `memo`
- 既に同じ日付のメモドキュメントが存在する場合（`beacon doc list --scope memo --json` で確認）、新規作成ではなくユーザーに「既存ドキュメントへ追記しますか？」と確認する

本文フォーマット:
```markdown
# YYYY-MM-DD セッションメモ

このセッション中に残したメモのうち、将来参照する価値があるものをまとめて記録した。

## [メモのテーマ／主題]

**経緯**: [どんな議論・作業で生まれたか]

**残す理由**: [将来どの場面で参照されるか]

**メモ本文**:
> [originalのメモtext]

[必要なら補足: 関連MS、関連エントリ、補強情報など]

---

## [次のメモのテーマ]
...
```

### C. ユーザーへの提示と承認

統合ドキュメントの内容をユーザーに提示し、承認を待つ:
```
N件のメモのうちM件を memo ドキュメントに残します。

[統合ドキュメントの内容プレビュー]

このまま記録してよいですか？
```

ユーザーが内容を修正したい場合は調整する。

### D. 書き込み

**heredoc は必ず quoted EOF (`<<'EOF'` または `<< 'EOF'`) を使う**: 非引用 `<<EOF` だと shell が中身の backtick (`` ` ``) を command substitution として展開し、本文が silent corrupt する (2026-06-10 LPS dogfood で観察された病理、e-1401)。

承認後、Bash ツールで実行:
```bash
beacon doc add "セッションメモ YYYY-MM-DD" --scope memo --stdin <<'EOF'
<統合ドキュメントの本文>
EOF
```

既存ドキュメントへの追記の場合:
```bash
beacon doc update <doc-id> --content "<既存本文 + 新セクション>"
```

### E. メモのクリア

書き込み（または「全て残す価値なし」と判定）後、Bash ツールで実行:
```bash
beacon note clear
```

## Step 4.7: セッションの判断 (decision) 過不足チェック (ms-164 e-6031)

session log (Step 4) は「このセッションで何が起きたか」の**物語 (narrative)** を残す。それとは別に、target 一級化では「1 つの作業対象 (target) で下した判断」は **decision arm** (= 誰が / なぜ / 何を根拠に判断したか、の監査可能な記録) に target 帰属で残るのが正。判断は `/beacon-log` の Step 4.7 (コミット単位の log-time backstop) で逐次記録されるが、記録漏れ・誤帰属が起こりうる。session-end は**そのセッションの全体像を持つ唯一の地点**なので、ここで「このセッションで下した判断が decision arm に過不足なく載っているか」を締める。これで session log は現状維持のまま、session-end が target 一級化に準拠する。

> **なぜ decision で、session log ではないか**: session log は横断的 (= 1 セッションが複数 target に跨りうる) な**叙述**で、判断そのものではない。「1 target で下した判断」の一級の置き場は target の decision arm。session-end はその arm がセッションの実作業を正しく反映しているかを reconcile (= 突合して過不足を締める) する。

### Step 4.7a: 前提ゲート (cloud 専用)

decision stream は cloud プロジェクトのみ。**local mode (= `.beacon/cloud.json` 不在) では decision arm が無いのでこの Step を丸ごとスキップ**する。cloud 到達不能・未認証も同様に **黙ってスキップ** (= session-end 本体を止めない、best-effort)。

### Step 4.7b: このセッションの判断を取得

現セッションの `session_id` を解決する:
```bash
beacon session id
```

その sid で、このセッションが記録した判断だけを引く (ms-164 e-6030 の絞り込み):
```bash
beacon decision list --session <session_id> --json
```

- 返り JSON の各行は `decision` (何を) / `kind` / `rationale` (なぜ) / `evidence` (根拠) / `who.session_id` / `related.target_id` を持つ。
- **サーバ側フィルタが未反映な環境への保険**: 返った行の `who.session_id` が現 sid と一致するものだけを自分でも絞り込む (= サーバが古く絞れていなくても取り違えない)。
- worked-target を主語にしたいときは `--target <target_id>` も併用できる。target は Step 4 で session log に載った `target_ids` (無ければ `occupation.resolve_worked_targets` が解決した集合) を使う。

### Step 4.7c: セッションの実作業と突合して過不足を判定

「このセッションが実際に下した非自明な判断」を、セッションの実作業から洗い出して、Step 4.7b で取得した decision と突き合わせる。実作業の材料:

- Step 1 で集めた **このセッションのコミット** (commit message = 何を選び、なぜそうしたか)
- このセッション中の **タスク done / PR レビュー採否 / target 完遂判定** (これらは自動で decision を生むが、漏れや文脈不足がありうる)
- Step 3 サマリー本文に書いた **設計判断・方針転換**

各判断について 3 通りに分類する:

- **不足 (加筆)**: 非自明な判断 (例: 「A ではなく B を採った」「既存機構を再利用し専用実装を却下した」) がコミット等に現れているのに、対応する decision が無い → **加筆する** (下記 4.7d)。
- **誤り / 誤帰属 (訂正)**: 記録済み decision の `related.target_id` が誤っている、`rationale` が実装と食い違う等 → decision stream は **追記専用 (append-only) で上書きできない**ので、**訂正を上書きせず、正しい判断を superseding (= 上書きに代わる後追い訂正) として加筆**し、evidence に元の `decision_id` と「訂正: 〜」を明記する。
- **過剰 (指摘のみ)**: 重複・些末な自動記録が多すぎる → append-only なので削除はしない。Step 5 レポートに 1 行「decision N 件が重複気味」と**指摘するだけ**に留める (= 記録を壊さない)。

**判断の呼び水**: 「掃除機を買った ≠ 掃除した」と同じで、**コミットしただけの些末な変更は decision ではない**。decision に値するのは「別の選択肢がありえたのに、こちらを選んだ」判断のみ。無ければこの Step は「過不足なし」で終える (= 過剰記録もノイズ)。

### Step 4.7d: 不足分の加筆

不足と判定した判断を、1 件ずつ Bash ツールで記録する (= `/beacon-log` の decision backstop と同じ統一 arm に載せる):
```bash
beacon decision record \
  --what "<何を選んだか — 1 行>" \
  --rationale "<なぜそうしたか>" \
  --kind log-backstop \
  --evidence "<commit hash / file:line / url — 判断を裏付ける実 link (必須)>" \
  --related-task "<関係する e-XXX があれば>"
```

- `--evidence` は必須 (= 一級の判断は根拠を link する)。裏付けが本当に無い判断は加筆しない (= phantom を作らない)。
- target 帰属は `--related-task` (task 経由) か、サーバが認証・session context から補う `who` で決まる。誤帰属を訂正する superseding の場合は `--evidence` に元 `decision_id` を含める。

### Step 4.7e: レポートへの反映

Step 5 の終了レポートに 1 ブロック添える:
```
判断 (decision) 過不足チェック:
  取得: [N]件 (session <sid 短縮>)
  加筆: [M]件 — [各 what を 1 行]
  訂正: [K]件 — [superseding した理由を 1 行]
  指摘: [重複/過剰があれば 1 行]
```
過不足が無ければ「判断: 過不足なし ([N]件を確認)」の 1 行で済ませる。

## Step 5: 終了レポート

ユーザーに結果を提示:
```
Beacon セッション終了
---
Summary: [更新したサマリー]

Active: [ms-id] [title] ([progress]%)
  残タスク: [N]件
  - [id] [description]
  ...

判断 (decision) 過不足チェック: [Step 4.7e のブロック、または「過不足なし」]
---
```

## 制約

- データ取得は Bash ツール経由の beacon CLI のみ。project.json を直接読まない。
- サマリーの書き込みは `beacon session end --summary "<text>"` 経由のみ (旧 `beacon summary "<text>"` は ms-57 e-1040 で廃止、ignored を返す)。
- 未コミット変更がある場合、勝手にコミットしない。ユーザーに判断を委ねる。
- `beacon note clear` はメモのレビュー後にのみ実行する。スキップしない。
