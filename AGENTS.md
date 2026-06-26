# AGENTS.md (Codex CLI 向け案内)

> 本ファイルは Codex CLI が beacon repo で動くときに読む案内ドキュメントです。 **具体的な実行手順は `.agents/skills/beacon-init/SKILL.md`** に置いてあり、 こちらは「なぜそうするか」 と「使うべきコマンドの大枠」 を示します。

## Beacon を Codex から使うときの大原則

1. **`bare beacon` を直接叩かない**。 PATH (= コマンド検索パス) 上には古い `beacon` (例: homebrew `0.2.1`) が新しい install を黙って隠している可能性がある。 古い CLI は `bus` / `sessions` の subcommand を持っておらず、 DM 等が silently 失敗する。
2. **必ず `BEACON_BIN=<absolute path>` で実行する**。 `BEACON_BIN` は `lib/commands.py:_resolve_session_id()` の env override 経路で参照され、 PATH を経由せずに「使う 1 本」 を固定できる。
3. **`BEACON_BIN` の値は Codex run の中で動的に決める**。 別マシン / 別 clone / pipx vs brew 環境で正解が変わるため、 AGENTS.md や global config に絶対パスを書き込まない。

## 入口は `.agents/skills/beacon-init/SKILL.md`

Codex が beacon repo で何かを始めるときは、 まず `.agents/skills/beacon-init/SKILL.md` Skill を起動してください。 Skill は:

1. `python3 <repo>/scripts/beacon-bin-resolver.py` を実行して、 使うべき `beacon` 絶対パスと健全性 verdict を得る
2. `verdict == hard_fail / no-candidate` なら user に修正方法を 1 行で出して停止
3. `verdict == ok / soft_warn` なら以降の Beacon CLI 呼び出しで `env BEACON_BIN=<selected_bin> <selected_bin> ...` 形式を使う

## DM 受信を有効化する (= Beacon Codex plugin / e-2508 minimum viable)

DM (= 別 session からの直接メッセージ) を Codex の prompt 冒頭に inject させるには、 `plugins/beacon/` 配下の Codex plugin を install します。 1 step (= 1 Skill 呼び出し) で hook 登録 + daemon 起動が完了します。

```
# Codex 起動後:
$beacon-codex-bridge start

# 確認:
$beacon-codex-bridge status

# 停止:
$beacon-codex-bridge stop
```

裏で行われていること:

1. `~/.codex/hooks.json` の `UserPromptSubmit` に Beacon の hook entry を冪等 merge (= 既に同 cwd の entry があれば no-op)
2. `<cwd>/.beacon/codex/receive-loop.pid` を読んで daemon の running 状態を確認
3. 未起動なら `scripts/codex-receive-loop.py` を nohup 起動 (= log は `<cwd>/.beacon/codex/receive-loop.log`)
4. 既に running なら no-op (= collision 防止、 cwd-scoped lock)
5. stale pidfile (= pid が dead) は自動 cleanup

**`nohup python3 scripts/codex-receive-loop.py &` を別 terminal で打つ必要はありません**。 tmux ad-hoc launcher は dev / dogfood 専用扱いで、 product path は plugin Skill 経由です。

plugin 自体の install (= `codex plugin add beacon@personal`) と marketplace entry の整備は別途 (= MVP では skill 直叩きが優先、 marketplace 整備は follow-up task)。

## 関連 task / SPEC

- `ms-93` Codex 対応 — 全体 MS (= `beacon doc show <ms-93 SPEC doc>` 参照)
- `e-2276` Codex 側 phase 0 wrapper (= BEACON_BIN 固定 + doctor PATH gate)
- `e-2497` Codex 側 receive loop adapter (= 固定 session_id + heartbeat、 phase 1 本命)
- `e-2508` Codex plugin lifecycle (= 本セクションの実装、 `plugins/beacon/`)
