---
name: beacon-operation-setup
description: Operationを輪郭(todo)から実稼働(open)まで会話で組み立てる。新規Operationの作成と、既存todoOperationの活性化の両方に対応。OperationTasks（準備項目）とSPECドキュメント（ログ取得手順）をセットで整備する。
version: 2.1.0
triggers:
  - /beacon-operation-setup
  - Operationをセットアップ
  - Operationを活性化
  - 運用監視を始めたい
  - バッチの記録を始めたい
---

# Beacon Operation Setup

> Operationを輪郭(todo)から実稼働(open)まで組み立てるSkill。新規作成と既存todo活性化の両対応。

## cwd 解決（最重要）

このSkillは Claude Code をホームディレクトリで起動したまま `~/<proj>/` 配下の Operation を扱うケースを想定する。

以下の優先順位で **作業ディレクトリ** を決定する（以降 `$PROJECT_DIR` と呼ぶ）:

1. **hook が渡した `(project: ...)` パス**を additionalContext から抽出（trigger 経由起動時）
2. ユーザーが `/beacon-operation-setup <op-id>` のように引数で渡し、現在の cwd 配下に `.beacon/project.json` がある場合は cwd を使う
3. それ以外は Bash ツールで `pwd` の結果を `$PROJECT_DIR` とし、ホームディレクトリそのものなら abort

**以降、すべての Bash 呼び出しは `cd "$PROJECT_DIR" && ...` 形式で実行する。**

## 前提条件チェック

```bash
cd "$PROJECT_DIR" && test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
`NO_BEACON` なら終了。

## Step 0: 対象Operationの特定

```bash
cd "$PROJECT_DIR" && beacon operation list --json
```

todo状態のOperationが存在するか確認:

- **todoOperationあり**: ユーザーに提示
  ```
  todo状態の Operation が以下あります:
    1) op-1 "Service health monitoring"
       hint: Cloud Run 本番稼働後に有効化
    2) op-2 "Cost watch monthly"

  どれをセットアップしますか？それとも新規作成？
  ```
- **無しまたは「新規」を選択**: Step 1（新規作成フロー）へ
- **既存を選択**: Step 1.5（既存活性化フロー）へ

## Step 1: 新規Operation情報収集

以下を会話で収集（既に会話の流れで言及されていれば再質問しない）:

### 1a. タイトル・目的・対象
```
何を継続的に運用したいですか？

例:
- 「Cloud Run の稼働状況を毎日監視したい」
- 「月次でGCPコストを確認したい」
- 「週次で利用者数を集計したい」
```

タイトルと objective を抽出する。

### 1b. log_source（識別名）

監視対象を識別する英数字スラッグ（例: `cloud_run_health`, `gcp_billing`, `usage_metrics`）を収集または推測。

### 1c. 活性化条件のヒント（任意）
```
このOperationを動かし始めるタイミングはいつですか？

例:
- 「本番デプロイが完了した後」
- 「ユーザーが10人を超えたら」
- 「今すぐ」
```

自由テキストで `activation_hint` に保存（厳密ルールではなく、AIが文脈で判断する材料）。

### 1d. スケジュール
```
どのくらいの頻度でチェックしますか？

  daily     — 毎日
  weekdays  — 平日のみ
  weekly    — 週1回

（デフォルト: weekdays）
```

### 1e. ログ取得方法・判断基準

```
ログはどう取得しますか？

例: コマンド `gcloud run services logs --service=X --limit=50`、ファイル `~/logs/latest.json`、URL等
```

```
どのような状態を「正常」「警告」「エラー」と判断しますか？
```

### 1f. Run Record の記載項目

```
毎回の run record で必ず記録したい定量情報は何ですか？
記録者（Claudeまたは人間）によって粒度がブレないよう、フィールドを固定しておきます。

例:
- 処理件数 / ユニーク件数 / エラー件数
- 処理時間 / コスト
- 主要API呼び出し回数
- 直近N日との比較

「思いつかない」場合は「処理件数とエラー件数」のような最小セットで進めます。
```

ユーザーの回答から、毎回記録すべき項目のリストを抽出する（後でSPECに埋め込む）。

## Step 1.5: 既存todoOperationの活性化フロー

Step 0 で既存を選んだ場合:

```bash
cd "$PROJECT_DIR" && beacon operation show <op-id>
```

既存の情報（title / objective / hint / 既存OperationTasks / 既存SPEC）を読み込む。

不足している情報のみユーザーに質問（log_source、ログ取得方法、判断基準等）。OperationTasks が既にあれば、それを参考にして「他に必要な準備」を提案する。

## Step 2: OperationTasks の設計

このOperationを **open化（稼働状態）にするために必要な準備項目** を列挙する。

各OperationTaskに:
- description（何をやるか）
- motivation（なぜ必要か） 
- acceptance_criteria（どうなったら完了か）
- priority（high/middle/low）

例（"Cloud Run health monitoring"）:
```
OperationTasks（このOperationを open化するための準備）:
  1. ヘルスチェックエンドポイント /health を実装
     なぜ: 監視の起点になる
     完了条件: 200 を返すエンドポイントが本番に存在
     priority: high

  2. アラート閾値の決定
     なぜ: 何を warning / error と判定するか合意が必要
     完了条件: SPECドキュメントに閾値が明記されている
     priority: middle

  3. 障害発生時のIncident起票テンプレート整備
     なぜ: 異常検知時に即対応できる体制
     完了条件: Skillが対応している
     priority: low
```

ユーザーに提示し、追加・修正・削除を受け付ける。

## Step 3: 確認

```
以下の内容で進めます:

  Operation: [title]
    objective: [obj]
    log_source: [log_source]
    schedule: [schedule]
    activation_hint: [hint]

  OperationTasks (準備項目):
    1. [desc] [priority]
    2. ...

  SPECドキュメント (ログ取得手順):
    取得方法: [...]
    判断基準: [...]

これで Operation を todo 状態で作成し、すべての準備が終わったら activate しますか？
```

## Step 4: Operation作成 + OperationTasks 登録 + SPEC生成

### 4a. Operation を todo で作成（新規の場合のみ）

```bash
cd "$PROJECT_DIR" && beacon operation create "<title>" \
  --schedule <schedule> \
  --log-source <log_source> \
  --objective "<objective>" \
  --hint "<activation_hint>" \
  --priority <priority>
```

stdout から op-id を取得。既存活性化フローではこのステップをスキップ。

### 4b. OperationTasks を一括追加

各タスクに対して:
```bash
cd "$PROJECT_DIR" && beacon operation task add "<description>" -o <op-id> \
  --priority <priority> \
  --why "<motivation>" \
  --ac "<acceptance_criteria>"
```

### 4c. SPECドキュメント生成

```bash
cd "$PROJECT_DIR" && beacon doc add "<log_source> ログ取得・解釈手順" --scope spec --op <op-id> --stdin <<'EOF'
# <log_source> ログ取得・解釈手順

## ログ取得

[Step 1e で収集した取得方法]

## ステータス判定

[Step 1e で収集した判断基準]

## Run Record 記載項目

毎回の run record の description に以下の情報を含めること。粒度が記録者によってブレないようフィールドを固定する。

### 必須項目
[Step 1f で収集した項目を箇条書きで]
- 処理件数（例: 7件）
- エラー件数（例: 0件）
- [...]

### 推奨フォーマット
```
[項目1]: [値]、[項目2]: [値]、[項目3]: [値]
（主要トピック1-2文）
```

例:
```
処理 7件 / エラー 0件 / スキップ 7件 / LLM呼出 0件 / コスト USD 0.40。
新規ユーザーがいないため LLM 抽出が走らず、コストは固定費のみ。
```

### 記載ルール
- ステータスが warning / error の場合、原因候補も description に含める
- 前日比・傾向があれば言及する（増加/減少のニュアンス）
- 「データではなく解釈を記録する」: 生ログの転記ではなく、運用判断の文脈で書く

### シェル展開回避

`beacon run record --desc "..."` で `$` を含む文字列を渡す際、シェルが変数展開してしまうことがある（例: `$0.40` → `/bin/zsh.40`）。回避策:

- `$` 記号は避け、`USD 0.40` のように単位を明示
- どうしても `$` が必要な場合はシングルクォートで囲む: `--desc 'cost $0.40'`

## 記録ガイドライン

- ok: 正常範囲内。必須項目を全て記載
- warning: 閾値接近または軽微な問題。原因候補・傾向も記録
- error: 対処が必要な問題。Incident 起票を検討
EOF
```

## Step 5: 活性化判断

OperationTasks の状況を見て判断:

```bash
cd "$PROJECT_DIR" && beacon operation task list -o <op-id>
```

### 全 OperationTasks が done

「準備が完了しています、`open`（稼働状態）に遷移しますか？」とユーザーに確認。  
承認なら:
```bash
cd "$PROJECT_DIR" && beacon operation activate <op-id>
```

### まだ done になっていない OperationTasks がある

`todo` のまま終了。  
ユーザーに案内:
```
OperationTasksの準備が完了したら、再度 /beacon-operation-setup でこの Operation を選んで活性化してください。

未完了タスク:
  - [e-N] [title]
  - ...
```

OperationTasks を `in_progress` にしたい場合、ユーザーは:
```bash
cd "$PROJECT_DIR" && beacon operation status <op-id> in_progress
```

## Step 6: 完了メッセージ

```
Operation セットアップ完了

  [op-id] "[title]"  [status]
  
  OperationTasks: [done]/[total] 完了
  SPEC: [<log_source> ログ取得・解釈手順]
  
  [status == "open" の場合]
  → 次回セッション開始時からチェックトリガーが届きます。
  → トリガーが届いたら /beacon-operation-review で記録します。
  
  [status == "todo" の場合]
  → OperationTasks を消化してから再活性化してください。
```

## 制約

- Bash ツールで CLI コマンドを実行する（ユーザーに `!` で実行させない）
- ユーザーが明示的に答えた情報は再度聞かない
- Operation ID は `beacon operation create` の stdout から取得する（仮定しない）
- 既存todoOperation活性化フローでは、既存情報を尊重する（上書きしない、不足分のみ補完）
- **すべての Bash 呼び出しに `cd "$PROJECT_DIR" && ...` を前置する**。Claude Code がホームで起動していても正しく動くため。
