---
name: beacon-member
description: プロジェクトメンバーの操作 (= 招待 / 役割変更 / 削除) を AI が対話で行う Skill。3 subaction (invite / role-change / remove) を 1 Skill に統合し、owner 変更や remove は二段確認で誤発火を構造的に防ぐ。
version: 0.1.0
triggers:
  - /beacon-member
  - member 招待
  - メンバー招待
  - role 変更
  - 役割変更
  - member 削除
  - invite member
---

# Beacon Member

> プロジェクトメンバー操作 (= 招待 / 役割変更 / 削除) を 1 つの対話 Skill に統合する。CLI と Web UI には個別コマンドが揃っているが、AI が「招待してください」と頼まれた時に裸の `beacon member invite` を叩くと、招待先 email 取り違え / role 誤指定 (= editor のつもりが owner) / 削除誤発火 のリスクがある。
>
> 本 Skill は draft 表示 + 確認を必ず通し、特に **owner 変更** と **remove** は二段確認で誤操作を構造的にガードする。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

### 守ること

1. **読み手目線 1 行から始める**: 「何が嬉しいか」「何が困るか」をユーザー体験の言葉で。技術用語ではなく価値で書く
2. **横文字 3 段階**:
   - そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP`)
   - 初出時に日本語注: 技術概念 (`role (= 役割)` / `invitation (= 招待状)` / `owner / editor / viewer`)
   - 日本語化が望ましい: 一般概念 (configure → 設定 / receiver → 受信側 / audit → 監査)
3. **ID 参照には文脈**: `e-XXXX` / `UC?` / `ms-XX` / `member-XX` は初出に必ず『何の話か』1 行添える
4. **尻切れトンボ禁止**: 主語と述語を省略しない、論理関係を明示

### Self-review (生成直後に必ず)

文章を書いた直後、自分で読み返して以下を自問:
- (a) 読み手 (非開発者を含む) は 1 度読んで意味が取れるか?
- (b) 一般概念の横文字 (configure / receiver / audit 等) が残ってないか?
- (c) ID 参照に『何の話か』1 行添えたか?
- 違反していたら書き直し。enforce ではないが必須の self-check。

### 詳細

詳しい原則と例は CORE doc `entry-writing-principle` (doc_id `F3ZkqT0pKS6JpR8dn70n`) 参照。

---

## Role の定義 (= 横文字 3 段階に基づく注解)

このプロジェクトの role (= 役割) は 3 段階:

- **owner** (= 管理者): プロジェクト設定変更 / member 招待 / role 変更 / member 削除 / 他 owner の昇降格が可能
- **editor** (= 編集者): commit / task / doc / Operation / DM の作成・変更が可能、設定変更とメンバー操作は不可
- **viewer** (= 閲覧者): 読み取りのみ、書き込み一切不可 (= 監査担当 / 外部報告閲覧者向け)

各操作のステップで role の意味を 1 行ずつ補足表示し、招待者・操作者が間違えないようにする。

---

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` の場合、この Skill は何もせず終了する。
- 全 subaction で cloud mode 必須 (= `.beacon/cloud.json`)。無ければ:
  ```
  member 操作は cloud mode (= cloud sync 有効) が必須です。
    1. beacon auth login
    2. beacon cloud setup
  を実行してから再試行してください。
  ```

自分の role を確認する (= 後段の権限ガードに使う):
```bash
beacon member whoami --json
```

返ってきた role を `self_role` として控える。owner でない場合、role-change / remove の subaction は権限不足で実行できないため、subaction 解釈時に親切なエラーを返す。

---

## Step 0: subaction の判定

ユーザーが `/beacon-member <subaction> [...args]` で起動した場合、第 1 引数を subaction として採用。引数なし or 不明 subaction の場合は picker:

```
どの操作を実行しますか?
  1. invite        新しいメンバーを招待する
  2. role-change   既存メンバーの role (= 役割) を変更する
  3. remove        メンバーを削除する (= 注意: 取り消せません)

選択 (番号 or subaction 名, cancel で中止):
```

判定された subaction に対応する Step (1〜3) へ分岐する。

---

## Step 1: invite (= 新規メンバー招待)

### 1-a: 招待先と role の引き出し

ユーザーに以下を尋ねる:

```
新しいメンバーを招待します。以下を教えてください:

  招待先 email (= 招待状を送る宛先):
  付与する role:
    - viewer  読み取り専用 (= 外部報告 / 監査)
    - editor  通常の作業者 (= commit / task / DM 可)
    - owner   管理者 (= 設定変更 / member 操作可、慎重に)
  招待メッセージ (= 受信者に届く 1〜3 行、空 Enter で default):
```

### 1-b: 重複チェック

入力された email が既にメンバーまたは pending invitation (= 招待状送付済) でないか確認:

```bash
beacon member list --json
beacon member invitation list --json
```

重複していたら以下を返す:

```
警告: <email> は既に <既存 role / pending 状態> です。
  - 既メンバー: role を変えるなら /beacon-member role-change
  - pending 招待: 取り消すなら beacon member invitation cancel <invitation-id>
```

### 1-c: owner 招待時の追加確認 (= 重 role の構造的ガード)

`role == owner` を選択された場合、追加 step:

```
警告: owner 招待は重い操作です。
  - 被招待者は project の設定変更 / 他メンバー削除 / 全 owner の昇降格 が可能になります
  - 通常の作業者には editor が適切です

本当に owner として招待しますか? (yes / change-to-editor / cancel):
```

`change-to-editor` 選択で role を editor に自動変更して進む (= 親切経路)。

### 1-d: draft 表示

```
以下の招待を送ります:

  to:       <email>
  role:     <role>
  message:  "<message>"
  expires:  <default 7 日後>

送信しますか? (yes / edit / cancel)
```

### 1-e: 実行

```bash
beacon member invite <email> --role <role> --message "<message>" --json
```

`--message` が長い場合は **single quote または quoted heredoc** で渡す (= double quote + backtick の zsh 展開を防ぐ)。

### 1-f: 結果報告

```
✓ <email> に招待を送りました
  role:           <role>
  invitation_id:  inv-XXXX
  invitation URL: <URL>

招待 URL を直接 (= Slack / DM / メール等で) 共有してください。
被招待者が accept すると member list に追加されます。
取り消すには: beacon member invitation cancel inv-XXXX
```

---

## Step 2: role-change (= 既存メンバーの役割変更)

### 2-a: 権限ガード

`self_role != owner` の場合、明示エラーで終了:

```
role-change は owner のみ実行できます。
あなたの role: <self_role>。owner に依頼してください。
現在の owner 一覧:
  - <owner email 一覧>
```

### 2-b: 対象 member の picker

```bash
beacon member list --json
```

```
プロジェクトのメンバー:
  1. alice@example.com  (owner,  参加: 2026-05-10)
  2. bob@example.com    (editor, 参加: 2026-05-15)
  3. carol@example.com  (viewer, 参加: 2026-06-01)

どのメンバーの role を変えますか? (番号 or email, cancel で中止):
```

### 2-c: 新 role の指定

```
<email> の現在の role は <現 role> です。
新しい role:
  - viewer  読み取り専用
  - editor  通常の作業者
  - owner   管理者 (= 慎重に)
```

### 2-d: owner 昇格 / 降格 の追加ガード (= 二段確認)

#### owner 降格 (= 自分以外の owner を editor/viewer に下げる)

```
警告: <email> の role を owner → <new-role> に下げます。
  - <email> は今後、設定変更 / member 操作 ができなくなります
  - 現在の owner 数: N 人 → 操作後: M 人
  - owner が 0 人になる操作は防がれます (= サーバ側 reject)

確認のため、対象の email を完全一致で入力してください:
```

#### owner 昇格 (= editor/viewer を owner に上げる)

```
警告: <email> の role を <現 role> → owner に上げます。
  - <email> は今後、設定変更 / 他 owner の昇降格 / member 削除 が可能になります
  - 慎重に: owner は project に対して強い権限を持ちます

本当に owner に昇格しますか? (yes / cancel)
```

email 完全一致 (降格時) または明示 yes (昇格時) が無ければ中止。

### 2-e: 通常 role 変更 (editor ↔ viewer) の draft 表示

owner が絡まない role 変更は単段確認:

```
以下を実行します:

  beacon member role change <email> --role <new-role>

  <email>: <現 role> → <new-role>

実行しますか? (yes / cancel)
```

### 2-f: 実行

```bash
beacon member role change <email> --role <new-role> --json
```

### 2-g: 結果報告

```
✓ <email> の role を <現 role> → <new-role> に変更しました
  通知: <email> に role 変更通知が届きます
  audit log: beacon member log show <email>
```

---

## Step 3: remove (= メンバー削除、二段確認必須)

### 3-a: 権限ガード

`self_role != owner` の場合、role-change と同じエラーで終了。

### 3-b: 対象 member の picker

Step 2-b と同じ。

### 3-c: 影響範囲の表示 (= 削除前に必ず)

削除対象が **自分自身** の場合は別経路:

```
警告: 自分自身を remove しようとしています。
このプロジェクトから抜けるには /beacon-project leave を使ってください (= leave は自発的な離脱、remove は他者による削除)。
```

別メンバーの場合:

```
警告: <email> (role=<role>) をプロジェクトから remove します。

これにより以下が起きます:
  - <email> は今後、project の全コンテンツ (= task / doc / DM / Operation) にアクセスできなくなります
  - <email> が過去に行った commit / task 変更 / doc 編集 は audit 上は残ります (= 透明性のため削除しない)
  - 再加入には新規招待が必要です
  - 取り消し不可: remove は revert できません

進める場合は次の確認に進みます。
```

### 3-d: 二段確認 (= 削除対象の email 完全一致 + 最終 yes)

```
削除対象の email を完全一致で入力してください (= 誤発火防止):
```

ユーザーが email を完全一致で入力した場合のみ次に進む:

```
最終確認: <email> を本当に remove しますか?

  - revert 不可
  - audit log に削除実行者 (= あなたの email) が記録されます

(yes / cancel)
```

### 3-e: owner remove の追加ガード

対象が **owner** の場合、上記に加えて:

```
警告: <email> は現在 owner です。
remove すると owner 数が <N> → <M> になります。
project に owner が 0 人になる場合、サーバ側で reject されます。

先に役割変更 (= editor に降格してから remove) を推奨しますか?
  - yes → /beacon-member role-change に誘導して中断
  - そのまま remove → 進む
  - cancel → 中止
```

### 3-f: 実行

```bash
beacon member remove <email> --confirm "<email>" --json
```

`--confirm` flag に email を渡し、CLI 側でも一致検証 (= 二重防御)。

### 3-g: 結果報告

```
✓ <email> を remove しました
  audit log:    beacon member log show <email>
  通知:        <email> に削除通知 + 残メンバー全員に通知が届きます
  再招待:      /beacon-member invite で再度招待可能 (= 新規 member として扱われます)
```

---

## 共通: エラーハンドリング

| エラー | 対処 |
|---|---|
| email が不正な形式 | 「email 形式が不正です: <input>」と返し、再入力を促す |
| owner 0 人になる操作 | 「この操作で owner が 0 人になります。先に別 member を owner に昇格してください」と返して中止 |
| 招待先が既存メンバー | Step 1-b で重複チェック済、それ以外で検知したらサーバエラーをそのまま提示 |
| pending invitation 期限切れ | 「招待が期限切れです。再招待を実行します (= 新規 invite に誘導)」 |
| cloud 未認証 | 前提条件チェックで弾く |
| 権限不足 (= owner でないのに role-change / remove を試行) | Step 2-a / 3-a で弾く |

---

## 制約

- **二段確認 (= email 完全一致 + 最終 yes)** は以下で必須:
  - owner → 他 role への降格 (Step 2-d)
  - member remove (Step 3-d, 3-e)
- AI は role を勝手に推奨しない (= 「owner がよさそうです」のような誘導禁止)。ユーザーが選んだ role をそのまま採用し、必要なら警告だけ出す。
- 招待 URL は機密扱い (= URL を持つ者は accept できる)。Skill は URL を生成して提示するだけで、外部送信 (= Slack 連携等) は別 Skill に委ねる。
- `--message` / `--confirm` の長文は **single quote または quoted heredoc** で渡す (= zsh の double-quote backtick 展開を避ける)。

---

## 関連 Skill (= 役割分担)

- `/beacon-trek` の `member-invite` subaction — **trek 単位** のメンバー招待 (= trek = 分散協奏作業領域)。本 Skill は **project 単位** のメンバー操作。両者は重ならない (project member は project に紐づき、trek member は trek に紐づく)。
- `/beacon-cloud` — cloud sync の操作 (= push / pull / off / open)。member 操作の前提となる cloud mode の入口。
- `/beacon-dm-send` — メンバー間のメッセージ送信。役割変更 / 削除の事前合意取りに使う。
