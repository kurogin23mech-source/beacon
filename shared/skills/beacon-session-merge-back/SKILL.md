---
name: beacon-session-merge-back
description: /beacon-session-fork で立てた子 worktree を片付ける Skill。active な fork を picker で選び、対応 branch が main に取り込まれているかを git で確認、merge 済なら git worktree remove + git branch -d で物理削除する。未 merge は警告して中止。fork → 並列実装 → cleanup の往復を 2 Skill で完結させるペア Skill。
version: 0.1.0
---

# Beacon Session Merge-back

> `/beacon-session-fork` で立てた子 worktree を片付ける Skill。`/beacon-session-fork` とペアで、fork → 並列実装 → cleanup の往復を **2 Skill で完結** させる。
>
> 親 worktree から叩く想定。active な fork を picker で選び、対応する子 branch が main に取り込まれているかを git で確認、取り込み済なら `git worktree remove` + `git branch -d` で物理削除する。**未 merge の子は強制削除しない** — 未マージ branch の作業を黙って失うことを構造的に防ぐ。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。詳細は CORE doc `entry-writing-principle` (doc_id `F3ZkqT0pKS6JpR8dn70n`) 参照。

## 前提条件チェック

Bash ツールで実行:

```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` → 「Beacon プロジェクトのルートで実行してください」と返して終了

## Step 1: active な fork 一覧の取得

Bash ツールで実行:

```bash
beacon session fork list --json
```

stdout に JSON 配列が返る:

```json
[
  {
    "worktree_path": "/Users/.../.worktrees/ms-12-fork-abc123",
    "target_ms_id": "ms-12",
    "target_ms_title": "...",
    "child_branch": "ms-12-fork-abc123",
    "parent_session_id": "...",
    "parent_branch": "main",
    "created_at": "..."
  },
  ...
]
```

- 空配列 (`[]`) → 「active な fork がありません」と表示して終了
- 1 件以上 → Step 2 へ

## Step 2: picker で対象を選ぶ

ユーザーに 1 行ずつ提示:

```
active な fork が N 件あります。どれを cleanup しますか？

1. ms-12 "..." (child=ms-12-fork-abc123, created 2026-06-12T03:00)
2. ms-15 "..." (child=ms-15-fork-def456, created 2026-06-12T05:30)

番号で選ぶか、cancel で中止してください。
```

- 番号で選択 → 対応 fork の `worktree_path` と `child_branch` を控える
- `cancel` → 中止

ユーザーが選んだ fork を `$TARGET_FORK` として記憶 (`worktree_path` / `child_branch` を保持)。

## Step 3: 子 branch が main に取り込まれているか確認

Bash ツールで実行 (親 repo のルートで):

```bash
git fetch origin main 2>&1
git branch --merged origin/main | grep -E "^\s*$(echo "$TARGET_FORK_CHILD_BRANCH" | sed 's/[.[\*^$()+?{|]/\\&/g')\s*$"
```

(child_branch は変数として埋め込み、正規表現メタ文字はエスケープ。実用は単純な文字列マッチで足りるはず — 念のためエスケープ)

判定:

- **マッチした (= merged)** → Step 4 (cleanup) に進む
- **マッチしなかった (= unmerged)** → 念のため `gh pr list --state merged --head $TARGET_FORK_CHILD_BRANCH --json number,mergedAt 2>/dev/null` も試す。1 件以上返れば「PR 経由で merged」と判定して Step 4 へ
- どちらも空 → **未 merge と判定**、Step 5 (警告 + 中止) へ

## Step 4: cleanup 実行 (= merge 済の場合のみ)

Bash ツールで実行:

```bash
git worktree remove "$TARGET_FORK_WORKTREE_PATH"
git branch -d "$TARGET_FORK_CHILD_BRANCH"
```

- `git worktree remove` が失敗 (= worktree 内に未 commit の変更がある等) → 「worktree に未 commit の変更があります。中身を確認するか `--force` を使うか判断してください」と提示して中止。**自動で `--force` を渡さない** (= ユーザー判断)
- `git branch -d` が失敗 (= branch が未マージと git が判断) → Step 3 で merged 判定したのとずれているので警告だけ出して続行 (= worktree は既に消えた状態)

成功したら次へ。

## Step 5: 未 merge の場合の警告と中止

```
⚠ child branch "<branch>" は origin/main に取り込まれていません。

中身を確認してください:
  - PR がまだの場合: `gh pr create --base main --head <branch>`
  - PR が open なら merge を待つ
  - もう要らないなら手動で消す: `git worktree remove --force <wt-path> && git branch -D <branch>`

本 Skill は未 merge の作業を黙って失う経路を持ちません。明示判断後、再度 /beacon-session-merge-back を実行してください。
```

中止して終了。

## Step 6: 結果報告 (= cleanup 成功時)

```
✓ fork を cleanup しました

  removed worktree: <wt-path>
  deleted branch:   <child-branch>
  target_ms:        ms-XX <title>

残りの active な fork は `beacon session fork list` で確認できます。
```

ユーザーは追加で他の fork を cleanup する場合は `/beacon-session-merge-back` を再度叩く。

## 制約

- 本 Skill は **未 merge の child branch を強制削除しない**。`--force` 系を自動で渡さない設計で、未マージの作業が黙って失われる経路を構造的に塞ぐ
- picker で見える fork は `beacon session fork list` の出力 (= `.worktrees/` 配下に `.beacon/fork.json` があるもの) のみ。`/beacon-session-fork` で立てたもの以外 (= `beacon milestone start` の worktree 等) は対象外
- 親 ↔ 子の関係 (`.beacon/fork.json` の `parent_session_id`) を Skill では明示的にチェックしない。誰の親かに関わらず、現在の repo の active な fork はすべて picker に出す (= 並走セッションが他人の fork を巻き込み消しできない構造的ガードは別途 future)
- `git fetch origin main` を冒頭で 1 回叩くので、ネットワーク無し環境では失敗する可能性。その場合は local の `main` ベースで `--merged` 判定する fallback まで本 Skill で扱う

## 関連

- `/beacon-session-fork` — 対になる Skill。worktree を立てるほう
- `/beacon-session-start` — fork した子セッションが起動した時、ヘッダに親情報を表示する (`.beacon/fork.json` 読み込み経路、e-1551)
- `beacon session fork list` — 本 Skill の picker source となる CLI (e-1553)
