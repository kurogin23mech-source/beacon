---
name: beacon-drift-check
description: beacon doctor の drift 系 warn を整形して、どの Skill のどの行を直すべきかと推奨修正を一覧で返す。修正は提案のみ、自動実行はしない。
version: 1.0.0
triggers:
  - /beacon-drift-check
  - drift をチェック
  - 整合性チェック
  - Skill 整合性を見て
---

# Beacon Drift Check

> `beacon doctor` の **drift 系 warn** (= ms-61 で追加した check #8 / check #9 由来の整合性ずれ) を、開発者・AI が即修正に入れる形に整形して返す。
>
> 検出は `beacon doctor` 本体に任せ、本 Skill は **UI と修正提案** を担う分業。自動修正・自動 PR は持たない (= alert + 人間 / AI 修正ループ運用、ms-61 SPEC 設計方針 2)。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

### 守ること

1. **読み手目線 1 行から始める**: 「何が嬉しいか」「何が困るか」をユーザー体験の言葉で。技術用語ではなく価値で書く
2. **横文字 3 段階**:
   - そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP` / `Tauri` / `WebSocket`)
   - 初出時に日本語注: 技術概念 (`allowlist (= 許可リスト)` / `opt-in (= 個別許可)` / `subcollection (= 子コレクション)`)
   - 日本語化が望ましい: 一般概念 (configure → 設定 / receiver → 受信側 / audit → 監査 / hit → 一致 / install → 設置 / merge → 取り込み / deploy → 配置)
3. **ID 参照には文脈**: `e-XXXX` / `UC?` / `ms-XX` は初出に必ず『何の話か』1 行添える
4. **尻切れトンボ禁止**: 主語と述語を省略しない、論理関係を明示

### 詳細

詳しい原則と例は CORE doc `entry-writing-principle` 参照。

---

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

## Step 1: `beacon doctor` を実行して drift 系 warn を抽出

Bash ツールで実行:
```bash
beacon doctor 2>&1
```

`beacon doctor` は全 check (1〜9) を回し、各 check 由来の warning を `WARN [<tag>] <message>` 形式で stdout に書く。本 Skill が拾う tag は **2 種類**:

| tag | 出典 check | 意味 |
|---|---|---|
| `skill-cli-drift` | check #8 (ms-61 / e-1570) | `~/.claude/skills/<name>/*.md` が叩く `beacon <subcmd>` のうち、現在 CLI に存在しないものがある |
| `project-stale` | check #9 (ms-61 / e-1571) | ローカル `.beacon/project.json` と cloud の milestone / operation 件数が食い違っている、または mtime が閾値超で古い |

他 tag (`PATH` / `hooks` / `skills` / `token` / `cloud.json` / `dup-id` / `skills-drift`) は環境設定系で本 Skill のスコープ外。素通りさせる。

### 出力を Python でパース

`WARN [skill-cli-drift]` ブロックの構造 (= 1 行目に `WARN [skill-cli-drift]` で始まる見出し行、続く字下げ行に `L<行番号>: <抽出されたコマンド句>` が 1〜3 件、必要なら `(+N more unique reference(s))` の続き表示、最後に説明文と opt-out hint が 4 行ほど続く):

```text
WARN [skill-cli-drift] Skill NAME references unknown subcommand(s):
       L<行番号>: <抽出されたコマンド句>
       L<行番号>: <抽出されたコマンド句>
       (+N more unique reference(s))         <- 任意
       <説明文 (4 行 + opt-out hint)>
```

(↑ 上記の `NAME` プレースホルダは実出力では Skill 名が入る。本 markdown では doctor の Skill ↔ CLI drift check 自身に誤検知されないよう、テンプレートを `beacon <word>` パターンと一致しない形に書き換えてある)

`WARN [project-stale]` ブロックの構造:
```
WARN [project-stale] .beacon/project.json differs from cloud:
       milestones: local=N, cloud=M
       operations: local=N, cloud=M
       (local mtime: NN min ago)
       Run: beacon cloud pull   (to refresh local cache)
       Opt-out: BEACON_DOCTOR_SKIP_CLOUD_SYNC=1
```
または mtime 超過 soft warn:
```
WARN [project-stale] .beacon/project.json was last touched NN minutes ago (threshold MM min).
       <説明 + cloud pull 推奨>
```

正規表現または heredoc Python で各 WARN ブロックを切り出し、tag / Skill 名 / 行番号 / コマンド句 / count diff 等を構造体として保持する。

## Step 2: 推奨修正の生成

各 drift エントリに対し、**推奨修正** を 1 行で添える。自動修正はしない。

### skill-cli-drift の場合

| 状況 | 推奨修正の出し方 |
|---|---|
| 不在の `beacon <X>` を呼んでいる | 「`<X>` に最も似た現存コマンドは `<Y>` (`beacon help` で全リスト確認可)。markdown を `beacon <Y>` に書き直すか、該当行を削除」 |
| 将来構想の例として書かれている (本文に「将来的には」「未来形」等) | 「将来構想の記述を引用ブロック (= 4 スペース字下げ or > 引用) に移すか、注釈で『現状未実装』と明示すれば doctor の drift 検知に引っかからなくなる」 |
| Claude Skill の slash command (`/beacon-XXX`) と混同してる | 「CLI の `beacon XXX` ではなく Claude Skill の `/beacon-XXX` を意図しているなら、行頭スラッシュを付ければ drift に引っかからない」 |

**「最も似た現存コマンド」の推定**: `beacon help --json` で取得した command list の各エントリと、drift コマンド句との **Levenshtein 距離** または **prefix 一致** で類推。完全自動推定が困難なら、`beacon help` を実行して候補を出力するよう案内するだけでよい。

### project-stale の場合

`beacon cloud pull` の実行を推奨する 1 行を添える:
```
推奨: `beacon cloud pull` でローカル cache を最新に更新
```

mtime 超過 soft warn の場合は、外部セッションの書き込みがあったか確認した上で同じ pull を推奨。

## Step 3: 構造化テキストで提示

ユーザーに以下の形式で表示する:

```
Beacon drift check 結果 (beacon doctor check #8 / #9 由来)
=========================================================

【Skill ↔ CLI drift】 N 件
  Skill: <skill-name>
    L<line>: `beacon <phrase>`
      推奨: <修正提案 1 行>
      関連: ms-61 SPEC (= CLI ↔ Skill 整合性の forcing function 化) / `entry-writing-principle` CORE doc
  ... (skill 毎にグループ)

【プロジェクトキャッシュずれ】 N 件 / OK
  milestones: local=NN, cloud=MM
  operations: local=NN, cloud=MM
  推奨: beacon cloud pull
  関連: ms-61 SPEC 受入条件 2 / fork worktree の symlink 経路 (ms-67 e-1554)

---
修正は自動実行しません。各 推奨 を読んで該当 Skill markdown を直接編集してください。
編集後は `beacon doctor` で再 check して warn が 0 件になることを確認できます。
```

### Step 3a: 0 件 (= drift なし) の場合

`beacon doctor` の stdout に `skill-cli-drift` も `project-stale` も無ければ、**1 行だけ** 返して終了:

```
Beacon drift check: drift なし。`beacon doctor` の Skill 由来 / cache 由来 warn は 0 件です。
```

ms-61 SPEC 受入条件 5 が満たされている状態のシグナル。

## Step 4: トリガークリア (任意)

Step 3 を提示した後、ユーザーが「全部直す」「順次修正」と指示した場合は、本 Skill は **指示だけ受け取って終了** する。実際の編集はユーザー / 別 Skill に委ねる (= Edit / Write ツールで markdown を直接編集)。

## 制約

- **自動修正禁止** (ms-61 SPEC 設計方針 2): markdown の編集 / PR 起票 / commit を本 Skill は行わない。alert + 人間 / AI 修正ループの分業を守る
- **検出ロジックは `beacon doctor` に集約**: 本 Skill 内で正規表現を独自実装して `~/.claude/skills/` を walk しない。`beacon doctor` 経由でのみ判定する (= 真値源を 1 つに保つ)
- **その他 doctor warn は素通り**: 環境設定系 (PATH / hook / cloud.json / token / dup-id 等) は本 Skill のスコープ外、表示にも含めない (= 役割分離)
- **出力は読み手目線**: drift してる Skill 名・行番号・推奨修正の 3 点セットで揃える、開発者用語の羅列は避ける

## 関連

- 出典 check の実装: `lib/commands.py` の `_doctor_check_skill_cli_drift` / `_doctor_check_project_staleness`
- ms-61 SPEC (= CLI ↔ Skill 整合性の forcing function 化): doc_id `ms-61-spec-cli-skill-整合性の-forcing-function-化-drift-構造防止`
- 関連 task: e-1570 (Skill ↔ CLI check), e-1571 (project staleness check), e-1573 (= 全 Skill 一括スキャン、本 Skill を使って初回 drift を memo doc に記録), e-1574 (= 既存 drift fix、本 Skill の結果に基づいて順次修正)
