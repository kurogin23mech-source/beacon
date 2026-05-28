---
name: beacon-operation-review
description: 定期チェックトリガー発火時に実行。Operation に紐づく SPEC ドキュメントの手順でログを取得・解釈し、run record を記録する。問題があれば Incident を起票する。
version: 1.0.0
triggers:
  - /beacon-operation-review
  - バッチ確認
  - 運用チェック
---

# Beacon Operation Review

> 定期チェックトリガー発火時に実行。SPECに従いログを取得・解釈し、run record を記録する。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 0: 引数チェック

ユーザーが `/beacon-operation-review op-1` のように Operation ID を指定した場合、それを対象とする。
指定がない場合は `beacon trigger check` の結果から対象 Operation ID を特定する。

## Step 1: 対象 Operation の確認

Bash ツールで実行:
```bash
beacon operation show <op-id> --json
```

Operation の `log_source`・`schedule`・`entries`（直近 run_record）を確認する。

## Step 2: SPEC ドキュメントを読む

Step 1f でセッション開始時に自動読み込み済みの場合はそれを使う。
未読みの場合は Bash ツールで実行:

```bash
beacon doc list --scope spec --op <op-id> --json
```

結果があれば:
```bash
beacon doc show <doc_id>
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
beacon run record -o <op-id> --batch <log_source> --status <ok|warning|error> --desc "<Step4のdescription>"
```

## Step 6: 問題があれば Incident 起票

Step 4 でステータスが `warning` または `error` の場合、かつユーザーが Incident として記録すべき問題と判断した場合:

```bash
beacon incident open "<問題のタイトル>" -o <op-id> --desc "<詳細な説明>"
```

Incident 起票の判断基準:
- `warning` で一時的な揺れと判断 → 起票不要（description に記録のみ）
- `warning` で継続的または増加傾向 → 起票推奨
- `error` → 原則として起票

## Step 6.5: 既存 open Incident のクローズ誘導 (e-595)

このレビュー対象の Operation に紐づく **既存の open Incident** が無いか確認する。

```bash
beacon incident list -o <op-id> --json
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
