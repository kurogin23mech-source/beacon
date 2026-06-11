---
name: beacon-incident-report
description: Incident を close した上で、原因/対応/再発防止を含むインシデントレポートを report スコープ Doc として保存する。close → report 作成のフローを一体で進める。
version: 1.0.0
triggers:
  - /beacon-incident-report
  - インシデントレポート
  - インシデント閉じる
---

# Beacon Incident Report

> 解決済みインシデントの close と、その文脈を残す report 作成を **一体的に** 進める Skill。
>
> 背景: UX レビュー (UC7-L8 / e-595) で「インシデントが open のまま放置される」実害があった。原因は `beacon incident close` だけ呼んで終わり、後で振り返ったときに「なぜ起きたか / どう直したか」が分からない構造。
> この Skill は close と report 作成を必ずセットにし、構造的にクローズ漏れと文脈喪失の両方を防ぐ。

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

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` の場合、この Skill は何もせず終了する。

## Step 0: 対象 Incident の特定

引数 (`/beacon-incident-report e-XX`) で entry id が指定されていれば、それを対象とする。

引数が無い場合は、open Incident 一覧を提示してユーザーに選ばせる:

```bash
beacon incident list --json
```

```
未解決の Incident:
  1. [e-321] "DB connection timeout" (op-3, open since 2026-05-20)
  2. [e-340] "Cloud Run cold start spike" (op-3, open since 2026-05-22)
  3. ...

どの Incident を report 化しますか？番号または entry id を教えてください。
```

ユーザーの選択を待ってから次へ進む。

## Step 1: コンテキスト取得

選択された Incident の詳細を取得:

```bash
beacon incident show <entry_id> --json
```

加えて、紐づく Operation の SPEC ドキュメントと、Incident 起票以降の run record を確認する:

```bash
# SPEC doc (運用手順の確認)
beacon doc list --scope spec --op <op-id> --json
# 各 doc は beacon doc show <doc_id> で取得

# Incident 起票以降の run record
beacon run list -o <op-id> --json
```

これらは AI が report の本文を書くための **文脈収集** であり、ユーザーには見せない。

## Step 2: 解決状況のヒアリング

ユーザーに簡潔に確認:

```
Incident [e-XX] "[title]" を close し、report を作成します。

以下を教えてください (簡潔で OK、後で AI が文章化します):

1. 根本原因 (なぜ起きたか)
2. 対応 (何を直したか / どのコミット・PR・設定変更が修正)
3. 再発防止 (同じ問題を防ぐために加えた仕組み・ルール、無ければ "なし")
4. 学び (今回の判断・経験で残しておくべきもの、無ければ "特になし")
```

ユーザーが回答するまで待つ。

## Step 3: report ドキュメント本文の生成

ユーザーの回答 + Step 1 で集めた文脈 (Incident の元 description, 関連 run record, SPEC の影響範囲) を統合し、以下の構造で Markdown を生成する:

```markdown
---
scope: report
incident_id: <entry_id>
operation_id: <op-id>
closed_at: <YYYY-MM-DDTHH:MM:SSZ>
---

# Incident Report: <title>

## サマリー
1〜2 文で何が起き何を直したか。

## タイムライン
- 起票: <created_at> — <最初に観測された症状>
- 観測経緯: 関連する run record があれば箇条書きで
- 解決: <closed_at>

## 根本原因
ユーザー回答 #1 を起点に、観測されたログ/run record の証拠を添えて記述。

## 対応
ユーザー回答 #2。コミットハッシュや PR 番号があれば全部入れる。

## 再発防止
ユーザー回答 #3。「なし」なら「現時点では追加対策なし。同症状が再発したら再 review」と明記。

## 学び・残しておくべき判断
ユーザー回答 #4 (任意)。
```

scope は **必ず `report`** にする (他の振り返りや SPEC との区別をつけるため)。

## Step 4: report ドキュメントの保存

```bash
beacon doc add --scope report --op <op-id> --title "Incident Report: <title>" --content "<生成した本文>"
```

成功すると doc_id が返る。次のステップで Incident の close 時に reference する。

## Step 5: Incident の close

ここで初めて Incident を close する。SPEC で重要なのは **report 作成より前に close しない** こと (close だけ走って report 作成が忘れられるパターンを防ぐため)。

```bash
beacon incident close <entry_id> --resolution "<1〜2文の resolution summary>。詳細は report doc:<doc_id>"
```

`--resolution` には必ず report doc_id への参照を入れる (将来この Incident を遡って見たとき、文脈にたどり着けるようにする)。

## Step 6: 結果報告

ユーザーに簡潔に報告:

```
✓ Incident [e-XX] "[title]" を close しました
✓ Report doc を作成しました: <doc_id>

resolution: <resolution の冒頭>

確認するには: beacon doc show <doc_id>
```

## 制約 / 例外処理

- **report 作成に失敗した場合は close もしない**。Step 4 で `beacon doc add` が失敗したらユーザーに通知して停止。
- Step 2 でユーザーが「やっぱり close しない」と判断したら、その場で停止する (中途半端な report は残さない)。
- 引数で指定された entry が `incident` 以外、または `closed` 済みの場合は明示エラーを出して停止する。
- この Skill は **必ず /beacon-operation-review および /beacon-session-start からの誘導経路** にも沿う (e-595 の構造的 close 誘導の終端)。
