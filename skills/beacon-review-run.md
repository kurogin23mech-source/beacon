---
name: beacon-review-run
description: AX / 思想レビューを、実装者セッションと別視座の独立サブエージェントで走らせる。review-kernel (原典 + 機械採取 diff) を fresh subagent に渡し、実装者の会話文脈を継承させないことで自己レビューを構造的に不能にする。複数モデルへ fan-out して多視点パネル化できる。汎用 (Beacon 非依存)。
version: 0.1.0
triggers:
  - /beacon-review-run
---

# Beacon Review Run — 独立レビューの実行層 (ms-119 e-3947)

> レビューの価値の源泉は **独立性** (AX 原典 §2 計器の必然): 実装を書いた本人は
> 文脈が最大化して、自分の命名不一致・隠れ穴・思想 drift が見えない。だから
> 「文脈ゼロの独立した審査役」だけが妥当な計器になる。
>
> このスキルは、その独立性を **お願いでなく構造で** 担保する。実装者セッション
> (= あなた) は **レビューを自分で書かない**。`beacon review context` が組み立てた
> bundle (= 原典 + 機械採取した差分だけ) を、**新しいサブエージェント**に渡して
> 判定させる。新しいサブエージェントは、あなたの会話文脈・コミットの意図説明を
> 一切継承しないので、身内びいき (self-review) が物理的にできない。

## いつ使うか

- PR / 変更差分に AX (= AI が誤りにくい interface か) レビューを当てたいとき。
- target (マイルストーン等) の完了主張時に、実装が SPEC / vision 通りか (思想) を
  照合したいとき。
- 実装者本人のセッションで「自分でレビューしようとしている」と気づいたとき
  (= それは計器が濁る。必ずこのスキル経由で別視座に投げ直す)。

**目的達成レビュー (target が目的を果たしたか) はこのスキルの対象外**。あれは
owned な判定なので `beacon target review-request` → 人間 approve で担う (別視座は
人間承認そのものが担保する)。

## 引数

`/beacon-review-run <type> <target> [options]`

- `type`: `ax` | `philosophy` | `both` (既定 `ax`)
- `target`: `--pr <n>` または `--diff-ref <base...head>` (どちらか必須)
- `--origin-doc <doc-id>`: 思想レビューの原典 (対象 target の SPEC doc)。`philosophy`
  / `both` で必須。AX では不要 (原典は principles.md 固定)。
- `--models <m1,m2,...>`: 判定サブエージェントのモデル。既定は実装者と別の 1 体
  (`fable`)。複数指定でパネル化 (fan-out、多視点)。指定可能: `fable` / `haiku` /
  `sonnet` / `opus`。
- `--mode <diff|full-surface>`: 既定 `diff`。full-surface は棚卸し監査 (網羅的)。

引数が省略されていれば対話で埋める。ただし **type と target は必ず確定してから**
kernel を呼ぶ。

## 手順

### Step 1: 前提チェック

Bash で:
```bash
beacon-find-root >/dev/null && echo OK || echo NO_BEACON
```
`NO_BEACON` なら「Beacon プロジェクトのルートで実行してください」と返して終了。

### Step 2: review-kernel bundle を組み立てる (原典 + 機械採取 diff だけ)

**あなたは diff を目視で要約しない**。kernel に機械採取させる。Bash で:

```bash
# AX:
beacon review context --type ax --pr <n>            # or --diff-ref <base...head>
# 思想:
beacon review context --type philosophy --origin-doc <spec-doc-id> --pr <n>
```

stdout に 1 行 JSON が返る (`origin` / `artifact` / `independence_contract` /
`mode` / `gaps`)。`both` の場合は 2 回叩いて 2 bundle を得る。

- `gaps` が非空 (例: 思想レビューで SPEC 本文が空 / diff が空) なら、その gap を
  ユーザーにそのまま伝えてから続行するか確認する (方針5 の gentle forcing
  function — 原典が弱い target は「条件が未記載」と晒される)。
- kernel が原典 / diff を取れずエラー終了したら、そのエラーをユーザーに提示して
  中止 (原典無しの思想レビューは無効)。

### Step 3: 判定サブエージェントを起動する (独立性の構造保証)

**ここが核心**。**Agent tool** で **新しいサブエージェント** を起動する。これは
あなたの会話文脈を継承しない新しい実行なので、独立性が構造的に成立する。

各判定エージェントへ渡すプロンプトは、次の 3 つ **だけ** で構成する:

1. **判定の作法 (原典 harness)**: レビュー種別に対応する instrument skill 本文を
   Bash で読んで貼る。
   - AX → `cat "$(beacon _install-root)/skills/ax-review/SKILL.md"`
   - 思想 → `cat "$(beacon _install-root)/skills/philosophy-review/SKILL.md"`
2. **kernel bundle**: Step 2 の JSON をそのまま (origin = 原典本文、artifact =
   差分、independence_contract、mode)。
3. **出力指示**: 「instrument skill が定める JSON スキーマだけを返せ。散文の前置き
   を付けるな」。

**渡してはいけないもの (計器を濁す)**: あなたのセッションの会話履歴、コミットの
意図説明、「本当はこうするつもり」、実装者の弁明。bundle の
`independence_contract` を判定エージェントのプロンプト冒頭にも明示的に置く。

**モデルの独立性 (e-3947 / e-3890)**:
- Agent tool の `model` を **実装者 (あなた = 通常 opus) と別**にする。既定は
  `fable`。
- `--models` が複数なら、その各モデルで **並列に** サブエージェントを起動し
  (1 メッセージ内で複数 Agent 呼び出し)、多視点パネルにする。同一モデルでも
  プロンプト末尾に視点ラベル (例: 「命名の一貫性を最優先で見よ」「silent no-op
  を最優先で見よ」) を変えて複数レンズに割ることもできる (AX の 6 原則を独立
  レンズに割る fan-out)。
- パネルの上限は 3 体を目安 (並列 subagent の実務上限、CORE doc
  `ZoFyYeaRGa0FeVrj3jZt` の 2 推奨 / 3 max に従う)。

`subagent_type` は `Explore` などの読み取り系ではなく、汎用 (`general-purpose`)
を使う (findings を構造化して返させるため)。

#### Step 3.1 (任意, e-3893): prior の増強

ユーザーが明示的に望む場合のみ、判定プロンプトに **optional な prior** を足せる
(既定は足さない — 計器はまず素の原典 + diff で判定するのが基本)。

- **L2 記憶層の failure / surprise**: 過去に同種の interface で踏んだ失敗があれば、
  「この観点は過去に踏んでいる」というヒントとして添える。
- **application-map** (`beacon doc show application-map`): 「近い既存機能があるか」
  の索引。二重実装・命名衝突の finding を補強する prior。

prior はあくまで増強で、原典を置き換えない。足したら bundle にその旨を注記する
(判定エージェントが「原典 vs prior」を取り違えないように)。

### Step 4: findings を集約して提示する

各判定エージェントが返した JSON findings を集める。

- **dedup**: パネル (複数モデル / レンズ) が同じ欠陥を別々に挙げたら 1 件に統合
  し、「N 体中 M 体が指摘」と合意度を添える (多視点の価値は合意度で見える)。
- **severity 順**に並べて提示する (AX: high / misleading / medium / low、思想:
  high / medium / low)。
- 各 finding は instrument skill のスキーマのまま見せる (AX: principle / defect /
  why_ai_errs / structural_fix、思想: spec_ref / promised / implemented / drift)。
- 判定エージェントが返した `skill_gaps` を必ず 1 か所に集約して提示する (=
  レビュー Capability 自身の改善入力)。

### Step 5: 助言なので act は作業判断に委ねる (非 blocking)

AX / 思想は **advisory** (= 非 blocking の助言、SPEC 方針4)。findings は「ここに
drift、直すか?」の提示まで。gate 化して作業を止めない。ユーザーに次を委ねる:

- **今直す** → 該当 finding の `structural_fix` を適用する作業に入る。
- **記録して後で** → Step 6 のポータブル出力へ。
- **却下** → 却下理由を 1 行添えて終わり (findings は消さず提示済みログに残す)。

### Step 6 (任意, e-3891): ポータブル出力

findings を対象リポ / チームに運べる形に配線する。ユーザーが望む出力先を選ぶ:

- **PR コメント**: `gh pr comment <n> -b "<findings 要約>"` で PR に残す (PR レビュー
  の一部として)。
- **レポート doc**: `beacon doc add --scope report --title "AX review: <target>"
  --stdin` で監査可能な記録として保存。
- **Beacon task 化 (optional 増強)**: high / misleading の finding を
  `beacon task add "<structural_fix>" -m <ms-id>` で follow-up task にする。既定
  では task 化しない (レビューは task queue を勝手に膨らませない — /beacon-log と
  /beacon-task の責務分界)。ユーザーが望んだ finding だけ task 化する。

## PR レビュー経路からの起動 (e-3892)

PR レビュー (`/review` / `/code-review`) の中で「この変更の AX はどうか」を見たく
なったら、その場で `/beacon-review-run ax --pr <n>` を呼ぶ。正しさ (bug) レビュー
とは独立レンズなので、両方を別々に回してよい (このスキルは AX / 思想だけ、正しさ
は既存の PR レビューが担う)。実装者本人の PR でも、このスキルは別視座の
サブエージェントに投げるので self-review にならない。

## 制約

- **実装者セッションでレビューを inline で書かない**。必ず Step 3 の fresh
  サブエージェント経由。これがこのスキルの存在理由。
- **判定エージェントに実装者の会話文脈を渡さない**。渡すのは bundle (原典 + 差分)
  と instrument skill だけ。
- **判定モデルは実装者と別**を既定にする (`fable`)。同一モデルにするのはユーザー
  が明示指定したときだけ。
- **目的達成レビューはこのスキルの対象外** (`beacon target` + 人間 approve)。
- 原典が取れない思想レビューは実行しない (kernel が gap を返す — それ自体を
  findings として扱い、SPEC を書く forcing function にする)。

## 関連

- `beacon review context` — kernel (原典 + 機械採取 diff の bundle 組み立て)。
- `skills/ax-review/` — AX 判定 instrument (原典 principles.md + harness)。
- `skills/philosophy-review/` — 思想判定 instrument。
- `beacon target review-request` — 目的達成レビュー (owned verdict、人間承認)。
- CORE doc `ZoFyYeaRGa0FeVrj3jZt` — 並列 subagent の上限 (2 推奨 / 3 max)。
