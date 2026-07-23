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

- target (マイルストーン等) の完了主張時に、**目的達成の証拠**を独立に生成したい
  とき (`attainment` mode, ms-119 e-4005)。verdict (= 達成か) は人間の所有のままだ
  が、その判断材料 (各受入条件 met/partial/not-met + 根拠) を実装者の自己申告でなく
  文脈ゼロの独立 judge に実コードから作らせる。

> **目的達成 (attainment) の verdict は人間の所有** (SPEC 方針2)。このスキルは
> verdict を**確定しない** — 独立 judge が SPEC × 実コードで**証拠**を作り、人間が
> それを見て `beacon target approve` を押す (approve は e-4006 で AI セッション拒否)。
> AX / 思想が「助言 findings そのものが成果」なのに対し、attainment は「証拠を作り、
> 確定は人間」という非対称を持つ。この分離が、実装者が自分の達成を自己申告する穴
> (2026-07-23 に ms-119 で実際に踏んだ) を構造で塞ぐ。

## 引数

`/beacon-review-run <type> <target> [options]`

- `type`: `ax` | `philosophy` | `both` | `attainment` | `maintainability` (既定 `ax`)。
  `maintainability` (= AX の兄弟、AI がコードを**変更する**体験) は AX と同じく
  `--pr` / `--diff-ref` を取り、原典は固定 (principles.md)。PR 作成時に AX と並んで
  自動発火する (fires_on=pr-open)。
- `target`: `--pr <n>` または `--diff-ref <base...head>` (ax/philosophy でどちらか
  必須)。`attainment` では `--target <ms-XX|op-X>` が必須 (原典 = その target の
  SPEC を自動解決)、`--pr` / `--diff-ref` は任意の補助差分。
- `--origin-doc <doc-id>`: 思想レビューの原典 (対象 target の SPEC doc)。`philosophy`
  / `both` で必須。AX では不要 (原典は principles.md 固定)。
- `--models <m1,m2,...>`: 判定サブエージェントのモデル。**既定は type ごとに記述子の
  `default_judge_model`** (skills/<type>/review-type.json)。現状: `philosophy` = `fable`、
  `ax` / `maintainability` = `sonnet`。設計意図: **独立性 (= 文脈ゼロの別プロセス) が本体**
  で、そこは全 type 共通。頻繁に発火し機械層 (full-surface / lint / drift ガード) が
  下支えする ax / maintainability は capable だが軽めの `sonnet`、判断勝負で稀にしか
  発火しない philosophy は `fable`、とコストを type で階層化する (安さのために `haiku`
  まで落とさない — 独立でも判定が甘いと計器にならない)。`--models` で明示上書き可、
  複数指定でパネル化 (fan-out、多視点)。実装者と同一モデルは避ける (e-3988、kernel が
  警告を bundle に載せる)。
- `--mode diff | full-surface`: 既定 `diff` (変更差分をレビュー)。`full-surface`
  (e-3987) は AX 用の棚卸し監査で、各コマンド群の help / エラー / exit code を機械採取
  した surface-snapshot を artifact にする (差分に現れない既存コードの AX 欠陥も拾う)。

**モデル独立性を kernel に照合させる (e-3988)**: kernel を呼ぶとき
`--judge-model <使う判定モデル>` と `BEACON_IMPLEMENTER_MODEL=<あなたのモデル>` を渡すと、
両者が一致した場合に bundle の `gaps` に警告が **機械的に** 載る (散文のお願いでなく、
判定エージェントの入力そのものに警告が届く)。一致警告が出たら別モデルに切り替える。

引数が省略されていれば対話で埋める。ただし **type と target は必ず確定してから**
kernel を呼ぶ。

## 手順

### Step 1: 前提チェック (command 不在 と root 不在を分けて診断する — e-3989)

`beacon-find-root` の非 0 終了を一律「Beacon ルートで実行を」と診断すると、実際には
`beacon` が PATH に無いだけ (install / PATH の問題) の人を誤誘導する。**存在確認を
先に**行い、2 つの失敗を別々に案内する。Bash で:

```bash
if ! command -v beacon-find-root >/dev/null 2>&1; then
  echo NO_COMMAND
elif ! beacon-find-root >/dev/null 2>&1; then
  echo NO_ROOT
else
  echo OK
fi
```

- `NO_COMMAND` → 「`beacon` が PATH にありません。install / PATH を確認してください
  (例: `pipx install beacon` または repo の `bin/` を PATH に追加)」と返して終了。
  **「ルートで実行を」とは言わない** (原因が違う)。
- `NO_ROOT` → 「Beacon プロジェクトのルートで実行してください」と返して終了。
- `OK` → 続行。

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

**モデルの独立性 (e-3947 / e-3890 / e-3988)**:
- Agent tool の `model` を **実装者 (あなた = 通常 opus) と別**にする。既定は
  `fable`。
- **構造チェック (e-3988)**: kernel を呼ぶとき `BEACON_IMPLEMENTER_MODEL=<あなたの
  モデル>` を渡すと、bundle に `implementer_model` が載る。判定モデルを決めたら
  `implementer_model` と一致していないか必ず確認する (一致 = 共有の盲点が残り独立性
  が弱い)。一致していたら別モデルに切り替えるか、ユーザーが明示的に「同一モデルで
  よい」と言った場合だけ続行する。散文のお願いでなく、bundle が運ぶ事実で照合する。
- `--models` が複数なら、その各モデルで **並列に** サブエージェントを起動し
  (1 メッセージ内で複数 Agent 呼び出し)、多視点パネルにする。同一モデルでも
  プロンプト末尾に視点ラベル (例: 「命名の一貫性を最優先で見よ」「silent no-op
  を最優先で見よ」) を変えて複数レンズに割ることもできる (AX の 6 原則を独立
  レンズに割る fan-out)。
- パネルの上限は 3 体を目安 (並列 subagent の実務上限、CORE doc
  `ZoFyYeaRGa0FeVrj3jZt` の 2 推奨 / 3 max に従う)。

`subagent_type` は `Explore` などの読み取り系ではなく、汎用 (`general-purpose`)
を使う (findings を構造化して返させるため)。

#### Step 3.1: prior は判定プロンプトに注入しない (独立性の構造保証)

**判定エージェントへ渡すのは kernel bundle (原典 + 機械採取 diff) だけ**。それ以外の
prior — とりわけ **L2 記憶層 (failure / surprise)** — を判定プロンプトに足しては
ならない。L2 記憶は **実装者自身の過去セッションから蒸留された文脈**であり、それを
judge に入れると bundle の外から実装者由来の文脈が再流入し、self-review の穴が別ドア
から開く (= このスキルが塞ぐはずの独立性を、prior が壊す)。

> **経緯 (ms-119 思想レビュー finding, 2026-07-23)**: 当初この Step は「optional な
> prior を既定 off + 注記で足せる」としていた (旧 e-3893)。だが独立2体の思想レビュー
> が、これは「文脈ゼロでない judge は無効」という原典 §2 と、SPEC 方針7 が『弱い』と
> 断じた *お願いベースの抑止* に依っている点で矛盾する、と指摘した。構造で閉じる原則
> に従い、prior 注入経路を撤去した。
>
> application-map のような **実装者 session に依らない外部の事実索引** を judge に
> 見せたい場合は、ad-hoc なプロンプト注入でなく **kernel bundle の
> `external_references` slot** に named reference として機械的に載せる (e-3997 で実装)。
> bundle が判定の完全な入力である不変条件を保ったまま拡張できる唯一の経路。載せて
> よいのは repo / registry 由来の事実だけ (= 実装者 session の会話・L2 記憶は不可)。

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

## 目的達成 (attainment) モード — 証拠の独立生成 (ms-119 e-4005)

`type == attainment` のときは、上の Step 2〜5 を次の変形で行う。**verdict は人間所有**
のまま、**証拠だけ**を独立 judge に作らせるのが違い。

### A-1. attainment bundle を組み立てる

```bash
beacon review context --type attainment --target <ms-XX|op-X>   # 補助差分が要れば --pr <n> / --diff-ref を足す
```

stdout の JSON は `origin` (= target の SPEC 原典、自動解決)・`criteria` (= target の
objective / acceptance)・`artifact` (= 任意の差分)・`judge_contract`・`verdict_ownership`
(= `"human"`)・`gaps` を持つ。`gaps` が非空 (SPEC 未添付 / 空) ならそのまま人間に晒す
(方針5)。

### A-2. 独立 judge に「証拠」を作らせる

**Agent tool** で **新しいサブエージェント**を、実装者 (あなた) と**別モデル** (既定
`fable`) で起動する。渡すのは次の 3 つ **だけ**:

1. bundle の `judge_contract` (= 「自己申告でなく実コードから各受入条件を検証し、
   met/partial/not-met + 根拠を出せ。verdict の確定権は人間」)。
2. bundle 全体 (origin = SPEC 本文、criteria、artifact、independence_contract)。
3. 出力指示: 次の JSON スキーマだけを返す —
   ```json
   {"criteria": [{"criterion": "...", "verdict": "met|partial|not-met", "evidence": "確認した実コード/テスト/挙動"}],
    "overall_verdict": "attained|not-attained", "verdict_reason": "..."}
   ```

judge には **repo の読み取りを許す** (実コード検証が仕事)。だが**実装者セッションの
会話文脈・コミットの意図説明は渡さない** (文脈ゼロの計器)。**あなた (実装者) は証拠を
手書きしない** — これがこのモードの存在理由。

### A-3. 証拠を人間に提示し、確定は人間に委ねる

judge が返した証拠 (各 criterion の met/partial/not-met + 根拠 + overall verdict) を
そのまま人間に提示する。**あなたは verdict を確定しない**:

- 目的達成レビュー依頼がまだ無ければ `beacon target review-request <target> --new-state
  <state> --intent "<要約>" --evidence <refs>` で作る (依頼は AI が作ってよい)。
- **確定 (approve) は人間**: `beacon target approve <entry-id>` は e-4006 で AI セッション
  を拒否する。人間が証拠を見て `BEACON_SESSION_KIND=human`（または明示 override）で
  approve するか、却下する。あなたはここで止まり、人間の判断を待つ。
- **監査痕跡 (e-4006 audit)**: 承認は「どの signal でゲートを通ったか」を記録する。独立
  judge が証拠を作った場合は approve 時に `BEACON_EVIDENCE_SOURCE=independent-judge:<model>`
  を渡すと、その出所が承認記録に残る。AI セッションが override で承認すると記録に
  `⚠ AI セッションが override で承認` と刻まれ、自己承認は隠せず必ず後から見つかる
  (この MS の核心 = 「やれなくする」ではなく「隠せなくする」)。

### A-4. 自己検証 (dogfood)

このモードを **ms-119 自身**に当てると、「発火スパインの PR-open leg」「approve の
人間 guard」等が実コードで満たされたかを独立 judge が判定する。今日 (2026-07-23) 実装者
が自己申告で done にした穴が塞がっていれば `attained`、まだなら `not-attained` が返る
— これが ms-119 の受入条件そのもの。

## PR レビュー経路からの起動 (e-3892)

PR レビュー (`/review` / `/code-review`) の中で「この変更の AX はどうか」を見たく
なったら、その場で `/beacon-review-run ax --pr <n>` を呼ぶ。正しさ (bug) レビュー
とは独立レンズなので、両方を別々に回してよい (このスキルは AX / 思想だけ、正しさ
は既存の PR レビューが担う)。実装者本人の PR でも、このスキルは別視座の
サブエージェントに投げるので self-review にならない。

### Step P: レビュー実施を記録して gate を解消 (`--pr` 指定時のみ、ms-119 e-4060)

`--pr <n>` 付きで走らせた場合、judge の verdict をユーザーに提示した**あと**に、
そのレビューが実施済みであることを記録する:

```bash
beacon review done --type <ax|maintainability> --pr <n>
```

これは PR-open で発火した `<type>-review-due` トリガーを消し、`beacon pr
approve` / `beacon pr merge` の**レビュー未実施ブロックを解消**する。ms-119 e-4060:
レビューは「発火するだけ」では意味がなく、approve/merge が実施を構造的に待つ
(未実施なら refuse)。だから **走らせたら必ず done を記録する** ことでループが
閉じる。findings が request-changes 相当でも「レビューは実施した」ので done は
記録してよい (verdict の内容と、レビューを走らせた事実は別)。複数種別 (AX +
maintainability) が発火している PR は、各種別を走らせるたびに done を記録する。

## 制約

- **実装者セッションでレビューを inline で書かない**。必ず Step 3 の fresh
  サブエージェント経由。これがこのスキルの存在理由。
- **判定エージェントに実装者の会話文脈を渡さない**。渡すのは bundle (原典 + 差分)
  と instrument skill だけ。
- **判定モデルは実装者と別**を既定にする (`fable`)。同一モデルにするのはユーザー
  が明示指定したときだけ。
- **目的達成 (attainment) は証拠だけを独立生成し、verdict は確定しない**。approve は
  人間 (e-4006 で AI セッション拒否)。実装者が証拠を手書きするのは禁止 — judge に
  実コードから作らせる。
- 原典が取れない思想レビューは実行しない (kernel が gap を返す — それ自体を
  findings として扱い、SPEC を書く forcing function にする)。

## 関連

- `beacon review context` — kernel (原典 + 機械採取 diff の bundle 組み立て)。
- `skills/ax-review/` — AX 判定 instrument (原典 principles.md + harness)。
- `skills/philosophy-review/` — 思想判定 instrument。
- `beacon target review-request` — 目的達成レビュー (owned verdict、人間承認)。
- CORE doc `ZoFyYeaRGa0FeVrj3jZt` — 並列 subagent の上限 (2 推奨 / 3 max)。
