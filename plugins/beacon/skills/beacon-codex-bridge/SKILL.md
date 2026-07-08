---
name: beacon-codex-bridge
description: Beacon の DM 受信 bridge を Codex CLI で 1 step で扱う Skill。 UserPromptSubmit hook の install / receive-loop daemon の lifecycle (= start / stop / status / restart) を統合する。 daemon を別 terminal で nohup する手動運用が不要になる。 user が "$beacon-codex-bridge start" 等で起動。
---

# beacon-codex-bridge

> Beacon の Codex 対応 plugin の中核 Skill。 hook install と receive-loop daemon の lifecycle を 1 つにまとめ、 user が「daemon を別 terminal で nohup する」 ことを意識せずに DM 受信を有効化できるようにする。

## 引数

- `start` — UserPromptSubmit hook を `~/.codex/hooks.json` に install (= 冪等) + receive-loop daemon を起動 (= 既に running なら no-op)
- `stop` — daemon を graceful 停止 (= SIGTERM)
- `restart` — stop → start
- `status` — daemon の running 状態 + hook install 状態 + session pointer を表示
- `uninstall` — hook を `~/.codex/hooks.json` から除去 + daemon stop (= plugin 自体は `codex plugin remove beacon@personal` で別途)

引数なしの場合は `status` 相当を実行。

## 前提

- `BEACON_INSTALL_ROOT` 環境変数 が beacon の checkout を指している (= 例 `/Users/r_kida2/tools/beacon`)。 未設定なら `which beacon` の resolve から推測。
- 当該 cwd に `.beacon/cloud.json` が存在 (= cloud mode project)。 local-only project では bridge は意味を持たない。
- Codex CLI で `~/.codex/hooks.json` が編集可能。

## 実装フロー

各 subaction を Bash 経由で plugin scripts 配下の lifecycle CLI に委譲する:

```bash
BRIDGE="$BEACON_INSTALL_ROOT/plugins/beacon/scripts/beacon-codex-bridge"
python3 "$BRIDGE" <subaction> --cwd "$(pwd)"
```

`beacon-codex-bridge` (Python script) が実装する責務:

1. **`install-hook`** — `~/.codex/hooks.json` を読み、 `UserPromptSubmit` 配列に Beacon の hook entry が無ければ append。 同 entry (= command が `codex-inbox-hook.py` を指す) が既にあれば no-op。 冪等。
2. **`uninstall-hook`** — `~/.codex/hooks.json` から Beacon の hook entry を除去。 他の entry は触らない。
3. **`start`** — `install-hook` の後、 `scripts/codex-receive-loop.py` を nohup 起動 (= `<cwd>/.beacon/codex/receive-loop.log` に redirect)。 既存 pidfile が live ならその pid を返して exit 0 (= idempotent)。 stale pidfile (= pid が dead) は削除してから起動。
4. **`stop`** — pidfile から pid を読み、 SIGTERM。 pidfile が無い / pid が dead なら no-op で exit 0。
5. **`restart`** — `stop` → `start`。
6. **`status`** — pidfile 状態 + hook install 状態 + `<cwd>/.beacon/codex/receive-loop.session.json` を表示。

## エラーハンドリング

- `BEACON_INSTALL_ROOT` 未解決 → 「beacon の checkout を `BEACON_INSTALL_ROOT` で指してください」 と user に促す
- `.beacon/cloud.json` 不在 → 「cloud mode project でのみ動作します。 `beacon cloud setup` を実行してください」
- hook install で `~/.codex/hooks.json` が malformed → bridge は何もせず exit 1 + 状況を表示 (= user 手動修正を促す)

## user UX

```
$beacon-codex-bridge start

→ ✓ hook installed at ~/.codex/hooks.json (UserPromptSubmit)
  ✓ daemon started: pid=12345 sid=sv-... project=...
  → DM が届くと次の prompt 冒頭に表示されます

$beacon-codex-bridge status

→ hook:    installed ✓ (UserPromptSubmit → .../scripts/codex-inbox-hook.py)
  daemon:  running ✓ (pid=12345, since 2026-06-26T10:00:00Z)
  session: sv-...  project=beacon-b95643
  inbox:   0 unread / 13 archived

$beacon-codex-bridge stop

→ ✓ daemon stopped (pid=12345)
  hook は残ります (= 次回 start で再利用)
  完全に外すなら $beacon-codex-bridge uninstall
```

## 自律応答 (= armed) と受信 transport (ms-93 / e-2519)

default の受信は **pull-on-prompt** (= 届いた DM を inbox に貯め、 次の user prompt 冒頭に見せる) のみ。 user が席を外している間は Codex は何もしない。 これを超えて **DM をトリガーに Codex が自律返信する** には、 daemon を `--app-server` (+ `--armed`) opt-in で起動する (= 通常は `bcodex --armed` launcher 経由)。

autonomous 返信の transport は 2 つ:

- **app-server (= option D、 本命)**: `codex app-server` を 1 つ長寿命で保持し、 届いた DM を JSON-RPC の turn として流し込んで返信を得る。
- **exec-worker (= option B、 fallback、 e-2519 AC 2)**: app-server が起動できない時 (= experimental transport の失敗、 または `--app-server` 無しの `--armed`) に、 **DM 1 通ごとに別の `codex exec` worker を 1 発 spawn** して返信を生成する。 sandbox は保守的に **read-only** default (= 自律返信が workspace を書き換えない、 override は `BEACON_CODEX_EXEC_SANDBOX`)。
- **desktop 通知 (= option C、 安全 fallback、 e-2519 AC 3)**: 自律返信を一切したくない安全モード / local 環境向け。 armed でない (= 自律 transport が無い) 状態で `BEACON_CODEX_DESKTOP_NOTIFY=1` を設定すると、 DM 到着時に **desktop 通知** (macOS osascript / Linux notify-send / Windows toast) を出すだけに留める。 Codex は自律返信せず、 user が気付いて手で返信する (= 次の prompt で inbox 経由 injection)。 通知は 30 秒に 1 回に throttle。

**重要な semantic (= 誤解防止)**: どちらの transport も、 **user が見ている既存の Codex TUI を wake するわけではない**。 app-server は別 thread、 exec-worker は別 headless プロセスを立てて返信する。 「DM が届くと自律 action が起きる」 体感は成立するが、 手元の対話窓がひとりでに喋り出すのではない。

**安全則**: `--armed` (= 明示 opt-in) と bus budget grant (= 自動返信回数の上限) の両方が無ければ autonomous 返信は起きない。 budget は `bcodex --armed --armed-turns N` が launcher 起動時に grant する。 headless daemon は自分で budget を self-grant しない (= 人間起点の launcher だけが授権する)。

## 関連

- 上位 task: ms-93 / e-2508 (= Codex 用 plugin 形式の installer)
- 関連 task: ms-93 / e-2519 (= push 受信経路: app-server D + exec-worker B fallback + armed)
- 関連 SPEC: ms-93 SPEC + e-2502 SPEC (= bus protocol 共通 core + adapter)
- 関連 script: `scripts/codex-receive-loop.py` (= daemon 本体)、 `scripts/codex-inbox-hook.py` (= hook 本体)
- tmux ad-hoc launcher (= `tmux new-session -d 'python3 scripts/codex-receive-loop.py'`) は dev / dogfood 専用、 product path はこの Skill を経由する。
