---
name: beacon-session-merge-back
description: /beacon-session-fork で立てた子 worktree を片付ける Skill。active な fork を picker で選び、対応 branch が main に取り込まれているかを git で確認、merge 済なら git worktree remove + git branch -d で物理削除する。未 merge は警告して中止。fork → 並列実装 → cleanup の往復を 2 Skill で完結させるペア Skill。
version: 0.1.0
triggers:
  - /beacon-session-merge-back
  - /beacon-merge-back
  - fork を片付ける
  - fork を閉じる
  - worktree を片付ける
  - cleanup fork
  - merge back
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
    "created_at": "...",
    "unpromoted_notes": 3,
    "notes_path": "/Users/.../.worktrees/ms-12-fork-abc123/.beacon/session_notes.jsonl",
    "own_session_id": "sv-...",
    "idle_seconds": 42.0
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
     ⚠ 作業中 (1 分前まで活動) / ⚠ 未昇格の引き継ぎメモ 3 件
2. ms-15 "..." (child=ms-15-fork-def456, created 2026-06-12T05:30)
     最終活動: 43.3 時間前

番号で選ぶか、cancel で中止してください。
```

- 番号で選択 → 対応 fork の `worktree_path` と `child_branch` を控える
- **`unpromoted_notes` と `idle_seconds` を必ず各行に出す (ms-178 e-6702/e-6703)**。
  この 2 つが本 Skill の存在理由に直結する: `unpromoted_notes` が 1 件以上の fork を
  消すと、まだドキュメントへ昇格していない引き継ぎメモ (= その fork の判断の軌跡) を
  失う。`idle_seconds` が小さい fork は **他セッションがまだ作業中** で、消すとその
  セッションの足元が外れる。表示形式:
  - `idle_seconds` が `null` → 「⚠ 作業中か判定できません」(= 空いているとは限らない)
  - `idle_seconds` が閾値 (既定 300 秒、`BEACON_FORK_IDLE_THRESHOLD_S` で変更可) 未満
    → 「⚠ 作業中 (N 分前まで活動)」
  - それ以上 → 「最終活動: N 時間前」
  - `unpromoted_notes` が `null` → 「⚠ メモ件数を読めません」(= 0 件ではない)
  - `unpromoted_notes` が 1 以上 → 「⚠ 未昇格の引き継ぎメモ N 件」
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
beacon session fork cleanup "$TARGET_FORK_WORKTREE_PATH"
```

**`git worktree remove` を直接叩かない (ms-178 e-6702)**。削除は CLI 側の verb が所有する。
理由: `git worktree remove` は `.beacon/` ごと消すため、その fork がまだドキュメントへ
昇格していない引き継ぎメモを、控えも警告も件数表示もなく失う。2026-09-29 に実際に発生し、
レビュー採否を含むメモ 3 件が失われた。`beacon session fork cleanup` は削除の前に
メモを worktree の外 (`.beacon/fork-notes-backup/`) へ退避し、**退避が取れなければ
削除しない** (`beacon note clear` と同じ順序保証)。branch の削除も同じ verb が行う。

この verb は以下のいずれかに当たると **拒否して終了コード 1 を返す**:

| 拒否理由 | 意味 |
|---|---|
| branch が origin/main に未取り込み | 消すとコミットが失われる |
| このフォークに未コミットの変更がある | 取り込み確認は履歴しか見ないので検出できない。消すと失われる |
| その fork でまだ作業されている | 他セッションの足元を外す (e-6703) |
| 作業中か判定できない | 「判定できない」は「空いている」ではない |
| メモの退避が取れない | 退避の取れない削除は行わない |

拒否されたら **その内容をそのままユーザーに提示して中止する**。`--force` を自動で
付けてはならない (= ユーザー判断)。未昇格メモがあると告げられた場合は、
「その fork で `/beacon-session-end` を走らせてメモを昇格させてから片付ける」経路を
提案する (= 昇格を飛ばして消させない)。

`--force` が上書きできるのは **人間が引き受けられる種類のリスク** だけ (未取り込み /
未コミットの変更 / 作業中 / 判定不能)。**メモの退避が取れない場合は `--force` でも通らない** — データ保全の
物理的な可否は人間が引き受けられるものではないため、`hard_blocker` として force の管轄外に
置いてある (`--json` の `backup_failed` で判別できる)。この区別は独立レビュー (PR #770) の
指摘で入った: それ以前は force が退避失敗ゲートまで素通りし、手順書の記述と実装が
食い違っていた。

拒否されても **メモの控えは取られている**。退避は拒否判定より先に走るので、拒否メッセージに
出る退避先パスをそのままユーザーに伝える (= 消せなかったが控えは残っている、を明示する)。
`--json` では `notes_backup` に入る (ms-166 e-6780)。

`--force` は **cleanup 専用の旗**。`beacon session fork list --force` や
`beacon session fork <ms-id> --force` は両フロントで拒否される (ms-166 e-6782)。

削除結果の読み方 (ms-166 e-6781): `removed` は **worktree を消せたか** だけを表す。
branch の削除可否は `branch_removed` で別に返る。`removed: true` + `branch_removed: false`
は「worktree は消えたが branch が残っている」状態で、`errors` に理由が入る
(`--force` で未取り込みを上書きしたときは `git branch -d` が必ず拒否するのでこの形になる)。
この場合 **再実行しても解決しない** (その worktree はもう fork 一覧に無いので
「not an active fork worktree」で止まる)。残った branch を消すかはユーザー判断として提示する。

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
