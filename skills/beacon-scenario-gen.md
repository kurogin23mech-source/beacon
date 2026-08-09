---
name: beacon-scenario-gen
description: SPEC (目的+設計方針+受入条件) を AI が読み解いて、実操作シナリオ (ペルソナ操作列 + 環境刺激列 + 観測アサーション) を生成・保存する。オラクル(期待結果)は SPEC の一文に紐づけ、観測方法にも出典を付ける。ms-136 自動デバッグ基盤の生成器。
version: 0.1.0
triggers:
  - /beacon-scenario-gen
  - シナリオを生成
  - SPECからテストを作る
  - 自動デバッグシナリオ
  - scenario generate
---

# Beacon Scenario Generator (ms-136 e-4699)

> SPEC を読み解いて「実行可能シナリオ」を生成・保存する。生成の非決定性は
> **生成時だけ**に隔離し、保存物は決定的に再生可能な資産にする (leader 論点1芯 /
> e-4702 の回帰・attainment 再利用の土台)。生成器 (AI) は 1 回走って artifact を
> 産むだけ、artifact は以後ずっと決定的に replay される。

## 文章の書き方 (Beacon 全体の哲学)

読み手には非開発者が含まれる。読み手目線 1 行 / 横文字 3 段階 (固有名詞は素通し・
技術概念は初出に日本語注・一般概念は日本語化) / ID 参照に文脈 / 尻切れトンボ禁止。
特に **観測アサーションの spec_source / observation_basis は受信側 (人間/leader) が
diff で検分する** ので、根拠を省略しない。

## このシナリオが何か (3部構成 = 正直な構造)

シナリオ = **(ペルソナ操作列) + (環境刺激列) + (観測アサーション)** の宣言的資産。
ステップを型で峻別する — 誰が動かしたかを溶かさない (leader 案B refined):

| step.kind | 何を表すか | runner の扱い |
|---|---|---|
| `persona_cli` | ペルソナ自身の操作。`argv` は実 production CLI (`beacon` サブコマンド) | subprocess で黒箱実行=検証対象。想定外の非ゼロ終了は journey 失敗 |
| `inbound_stimulus` | 環境刺激 (顧客が返信した 等。ペルソナの agency の外) | runner が in-process で inward_inject seam に流す。**CLI verb には出さない** |
| `assert` | 観測アサーション | 直前 persona_cli の出力 (または構造不変条件) に照合 |

> `inbound_stimulus` を CLI 操作に溶かさない理由: 顧客の返信はペルソナの操作では
> なく環境刺激。CLI 操作列に混ぜると「誰かが打った操作」に偽装され情報が失われる。
> かつ production CLI verb に inject を出すと本番に偽の顧客返信を注入できる footgun
> になる (data-immutability 整合)。

## Step 1: 対象 SPEC を読む

引数の `ms-XX` または `doc-id` から SPEC を取得する。**コードは読まない**
(方針3 = オラクルはコード非参照)。

```bash
beacon doc list --scope spec --ms <ms-id> --json   # doc-id 特定
beacon doc show <doc-id>                            # 本文取得
```

SPEC の **目的 / 設計方針 / 受入条件** を読み解く。目的が「何を・なぜ・どこまでで
成功か (spirit)」、方針が制約、AC が具体的観測点を与える。

## Step 2: journey を組み立てる (目的から、AC 字面でなく)

ペルソナ × ゴールの journey を、そのプロジェクトの **実 CLI 操作** に翻訳する:

1. **seed**: `{profession, name, objective}`。SPEC の職種に合わせる (sales / dev …)。
2. **persona_cli 列**: ペルソナがゴールに向かって打つ実コマンド (`opportunity add`,
   `communication add … --direction outbound`, `opportunity judge … advance` 等)。
   一時 project の ID は決定的 (opp-1 …) なので参照してよい。
3. **inbound_stimulus**: 本物の返信が来ない箇所に擬似着信を注入 (`target` / `summary`
   / `channel` / `expect_ingested`)。
4. 送信 intent で自然に止める。外向き送信は撃たない (方針4/5)。

## Step 3: オラクル (assert) の規律 — ここが核心 (方針3 + leader 論点2)

各 assert に **両軸の出典**を必ず付ける。片方でも欠けると runner / store が拒否する。

### spec_source = 期待値の真実 (何が真か)
- 期待値は **人間が書いた SPEC の文言**から取る (コードや AI の推論から合成しない)。
- **主張の強度に一致する一文を引く**。弱い上位文で強い主張を裏書きしない
  (leader tightening 1)。例: assert が `ball == self` (方向まで特定する強い主張) なら、
  方向 `self` を実際に述べる文 (例「取り込み→ball が**自分に**戻る」) を主出典にする。
  AC が「ball 更新」までしか言っていないなら、それだけで `==self` を裏書きしない。
- **両引き**が理想: AC (=受入基準) + 方針 (=具体の方向/機構) を併記。

### observation_basis = 観測機構 (どう観測するか)
- **どの CLI コマンド/フィールドで観測するか + なぜその field が SPEC の
  ユーザー可視概念に対応するか** を 1 文で書く。
- 観測 field は **SPEC が語るユーザー可視概念の実現**であるものだけ使う。実装都合の
  内部 artifact は不可 (leader 論点2)。
- **同名の罠を明示回避**: 例、SPEC の「ボール(手番)」は `communication list --json` の
  `ball` (derive_ball の派生値=概念の実現) で観測する。`opportunity` の
  `who_has_the_ball` (静的初期値・返信後も不変) は同名だが概念を実現しないので
  **使わない** ——これを observation_basis に書き、buggy な同名 field への silent
  trust (緑でも嘘) を保存物 diff で捕まえられるようにする。

### assert の種別
- `exit_code` / `stdout_contains` / `stdout_not_contains` / `json_path` : 直前
  persona_cli の出力に照合。
- `structural_invariant` (`invariant: no_cloud_json` 等): 「外部送信が起きない」の
  ような**検証可能な安全性**を、CLI field でなく構造不変条件で観測する満たされた
  assert。observation_basis に `structural-invariant (不変条件で検証)` と明記する。

## Step 4: 翻訳不能 AC は quality_signals へ (カテゴリ必須, leader 論点3)

実行可能・観測可能な assert に翻訳できない AC/目的は握りつぶさず `quality_signals[]`
に残す。各項目に `reason_type` を必ず付ける:

- `needs-observable-rewrite`: そもそも観測可能に書けていない = **SPEC 品質欠陥**
  (書き直し signal)。
- `out-of-scope-boundary`: 観測が transmission (実送信が届くか) / UI レンダー経由で
  しか不可 = 方針4 で**正しく除外** (SPEC 欠陥ではない、非難でない)。

> **混同禁止**: 方針4 の out-of-scope 例は「メールが実際に届くか」であって「送信が
> 起きないこと」ではない。後者は我々が構造保証する検証可能な安全性なので、
> quality_signal でなく `structural_invariant` の satisfied assert にする (Step 3)。
> quality_signals を濁らせると SPEC 欠陥検出の価値が薄れる。

## Step 5: 保存 → 実行で確認

生成した scenario JSON を保存し (検証込)、実 CLI で replay green を確認する:

```bash
# 一時ファイルに JSON を書いてから
beacon scenario save /path/to/generated.json      # scenarios/<ms>/<slug>.json へ
beacon scenario run scenarios/<ms>/<slug>.json    # 実CLIで journey を回す (exit 0=green)
```

`save` は契約検証 (両軸 provenance + quality_signals カテゴリ + milestone/spec_ref
必須) を通す。`run` は e-4698 runner を呼ぶ (headless=CI でもそのまま)。落ちたら
report の `failure` (index / kind / reason / spec_source / observation_basis) を読み、
オラクルの誤りか実装の欠陥かを切り分ける (層 bisect は e-4700)。

## Step 6: 提示

保存パスと、生成した assert の spec_source / observation_basis の要点、
quality_signals の分類を human に提示する。特に **同名 field の罠を回避した判断** と
**弱い文で強い主張を裏書きしていないか** は自己点検して明示する。

## 制約

- **コード非参照**: オラクルは SPEC の人間文言から。実装ロジックは読まない (公開 CLI
  観測面を知るのは可、それは黒箱インターフェース)。
- assert は両軸 (spec_source + observation_basis) 必須。無ければ store/runner が拒否。
- `inbound_stimulus` は runner in-process 制御プレーン専用、production CLI verb に
  しない。
- 生成の非決定性は生成時のみ。保存物は決定的 replay 資産。
