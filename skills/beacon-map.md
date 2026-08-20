---
name: beacon-map
description: アプリケーション全貌マップ (= 今このプロダクトに何ができるかを写した現在地の索引、CORE doc `application-map`) を生成・再生成・reconcile する。無ければ全 surface を列挙して初版を作り、有れば source と照合して足す＆消す。新機能を足す前に「近い既存機能があるか」を引ける状態を保つ。
version: 1.0.0
triggers:
  - /beacon-map
  - 全貌マップ
  - application map
  - 機能マップを作って
  - 機能マップを更新
---

# Beacon Application Map

## profession gate (ms-109 e-3404)

まず `beacon status --json` の `profession` を確認する。`dev` 以外 (例 `sales`) の
プロジェクトでは、全貌マップは **開発インスタンスの surface (= コード / CLI / Skill の入口) 索引** で
あって当該職種の対象外なので、**何も生成・reconcile せず 1 行で断って終了する**:

```
全貌マップ (application-map) は開発インスタンス専用の索引です。この
プロジェクト (profession=<値>) では対象外なので生成しません。
```

`profession` が `dev` または未設定のときのみ、以下の本編に進む。

> **全貌マップ** = project-vision (目的地) / milestone 履歴 (軌跡) に続く **3 つ目の軸 = 現在地の断面**。
> 「今このプロダクトが何であるか (= 何ができるか)」を写した索引。新機能を足す前にここを引いて
> **二重実装を防ぐ** (= 似た機能が既にあるかを 1 発で確認できる) のが唯一の目的。
>
> 実体は CORE doc `application-map` (固定 id、project-vision と同格の常設・session-start 常時参照)。
> 機械照合は `scripts/check-map-drift.py` (= 機械的 reconcile)、本 Skill は **AI 判断 (surface を価値で束ねて
> 散文化する / drift を curate する)** を担う分業 (= Beacon の tool/skill 分離原則、ms-104 SPEC)。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全文章は非開発者を含む読み手に読めるように書く。全貌マップの surface 説明も同じ。

1. **読み手目線 1 行**: surface は「何ができるか」を価値の言葉で 1 行。実装手段は書かない
2. **横文字 3 段階**: 固有名詞 (`MCP` / `Firestore` / `Tauri`) はそのまま / 技術概念は初出に日本語注 / 一般概念は日本語化
3. **ID 参照に文脈**: `ms-XX` / `e-XXXX` の初出に『何の話か』1 行
4. **尻切れトンボ禁止**: 主語・述語・論理関係を省略しない

詳細は CORE doc `entry-writing-principle` 参照。

---

## 前提条件チェック

Bash ツールで実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、このSkillは何もせず終了する。

repo root を保持 (以降 `__ROOT` と表記):
```bash
__ROOT=$(beacon-find-root)
```

## Step 0: 既存判定 — 生成モードか reconcile モードか

Bash ツールで実行:
```bash
beacon doc show application-map 2>/dev/null && echo "---EXISTS---" || echo "---MISSING---"
```

- `---MISSING---` → **Step G (生成モード)** へ (= 初版を作る / 既存プロジェクトの backfill)
- `---EXISTS---` → **Step R (reconcile モード)** へ (= 既にある地図を source と照合して足す＆消す)

---

## Step R: reconcile モード (地図が既にある)

### R1. 機械照合を走らせる

```bash
python3 "$(beacon _install-root)/scripts/check-map-drift.py" --doc-id application-map
```

出力の `書き漏れ (missing)` と `幽霊 (phantom)` を読む。

- **`SKIP:` で始まる行が出た (e-5320)** → このプロジェクトは beacon 本体の source repo ではないため、機械照合 (書き漏れ / 幽霊検出) が使えない。機械照合の真値源 (beacon の CLI/API/Skill 構造) は beacon 固定なので、他プロジェクトの map には安全網が無い。ユーザーに「この map は AI 維持のみ (機械の安全網なし) です」と 1 行伝えた上で、機械照合の結果に頼らず R2 を **AI 判断だけ**で進める (= source と付き合わせる代わりに、地図本文と実プロジェクトの実態を AI が突き合わせて curate する)。R3 の再照合 (exit 0 確認) も同様に SKIP になるので、drift ゼロの機械確認は求めない。
- **両方 0 (exit 0)** → drift 無し。「全貌マップは source と一致しています (書き漏れ0 / 幽霊0)」と 1 行返して終了。
- **どちらか > 0** → R2 へ。

### R2. drift を curate する (AI 判断)

現在の地図本文を取得:
```bash
beacon doc show application-map
```

機械照合が挙げた各項目を、**足す / 消す** で解消する:

- **書き漏れ (missing)** = source に実在するが地図に無い surface。
  - その surface がどの価値エリア (章) / 価値 (節) に属するかを AI が判断し、価値 1 行 + 楔を足す。
  - 単発でなく noun / prefix 単位でまとめて増えたなら **family 楔** に寄せる (下記の楔文法参照)。
- **幽霊 (phantom)** = 地図にあるが source に無い surface。
  - 実装が消えた (= 廃止) なら、その行を地図から**消す**。
  - 綴り / メソッド名の誤り (例: `GET` を `WEBSOCKET` に) なら**直す**。
  - source 側にあるはずなのに列挙されない場合は enumeration 漏れを疑い、`scripts/check-map-drift.py` の対象 (dispatch dict / route decorator / skills dir) を確認。

### R3. 更新して drift ゼロを確認

curate した本文で更新 (stdin 経由):
```bash
printf '%s' "<curate 済み本文>" | beacon doc update application-map --content -
# もしくは一時ファイルに書いてから: cat /tmp/map.md | beacon doc update application-map --content -
```

再照合して **書き漏れ0 / 幽霊0 (exit 0)** を確認:
```bash
python3 "$(beacon _install-root)/scripts/check-map-drift.py" --doc-id application-map
```

ゼロになるまで R2〜R3 を繰り返す。ゼロになったら結果をユーザーに 1 行報告して終了。

---

## Step G: 生成モード (地図がまだ無い)

### G1. 実在 surface を列挙する

```bash
python3 "$(beacon _install-root)/scripts/check-map-drift.py" --enumerate --json
```

`cli` (dispatch dict のキー) / `api` (route) / `skill` (skills dir) の 3 集合が返る。これが**網羅すべき全 surface**。
裏方の仕組み (= CLI verb を持たない受信ループ / hook / storage backend 等) は enumeration に出ないので、
source を読んで AI が拾う (`file:` 楔で表す、下記参照)。

### G2. 価値エリア (章) → 価値 (節) → surface の3段で束ねる

**種**: `beacon status --json` の milestone 一覧と project の `objective` を下敷きに章立てを起こす。ただし
milestone は「軌跡」なので、done / 廃止された機能は**現在地に刈り込む** (= 現存する surface だけ地図に載る)。

**構造**: `## X. 章タイトル (価値エリア)` → `### X1. 節タイトル (価値)` → 箇条書きの surface 行。
- 章は 7±2、数ヶ月不変になる粒度で切る (= 変化速度の遅い層)
- surface 行は「価値を 1 行、ユーザーの言葉で」+ 行末に楔

### G3. 楔文法 (照合の核) — 各 surface 行に必ず添える

楔は `` `type:ident` `` の 1 形式。読む時は視線を滑らせてよい軽い付随情報。

| type | exact | family (束ね) |
|---|---|---|
| `cli:` | `` `cli:beacon task done` `` | `` `cli:beacon task *` `` (= その noun 配下すべて) |
| `api:` | `` `api:POST /api/projects/{id}/entries/{e}/done` `` | `` `api:* /api/treks/*` `` (= method `*` + path prefix) |
| `skill:` | `` `skill:/beacon-task` `` | (原則 exact) |
| `file:` | `` `file:channel/bus.mjs` `` | glob 可 `` `file:scripts/check-*.py` `` (= 裏方の仕組み用) |

- **family を default** にして地図が全 leaf 列挙で膨らむのを防ぐ (= 例: `beacon trek` の 29 サブコマンドは `cli:beacon trek *` 1 本)
- 個別 exact は「価値が節をまたいで割れる時だけ」
- 楔は `type:` プレフィクス必須。だから散文中に例として書いた素の `beacon <サブコマンド>` は照合対象外 (= 誤検出しない、例を自由に書ける)
- 裏方の仕組みは **二重実装リスクが宿る所だけ** `file:` で楔る (= 受信ループ / hook 入口 / storage backend)。drift check 群のように保守価値の低い集合は glob-family 1 本で足りる

### G4. reconcile でゼロにする (= 生成の検算)

初版本文を一時ファイルに書き、照合:
```bash
python3 "$(beacon _install-root)/scripts/check-map-drift.py" /tmp/application-map-draft.md
```
`書き漏れ` が残れば足す、`幽霊` が残れば消す/直す。**書き漏れ0 / 幽霊0 (exit 0)** になるまで直す。
これが「網羅した」ことの機械的保証 (= 目視での取りこぼしを塞ぐ)。

- **`SKIP:` が出た場合 (e-5320)** → beacon 本体以外のプロジェクトでは機械照合が使えない (真値源が beacon の surface 固定)。この検算はスキップし、AI が目視で網羅性を担保して次へ進む。ユーザーに「機械の検算は使えないため AI 判断で網羅した」と 1 行添える。

### G5. CORE doc として登録

drift ゼロを確認した本文を、固定 id `application-map`・scope core で登録:
```bash
cat /tmp/application-map-draft.md | beacon doc add "アプリケーション全貌マップ" --scope core --id application-map --content -
```

登録後、live doc を再照合してゼロを確認:
```bash
python3 "$(beacon _install-root)/scripts/check-map-drift.py" --doc-id application-map
```

ユーザーに「初版を N surface で生成、drift ゼロ」と報告して終了。

---

## 分量制約 (両モード共通)

地図全体を **session-start の予算内に収める** (= 肥大させない、目安 4k tokens 程度)。
超えそうなら **surface を消すのではなく family 楔に集約して圧縮する** (= 網羅は保ったまま表現を畳む)。
章を 2 段に潰せる余地があれば畳む (SPEC §2)。

## 制約

- **機械照合ロジックは `scripts/check-map-drift.py` に集約**: 本 Skill 内で surface 列挙を独自実装しない (= 真値源を 1 つに)
- **reconcile はメンテのトリガーでもある**: append (足すだけ) にしない。幽霊 (= 廃止 surface) の削除を必ずやる (SPEC §4)
- **地図は「現在地」であって「軌跡」ではない**: done / 廃止された機能は載せない。milestone は初回の種にするだけ (SPEC §6)
- **drift ゼロで終える**: 生成・reconcile どちらも、最後に `--doc-id application-map` で exit 0 を確認してから終了する

## 関連

- 機械照合スクリプト: `scripts/check-map-drift.py` (= surface 列挙 + 両方向 diff、e-3151)
- CORE doc `application-map` (= 生成物、e-3152)
- ms-104 SPEC (= 全貌マップの設計方針): doc_id `amSbihUWTZ8Pd3WyxfdU`
- 自動メンテ経路 (ms-104 e-3342 で再配置): **主 forcing function は出荷フロー** = `/beacon-deploy` の Step 4.5 と `/beacon-push` の release 判定後 (Step 4.5) が「出荷した瞬間に地図を直す」を促す。map-drift trigger は **release 数基準の低優先 backstop** (= 出荷フローで取りこぼした時の安全網、旧 commit 数 proxy から e-3342 で降格)。deploy record が残す `map-reconcile` trigger も session-start で再掲される安全網。
- session-start が「地図が無ければ本 Skill を提案」する (= 既存プロジェクトの backfill 契機)
- 関連 MS: ms-85 (surface area を畳む側 / 本 MS は見える化する側で対)
