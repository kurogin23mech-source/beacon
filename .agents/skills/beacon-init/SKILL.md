---
name: beacon-init
description: Beacon プロジェクトを Codex CLI から初期化・運用する。最初に PATH 健全性 (= 古い beacon が新しい install を黙って隠していないか) を確認してから、Beacon 機能を使い始める。
triggers:
  - beacon init
  - beacon を始めたい
  - beacon の準備を確認したい
---

# Beacon Init (Codex 版)

> Codex CLI から Beacon を使い始める入口。PATH 上の `beacon` 衝突を構造的に検出し、この Codex run の中で「使える 1 本」 を固定する。
>
> 永続的な hook env 注入は Beacon Codex plugin (= ms-93 phase 1、 別 task) の責務。 本 Skill は **この run の中で BEACON_BIN を解決して使う** ところまで責任を持つ。

## 文章の書き方

Beacon に書き込む文章は、非開発者を含む読み手が 1 度読んで意味を取れる形にする:
- **読み手目線 1 行から始める** (= 何が嬉しいか / 何が困るか)
- **横文字 3 段階**: 固有名詞そのまま (`Codex` / `Beacon` / `PATH`)、 技術概念は初出に日本語注、 一般概念は日本語化
- **ID 参照には文脈** (= `e-XXXX` / `ms-XX` は初出に「何の話か」 1 行)
- **尻切れトンボ禁止**

---

## Step 1: BEACON_BIN gate (= e-2276 / 入口で必ず通す)

Codex run の **最初** に、 PATH (= コマンド検索パス) 上の `beacon` を 1 本選んで健全性を判定する。 これを skip すると、 古い beacon が `bus` / `sessions` の subcommand を持っていないのに silently 動作したり、 PATH 衝突で意図しない beacon が呼ばれたりする。

Bash ツールで実行:

```bash
__BEACON_REPO_ROOT=${BEACON_REPO_ROOT:-$(beacon-find-root 2>/dev/null || pwd)}
python3 "$__BEACON_REPO_ROOT/scripts/beacon-bin-resolver.py"
```

stdout は 1 行 JSON。 以下の field を読む:

- `verdict`: `ok` / `soft_warn` / `hard_fail` / `no-candidate`
- `selected_bin`: 選ばれた `beacon` の絶対パス (= この run で使う 1 本)
- `selected_source`: `env` / `install_root` / `path`
- `advice`: 1 行の user 向け推奨メッセージ
- `hard_fail_codes` / `soft_warn_codes`: 判定根拠

Skill 用 fallback: resolver script が見つからない (= 古い beacon repo、 Python 不在等) ときは silent skip して、 user に「`beacon-bin-resolver.py` を含む beacon (v0.49.0+) を pip / brew / source install してから再起動してください」 と 1 行返して終了する。 phase 0 段階では best-effort、 user を block しない。

## Step 2: verdict に応じた分岐

### 2a. `hard_fail` / `no-candidate` の場合 (= ここで止まる)

user に **1 メッセージ** で advice を出して終了する:

```
⚠ Beacon は今の環境では使えません。

  <advice>

修正方法:
  1. BEACON_BIN を新しい install の絶対パスに設定 (= 例: export BEACON_BIN=/Users/<you>/tools/beacon/bin/beacon)
  2. もう一度 /beacon-init を実行

(永続的に Codex に BEACON_BIN を覚えさせるには Beacon Codex plugin の install を待ってください。 phase 0 ではこの Codex run の中で envvar を 1 度設定して再実行する手順を推奨します)
```

Beacon の以降の操作 (= init / DM / Skill 起動) は走らせない。

### 2b. `soft_warn` の場合 (= 1 行 note のみ)

user に 1 行で状況を伝えて、 Step 3 に進む:

```
ℹ Beacon: <advice>
  (selected: <selected_bin>、 source=<selected_source>)
```

例えば `multiple-binaries` 単体なら 「primary は健全だが他にも beacon が PATH 上にある」 という cosmetic な情報。 続行可能。

### 2c. `ok` の場合

何も表示せず Step 3 へ進む。

## Step 3: この Codex run で BEACON_BIN を固定する

この Codex run の **以降の全 Beacon CLI 呼び出し** で `selected_bin` を使う。 永続的な env 設定はしない (= Codex 設定 / shell profile を変更しない)。

実装方法は 2 つあり、 Skill / Codex run のスタイルに合わせて選ぶ:

### (i) コマンドごとに env prefix (推奨、 最小副作用)

毎回の Beacon CLI 呼び出しで:

```bash
env BEACON_BIN="<selected_bin>" "<selected_bin>" status
env BEACON_BIN="<selected_bin>" "<selected_bin>" bus send ...
```

なお `selected_bin` が `bare beacon` のときは `env BEACON_BIN=$(command -v beacon) beacon ...` 相当。

### (ii) Skill 内 shell session に export (= 簡便だが影響範囲広い)

Skill が同一 Bash セッション内で複数コマンドを連続実行する場合:

```bash
export BEACON_BIN="<selected_bin>"
beacon status   # PATH 上の beacon が呼ばれるが、 内部で BEACON_BIN を honour する CLI なら問題なし
"$BEACON_BIN" status   # 確実にしたい場合は直接呼び出し
```

注意: `export` した env は **同じ Bash セッション内の以降のコマンド** にだけ効く。 Codex の Bash ツールが session を都度開く設計なら (i) を、 永続的なら (ii) を選ぶ。

## Step 4: Beacon プロジェクト初期化に進む

ここから先は標準の Beacon 初期化フロー。 user に以下を聞く:

```
Beacon プロジェクトを初期化します。 以下を教えてください:

  📛 プロジェクト名:   [Codex cwd の basename を draft として提示]
  🎯 大目的:           [README.md / package.json description があれば draft]
```

確認後、 `selected_bin` で初期化:

```bash
env BEACON_BIN="<selected_bin>" "<selected_bin>" init --name "<name>" --objective "<objective>"
```

(以降の進行は Claude Code 版 `/beacon-init` Skill と同じ。 既存リポなら Project Archaeology へチェイン、 新規なら milestone 提案、 等)

## Step 5: 永続化への案内 (= phase 1 で扱う、 今は 1 行案内)

Codex run を再起動するたびに resolver を毎回叩くのは妥当だが、 user が 「毎回 BEACON_BIN を設定するのは煩わしい」 と感じたら、 ms-93 phase 1 (= Beacon Codex plugin) が hook env / config 経由で恒久化する経路を提供する予定であることを 1 行伝える:

```
ℹ 毎回 BEACON_BIN を設定するのが煩わしい場合は、 Beacon Codex plugin の land (= ms-93 phase 1) を待ってください。 plugin は hook 経由で BEACON_BIN を自動注入する予定です。
```

これ以上の永続化操作 (= `~/.codex/config.toml` 書き換え / shell profile 編集等) は本 Skill では行わない。 phase 0 = この run の中で正しく動かす、 が責務範囲。

---

## 制約

- Step 1 の BEACON_BIN gate は **必ず最初に走らせる**。 skip すると古い beacon が silently 使われる経路が残る
- Step 2a (hard_fail / no-candidate) で止めたら、 以降の Beacon 操作は実行しない
- 永続化 (= shell profile / `~/.codex/config.toml` / `~/.codex/hooks.json` 書き換え) は本 Skill の責務外。 phase 1 の Beacon Codex plugin が担う
- AGENTS.md global override で BEACON_BIN を絶対パス指定するのは推奨しない (= 別マシン / 別 clone で壊れる)。 AGENTS.md は説明のみ
