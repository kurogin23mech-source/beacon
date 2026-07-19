---
name: beacon-sales-dossier
profession: sales
description: 顧客(Account)について分かったこと（組織・意思決定プロセス・課題・予算サイクル・キーパーソン・嗜好など）を、面談やり取りのたびに時系列で顧客ドキュメントに積み上げて資産化する。閲覧と追記の両対応。「顧客ドキュメント」「顧客について記録」「dossier」「この顧客の情報まとめて」等で起動。
version: 1.0.0
triggers:
  - /beacon-sales-dossier
  - 顧客ドキュメント
  - 顧客ドキュメントを見せて
  - 顧客について記録
  - この顧客の情報
  - dossier
  - 顧客プロファイル
  - 顧客資産
---

# Beacon Sales Dossier (顧客ドキュメント = 顧客を資産化する累積知識)

> 営業 (profession=sales) プロジェクトで、**顧客(Account)について分かったこと**を
> 時系列で 1 本の顧客ドキュメントに積み上げる。回を重ねるほど分厚くなり、次の一手の
> 質が上がる「顧客の資産」になる。ms-106 e-3550。
>
> **証跡(Communication)とは別物**: Communication は「何が起きたか」のイベントログ
> (= 営業の Commit)。顧客ドキュメントは、そこから蒸留した「顧客について何が分かって
> いるか」の累積プロファイル。開発 Beacon の SPEC / 判断軌跡に相当する、target に
> 紐づく累積知識 (ms-109 の target-class 概念、開発の MS は SPEC を積む・営業の
> Account は dossier を積む)。
>
> **追記は append-only**: 既存の記述を書き換えず、日時付きの新セクションを足すだけ。
> データ不変性 (data-immutability) の原則と整合し、いつ何が分かったかを辿れる。

## 文章の書き方 (Beacon 全体の哲学)

顧客ドキュメントに書く知見は、後で読む人 (非開発者を含む) が 1 度で掴める自然な
日本語で書く。社内略語を持ち込まない。「誰が」「何を」「なぜ」が分かる形にする。

## 前提条件チェック

Bash ツールで営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" \
  && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` の場合、「営業プロジェクトでのみ使えます」と伝えて終了する。cloud mode で
`project.json` を直接読めない場合は `beacon account list` が動くかで代替判定してよい。

## Step 1: 対象顧客(Account)の特定

ユーザーの発話や直近の文脈から、どの顧客の dossier かを決める。

- 引数や発話に `acc-XX` があればそれを使う。
- 顧客名しか無い場合は `beacon account list --json` で名前照合して `acc-id` を確定する。
  複数候補や曖昧なときはユーザーに確認する (誤った顧客に知見を混ぜない)。
- 対象が特定できない場合はユーザーに「どの顧客ですか？」と尋ねる。

以降、確定した顧客を `$ACC`、その表示名を `$ACC_NAME` と呼ぶ。

## Step 2: 顧客ドキュメントの所在確認 (無ければ作成を提案)

各顧客につき正規の顧客ドキュメントは **1 本**。固定 ID `dossier-$ACC` で addressable にする。

```bash
beacon doc list --account "$ACC" --scope spec --json
```

- `dossier-$ACC` が既にあれば、それが対象。
- 無ければ「まだ $ACC_NAME の顧客ドキュメントがありません。作成しますか？」と提案し、
  承認されたら Step 4 の「新規作成」経路へ進む。

## Step 3: 閲覧 (現在の dossier を見せる)

対象 dossier があれば内容を取得して提示する:

```bash
beacon doc show dossier-$ACC
```

frontmatter を除いた本文を、読みやすく要約 or そのまま提示する。ユーザーの意図が
「見たいだけ」ならここで完了し、Step 6 (次の一手) へ。

## Step 4: 追記 (新しく分かった知見を日時付きで足す) — append-only

ユーザーが「これを記録して」「今日分かったことを足して」等と言った場合、または面談の
振り返り (beacon-sales-meeting-wrap) から知見が渡された場合、**既存本文を保持したまま**
日時付きの新セクションを末尾に足す。

1. 追記する知見をユーザーの言葉から 1〜数行にまとめる (何が分かったか。組織構造 /
   意思決定プロセス / 課題 / 予算サイクル / キーパーソン / 嗜好 など)。
2. 既存本文を取得 (Step 3 の `doc show` 結果を再利用)。**無い場合 (新規)** は
   1 行目に見出し `# 顧客ドキュメント: $ACC_NAME` を置いて始める (この h1 見出しが
   一覧表示のタイトルになる。無いと doc_id がそのまま出る)。
3. 新セクションを組み立てる (今日の日付は Bash で `date +%Y-%m-%d`):
   ```
   ## <YYYY-MM-DD>

   <知見の本文 (1〜数行)>
   ```
4. **既存本文 + 空行 + 新セクション** を全体として書き戻す (既存部分は一切書き換えない。
   新規なら「h1 見出し + 空行 + 最初の日時セクション」):

   ```bash
   # 新規作成のとき
   beacon doc add --id dossier-$ACC --scope spec --account "$ACC" \
     --content "$NEW_FULL_CONTENT" \
     --title "顧客ドキュメント: $ACC_NAME"

   # 既存に追記するとき (content は「既存本文 + 新セクション」)
   beacon doc update dossier-$ACC --account "$ACC" \
     --content "$NEW_FULL_CONTENT"
   ```

   本文は長くなりがちなので、`--content` に直接渡すか、一時ファイルに書いて
   `--stdin` で渡す (`... doc add --id dossier-$ACC ... --stdin < /path/to/file`)。

**厳守**: 既存セクションの文言を編集・削除しない。訂正が必要な場合も、古い記述は残し、
新しい日時セクションに「訂正: 〜」と追記する (履歴を辿れる形を保つ)。

## Step 5: 記録の確認

追記/作成が成功したら、足した日時セクションの要約を 1〜2 行でユーザーに返す。
「$ACC_NAME の顧客ドキュメントに『〜』を追記しました (dossier-$ACC)」。

## Step 6: 次の一手 (target を前進させる)

> Beacon 行動原則: どの操作も最後に「この target を次に前進させる一手」を提示する
> (CORE doc `target-advancement-frame`)。顧客ドキュメントの目的は、次の商談を前へ
> 進めることにある。

閲覧/追記した内容を踏まえ、**この顧客について次に確かめるべきこと・次の一手**を 1〜2 個
offer する。例:

- 「意思決定者がまだ不明です。次回の面談でキーパーソンを確認しますか？」
- 「予算サイクルが 3 月締めと分かりました。進行中の商談 opp-X の遷移日をそこに合わせて
   引き直しますか？」
- 「課題が明確になったので、提案準備フェーズの企画に反映できます。商談を前に進めますか？」

実行は自動でなく offer。外向きの連絡や商談フェーズの前進は、それぞれの Skill
(beacon-sales-email / beacon-sales-schedule / beacon-sales-cockpit 等) に橋渡しする。

## 自動蓄積との関係 (e-3747 で配線)

面談の振り返り (beacon-sales-meeting-wrap) や off-channel やり取りの記録
(beacon-sales-communication) で新しい顧客知見が判明したとき、それらの Skill が
「この記録に新しい顧客知見があるか」を判定し、あれば本 Skill の追記経路を呼んで
顧客ドキュメントに足す。その配線 (hook / Skill チェーン) は e-3747 の範囲。

## 制約

- **append-only**: 既存記述の書き換え・削除をしない (data-immutability と整合)。
- **顧客ごとに 1 本**: 固定 ID `dossier-<acc-id>`。同じ顧客に複数の dossier を作らない。
- **証跡と混ぜない**: 事実のイベントログは Communication、蒸留した知見は dossier。
- 読み取り (`doc show` / `doc list`) は安全。書き込みは `doc add` / `doc update` のみ。
