---
name: beacon-retro
description: 週次振り返りドキュメントを生成し、ユーザーとディスカッションする。課題→判断→結果の文脈で再構成。
version: 0.2.0
triggers:
  - /beacon-retro
  - 週次振り返り
  - 今週のふりかえり
  - retro
  - 振り返りしよう
---

# Beacon Retro

> 週次の活動を「課題→判断→結果」の文脈で再構成し、振り返りドキュメントを生成する。

## cwd 解決

trigger 経由起動時は hook の `(project: ...)` パスを `$PROJECT_DIR` として使う。明示が無い場合は `pwd` を使い、ホーム直下なら abort。以降全 Bash 呼び出しに `cd "$PROJECT_DIR" && ...` を前置。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
cd "$PROJECT_DIR" && test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 1: 週次データの収集

Bash ツールで以下を **並列に** 実行:

### 1a. beacon エントリ
```bash
cd "$PROJECT_DIR" && beacon retro --prepare [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```
- デフォルトは今週（月曜〜今日）
- ユーザーが期間を指定した場合は `--since` / `--until` を付加

### 1b. git コミット一覧（コンテキスト補完用）
```bash
cd "$PROJECT_DIR" && git log --since="YYYY-MM-DD" --until="YYYY-MM-DD" --oneline --no-merges
```
- beacon エントリにないコミットがあれば補足情報として使用

## Step 2: 振り返りドキュメントの生成

Step 1 のデータを元に、以下の構造でマークダウンドキュメントを生成する。

### 構造

```markdown
# Weekly Retro: YYYY-MM-DD 〜 YYYY-MM-DD

## 今週の取り組み

### [課題/目的のタイトル]
- **課題**: なぜこれに取り組んだのか（背景・動機）
- **手段**: どういうアプローチを取ったか（技術選定・設計判断）
- **結果**: 何が達成されたか、何が残ったか

### [次の課題/目的のタイトル]
...

## 方向性チェック
- [観察] 事実ベースの振り返り（何が起きたか）
- [問い] 次週に向けた問い（この方向でいいのか、優先度は正しいか）

## 得た知見
- 今週の開発で学んだこと、発見したパターン
- CLAUDE.md や memory に反映すべき候補があれば明示

## 次週のヒント
- 残タスク・未着手MSから、次に取り組むべき候補を提案
```

### 生成ルール

- **「今週やったこと」は表層的なリストにしない**。コミット一覧やタスク消化リストではなく、課題単位でグルーピングし、文脈（なぜ→どうした→どうなった）を再構成する
- 同じ課題に対する複数のコミット/タスクは1つにまとめる
- beacon の summary フィールドに蓄積された経緯・判断を活用する
- 完了したMSだけでなく、進行中のMSの進捗も含める

## Step 3: ドキュメントの保存

生成したドキュメントを Write ツールで保存:
```
.beacon/retro/YYYY-WNN.md
```
- `YYYY` は年、`WNN` は ISO 週番号（例: `2026-W19.md`）
- `.beacon/retro/` ディレクトリが存在しない場合は作成する

## Step 4: ディスカッションの開始

ドキュメントをユーザーに提示し、以下の観点でディスカッションを促す:

```
振り返りドキュメントを生成しました: .beacon/retro/YYYY-WNN.md

確認したいポイント:
1. [方向性チェックの問いから最も重要なもの]
2. [得た知見でCLAUDE.mdに反映すべきもの]
3. [次週のヒントで優先度を決めたいもの]

どこから話しましょうか？
```

## Step 4.5: CORE doc 昇格の提案（e-574 / UC5-J1'）

Step 4 のディスカッションで **永続化価値のある気づき** が出た場合、それを CORE doc として残すことをユーザーに提案する。

### 判定基準（AI 主体で判断）

retro のディスカッション内容を読み返し、以下のいずれかに該当する発見があれば「CORE doc 候補」として抽出する:

1. **新しい設計原則・方針の言語化**: 「○○は△△で統一する」「□□の場合は××する」のような、今後も繰り返し参照されるルール
2. **既存 CORE doc と矛盾する判断**: アーキテクチャや運用方針の転換（既存 CORE の更新候補）
3. **再発しそうな失敗パターンの教訓**: 「○○すると××で破綻するので避ける」のような、未来の自分への戒め

該当しない場合（単なる進捗確認・既知ルールの再確認のみ）はこの Step をスキップする。retro doc 自体に「学び」として残せば十分。

### 提案文（実行は user 承認後）

```
今回の振り返りで、以下の気づきは CORE doc 化する価値がありそうです:

  1. [気づきの要約 — 1〜2文]
     scope: core / 既存 CORE doc 更新（doc_id: xxx）
     理由: [なぜ永続化価値があるか]

  2. ...

これらを CORE doc に昇格させますか？（部分選択も可）
```

### 承認時の実行

ユーザーが承認したら Bash ツールで実行:

- 新規 CORE doc:
  ```
  cd "$PROJECT_DIR" && beacon doc add "<title>" --scope core --stdin <<'EOF'
  [本文]
  EOF
  ```
- 既存 CORE doc 更新:
  ```
  cd "$PROJECT_DIR" && beacon doc update <doc_id> --stdin <<'EOF'
  [更新後の本文]
  EOF
  ```

### 設計判断（なぜこのStepが必要か）

retro doc は履歴として永続化されるが、`scope: retro` は session-start で **自動読み込みされない**（core / spec / 関連spec のみ）。一方 CORE doc は全セッションで常時参照される。

「重要な学びだけど retro doc に埋もれる」を避けるため、AI が能動的に CORE 昇格を提案する。これは /beacon-log Step 6 (ドキュメント評価) と同じ発想で、retro の文脈内で適用する。

## Step 5: レビュー完了の記録

ユーザーが振り返りの議論を終えたと判断できたら（「OK」「以上」「振り返り終わり」等）、Bash ツールで実行:
```bash
cd "$PROJECT_DIR" && beacon retro done
```
これによりダッシュボードの振り返り警告が消え、e-575 の persistent retro trigger も解除される。

**判断基準**: ユーザーが方向性チェックの問いに回答し、次のアクションが決まった時点でレビュー完了とみなす。ドキュメント生成直後に自動で叩いてはならない。

## 制約

- データ取得は Bash ツール経由の beacon CLI のみ。project.json を直接読まない。
- ドキュメント生成は Write ツールで `.beacon/retro/` に保存する。
- 振り返りは提案であり、CLAUDE.md の更新やタスク操作は **ユーザーの合意を得てから** 行う。
- CORE doc 昇格は **必ず user 承認後**。AI 単独で `beacon doc add --scope core` を叩かない。
- 全 Bash 呼び出しに `cd "$PROJECT_DIR" && ...` を前置する。
