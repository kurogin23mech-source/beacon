# Beacon plugin for Codex CLI

Beacon を OpenAI Codex CLI から Claude Code と同等に使うための plugin です。
DM (= 別 Codex / Claude Code session からの直接メッセージ) 受信 bridge と、
その lifecycle を 1 step で扱う Skill を同梱します。

- `skills/beacon-codex-bridge` — DM 受信 hook の install + receive-loop daemon の start/stop/status
- `skills/beacon-codex-armed` — 自律 DM 応答モード (= opt-in、budget gate 付き)

## Install (marketplace 経由)

beacon の checkout そのものが Codex の **marketplace** を兼ねます
(repo root の `.agents/plugins/marketplace.json` が目録)。plugin 本体は
`plugins/beacon/` に同梱済みなので、追加のコピーは不要です。

```bash
# 1. beacon checkout を marketplace として登録 (= 1 マシン 1 回)
codex plugin marketplace add /path/to/beacon        # 例: ~/tools/beacon

# 2. plugin を install
codex plugin add beacon@beacon

# 3. cloud-mode project の cwd で DM 受信を有効化
#    (BEACON_INSTALL_ROOT が beacon checkout を指している前提。
#     未設定でも `which beacon` から推測されます)
$beacon-codex-bridge start
```

`codex plugin list` に `beacon@beacon  installed, enabled` と出れば成功です。

### なぜ install 後も beacon checkout が要るのか

`codex plugin add` は plugin を `~/.codex/plugins/cache/` にコピーしますが、
bridge Skill は実行時に `$BEACON_INSTALL_ROOT/plugins/beacon/scripts/beacon-codex-bridge`
を呼び戻します (daemon 本体 `scripts/codex-receive-loop.py` と `lib/` は
beacon checkout 側にしか無いため)。plugin install は「Skill を Codex に
発見させる」役割で、実行実体は checkout 側という二層構造です。だから
marketplace source を checkout の絶対パスにするのが最も素直で、コピー由来の
drift も起きません。

## Update / Uninstall

```bash
# plugin.json の version を上げた後、install 済みを更新
codex plugin marketplace upgrade beacon      # git marketplace の場合のみ
codex plugin add beacon@beacon               # local marketplace は再 add で最新化

# 外す
$beacon-codex-bridge uninstall               # hook 除去 + daemon stop
codex plugin remove beacon@beacon            # plugin cache 除去
codex plugin marketplace remove beacon       # marketplace 登録解除
```

## marketplace 目録の制約メモ

- `policy.authentication` は `ON_INSTALL` / `ON_USE` のみ許容。beacon は
  install 時認証が無いので **field ごと省略**する (`NONE` を書くと
  `unknown variant` で reject される)。
- `source.path` は marketplace root からの相対 (`./plugins/beacon`)。repo を
  そのまま棚にできる。
- plugin manifest (`.codex-plugin/plugin.json`) には `hooks` field を書けない
  (validator が reject)。hook install は bridge Skill が `~/.codex/hooks.json`
  へ冪等 merge する経路で行う。
