---
name: beacon-operation-review
description: 定期チェックトリガー発火時に実行。Operation に紐づく SPEC ドキュメントの手順でログを取得・解釈し、run record を記録する。問題があれば Incident を起票する。
version: 1.1.0
triggers:
  - /beacon-operation-review
  - バッチ確認
  - 運用チェック
---

# Beacon Operation Review

> 定期チェックトリガー発火時に実行。SPECに従いログを取得・解釈し、run record を記録する。

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

## cwd 解決（最重要）

このSkillは trigger 発火 (session-start 表示) 経由で Claude Code がホームで起動された状態から呼ばれることが多い。

以下の優先順位で **作業ディレクトリ** を決定する（以降 `$PROJECT_DIR` と呼ぶ）:

1. **trigger / hook が渡した `(project: ...)` パス**を additionalContext から抽出
2. ユーザーが引数で渡したパスがあればそれを使う
3. それ以外は Bash の `pwd` 結果。ホーム直下なら abort

**以降、すべての Bash 呼び出しは `cd "$PROJECT_DIR" && ...` 形式で実行する。**

## 前提条件チェック

Bash ツールで以下を実行:
```bash
cd "$PROJECT_DIR" 2>/dev/null; beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 0: 引数チェック

ユーザーが `/beacon-operation-review op-1` のように Operation ID を指定した場合、それを対象とする。
指定がない場合は `beacon trigger check` の結果から対象 Operation ID を特定する。

## Step 1: 対象 Operation の確認

Bash ツールで実行:
```bash
cd "$PROJECT_DIR" && beacon operation show <op-id> --json
```

Operation の `log_source`・`schedule`・`entries`（直近 run_record）を確認する。

## Step 2: SPEC ドキュメントを読む

Step 1f でセッション開始時に自動読み込み済みの場合はそれを使う。
未読みの場合は Bash ツールで実行:

```bash
cd "$PROJECT_DIR" && beacon doc list --scope spec --op <op-id> --json
```

結果があれば:
```bash
cd "$PROJECT_DIR" && beacon doc show <doc_id>
```

**SPEC に書かれた手順に従ってログを取得する。**
SPEC が存在しない場合はユーザーに確認: 「どのようにログを取得しますか？ /beacon-operation-setup でSPECを作成することをお勧めします。」

## Step 3: ログ取得

SPEC の「ログ取得」セクションに従い、Bash ツール または Read ツールでログを取得する。

例）
- コマンドの場合: `Bash` でコマンド実行
- ファイルの場合: `Read` で読み込み
- URLの場合: `WebFetch` で取得

## Step 4: 解釈とステータス判定

取得したログを SPEC の「ステータス判定」基準に照らし合わせて評価する:

- `ok`: 正常範囲内
- `warning`: 閾値接近または軽微な問題
- `error`: 対処が必要な問題

### description の生成

**SPEC の「Run Record 記載項目」セクションに従ってフォーマットする。** セクションが存在する場合:

1. 必須項目を全て埋める（漏れがあればログを再読し補完する）
2. 推奨フォーマットに従って `[項目]: [値]` 形式で羅列
3. 主要トピックを1〜2文の解釈として追加
4. ステータスが warning / error なら原因候補を含める
5. シェル展開回避ルール（`$` を避ける、または `'...'` で囲む）を守る

SPEC にこのセクションが**無い場合**（古いOperation）:
- フリーフォーマットで処理件数・主要指標・傾向・解釈を1〜2文で書く
- このOperationには SPEC更新を提案: 「次回 /beacon-operation-setup で Run Record 記載項目セクションを追加するとフォーマットが安定します」

## Step 5: Run Record 記録

Bash ツールで実行:

```bash
cd "$PROJECT_DIR" && beacon run record -o <op-id> --batch <log_source> --status <ok|warning|error> --desc "<Step4のdescription>"
```

## Step 6: 問題があれば Incident 起票

Step 4 でステータスが `warning` または `error` の場合、かつユーザーが Incident として記録すべき問題と判断した場合:

```bash
cd "$PROJECT_DIR" && beacon incident open "<問題のタイトル>" -o <op-id> --desc "<詳細な説明>"
```

Incident 起票の判断基準:
- `warning` で一時的な揺れと判断 → 起票不要（description に記録のみ）
- `warning` で継続的または増加傾向 → 起票推奨
- `error` → 原則として起票

### Step 6a: 解決タスク化の提案 (e-591 / UC7-L4)

Incident を起票した直後、それを **「解決する作業」として MS にタスク化** するかをユーザーに提案する。提案だけで、実行はユーザー判断:

```
Incident [e-id] "[title]" を起票しました。
これを解決するタスクをどの MS に作りますか？

  候補:
    - ms-XX [active MS title] (推奨: Operation 関連の修正系 MS があればそれ)
    - ms-YY ...
    - 何も作らない（今は記録だけで、後で対応）

選んでもらえれば beacon incident escalate <e-id> -m <ms-id> を実行します。
```

### 判断ロジック

- **active MS が 1 つだけ** で、そのテーマが Incident 領域と関連深い → その MS を強く推奨
- **active MS が複数** → 候補を列挙してユーザーに選ばせる
- **関連 MS が見当たらない** → 「新しい MS を立てるかどうか」を聞く（`beacon milestone add` の話に切替）
- ユーザーが「不要」と言えば何もしない (`incident escalate` を呼ばない)

承認時:
```bash
cd "$PROJECT_DIR" && beacon incident escalate <incident-id> -m <ms-id>
```

これにより `linked_ms_task` でインシデントと task の双方向リンクが付き、後で retrospection で辿れる。

## Step 6.5: 既存 open Incident のクローズ誘導 (e-595)

このレビュー対象の Operation に紐づく **既存の open Incident** が無いか確認する。

```bash
cd "$PROJECT_DIR" && beacon incident list -o <op-id> --json
```

`status == "open"` のエントリが存在する場合、**毎回必ず提示する** (UX レビュー UC7-L8 で実害あり)。誘導文の例:

```
このオペレーションには未解決の Incident が [N]件 残っています:
  - [e-id] "[title]" (open since [created_at])
今回の run record の結果を踏まえて、解決済みのものはありますか？
- close する場合: /beacon-incident-report Skill を実行してください
  (close + report 作成までエスコートします)
- まだ未解決なら、本 Skill では何もしません
```

判断はユーザーに委ねる (この Skill は close を直接行わない)。`/beacon-incident-report` 経由で必ず report 作成まで一体的に進める。

## Step 7: 結果報告

ユーザーに簡潔に報告:

```
Run recorded: [op-id] / [batch] [✓ok/⚠warning/✗error]
  [description]
[→ Incident起票: [e-id] "[title]"]  ← 起票した場合のみ
[→ 未解決 Incident [N]件 — close 検討の機会です]  ← Step 6.5 で見つかった場合のみ
```

## 制約

- 書き込みは `beacon run record` と `beacon incident open` のみ
- close 操作は **直接行わない**。close は `/beacon-incident-report` Skill 経由で
  必ず report 作成までエスコートする (e-595)
- SPEC の手順に忠実に従う。独自の判断でログ取得方法を変えない
- 読み取り専用の操作（ログ取得）は Bash/Read/WebFetch を自由に使う
- **すべての beacon CLI 呼び出しに `cd "$PROJECT_DIR" && ...` を前置する**。Claude Code がホームで起動していても正しく動くため。
