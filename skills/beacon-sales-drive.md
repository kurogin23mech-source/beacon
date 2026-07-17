---
name: beacon-sales-drive
profession: sales
description: 商談に紐づく資料を Google ドライブに保管し、リンクを商談の記録として残す。資料の生成はせず保管・参照に徹する。「ドライブに格納」「資料を保管」「Driveに上げて」等で起動。
version: 1.0.0
triggers:
  - /beacon-sales-drive
  - ドライブに格納
  - 資料を保管
  - Driveに上げて
  - 資料格納
---

# Beacon Sales Drive

> 営業 (profession=sales) プロジェクトで、商談 (Opportunity) に紐づく資料
> (提案書・見積・議事録など、既に手元にあるファイル) を Google ドライブ (= クラウド上の
> 保管場所) に保管し、商談から参照できるようにする。task e-3360。
> **資料の自動生成はしない** (保管と参照のみ、生成はスコープ外)。

## 文章の書き方 (Beacon 全体の哲学)

ユーザーへの確認・報告は、相手が 1 度読んで意味が取れる自然な日本語で書く。
社内の略語・横文字を持ち込まない。フォルダ名やリンクは実物をそのまま提示する。

## 前提条件チェック

Bash ツールで実行し、営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && BEACON_JSON=1 python3 "$(beacon _lib-path)/commands.py" account_list >/dev/null 2>&1 && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` の場合 (= 営業テンプレートでないプロジェクト)、この Skill は「営業プロジェクトでのみ使えます」と伝えて終了する。cloud mode で `project.json` を直接読めない場合は `beacon opportunity list` が動くかで代替判定してよい。

以降、`$ROOT` は `beacon-find-root` の出力。内部コマンド (`opportunity_*`) は
ユーザー向け CLI 動詞ではないので `python3 "$(beacon _lib-path)/commands.py" <cmd>` で呼ぶ。

> 補足: 手元ファイルを Google ドライブに保管する流れは `creatron-invoice` Skill
> でも使っている (請求書 PDF を Drive に上げてリンクを返す)。同じ保管パターンを
> 再利用できるが、この Skill は営業商談向けに単体で完結させる。

## Step 1: 対象商談の特定

Bash ツールで商談一覧を取得し、ユーザーにどの商談の資料かを確認する:

```bash
beacon opportunity list
```

引数で `/beacon-sales-drive opp-3` のように商談 ID が渡されていればそれを採用。
無ければ一覧を提示し「どの商談の資料ですか？」と 1 問だけ聞く。対象を `$OPP` として保持。

## Step 2: 保管する資料の特定

保管したい資料を確認する。ローカルのファイルパス (例: `~/Downloads/提案書.pdf`) を
ユーザーに聞く。複数あればまとめて受け取る。**資料はユーザーの手元に既にあるもの**を
保管する前提で、この Skill 内で資料を作り起こすことはしない。

## Step 3: 保管先フォルダの特定

商談 / 顧客ごとに整理するため、保管先フォルダを決める。既存の運用フォルダ
(例: `営業/{顧客名}/`) があるかを Google ドライブで探す:

```
mcp__google-drive__listFolder で親フォルダの中を一覧し、対象顧客のフォルダを探す
```

- 既存フォルダが見つかれば、それを使ってよいかユーザーに確認する。
- 無ければ `mcp__google-drive__createFolder` で顧客 / 商談用フォルダを作る
  (作成先と名前をユーザーに確認してから)。

決めた保管先フォルダ ID を `$FOLDER` として保持。

## Step 4: アップロード (保管)

Google ドライブへファイルを保管する:

```
mcp__google-drive__uploadFile で $FOLDER 配下に対象ファイルをアップロードする
```

複数ファイルはそれぞれアップロードし、返ってきたファイル ID と表示リンクを控える。

## Step 5: 参照リンクの取得 (社外共有はユーザー承認後)

保管したファイルの参照 URL を得る:

```
mcp__google-drive__shareFile 等で共有リンク / webViewLink を取得する
```

**既定は社内のみ** (組織内で見られる範囲)。社外 (顧客など組織外) への共有リンク発行が
必要な場合は、**その旨と対象を必ずユーザーに確認し、承認を得てから**発行する。
確認なしに社外公開リンクを作らない。

## Step 6: 活動記録 (証跡) を必ず残す

保管できたら、対象商談に「どの資料をどこに保管したか」を活動記録として残す。
これを飛ばすと後で資料の在り処を辿れなくなるため必須:

```bash
BEACON_OPP_ID="$OPP" BEACON_ACTIVITY_DESC="[資料] <ファイル名> → <Driveリンク>" \
  python3 "$(beacon _lib-path)/commands.py" opportunity_activity
```

ファイルが複数なら、それぞれ (または 1 行にまとめて) 記録する。

> v1 補足: 現状 activity は「予定 (todo)」型で記録される。起きた事実 (event) 型の
> 記録は今後の精緻化対象 (description に [資料] を明記して代替)。

## Step 7: 結果報告

ユーザーに簡潔に報告 (保管先リンクを提示):

```
📁 資料を保管しました
  <ファイル名> → <Driveリンク>
  保管先: <フォルダ名>
  商談 [OPP] に活動記録を残しました。
```

保管を止めた場合 (ユーザーが中止 / 社外共有を承認しなかった等) は、
保管しなかった旨と理由を報告する。

## 制約

- **資料の自動生成はしない** (保管・参照のみ、生成はこの Skill のスコープ外)。
- **社外共有リンクの発行はユーザー承認後** (既定は社内のみ、無断で社外公開しない)。
- **保管できたら必ず Step 6 の活動記録を残す** (証跡を欠かさない)。
- `project.json` を直接書き換えない。内部コマンド / CLI 経由のみ。
- 複数 Google アカウントに注意 (営業用の Drive に保管する)。どのアカウントの
  Drive に入るかの精緻な選択 (アカウント指定) は今後の課題 (= 回しながら精緻化)。
