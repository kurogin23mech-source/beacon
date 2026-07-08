---
name: beacon-codex-armed
description: Codex セッションを「自律 DM 応答モード」に切り替える Skill。 Claude Code 側 /beacon-bus-armed の Codex 相当。 明示 opt-in + budget gate (= 自動返信の回数上限) + allowed channels + stop + audit を 1 フローで扱い、 席を外している間に届いた DM に Codex が自律返信できる状態を安全に作る。 arm / status / stop の 3 動作。
---

# beacon-codex-armed (Codex 自律 DM 応答モード)

> 「会話していなくても、 別セッションから DM が届いたら Codex が気付いて、 決めた回数まで自律的に返信する」 状態にこのプロジェクトを切り替える。 Claude Code の `/beacon-bus-armed` に対応する Codex 版。

## ⚠ 最初に伝える semantic (= 誤解防止、 e-2519 AC6 clear copy)

armed にすると、 DM をトリガーに **別の Codex が返信を生成する**:

- **app-server 経路 (= 本命)**: 長寿命の `codex app-server` 上に別 thread を立てて返信。
- **exec-worker 経路 (= fallback)**: app-server が使えない時、 **DM 1 通ごとに別の `codex exec` worker を 1 発 spawn** して返信 (sandbox は保守的に read-only)。

どちらも **user が見ている対話 TUI が勝手に喋り出すのではない**。 裏で別プロセス / 別 thread が返す。 「DM が自律 action を引き起こす」 体感は成立するが、 手元の窓は静かなまま。 これを user に必ず一言伝えてから arm する。

## 引数

- `arm` (default) — budget を確認・付与し、 armed daemon を起動する
- `status` — 現在 armed かどうか (budget 残 / daemon 状態 / allowed channels) を表示
- `stop` — 自律返信を止める (budget clear = soft、 daemon stop = hard)

## 前提条件チェック

Bash ツールで:
```bash
__ROOT=$(beacon-find-root) && [ -f "$__ROOT/.beacon/cloud.json" ] && echo OK || echo NO_BEACON_OR_CLOUD
```
`NO_BEACON_OR_CLOUD` なら「cloud mode の beacon project でのみ動きます」 と伝えて終了。 Codex が PATH に無ければ arm できない旨も添える。

## `arm` フロー

### Step 1: 現在の budget を確認

```bash
beacon bus budget show --json
```
- `{"armed": false}` → 未付与 (default)。 Step 2 へ。
- `{"total": N, "used": M}` → 既に armed。 残 `N-M` 回を伝え、 追加 grant するか聞く。

### Step 2: 明示 opt-in (= budget gate / 最大自動返信回数)

user に **必ず明示確認** する。 数字が autonomous 返信の上限 (= budget gate、 これを超えると自動で止まる安全弁):

```
Codex の自律 DM 応答モードを起動します。 最大何回まで自動返信していい?
  3  → 短い往復確認
  10 → 通常の dogfood
  50 → 長時間の会話実験
  0 / skip → キャンセル
```

`0` / skip なら何もせず終了。

### Step 3: allowed channels の確認 (任意)

どの channel の DM に自律反応するかの許可リスト (= 空なら全 channel)。 絞りたい user 向け:
```bash
beacon bus auto-execute list          # 現在の許可リスト
beacon bus auto-execute add --channel dm      # dm だけ許可、 等
```
通常は触らず default (= dm 中心) のまま進めてよい。

### Step 4: armed daemon を起動

**推奨 (= 新規に立てる)**: launcher が budget grant + app-server + armed を一括で行う。 別 terminal で:
```bash
bcodex --armed --armed-turns <N>
```
`--armed-turns N` が `beacon bus budget grant --turns N` を launcher 起動時に実行する (= headless daemon は自分で self-grant しない、 人間起点の launcher だけが授権する安全則)。

**既に bcodex セッション内に居る場合**: budget を付与し、 受信 bridge を armed で再起動:
```bash
beacon bus budget grant --turns <N>
beacon-codex-bridge restart --app-server --armed --cwd "$(pwd)"
```

起動後、 `status` で armed になったことを確認して user に伝える。

## `status` フロー

```bash
beacon bus budget show --json
beacon-codex-bridge status --cwd "$(pwd)"
beacon bus auto-execute list
```
以下を 1 画面で提示:
- armed か (budget 残回数)、 daemon が running か、 許可 channel
- audit: 自律返信の記録は `<cwd>/.beacon/codex/receive-loop.log` の `armed reply sent` 行、 および `beacon bus` 履歴で追える

## `stop` フロー (= disarm)

段階を user に選ばせる:

- **soft (= 返信だけ止める、 受信は継続)**:
  ```bash
  beacon bus budget clear
  ```
  budget が 0 になると daemon は自律返信しなくなる (= DM は inbox に貯まり、 次の prompt で見える通常挙動に戻る)。
- **hard (= 受信 daemon ごと止める)**:
  ```bash
  beacon-codex-bridge stop --cwd "$(pwd)"
  ```

## 安全則 (= AC6 まとめ)

1. **明示 opt-in が無ければ armed にならない**: budget grant (Step 2) を人間が承認しない限り autonomous 返信は起きない。
2. **budget gate で必ず止まる**: N 回で自動的に返信を止める。 暴走しない。
3. **self-grant 禁止**: headless daemon は自分で budget を付与できない。 launcher (= 人間起点) だけが授権する。
4. **disclosure gate**: 返信内容は server 側の envelope disclosure gate (= project の機密フィルタ、 ms-63) を通る。 armed でも機密が無制限に漏れる訳ではない。
5. **audit**: 全 autonomous 返信は receive-loop.log と bus 履歴に残り、 後から監査できる。

## 関連

- 対応 Skill: Claude Code 側 `/beacon-bus-armed` (= 同目的、 harness が違うので実装は別)
- 上位 task: ms-93 / e-2519 (= Codex push 受信経路: app-server D + exec-worker B + armed)
- 関連 Skill: `beacon-codex-bridge` (= 受信 daemon の lifecycle。 armed もこの daemon の一状態)
- 関連 CLI: `beacon bus budget` (= 回数上限)、 `beacon bus auto-execute` (= 許可 channel)、 `bcodex --armed` (= launcher)
