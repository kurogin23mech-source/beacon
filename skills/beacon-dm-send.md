---
name: beacon-dm-send
description: 別セッション (別マシンの自分や、別ユーザーのAI) に対話的に DM を送る。受信者選択 (live + healthy filter) → ペイロード入力 → envelope auto-issue → 送信を1フローで完結させる。手書きで起きがちな受信者ミスや --to 忘れ・project跨ぎ忘れを構造的に排除する。
version: 0.1.0
triggers:
  - /beacon-dm-send
  - DMを送る
  - DM 送信
  - DM 送って
  - DMで連絡
  - bus send
  - 別セッションに連絡
  - send dm
---

# Beacon DM Send

> 別セッションに対話的に DM を送る。受信者選択 → 本文入力 → 送信 を 1 つの流れで完結させる。
>
> 今日 (2026-06-09) の dogfood で観測された 4 つの手書きミスを構造的に排除するための Skill:
> (1) 受信者の project_id 間違い / (2) `--to <session_id>` 忘れ / (3) `--in-reply-to` 付き送信時の budget gate 不意打ち / (4) `--payload` の JSON 手書きでクオート崩壊。

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

Bash ツールで実行:
```bash
test -f .beacon/project.json && test -f .beacon/cloud.json && echo "OK" || echo "NO_BEACON_OR_CLOUD"
```
- `NO_BEACON_OR_CLOUD` の場合、「Beacon プロジェクトのルートで実行してください (cloud mode 必須)」と返して終了。

自分の session_id を控えておく (cross-project 跨ぎ判定や、後で `--verify` するときに使う):
```bash
beacon session id
```

自分のプロジェクト ID を控える (Skill 内で `cwd_project_id` と呼ぶ):
```bash
python3 -c "import json,sys; print(json.load(open('.beacon/cloud.json')).get('project_id',''))"
```

## Step 1: 受信者候補の取得 (live + healthy filter)

Bash ツールで実行 (Option C 統合: `--healthy` で「ポーリングで生存確認済み」のみに絞る):
```bash
beacon bus directory --live --healthy --since-min 5 --json
```

**`--healthy` がサーバ側で未対応の場合** (CLI が `unknown option` で exit 2、または stderr に "unrecognized" を返す) は、parallel agent の Option C PR が未マージなので、フラグなしで再試行:
```bash
beacon bus directory --live --since-min 5 --json
```
このフォールバック発生時、ユーザーに 1 行だけ補足:
「(true-heartbeat filter 未対応、live filter のみで listing)」

### Step 1.5: 0 件のときの fallback

`--healthy` で返ってきた sessions 配列が **空** の場合:

```bash
beacon bus directory --live --since-min 5 --json
```
で再取得し、ユーザーに明示警告:
「healthy filter で生存確認できた受信先が 0 件です。以下は live filter のみで listing。送信しても受け取られない可能性があります」

両方とも空なら「現在 listening 中の受信先がありません。相手側で `beacon bus listen` または MCP 接続が必要です」と伝えて終了。

## Step 2: メンバー情報の取得 (best-effort cross-reference)

Bash ツールで:
```bash
beacon member list --json
```

session.actor.email がメンバーの email と一致するなら、その人の email + role を picker 行に添える。一致しないなら machine / agent のみで表示する。member list が空でもエラーにせず無視する。

## Step 3: 候補表示と選択

各 session を以下のフォーマットで表示する (helpers の `render_candidate_line` と同じ規則):

```
1. [machine=DESKTOP-CHG6PAT, agent=DESKTOP-CHG6PAT] session=aa60cc21… healthy (age 3s) → dolphin.orca@gmail.com (editor)
2. [machine=WORKMACHINE] session=dc526151… healthy (age 1s)
3. [machine=mac-mini] session=6d270a08… stale (age 12m) → (member unknown)
```

ユーザーに尋ねる:
```
どの受信者に送りますか?
- 番号 (1, 2, 3, …) でピック
- session_id を直接貼り付け
- cancel で中止
```

選択された session の `session_id` を控える。

## Step 4: cross-project 判定

選択 session の `actor.project_id` (directory 応答に含まれる場合) と Step 0 で取得した `cwd_project_id` を比較する。

両者が異なる場合、ユーザーに明示確認:
```
この受信者は別プロジェクト (project_id=<their>) のセッションです。
cwd のプロジェクト (project_id=<mine>) ではなく、相手のプロジェクトに対して送信します。
- yes: そのプロジェクトに送信 (--project <their>)
- no: 中止
```

`yes` を選んだら `cross_project_id = <their>` を保持。両者が同じプロジェクト、または相手側の project_id が不明な場合は cross_project_id は空 (= cwd デフォルト)。

## Step 5: 本文の入力

ユーザーに本文を尋ねる:
```
本文を入力してください (改行 OK、空行 + Enter で送信):
```

本文を **そのまま文字列として** 保持する。**JSON エスケープは Skill が自動で行う** — ユーザーが `--payload '{"text":"..."}'` 形式で書く必要は無い。

オプションで `--action <name>` を渡す (受信側に auto-execute 権限を与える) かどうか聞く。デフォルトは空 (envelope は T1 で auto-action 無し)。
```
受信側に auto-execute 権限を与える action 名はありますか? (普通は空でEnter)
```

## Step 6: 送信確認

組み立てた argv をユーザーに見せて確認:
```
以下のコマンドで送信します:

  beacon bus send --channel dm --to <session_id> --payload '{"text":"<本文>"}' [--project <id>] [--action <name>]... --json

送信しますか? (yes / edit / cancel)
```

- `yes` → Step 7
- `edit` → Step 5 に戻る
- `cancel` → 中止

## Step 7: 送信実行

Bash ツールで上記コマンドを実行する。JSON 出力 (`--json`) を有効化することで stdout に event_id + delivery + envelope 情報が返るので、結果を読む。

**注意**: `--payload` の値は Python の `json.dumps({"text": "<本文>"}, ensure_ascii=False, separators=(",", ":"))` 相当で組み立てる。`Bash` ツールの引数に渡すとき、シェルに渡る形にしてエスケープに注意 (改行は `\n` に変換される)。実装上は Python heredoc で payload JSON を構築 → 環境変数経由でコマンドに渡すか、シングルクォートで囲んでそのまま渡す。

## Step 8: 結果報告

stdout の JSON を解析し、ユーザーに簡潔に報告:

```
✓ DM 送信完了
  event_id: <event_id>
  delivery: <propose-to-ai / auto-execute / notify-user-only>
  to: <session_id>
  envelope: T1 (auto-issued) / なし
```

### --verify フラグ付き呼び出し (optional)

ユーザーが `/beacon-dm-send --verify` で起動した場合、送信後に受信側の cursor 進行を確認する:

```bash
# 送信から 10 秒待って、相手の cursor が created_at を越えたか peek する
sleep 10
beacon bus listen --once --channel dm --recipient <相手 session_id> --json
```

注: これは MVP では「相手側が auto-ack で消化した」場合のみ正確に検知できる。完全な delivery-receipt は Option A の別タスクなので、ここでは「best-effort peek」と但し書きする。

`--verify` 指定が無い場合は送信後すぐ終了。

## エラー時の挙動

| エラー | 対応 |
|---|---|
| `bus directory` が API エラーで失敗 | エラーメッセージをそのまま提示して終了。`beacon cloud status` の確認を促す |
| 候補 0 件 (live filter でも空) | 「相手の listen が立っていない可能性。MCP 接続 or `bus listen` 起動を案内してください」と返して終了 |
| `bus send` が exit 1 (envelope reject / network failure) | エラーをそのまま提示。`--no-envelope` を試すかどうか聞く |
| `bus send` が exit 1 (server 404) | サーバが古い (envelope 未対応)。`--no-envelope` で再試行を提案 |

## 制約

- このSkill は受信者選択を **対話的に必ず通す**。`session_id` を直接渡しても候補表示は省略しない (= 「いま生きてる相手か」を必ず人間に見せる)。
- `--action` 付き送信は **明示確認後のみ**。デフォルトでは渡さない。
- 自分自身の session_id への送信は無意味なので警告する (受信側が自分 = `beacon session id` の出力と一致する場合)。
- cross-project 送信は **常に明示確認**。silently 飛ばさない。
- Budget gate (`--in-reply-to` 付き) は通常の `/beacon-dm-send` では発火しない。返信は `/beacon-dm-reply` を使うこと。

## 関連 Skill

- `/beacon-dm-reply` — 受信した DM への返信。budget gate を自動 handle、`--in-reply-to` 自動付与。
- `/beacon-bus-armed` — 自律 DM 応答モード (Monitor で listen を armed)。
