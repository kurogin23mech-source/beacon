---
name: beacon-session-fork
description: 別 bclaude セッションを別 worktree で 1 コマンドで立ち上げる Skill。git worktree add + .beacon/cloud.json コピー + beacon channel install + .beacon/fork.json 書き込みを裏で代行し、Mac (Terminal.app / iTerm2) では新ターミナルで bclaude を自動起動する。難しい MS を人間 + AI で対話的に並列実装するときの足場。
version: 0.1.0
---

# Beacon Session Fork

> 別 bclaude セッションを **1 アクションで** 別 worktree で立ち上げる Skill。難しいマイルストーン (= MS、Beacon が進捗を測る単位) を **人間と AI が対話的に並列実装** したいときの足場。
>
> サブエージェント dispatch (= /beacon-dispatch) は起動時の指示で全部決まり、進行中の軌道修正ができない (対話帯域ゼロ)。「動かしながら詰める」必要のある難しい MS では人間との対話帯域が必須なので、**別 bclaude セッションを別 worktree で立てる** ほうが筋がいい。ただし、worktree 作成 + cloud.json コピー + channel install + 新ターミナル bclaude の 4 手順をユーザーに踏ませるのは CORE 原則 `ux-principle-no-terminal` (= ユーザーをターミナル作業に戻さない) に反する。本 Skill が前 3 手順を裏で代行し、Mac では新ターミナル起動も自動化する。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。詳細は CORE doc `entry-writing-principle` (doc_id `F3ZkqT0pKS6JpR8dn70n`) 参照。

## 前提条件チェック

Bash ツールで実行:
```bash
test -f .beacon/project.json && echo "OK" || echo "NO_BEACON"
```

- `NO_BEACON` → 「Beacon プロジェクトのルートで実行してください」と返して終了

## Step 0: 引数の確定

ユーザーが `/beacon-session-fork ms-XX` のように引数で MS を指定したらそれを使う。
省略された場合、`beacon status --json` を呼んで in_progress な MS 一覧を提示し、どれに対して fork したいかを尋ねる。

確定した値を `$TARGET_MS` として保持。

## Step 1: CLI helper を呼ぶ (= 中核処理)

Bash ツールで実行:
```bash
beacon session fork "$TARGET_MS" --json
```

stdout に JSON が返る:
```json
{
  "worktree_path": "/Users/.../.worktrees/ms-XX-fork-<short-uuid>",
  "branch": "ms-XX-fork-<short-uuid>",
  "fork_record": {
    "parent_session_id": "...",
    "parent_branch": "...",
    "target_ms_id": "...",
    "target_ms_title": "...",
    "child_branch": "...",
    "channel_install": {"ok": true | false, "stderr": "..."}
  }
}
```

エラー時 (exit != 0):
- `Error: milestone not found: ...` → 提示して終了
- `git worktree add failed` → 提示して終了 (= 既に worktree 名が衝突している等)
- `channel_install.ok == false` → 続行はする (= 後で再実行できる)、ユーザーに通知

成功した `worktree_path` と `branch` を控えておく。

## Step 2: 新ターミナル自動起動 (Mac、ベスト努力)

`uname` が `Darwin` の場合のみ自動起動を試す。それ以外 (Linux / Win) は Step 3 のテキスト案内に直行する。

### Step 2a: 起動先ターミナル候補の検出

優先順位:
1. **iTerm2** が前面 (frontmost) なら iTerm2
2. **Terminal.app** が前面なら Terminal.app
3. どちらも前面でなければ Terminal.app をデフォルト

Bash ツールで実行:
```bash
osascript -e 'tell application "System Events" to name of first application process whose frontmost is true' 2>/dev/null
```

返ってきたプロセス名で分岐。

### Step 2b: osascript で新ターミナルを開いて bclaude を起動

**iTerm2** の場合:
```bash
osascript <<'EOF'
tell application "iTerm"
  create window with default profile
  tell current session of current window
    write text "cd '<worktree_path>' && bclaude"
  end tell
end tell
EOF
```

**Terminal.app** の場合:
```bash
osascript <<'EOF'
tell application "Terminal"
  do script "cd '<worktree_path>' && bclaude"
  activate
end tell
EOF
```

`<worktree_path>` は Step 1 で得た worktree_path で置換する。シングルクォートで囲むのは path に空白が入っても安全にするため。

失敗時 (exit != 0、権限拒否 / 該当アプリ不在等) は Step 3 のテキスト案内にフォールバックする。**起動失敗は致命的ではない** — ユーザーは手動で 1 行コピペすれば済む。

## Step 3: 結果報告 (両モード共通)

```
✓ ms-XX のフォークを作りました

  worktree:   /Users/.../.worktrees/ms-XX-fork-<short>
  branch:     ms-XX-fork-<short>
  target MS:  ms-XX <title>

[Mac で自動起動成功時]
新しいターミナルで bclaude が立ち上がります。
そちらで対話的に作業を進めてください。

[自動起動失敗時 / Mac 以外]
新しいターミナルを開いて以下を 1 行実行してください:

  cd "/Users/.../.worktrees/ms-XX-fork-<short>" && bclaude

[channel install 失敗時]
⚠ beacon channel install が失敗しました (DM 受信ができない可能性)。
  fork 先で `beacon channel install` を手で 1 回叩いてください。
```

## 制約

- このSkill は `beacon session fork <ms-id>` (= ms-67 / e-1549) を呼ぶ薄い層。中核の worktree 設定はすべて CLI helper 側にあり、本 Skill は引数捌き + Mac 自動起動 + 結果報告だけを担う。CORE doc `architecture-tool-skill-separation` (= Tool/Skill 層と CLI/API 層の分離原則) と整合
- 自動起動はベスト努力。失敗は致命的ではなく、テキスト案内にフォールバックする
- 同時に複数 fork するのは禁止しない (= ユーザー判断)。並走数の把握は `beacon session fork list` (= 別タスク e-1553) で参照
- fork 先の cleanup (= 作業終了後の worktree 削除) は別 Skill `/beacon-session-merge-back` (= 別タスク e-1552) に任せる。本 Skill は fork のみを担当

## 関連

- `/beacon-session-merge-back` — fork 先の cleanup (PR merge 後の worktree 削除 + branch 削除)
- `/beacon-session-start` — fork 先で bclaude が起動した時、自動で発火して親情報を表示する (= 別タスク e-1551 で `.beacon/fork.json` を読む改修を入れる)
- `/beacon-dispatch` — サブエージェントを並列起動する Skill。本 Skill とは責務分担: 単純で軌道修正不要な MS は dispatch、難しい MS は本 Skill で別 bclaude を立てて人間が対話制御する
