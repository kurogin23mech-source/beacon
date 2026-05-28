---
name: beacon-onboard
description: 新メンバーが招待されてプロジェクトに合流する初日のオンボーディング体験。プロジェクトの目的・直近の流れ・自分の担当範囲を 1 ターンで把握できるようにする。/beacon-init は新規プロジェクト作成、これは既存プロジェクトへの合流。
version: 0.1.0
triggers:
  - /beacon-onboard
  - プロジェクトに参加した
  - メンバーになった
  - onboard
  - 初日
---

# Beacon Onboard

> 招待された新メンバーが、合流初日に **「このプロジェクトは何のためにあって / いま何が起きていて / 自分は何を担当するのか」** を 1 ターンで掴むための Skill。

## 前提条件チェック

Bash ツールで以下を実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、`beacon cloud join <project-id>` でまずプロジェクトに合流するよう案内して終了

## Step 1: ユーザー識別

```bash
beacon member list --json
```

結果から、現在の git config user.email / user.name と一致するメンバーを探す。

| ケース | アクション |
|---|---|
| 一致するメンバーあり | そのメンバーが「自分」。Step 2 へ |
| 未登録 | 「このプロジェクトのメンバーとしてまだ登録されていません。プロジェクトのオーナーに `beacon member add <あなたの id> --role contributor` をお願いしてください」と返して終了 |

## Step 2: プロジェクト概要の読み上げ

並列で取得:
```bash
beacon doc show project-vision
beacon doc list --scope core --json
```

これらを 1 ターンで読み、**3〜5 文の要約** を作成してユーザーに提示する:

```
ようこそ [member.name] さん!

プロジェクト概要:
  [project-vision の「大目的」を 1 行で]
  [ターゲットユーザー: ...]

現在地（直近のフォーカス）:
  [project.summary が示す経緯を 1〜2 文で]

参照すべき CORE 原則:
  - [doc-classification など、特に重要なもの 2〜3 件をタイトルだけ列挙]
```

## Step 3: 担当範囲の提示

```bash
beacon status --json
```

結果から:
- `milestones[]` で `assignee == "<member-id>"` のものを抽出 → **「あなたが assignee」**
- `milestones[]` で `owner == "<member-id>"` のものを抽出 → **「あなたが owner」**
- `assignee == ""` で `status == "in_progress"` のもの → **「アサイン待ちの活発な MS」**

提示形式:

```
あなたの担当範囲:

  あなたが assignee の MS:
    [ms-id] [title] ([progress]% / [done_tasks]/[total_tasks])

  あなたが owner の MS:
    [ms-id] [title] ([status])

  まだアサイン待ちの活発な MS（合流に向く候補）:
    [ms-id] [title] — [objective を 1 行で]
```

担当 MS がゼロの場合は「まだ担当MSが割り当てられていません。オーナーに相談するか、上の候補から `beacon milestone update <ms-id> --assignee <あなたの id> --reason '自薦'` で自分を割り当ててください」と提示。

## Step 4: 直近の流れの提示

```bash
beacon retrospect "直近2週間"
```
（`/beacon-retrospect` 相当の検索 — もし重ければ簡略版として:）
```bash
beacon search "" --from $(date -v-14d +%Y-%m-%d) --limit 20
```

結果から **「メンバー全員のここ 2 週間の主要な動き」** を 3〜5 件にまとめて提示:

```
ここ 2 週間で起きたこと:
  - [日付] [actor]: [summary]
  - ...
```

これにより、合流時点で「直近何が話題になっていたか」を体感できる。

## Step 5: 次のアクション提案

最後に、合流初日にやるべきことを 2〜3 個提案:

```
次の一歩としておすすめ:
  1. あなたの担当 MS の SPEC を読む → `beacon doc list --scope spec --ms <ms-id>`
  2. 担当 MS が無ければ オーナーに「アサインしてもらえますか？」を聞く
  3. Web UI を開いて Active Members タブで他メンバーの動きを見る （該当機能が無ければスキップ）

何か質問があれば気軽に聞いてください。
```

## 制約

- **読み取り専用**: project.json への書き込みは一切行わない（自分のアサイン変更も含めて、明示的な beacon milestone update をユーザーに打ってもらう）
- **誰がメンバーかは beacon member list が唯一のソース**: git の commit author 等を勝手にメンバーと判定しない
- **このSkillは 1 ターンで完結させる**: Step 1〜5 を一気に出力する。長い対話を要求しない（onboarding は初日 5 分で済むべき）

## 関連

- CORE doc: `project-vision` (Step 2 の主材料)
- 関連 task: e-624 (member CLI), e-625 (owner/assignee), e-626 (Active Members UI)
- 既存 Skill: `/beacon-session-start` (再訪時のコンテキスト復元), `/beacon-init` (新規プロジェクト作成、これとは別)
