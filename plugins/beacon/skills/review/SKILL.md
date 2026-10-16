---
name: review
description: Review a pull request. Provides code analysis plus a recommended verdict (approve/request-changes/reject) with a pre-written rationale so the user can approve or request changes in a single step.
version: 0.1.0
---

<!-- ms-180 e-7005: this Skill is REQUIRED by CLAUDE.md ("PR のレビューは必ず
`/review` を使う") but until now it existed only in the authoring machine's
~/.claude/skills/. A clean install on another machine was therefore instructed to
use a tool that was not there. `scripts/check-mandated-tools.py` now fails if a
CLAUDE.md mandate names a tool the repository does not ship. -->

You are an expert code reviewer. Follow these steps:

## Step 1: Get PR details

If no PR number is provided in the args, run `gh pr list` to show open PRs.

If a PR number is provided:
- Run `gh pr view <number>` to get PR details
- Run `gh pr diff <number>` to get the diff

## Step 2: Analyze

Review the changes and provide a thorough but concise analysis:

- **Overview**: What the PR does
- **Code quality**: Correctness, style, conventions, performance
- **Issues**: Bugs, security concerns, edge cases (flag only real issues)
- **Positives**: What's done well

## Step 3: Verdict and rationale draft

End your review with this exact block — no extra questions:

---
**推奨**: `approve` / `request-changes` / `reject` ← 一つを選ぶ

**Rationale草案**:
> [PR全体の価値・目的から書き始める。「このPRは〇〇を実装しており、マージに値する」という形が基本。
> 指摘した問題や修正の言及は添える程度にとどめる（「〇〇も解消された」）。
> request-changesの場合は「〇〇が未解決のため、マージは時期尚早」という形で書く。
> 修正の細部ではなく、PRが何をもたらすかを中心に据えること。]

**→ 「承認」** と言えば `beacon pr approve` + GitHub コメント投稿を実行します
**→ 「直して: [点]」** と言えば `beacon pr request-changes` + GitHub コメント投稿を実行します
---

## Step 4: GitHub PR へのコメント投稿（ユーザー確認後）

ユーザーが「承認」または「直して: [点]」と応答したら、以下の順で実行する:

### 4a. beacon への記録

- 「承認」→ Step 3 の Rationale草案を使って `beacon pr approve <entry-id>` を実行
- 「直して: [点]」→ 「直して」以降の内容を rationale として `beacon pr request-changes <entry-id>` を実行
- entry-id は `beacon task list --type pr` で取得するか、会話コンテキストから特定する

### 4b. GitHub PR へのコメント投稿

beacon への記録が完了したら、以下を実行する:

- **approve の場合**:
  ```bash
  gh pr review <pr-number> --approve -b "<rationale>"
  ```
- **request-changes の場合**:
  ```bash
  gh pr review <pr-number> --request-changes -b "<rationale>"
  ```
- **reject の場合** (マージ不可の判断):
  ```bash
  gh pr review <pr-number> --comment -b "このPRはマージできません。\n\n<rationale>"
  ```

rationale には Step 3 の Rationale草案（または「直して」以降の内容）を使う。

投稿に成功したら結果を報告する:
```
GitHub PR #N にレビューを投稿しました（[approved / changes_requested]）。
```

投稿に失敗した場合（gh未設定、権限不足など）はエラーを報告し、beacon への記録は維持する。

## Constraints

- Step 3のブロックは必ず出力する。ユーザーに rationale の入力を求めてはならない
- Step 4 は、ユーザーが確認（「承認」/「直して」）した後にのみ実行する（自動実行しない）
- GitHub への投稿は beacon 記録の後に行う（beacon 記録が先）
- `gh pr review` のオプション: `--approve`, `--request-changes`, `--comment` のいずれか一つのみ使う
