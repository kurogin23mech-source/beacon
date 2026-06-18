---
name: beacon-roadmap
description: project-vision COREドキュメントを読み、大目的に到達するまでのマイルストーン群（3〜7個）を一括設計・登録する。順序・依存関係も提案する。
version: 0.1.0
triggers:
  - /beacon-roadmap
  - ロードマップを描きたい
  - マイルストーン全体構想
  - 全部のMSを考えたい
---

# Beacon Roadmap

> プロジェクトビジョンから、最初の MS 1〜2 個 (minimal) または大目的達成までの MS 群 (full) を設計する。

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

## 文章の書き方 (必読)

MS の title / objective / acceptance_criteria を提案する時は、CORE doc **`entry-writing-principle`** (doc_id `F3ZkqT0pKS6JpR8dn70n`) の 3 層構造 + 横文字 3 段階 + ID 参照ルールに従う。Beacon のターゲットには非開発者が含まれるため、開発者の癖は読み手を排除する。要点:

1. **title は読み手目線で 1 行**: 「何が嬉しいか」をユーザー体験の言葉で。横文字最小、価値で書く (例: ✗「Firestore subcollection migration」→ ✓「データの保存先を整理して、書き込みの上限に当たらないようにする」)
2. **objective は背景 2-4 文**: 現状の症状 → 放置できない理由 → どう改善されると嬉しいか。開発者でなくても『何が嬉しいか』が分かる粒度
3. **acceptance_criteria は bullet で技術詳細**: 横文字には初出時に日本語注、別 task ID 参照には『何の話か』1 行を添える

## モード判定 (重要、CORE doc `4AS5ehyJc8mGU1gsiFvz` 準拠)

CORE doc 「Beacon Onboarding の本質: 最速アウトプット、構造は後から自然発生」に従い、起動時に **mode を判定**:

| 条件 | mode | 振る舞い |
|---|---|---|
| `/beacon-init` 直後の自動チェイン | **minimal** | 最初の MS 1〜2 個だけ提案、即実装着手の流れに |
| `/beacon-roadmap minimal` 引数 or 「最小から」発言 | **minimal** | 同上 |
| `/beacon-roadmap full` 引数 or 「全体ロードマップ」発言 | **full** | 5〜7 MS をフル roadmap、依存関係まで |
| 引数なし、既存 MS が 1 つ以上ある | **full** | 続きの roadmap として追加分を提案 |
| 引数なし、MS ゼロ、明示指定なし | **minimal** (デフォルト) | 最速アウトプット原則 |

minimal モードでは Step 2 / 2.5 を簡略化、Step 5 で 1〜2 MS だけ登録。  
full モードでは従来通り 5〜7 MS 一括設計。

## 前提条件チェック

Bash ツールで:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
beacon doc show project-vision 2>/dev/null
```

`project-vision` ドキュメントが存在しない場合の挙動 (mode 別):

- **minimal モード** (デフォルト): vision 不在でも進める。`beacon status --json` の `objective` (init で設定済み) を「達成したいこと」として扱う。  
  vision を強制しない (Philosophy: 構造化は default off)
- **full モード**: vision がないと全体設計しづらいので、ユーザーに案内:
  ```
  full ロードマップは project-vision を読んで設計するのが本来の形です。
  vision を先に書きますか？ それとも objective ベースで進めますか？
  ```

## Step 1: コンテキスト読み込み

Bash ツールで **並列に** 実行:
```bash
beacon doc show project-vision 2>/dev/null    # vision あれば読む、無くてもエラーにしない
beacon status --json                          # objective / 既存MS
```

vision があれば 6 セクション内容を踏まえる。  
無ければ `beacon status` の `objective` を「達成したいこと」として扱う (minimal モードは vision 不要)。

既に `in_progress` / `todo` / `observing` 状態の MS があれば、それを踏まえて (重複・衝突なし) 設計する。

## Step 2: マイルストーン設計 (mode 別)

### minimal モード (デフォルト、最速アウトプット)

**最初の MS 1〜2 個だけ** 設計する。設計原則:
- 「触って動くもの」を最短で作れる粒度 (1〜3 コミットで完了)
- objective + 軽い AC (3〜5 項目) のみ、SPEC は書かない
- 後続 MS は触ってから決める方針なので「ここまで描く」

設計例:
- objective が「家計簿アプリ」→ MS 1: 「手入力で支出を 1 件記録できる」 (それだけ)
- objective が「ブログ書きたい」→ MS 1: 「最低限の記事を 1 本 publish できる」

### full モード (明示指定 or 既存 MS あり)

ビジョンに従い、**3〜7個のマイルストーン** を順序つきで設計する。

### 設計原則

1. **「能力層」で分ける**: 機能ではなく、ユーザーが手にする能力レベルで階層化する
   - 例: 「最低限の体験ができる」→「データを蓄積できる」→「他人と共有できる」
2. **「何ができるようになるか」形式のtitle**: 「○○機能の実装」ではなく「○○できるようになる」
3. **前段が後段を可能にする順序**: MS1が終わったらMS2の前提が揃う、という連鎖
4. **最初のMSは小さく、すぐ実装可能なサイズに**: 開発者が即座に着手できるよう、最初のMSは1〜3コミットで完了できる粒度
5. **成功基準の網羅**: ビジョンの成功基準を、複数MSに分けて達成できるよう設計
6. **やらないことを尊重**: ビジョンの「やらないこと」をスコープに含めない

### 各MSの構成要素

各マイルストーンに次を持たせる:

- **title**: 「○○できる」形式（必須）
- **objective**: ユーザー目線で「このMSが完了したら何が実現するか」（1〜2文）
- **acceptance_criteria**: どうなったら達成と言えるか（箇条書き）
- **priority**: highest / high / middle / low / lowest（chest-up: 大目的への寄与で判定）
- **依存関係**: どのMSに依存するか（基本は直前のMS）

## Step 2.5: Operation 輪郭の同時提案 (full モードのみ)

**minimal モードでは Operation 提案を skip** (最初の 1 MS の段階では運用は早すぎる)。

full モードのみ、ビジョンの「成功基準」「やらないこと」を踏まえ、**プロジェクトが完成したあと運用継続が必要なOperationの輪郭** を 0〜5個提案する。

### 対象となるOperationの判断基準

- 「動き続けることで価値が出る」もの（監視・定期収集・継続コミュニケーション等）
- プロジェクトの規模・性質によりOperationが少ない/不要なケースもある（小さなツール、一回完結の制作物等）

すべてのプロジェクトにOperationを強制しないこと。本当に必要なものだけ提案する。

### 各Operationの構成要素

各Operationに次を持たせる:
- **title**: 「○○を継続的に○○する」形式
- **objective**: なぜこのOperationが必要か（ビジョンの何を支えるか）
- **schedule**: daily / weekdays / weekly のいずれか（粗い段階の見立て）
- **activation_hint**: いつ動かし始めるべきか（自由テキスト、AIへのヒント）
- **対応 Milestone**: どのMSが完成した後に活性化すべきか
- **初期 OperationTasks**: 活性化に必要な準備項目 2〜3個（粗い段階で）

## Step 3: 提案の提示 (mode 別)

**ms-68 / e-1643 補足 (= entry-writing principle の draft 表示)**: 本 Step は既に draft 提示型 (= `beacon milestone add` を叩く前に全 MS の title / objective / AC をユーザーに見せて承認を取る形) で設計されており、ms-68 SPEC の「書き込み直前の draft 表示」要件を満たす。提示前に各 MS の objective / AC について self-review 4 原則 (読み手目線 1 行 / 横文字 3 段階 / ID 参照に文脈 / 尻切れトンボ禁止) を 1 度通し、特に MS title は **完成後に何ができるようになるか** をユーザー体験の言葉で書く (技術的な工程名は避ける)。

### minimal モードの提示 (デフォルト、最速アウトプット)

最初の MS 1〜2 個だけを軽量に提示:

```
ざっくり、最初の一手として「[ms-1 title]」を提案します。
  - objective: [1〜2 行]
  - 完了の目安: [AC を 2〜3 個、観察可能な形で]

これでまず動くものを作ってから、次の MS は触ってみて見えてきたら追加しましょう。
(全体像をいま描きたければ「フル roadmap で」と言ってください)

このまま登録して着手しますか？
```

ユーザー OK で Step 5 (1〜2 MS 登録 + 最初の MS active 化)。  
「フルで」と言われたら full モードに切り替えて再生成。

### full モードの提示 (明示指定 or 既存 MS あり)

ユーザーに **ロードマップ全体** を一覧で見せる:

```
プロジェクトビジョンを踏まえて、こんなロードマップを考えました。

【全体像】
  大目的: [ビジョンから引用]
  ↓
  Phase 1: 最低限の動作確認ができる
  ↓
  Phase 2: [次の能力層]
  ↓
  ...

---

## ms-A: [title]
- **objective**: ...
- **acceptance_criteria**:
  - ...
  - ...
- **priority**: middle
- **依存**: なし（最初のMS）

## ms-B: [title]
- **objective**: ...
- **acceptance_criteria**: ...
- **priority**: high
- **依存**: ms-A

...

---

【Operations】（運用継続が必要なもの、Step 2.5 で抽出された場合のみ）

## op-A: [title]
- **objective**: なぜ運用が必要か
- **schedule**: weekly
- **activation_hint**: いつ動かし始めるか
- **対応Milestone**: ms-B（このMSが完了したあと活性化候補）
- **初期OperationTasks**:
  - [準備項目1]
  - [準備項目2]

...

---

このロードマップで進めて大丈夫ですか？気になるところがあれば指示してください
(例: 「ms-3 はもっと分けたい」「順番違う」「これは要らない」など、自由に)。
無ければそのまま登録 → 最初のマイルストーン (ms-1) をアクティブにします。
```

(CORE doc `AeN9aPpjvh6URTQlFmb6` 準拠: 5 択メニュー禁止。open-ended な「気になれば指示してください、無ければそのまま進めます」が原則)

## Step 4: 修正フェーズ（必要時のみ）

ユーザーから具体的な指摘があれば、該当箇所を修正して再提示する:
- 「ms-X はもっと細かく / 粗く」: 該当 MS を分割 / 統合
- 「順番違う」: 順序入れ替え、依存関係を再計算
- 「ms-X を消して」: 削除
- 「これは違う、全体やり直し」: 別の切り口で再設計

修正後、Step 3 の形式で再提示。ユーザーが「OK」「そのまま」等で承認したら Step 5 へ。  
修正指示が無ければ Step 4 をスキップして直接 Step 5。

## Step 5: 登録 (Act first — CORE doc `AeN9aPpjvh6URTQlFmb6`)

ユーザー承認 (or 無修正での進行) で登録。**確認なしで実行**、結果を Step 6 で報告。

### minimal モード

- 提案した 1〜2 MS だけ登録 → 最初の MS を即 active 化
- **Operation 登録は skip** (Step 2.5 で提案していない)
- 依存関係: 2 MS の場合のみ `ms-2 depends_on ms-1`

### full モード

全 MS 登録 + 依存関係 + 最初の MS active 化 + Operation 登録。

### 各MSの追加 (両モード共通)

Bash ツールで順次実行（順序が重要）:

```bash
beacon milestone add "<title>" \
  --priority <priority> \
  --objective "<objective>" \
  --ac "<acceptance_criteria>"
```

戻り値で `ms-N` のIDが得られる。これを記録しておく。

### 依存関係の設定

各MS（最初のMS以外）に対して:

```bash
beacon milestone depends <ms-id> --on <previous-ms-id>
```

### 最初のMSをアクティブ化

```bash
beacon milestone start <最初のms-id>
```

### Operationの登録（full モードかつ Step 2.5 で提案した場合のみ）

各 Operation を **todo 状態** で作成:

```bash
beacon operation create "<title>" \
  --schedule <schedule> \
  --hint "<activation_hint>" \
  --objective "<objective>"
```

戻り値で `op-N` のIDが得られる。

各Operationに初期OperationTasksを追加:

```bash
beacon operation task add "<description>" -o <op-id> --priority <priority>
```

Operationsは全て todo 状態のまま登録される（活性化は対応Milestoneが完了した後、session-start での議論経由）。

## Step 6: 完了報告 (mode 別)

### minimal モード (Philosophy: 即着手、SPEC 提案しない)

```
ms-1 「[title]」を登録 + アクティブ化しました。

最初の一手として例えば:
  - [具体的にユーザーが触れる小さなアウトプット 1 つ、AI が推測で書く]

このまま実装に着手しますか？ (タスク細分化や SPEC は後から必要を感じたら `/beacon-task` `/beacon-spec` で)
```

ポイント:
- **SPEC 提案を default 出さない** (CORE doc `4AS5ehyJc8mGU1gsiFvz`: 構造化は default off)
- 1 つの具体的アクション (どこから手を付けるか) を AI が推測で示す
- ユーザーが「やる」と言えば実装着手、「タスク先に切りたい」「SPEC 書きたい」と言えば対応 Skill

### full モード

```
ロードマップを登録しました。

  ◐ ms-A: [title]   ← アクティブ（実装中）
  ○ ms-B: [title]
  ○ ms-C: [title]
  ...

最初のマイルストーン「[ms-A.title]」を開始しています。
このMSの最初のタスクから始めますか？ (構造化したければ `/beacon-spec [ms-A]` も使えます)
```

## 制約

- 既存MS（in_progress / todo / observing）と重複・衝突するMSは提案しない
- 各MSは「機能の実装」ではなく「ユーザーが何を手にするか」で表現する
- mode 別の上限: **minimal は 1〜2 MS**、**full は 3〜7 MS** (full で多すぎると消化できず、少なすぎると粒度が粗い)
- ビジョンがあれば「やらないこと」をスコープ外に保つ
- bulk add時にエラーが出たら、その時点で停止してユーザーに状況を報告する
- **Philosophy 適合**: minimal は CORE doc `PU9HG2IVQdW3tLiAJvix` (バイブコーダーのためのツール) と `4AS5ehyJc8mGU1gsiFvz` (最速アウトプット) に従う。`/beacon-spec` への自動チェーンはしない。
