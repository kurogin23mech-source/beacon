---
name: beacon-sales-card
profession: sales
description: 名刺の画像から会社・氏名・役職・連絡先を読み取り、顧客(Account)と担当者(Contact)として Beacon に起票する。スマホからのリモートモード利用を想定。「名刺取り込み」「名刺登録」等で起動。
version: 1.0.0
triggers:
  - /beacon-sales-card
  - 名刺取り込み
  - 名刺登録
  - 名刺
  - business card
---

# Beacon Sales Card

> 営業 (profession=sales) プロジェクトで、もらった名刺を撮って取り込み、
> 顧客 (Account) と担当者 (Contact) を起票する。営業フローの入口。
> ms-107 e-3356。外出先のスマホ (リモートモード = 手元の端末から Beacon を操作) からの利用を想定。

## 文章の書き方 (Beacon 全体の哲学)

起票する会社名・氏名・役職は、名刺に書かれた通りに正確に写す。読み取りが曖昧な
ところは勝手に補わず、ユーザーに確認する。社内の略語・横文字を持ち込まない。

## 前提条件チェック

Bash ツールで実行し、営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" account_list >/dev/null 2>&1 && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` の場合 (= 営業テンプレートでないプロジェクト)、この Skill は「営業プロジェクトでのみ使えます」と伝えて終了する。cloud mode で `project.json` を直接読めない場合は `beacon account list` が動くかで代替判定してよい。

以降、`$ROOT` は `beacon-find-root` の出力。以下のフローで使うのはユーザー向け CLI 動詞 (`beacon account ...`) のみ。

## Step 1: 名刺画像の取得と読み取り

ユーザーがリモートモード (= 外出先の端末) から送ってきた名刺の画像を、
モデル自身が **画像から直接読み取る** (vision)。外部 OCR ツールは使わない。

抽出する項目 (名刺に載っていて読み取れるものだけ):

- 会社名
- 氏名
- 役職
- メールアドレス
- 電話番号
- 住所

画像がまだ添付されていなければ、「名刺の写真を送ってください」と促して待つ。
読み取った内容は次のステップで使うため手元に保持する。

## Step 2: 会社 (Account) の照合

まず既存の顧客一覧を引き、同じ会社がすでに登録済みかを確認する:

```bash
beacon account list
```

- 同じ会社が既にあれば、その `acc-id` を再利用する (重複起票を避ける)。
- 無ければ新規に起票し、出力に出る `acc-id` を控える:

```bash
beacon account add "<会社名>"
```

読み取った会社名の表記が既存とわずかに違う (略称・法人格の有無など) 場合は、
同じ会社かどうかをユーザーに一度確認してから決める。

## Step 3: 担当者 (Contact) の起票

控えた `acc-id` に対し、名刺の担当者を追加する。役職・メールは
読み取れたものだけ渡す (無ければそのオプションを省く):

```bash
beacon account contact <acc-id> "<氏名>" --role "<役職>" --email "<メール>"
```

電話番号や住所も読み取れていれば、報告時にユーザーへ伝える
(現行 CLI の contact 起票で扱う主項目は氏名・役職・メール)。

## Step 4: 確認・報告

起票した会社と担当者をユーザーに提示する:

```
名刺を取り込みました:
  会社 (Account): [会社名] ([acc-id]、新規 / 既存)
  担当者 (Contact): [氏名] / [役職]
    メール: [メール]
    電話:   [電話]  (読み取れた場合)
```

読み取りが曖昧なフィールド (かすれ・手書き・判読困難など) があれば、
登録前に **一度だけ**「これで合っていますか？」と確認する。何度も聞き返さない。
ユーザーが直したら、その値で Step 2〜3 をやり直す。

## 制約

- **読み取りが不確実な項目は、登録前にユーザーに確認する** (勝手に補完しない)。
- 画像が無ければ登録に進まず、まず名刺の写真を促す。
- 会社は起票前に既存を照合し、重複起票を避ける。
- `project.json` を直接書き換えない。`beacon account` CLI 経由のみ。
- 会社名・氏名・役職は名刺の表記通りに正確に写す (社内略語を持ち込まない)。
