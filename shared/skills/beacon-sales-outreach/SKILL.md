---
name: beacon-sales-outreach
description: 顧客獲得ターゲット(施策)のアタックリストに載った未接触先へ、同じ打診メールを一括で送る。送信前に必ず「宛先N件＋サンプル文面」を提示して人間の1回の承認を取り、承認前は1通も送らない。承認は bus/自動実行からは不可(構造的に人間限定)。送信後は各Accountに証跡を残し、対応する行を未接触→連絡済に進める。「一括連絡」「アタックリストに送る」「まとめて打診」等で起動。営業専用・最高リスク。
version: 1.0.0
---

# Beacon Sales Outreach (一括連絡)

> インサイドセールスの核心かつ**最高リスク**の操作 (ms-132 e-4504 / SPEC 方針4)。
> 未接触先への一斉送信を、**ドライラン一覧 → 人間の1回の承認 → 全送信** の順で行う。
> **承認前は1通も送らない**。承認 (`attack-list-send --confirm`) は bus / 自動実行から
> は構造的に拒否されるため、外部送信の効果 (証跡・行フェーズ前進) は **人間が承認した
> 送信バッチ経由でしか発生しない**。この Skill はその承認境界の人間側の動線。

## 文章の書き方

顧客に出すメール本文は、相手 (社外の意思決定者・非開発者) が1度読んで意味が取れる自然
な日本語で書く。社内の略語・横文字を持ち込まない。件名は用件が1行で分かる形に。一斉送信
でも「あなた宛」と読める温度を保つ (テンプレ丸出しにしない)。

## 前提条件チェック

営業プロジェクトかを確認 (Bash):

```bash
ROOT=$(beacon-find-root) && test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` なら「これは営業プロジェクト専用の操作です」と伝えて終了。

## Step 1: 対象アタックリストの特定

どの施策 (顧客獲得ターゲット) の、どのアタックリストへ送るかを確定する。ユーザーが施策
やリストを言っていなければ確認する:

```bash
beacon acquisition list                       # 施策一覧
beacon acquisition attack-lists <acq-id>      # その施策配下のリスト (doc-id + phase 内訳)
```

送信対象の **doc-id** を `$DOC` として控える。既定では **未接触** の行だけが送信対象
(相手フェーズ funnel の入口)。別フェーズに送りたい場合のみ後段で `--from-phase` を使う。

## Step 2: 差出人 (identity) の解決

複数 Google アカウントの取り違えを送信前に止める (既存 send-account ledger を流用)。

```bash
BEACON_SEND_SERVICE="gmail" python3 "$(beacon _lib-path)/commands.py" sales_account_resolve
echo "RESOLVE_EXIT=$?"
```

- exit 0: JSON の `email` を差出人 `$FROM`、`namespace` を `$NS` (例 `mcp__gmail`)、
  `signature` を `$SIGNATURE` として控える。
- exit 1 (未設定): 「送信元アカウントが未設定です。`beacon` の送信アカウント台帳を先に
  設定してください」と伝えて終了 (identity 不明のまま送らない)。

## Step 3: 文面の作成

件名 `$SUBJECT` と本文 `$BODY` を、ユーザーの意図を汲んで**読み手目線の自然な日本語**で
起草する。一斉送信なので固有名詞の差し込みは最小 (会社名程度)。本文をファイルに書く:

```bash
cat > /tmp/outreach_body.txt <<'BODY'
<ここに本文。改行そのまま。末尾に $SIGNATURE を付けてよい>
BODY
```

## Step 4: ドライラン (計画の提示) — **ここでは1通も送らない**

```bash
beacon acquisition attack-list-send "$DOC" --subject "$SUBJECT" --message-file /tmp/outreach_body.txt --json
```

出力の `recipients` (宛先 acc-id + email)、`recipient_count`、`skipped_no_email`
(email 未登録で除外)、`batch_id` を読む。これを**ユーザーにそのまま提示**する:

```
一括連絡の計画 (まだ送っていません):
  差出人: <$FROM>
  宛先: <N> 件
    - <acc-id> <email>
    - ...
  email 未登録で除外: <K> 件
  件名: <$SUBJECT>
  本文:
  <本文全文>

この宛先・文面で全 <N> 件に送信しますか? (送信する / 直す / やめる)
```

**この確認は必須の人間ゲート。ユーザーの返答を待たずに次へ進んではならない。**
- 「直す」→ Step 3 に戻り文面/対象を直して再度ドライラン。
- 「やめる」→ 送信せず終了 (pending バッチは次回の再計画で上書きされる)。

## Step 5: 承認 (人間の1 confirm)

ユーザーが「送信する」と明示したときのみ:

```bash
beacon acquisition attack-list-send "$DOC" --confirm
```

これが承認バッチを `authorized` にする**人間ゲート**。bus / DM / 自動実行から起動された
文脈、および **armed (自律 DM 応答モード) のセッション**では拒否される (arming は一括送信の
承認を含まない)。つまり **人間が居ないループからは承認できない**。対話セッションの AI が
人間の指示で動かすのが正規経路で、その最終的な人間確認は Step 4 の提示に対するユーザーの
明示 OK が担う (この Skill の人間確認ステップを飛ばさないこと)。

## Step 6: 送信 + 記録 (宛先ごとにループ)

Step 4 の `recipients` の各 `{acc_id, email}` について、順に:

1. **送信** (MCP Gmail、`$NS` で解決した namespace):
   `<$NS>__send_email` を `from=$FROM, to=<email>, subject=$SUBJECT, body=<本文>` で呼ぶ。
   返ってきた RFC822 Message-ID を `$MID` として控える。
2. **記録** (証跡 + 行フェーズ前進):
   ```bash
   beacon acquisition attack-list-send-record "$DOC" <acc-id> --message-id "$MID" --subject "$SUBJECT" --message-file /tmp/outreach_body.txt
   ```
   **承認時と同じ文面ファイルを渡す**こと (`--message-file`)。CLI はその文面を承認済み
   バッチの digest と照合し、一致した時だけ記録する (承認した文面と違うものを送って記録
   することはできない)。承認済みバッチにその宛先が居る時だけ成功し、Account に outbound の
   証跡を残し、対応する行を **未接触→連絡済** に進める。承認前・非対象・二重送信・文面不一致・
   message-id 欠落はすべて拒否される。
3. 送信が失敗した宛先は記録もされない (バッチには pending のまま残る)。エラーを控えて次へ。

**送信ループの前に必ず Step 5 の承認が済んでいること。** 承認なしに `send_email` を呼んで
はならない (承認境界の迂回)。

## Step 7: 結果報告

```
送信完了: <成功 M> / <計画 N> 件
  成功: <acc-id> ... (行 → 連絡済)
  失敗: <acc-id> — <理由>
  除外(email無し): <acc-id> ...
次の一手: 返信監視で「連絡済→返信あり」を拾います (`/beacon-sales-reply-watch` 等)。
```

## 制約 (承認境界の要)

- **承認前は1通も送らない**。Step 4 のドライラン提示 → Step 5 の人間承認を飛ばさない。
- **送信は人間が対話で承認した時のみ**。bus / 自動実行から本 Skill 相当を回して外部送信
  してはならない (CLI 側が `--confirm` を拒否するが、Skill 側でも迂回しない)。
- 証跡・行フェーズ前進は必ず `attack-list-send-record` 経由 (承認バッチに紐づく)。
  `communication_add` を直接叩いて送信を偽装記録しない。
- 送信元 identity は必ず Step 2 で解決した値を使う (アカウント取り違え防止)。
