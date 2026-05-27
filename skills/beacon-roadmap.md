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

> プロジェクトビジョンから、大目的達成までのマイルストーン群を一括設計する。

## 前提条件チェック

Bash ツールで:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
beacon doc show project-vision 2>/dev/null
```

`project-vision` ドキュメントが存在しない場合:
```
プロジェクトビジョンドキュメントがありません。

ロードマップは「何を達成するか」が定義されていないと描けません。先に /beacon-vision でビジョンを整理しますか？
```

ユーザーが承認したら `/beacon-vision` を起動。それ以外は終了。

## Step 1: ビジョンの読み込み

Bash ツールで実行:
```bash
beacon doc show project-vision
```

stdoutの内容（6セクション）をAIが読み込み、内部で理解する。

あわせて既存のマイルストーン状態も把握:
```bash
beacon status --json
```

既に `in_progress` / `todo` / `observing` 状態のMSがあれば、それを踏まえてロードマップを設計する（既存MSと衝突・重複しないように）。

## Step 2: マイルストーン群の設計

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

## Step 2.5: Operation 輪郭の同時提案

ビジョンの「成功基準」「やらないこと」を踏まえ、**プロジェクトが完成したあと運用継続が必要なOperationの輪郭** を 0〜5個提案する。

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

## Step 3: 提案の提示

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

このロードマップで進めますか？

選択肢:
  1) OK、このまま登録して最初のMSをアクティブにする
  2) 一部修正したい（どこを変えたいか教えてください）
  3) もっと粒度を細かく/粗くしたい
  4) 順番を変えたい
  5) これは違う、もう一度設計し直したい
```

## Step 4: 修正フェーズ（必要時）

ユーザーが「2〜5」を選んだ場合、該当箇所を修正して再提示する。
- 「2」: 指摘された部分のtitle / objective / ac / priority を調整
- 「3」: 全体を分割（細かく）or 統合（粗く）して再提案
- 「4」: 順序のみ入れ替え。依存関係を再計算
- 「5」: 別の切り口で再設計

修正後、Step 3 の形式で再提示。ユーザーが「1」を選ぶまで繰り返す。

## Step 5: 一括登録

ユーザーが承認したら、各MSを順番に登録する。

### 各MSの追加

Bash ツールで全MSに対して順次実行（順序が重要）:

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

### Operationの登録（Step 2.5 で提案した場合のみ）

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

## Step 6: 完了報告

```
ロードマップを登録しました。

  ◐ ms-A: [title]   ← アクティブ（実装中）
  ○ ms-B: [title]
  ○ ms-C: [title]
  ...

最初のマイルストーン「[ms-A.title]」を開始しています。
このMSの最初のタスクから始めますか？それともこのMSの SPEC ドキュメントを先に書きますか？
```

## 制約

- 既存MS（in_progress / todo / observing）と重複・衝突するMSは提案しない
- 各MSは「機能の実装」ではなく「ユーザーが何を手にするか」で表現する
- ロードマップは3〜7個に収める。多すぎると消化できず、少なすぎると粒度が粗い
- ビジョンの「やらないこと」をスコープ外に保つ
- bulk add時にエラーが出たら、その時点で停止してユーザーに状況を報告する
