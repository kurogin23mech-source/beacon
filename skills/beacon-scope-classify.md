---
name: beacon-scope-classify
description: 新しい capability (CLI verb / Skill) を足したとき、その共有スコープ層 (L0〜L4) と所有 (職種 / プロジェクト) を「AI が提案 → 人間が確定」で台帳に登録する。点検ツールの --propose 出力を土台に、AI が実コードを読んで層を絞り、人間の承認を経て初めて lib/capability_ledger.py へ書き込む。既存4レビュー (ax/maintainability/philosophy/attainment) とは別立て。
version: 1.0.0
triggers:
  - /beacon-scope-classify
  - スコープ分類
  - 層分類
  - capability を分類
  - この機能はどの層
---

# Beacon Scope Classify

> 新しい capability (= 機能。CLI verb か Skill) を足したとき、それが **どの共有スコープ層 (L0〜L4)** に属し **誰の所有 (職種 / プロジェクト)** かを、**AI が提案し人間が確定する** forcing function (= 抜けを構造で防ぐ仕組み)。
>
> 検知は点検ツール `scripts/check-capability-scope.py` (未分類なら CI が赤) が担い、本 Skill は **提案の精緻化 + 人間確定 + 台帳への書き込み** を担う。AI が台帳を独断で書き換えることは絶対にしない (= 層の割り当ては最終的に人間が握るレビュー事項)。
>
> **既存4レビュー (ax=AI利用体験 / maintainability=AI変更体験 / philosophy=思想忠実度 / attainment=目的達成) とは目的が違う別立て**。あれらは「文脈ゼロの独立 judge が drift を確認する」レビューだが、本 Skill は「新しい機能の分類を提案して台帳に登録する」記帳。混同しない。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

1. **読み手目線 1 行から始める**: 「何が嬉しいか」を価値の言葉で
2. **横文字 3 段階**: 固有名詞 (`Firestore` / `MCP`) はそのまま / 技術概念 (`allowlist` = 許可リスト) は初出時に日本語注 / 一般概念 (configure / audit) は日本語化
3. **ID 参照に文脈**: `e-XXXX` / `ms-XX` の初出に『何の話か』1 行
4. **尻切れトンボ禁止**: 主語・述語・論理関係を省略しない

詳細は CORE doc `entry-writing-principle` (doc_id `F3ZkqT0pKS6JpR8dn70n`)。

## いつ呼ばれるか

- 新しい CLI verb / Skill を足した後、点検ツールが「UNCLASSIFIED」を報告したとき (= CI が赤 / `beacon doctor` / pre-commit で気付く)
- ユーザーが `/beacon-scope-classify` と明示的に呼んだとき
- 実装 PR の作成前後に「今回足した機能の層を決めたい」とき

CI の hard gate (未分類 → exit 1 → マージ不可) は本 Skill の外で既に効いている。本 Skill はその gate を **「AI が独断編集」ではなく「AI 提案 → 人間確定」** で解消する正規の動線。

## 前提条件チェック

Bash ツールで実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
`NO_BEACON` なら何もせず終了。この Skill は Beacon 本体リポジトリ (= `lib/capability_ledger.py` を持つ source tree) でのみ意味を持つ。台帳ファイルが無ければ「このプロジェクトには capability 台帳が無いため分類対象がありません」と伝えて終了する。

## Step 1: 分類ギャップの取得 (読み取り専用)

Bash ツールで、点検ツールを **提案モード** で走らせる (読み取り専用・常に exit 0):
```bash
python3 scripts/check-capability-scope.py --propose --json
```

stdout に 1 個の JSON:
```json
{
  "ok": true/false,
  "gap_count": N,
  "proposals": [
    {
      "capability": "payroll_run",        // verb キー or skill 名
      "kind": "verb" | "skill",
      "gap": "scope" | "owner",           // どちらの軸が欠けているか
      "noun": "payroll",                  // verb のみ
      "known_scope": "" | "L3",           // owner gap のとき既知の層
      "proposed_scope": "" | "L3",        // ツールの best-effort guess (空=signal無し)
      "proposed_owner": "" | "sales",
      "confidence": "high" | "low",
      "rationale": "...",
      "edits": [ {"file","dict","key","value_hint","note"}, ... ]
    }
  ],
  "scope_menu": { "L0": "...", ... },       // L0〜L4 の定義
  "owner_menu": ["backoffice","dev","sales"]
}
```

`ok == true` (= `gap_count == 0`) なら「未分類・未所有の capability はありません。分類対象なし」と伝えて **終了**。以降の Step は不要。

## Step 2: 各提案を AI が精緻化する

`proposals` の各要素について、ツールの guess を **鵜呑みにせず**、実コードを読んで層と所有を絞る。ツールの guess は名前トークン頼みの叩き台 (= `confidence: low` は signal 無し) で、本当の判断材料はコード。

### 参照する原典 (最優先)

CORE doc `Capability 共有スコープ台帳` (doc_id `37Svg6nD2FccJM27yBjq`) の L0〜L4 定義と依存不変条件。初回に 1 度 `beacon doc show 37Svg6nD2FccJM27yBjq` で読む。要点:

- **L0 — Beacon プロダクト運用**: Beacon 自体を運用する admin/dev の道具 (doctor / skill install / migrate)。一般職種機能ではない。**どの install でも動く** (例: `beacon doctor` は pip 版でも動く)。
- **L1 — 全職種共通**: 職種に依存しない協奏基盤 (bus / dm / auth / trek / session / member / trigger)。職種で具象化しない。
- **L2 — クラス抽象化層**: target 抽象に触れ職種ごとに具象化される (dev=milestone / sales=opportunity) が、**規則は職種共通** (doc / claim / status / target 系)。
- **L3 — 職種固有デフォルト**: ある職種にデフォルトで備わる (dev: milestone/task/pr/deploy、sales: account/opportunity/meeting)。所有 = **職種**。
- **L4 — プロジェクト個別最適**: 特定 1 プロジェクト向け。標準出荷ゼロ。所有 = **プロジェクト**。

### verb の場合 (`kind == "verb"`)

1. `lib/commands.py` の `cmd_<verb>` ハンドラ (と呼ぶ helper) を読む。
2. **依存不変条件を確認**: そのハンドラが職種具象 (`core.save_entry` / `core.find_target_milestone` / `sales_entities.*` / `data['milestones']` / `data['opportunities']` の直読み) に触れているか。
   - 職種横断で使うのに具象に触れている → それは **L1/L2 の漏れ** (= バグ) の可能性。層を上げる前に `occupation.record_target_entry` / `occupation.iter_target_records` 経由へ直す方が正しいこともある。その場合は分類でなく **修正を提案** する。
   - 特定職種の記録に触れ、その職種専用の操作 → **L3** + その職種。
3. どの install でも動く運用系 (= Beacon 自体の管理) → **L0**。

### skill の場合 (`kind == "skill"`)

1. `skills/<name>.md` の本文を読み、それが **駆動する CLI verb 群** を見る。
2. sales 専用 verb を駆動 → L3/sales。協奏基盤 verb (bus/trek/session) → L1。target 抽象 (doc/status/claim) を扱う知識・計画系 → L2。Beacon 自体の保守 (source tree でしか意味がない) → L4/そのプロジェクト。どの install でも動く運用系 → L0。

### 判断を書き出す

各 capability について、以下を確定する:
- **層 (L0〜L4)** と、その **根拠** (どの定義にどう当てはまるか、コードのどの行が決め手か)
- **所有**: L3 なら職種 (`owner_menu` から)、L4 ならプロジェクト id。L0/L1/L2 は所有なし (= 正しい空)。
- **層を上げず修正すべきケース** はここで分離し、分類でなく修正提案として扱う。

## Step 3: ユーザーに提示して確定を取る (必須ゲート)

精緻化した提案を **まとめてユーザーに提示** し、**明示的な承認を待つ**。承認前に `lib/capability_ledger.py` を書き換えてはならない。

提示フォーマット:
```
capability 分類提案 (N 件) — 確定すると台帳 lib/capability_ledger.py に登録します

1. [verb] payroll_run
   提案: L3 / 所有 dev
   根拠: cmd_payroll_run は core.save_entry (dev milestone recorder) を呼び、
         dev の勤怠タスクを記録する職種固有操作。どの install でも動く運用系ではない。
   編集先: _NOUN_SCOPE['payroll'] = 'L3'  +  _L3_NOUN_PROFESSION['payroll'] = 'dev'

2. [skill] beacon-backoffice-payroll
   提案: L3 / 所有 backoffice
   ...

（層を上げず修正すべきものがあれば別立てで）
⚠ foo_list は L2 (status 系) なのに data['opportunities'] を直読みしています。
   これは分類でなく漏れ。occupation.iter_target_records 経由へ直すのが正。分類保留。

この分類で台帳に登録してよいですか？ (全部OK / 番号指定で修正 / やめる)
```

- ユーザーが層 / 所有を **override** したらそれに従う (= 最終決定権は人間)。
- 「やめる」なら何も書かず終了。
- 新しい職種 (`owner_menu` に無い) が要るなら、まず `PROFESSIONS` への追加が必要な旨を伝え、確認を取る。
- 新しい L4 プロジェクト所有なら、そのプロジェクト id をユーザーに確認する。

## Step 4: 台帳への書き込み (承認後のみ)

ユーザーが承認した項目についてのみ、Edit ツールで `lib/capability_ledger.py` の該当 dict を編集する。編集先は提案の `edits[].dict` / `key` / `value_hint` が指す:

- verb の scope: `_NOUN_SCOPE[noun] = "<Lx>"` (verb 個別例外は `_VERB_SCOPE_OVERRIDE`)
- verb の owner: L3 → `_L3_NOUN_PROFESSION[noun] = "<職種>"` / L4 → `_L4_VERB_PROJECT[verb] = "<project>"`
- skill の scope: `_SKILL_SCOPE[name] = "<Lx>"` (族なら `_SKILL_PREFIX_SCOPE` に longest-wins ルール)
- skill の owner: L3 → `_SKILL_OWNER[name]` or `_SKILL_OWNER_PREFIX` / L4 → `_SKILL_PROJECT[name] = "<project>"`

**根拠を必ずコメントで残す**。台帳の既存エントリと同じく、なぜその層かを 1 行添える (後から監査する人間・AI のため)。

## Step 5: 検証 — 台帳が緑になったことを証明する

書き込み後、点検ツールを **通常モード** (= gate) で走らせ、未分類・未所有が解消したことを確認:
```bash
python3 scripts/check-capability-scope.py
```
`OK: every capability is classified ...` かつ exit 0 なら成功。関連テストも走らせる:
```bash
python3 -m pytest tests/test_capability_ledger.py -q
```

未分類が残る / テストが落ちるなら、編集が不完全 (例: L3 にしたのに owner 未登録)。提案の `note` に従って追補する。

## Step 6: 結果の提示

```
台帳更新: N 件を登録しました
  - payroll_run → L3 / dev
  - beacon-backoffice-payroll → L3 / backoffice
検証: check-capability-scope.py OK (exit 0) / test_capability_ledger.py 全 pass
保留 (修正が必要): foo_list — L2 の職種漏れ、occupation 経由へ直す follow-up を提案
```

保留にしたもの (= 層を上げず修正すべき漏れ) があれば、`/beacon-task` で follow-up task を切ることを提案する (自動では切らない)。

## 制約

- **AI 提案 → 人間確定**: Step 3 の明示承認なしに `lib/capability_ledger.py` を書き換えない。これが本 Skill の存在理由。
- **既存4レビューと別立て**: ax/maintainability/philosophy/attainment を呼ばない・混ぜない。
- **提案モードは読み取り専用**: Step 1 の `--propose` は台帳を書かない。書くのは Step 4 のみ。
- **分類と修正を分ける**: 職種漏れ (依存不変条件違反) は「層を上げる」で隠さず、修正として分離する。
- **過去分類の遡行改変をしない**: 既存の分類済みエントリは触らない。本 Skill は新規 gap の登録に閉じる。
