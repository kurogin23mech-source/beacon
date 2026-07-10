---
name: beacon-onboard
description: 新メンバーが招待されてプロジェクトに合流する初日のオンボーディング体験。プロジェクトの目的・直近の流れ・自分の担当範囲を 1 ターンで把握できるようにする。/beacon-init は新規プロジェクト作成、これは既存プロジェクトへの合流。
version: 0.1.0
---

# Beacon Onboard

> 招待された新メンバーが、合流初日に **「このプロジェクトは何のためにあって / いま何が起きていて / 自分は何を担当するのか」** を 1 ターンで掴むための Skill。

## 文章の書き方 (Beacon 全体の哲学)

Beacon に書き込む全ての文章 (task / マイルストーン / Operation / コミット / PR / レビュー / ドキュメント / ノート / セッションログ / リリース / デプロイ) は、**非開発者を含む読み手** が読めるように書く。これは Skill ごとの方針ではなく Beacon プロジェクト全体の哲学。

### 守ること

1. **読み手目線 1 行から始める**: 「何が嬉しいか」「何が困るか」をユーザー体験の言葉で。技術用語ではなく価値で書く
2. **横文字 3 段階**:
   - そのまま OK: 固有名詞 (`Firestore` / `pipx` / `MCP` / `Tauri` / `WebSocket`)
   - 初出時に日本語注: 技術概念 (`allowlist (= 許可リスト)` / `opt-in (= 個別許可)` / `subcollection (= 子コレクション)`)
   - 日本語化が望ましい: 一般概念 (configure → 設定 / receiver → 受信側 / audit → 監査 / hit → 一致 / install → 設置 / merge → 取り込み / deploy → 配置)
3. **ID 参照には文脈**: `e-XXXX` / `UC?` / `ms-XX` は初出に必ず『何の話か』1 行添える。例 ✗「e-1140 の AC のうち」→ ✓「e-1140 (自動応答の受信側挙動を hook で扱う) の受入条件のうち」
4. **尻切れトンボ禁止**: 主語と述語を省略しない、論理関係を明示

### Self-review (生成直後に必ず)

文章を書いた直後、自分で読み返して以下を自問:
- (a) 読み手 (非開発者を含む) は 1 度読んで意味が取れるか?
- (b) 一般概念の横文字 (configure / receiver / audit / hit / install / merge / deploy 等) が残ってないか?
- (c) ID 参照に『何の話か』1 行添えたか?
- 違反していたら書き直し。enforce ではないが必須の self-check。

### 詳細

詳しい原則と例 (病理の typology / 例外ケース / 良い例・悪い例) は CORE doc `entry-writing-principle` (doc_id `F3ZkqT0pKS6JpR8dn70n`) 参照。ただし本 Skill では上記 4 項目を **常に top of mind** で適用する (CORE 参照は補足、principal は本文埋め込み)。

---

## 前提条件チェック

Bash ツールで以下を実行:
```bash
beacon-find-root >/dev/null && echo "OK" || echo "NO_BEACON"
```
- `NO_BEACON` の場合、`beacon cloud join <project-id>` でまずプロジェクトに合流するよう案内して終了

## Step 1: ユーザー識別

```bash
beacon member list --json
```

結果から、現在の git config user.email / user.name と一致するメンバーを探す。

| ケース | アクション |
|---|---|
| 一致するメンバーあり | そのメンバーが「自分」。Step 2 へ |
| 未登録 | 「このプロジェクトのメンバーとしてまだ登録されていません。プロジェクトのオーナーに `beacon member add <あなたの id> --role contributor` をお願いしてください」と返して終了 |

## Step 2: プロジェクト概要の読み上げ

並列で取得:
```bash
beacon doc show project-vision
beacon doc list --scope core --json
```

これらを 1 ターンで読み、**3〜5 文の要約** を作成してユーザーに提示する:

```
ようこそ [member.name] さん!

プロジェクト概要:
  [project-vision の「大目的」を 1 行で]
  [ターゲットユーザー: ...]

現在地（直近のフォーカス）:
  [project.summary が示す経緯を 1〜2 文で]

参照すべき CORE 原則:
  - [doc-classification など、特に重要なもの 2〜3 件をタイトルだけ列挙]
```

## Step 3: 担当範囲の提示

```bash
beacon status --json
```

結果から:
- `milestones[]` で `assignee == "<member-id>"` のものを抽出 → **「あなたが assignee」**
- `milestones[]` で `owner == "<member-id>"` のものを抽出 → **「あなたが owner」**
- `assignee == ""` で `status == "in_progress"` のもの → **「アサイン待ちの活発な MS」**

提示形式:

```
あなたの担当範囲:

  あなたが assignee の MS:
    [ms-id] [title] ([progress]% / [done_tasks]/[total_tasks])

  あなたが owner の MS:
    [ms-id] [title] ([status])

  まだアサイン待ちの活発な MS（合流に向く候補）:
    [ms-id] [title] — [objective を 1 行で]
```

担当 MS がゼロの場合は「まだ担当MSが割り当てられていません。オーナーに相談するか、上の候補から `beacon milestone update <ms-id> --assignee <あなたの id> --reason '自薦'` で自分を割り当ててください」と提示。

## Step 4: 直近の流れの提示

直近の動きは `/beacon-retrospect` Skill (= プロジェクト史を自然言語で問い合わせる Skill) を呼んで取得する。Skill 起動が重い局面では、簡略版として `beacon search` で同等の入力を取れる:

```bash
beacon search "" --from $(date -v-14d +%Y-%m-%d) --limit 20
```

結果から **「メンバー全員のここ 2 週間の主要な動き」** を 3〜5 件にまとめて提示:

```
ここ 2 週間で起きたこと:
  - [日付] [actor]: [summary]
  - ...
```

これにより、合流時点で「直近何が話題になっていたか」を体感できる。

## Step 5: 次のアクション提案

最後に、合流初日にやるべきことを 2〜3 個提案:

```
次の一歩としておすすめ:
  1. あなたの担当 MS の SPEC を読む → `beacon doc list --scope spec --ms <ms-id>`
  2. 担当 MS が無ければ オーナーに「アサインしてもらえますか？」を聞く
  3. Web UI を開いて Active Members タブで他メンバーの動きを見る （該当機能が無ければスキップ）

何か質問があれば気軽に聞いてください。
```

## Step 6: DM 機能 (multi-session messaging) の有効化提案 (ms-54 e-1238)

合流時のオンボーディングが終わったあと、最後に 1 回だけ DM 機能の有効化を提案する。チーム作業ではこの機能がほぼ必須 (オーナーや先輩メンバーとリアルタイムに同期しながら走れる) なので、新メンバーには特に有用。

### Step 6a: opt-out 判定

opt-out が既に立っている場合は **沈黙**。Bash で以下を実行:

```bash
cd "$PROJECT_DIR" && python3 -c "
import os, json, sys
if os.environ.get('BEACON_NO_BUS', '').strip() in ('1','true','yes','on'):
    print('opted_out'); sys.exit(0)
p = os.path.join('.beacon','project.json')
if os.path.exists(p):
    try:
        with open(p) as f: d = json.load(f)
        if (d.get('bus') or {}).get('disabled'):
            print('opted_out'); sys.exit(0)
    except Exception: pass
g = os.path.expanduser('~/.beacon/config.json')
if os.path.exists(g):
    try:
        with open(g) as f: d = json.load(f)
        if (d.get('bus') or {}).get('disabled'):
            print('opted_out'); sys.exit(0)
    except Exception: pass
print('ok')
"
```

stdout が `opted_out` の場合は提案を出さない。`ok` の時のみ Step 6b に進む。

### Step 6b: 提案

```
最後に 1 つ。Beacon の DM 機能を有効化しますか？
オーナーや先輩メンバーが同じプロジェクトを別端末で開いていれば、
今このターミナルから直接「○○について相談したい」と DM を打てます。

  - はい → `beacon channel install` で有効化、bclaude で起動できる
  - あとで → 好きな時に `beacon channel install` で有効化
  - 要らない → `beacon channel opt-out` で将来の auto-install も止める
```

「はい」で `beacon channel install` を実行。「あとで」「要らない」は何もしない。

## GitHub owner / repo の取り扱い (= 推測禁止 / e-2370)

onboarding 中に install 手順 / clone コマンド / repository URL 等を生成・提示する場面が出てきたら、 GitHub の **owner (= 所有者) と repository 名は AI が推測してはならない**。 過去の dogfood (= 2026-06-24) で、 install prompt 生成時に AI が owner 名を勝手に推測 (= hallucination) して wrong owner の clone URL を流す事故が観察された。 user が手元で気付かないと、 wrong owner の clone url が install 手順に流れ、 install 失敗 / 別 user の repo を clone する事故 (= silently wrong url) になる。

### 構造的ルール

1. **取得は構造化 source から**: GitHub owner / repository 名が必要になったら、 必ず以下の順序で **コマンド実行による取得** を行う:
   ```bash
   # 第 1 候補: gh CLI (= 認証済 / fast)
   gh repo view --json owner,name -q '.owner.login + "/" + .name' 2>/dev/null

   # 第 2 候補: git remote (= gh が無い時)
   git remote get-url origin 2>/dev/null \
     | sed -E 's|^.*github\.com[:/]([^/]+)/([^/.]+)(\.git)?$|\1/\2|'
   ```

2. **取得失敗時は placeholder を残す**: 両方失敗 (= git remote が無い / GitHub 以外の remote / コマンド未インストール) したら、 AI 推測で埋めずに **明確な placeholder** をそのまま残す:
   - owner: `<your-github-owner>`
   - repo:  `<your-repo-name>`
   - URL 例: `https://github.com/<your-github-owner>/<your-repo-name>`

3. **絶対禁止**:
   - cwd ディレクトリ名 / プロジェクト名 / 招待主の email から owner を類推する
   - 過去の文脈 / training data から既知の owner 名を流用する
   - 「たぶん」「推定」 で owner を埋める

4. **user への提示**: placeholder を残した時は「GitHub owner / repo は取得できませんでした。 install 前にこの placeholder を user の値に置き換えてください」 と明示的に伝える。

この forcing function は本 Skill だけでなく、 `/beacon-init` Skill および server/static/join.html `buildSetupPrompt` (= 招待 install prompt) でも適用される (= 全 install prompt 経路に共通)。

## 制約

- **読み取り専用**: project.json への書き込みは一切行わない（自分のアサイン変更も含めて、明示的な beacon milestone update をユーザーに打ってもらう）
- **誰がメンバーかは beacon member list が唯一のソース**: git の commit author 等を勝手にメンバーと判定しない
- **このSkillは 1 ターンで完結させる**: Step 1〜5 を一気に出力する。長い対話を要求しない（onboarding は初日 5 分で済むべき）

## 関連

- CORE doc: `project-vision` (Step 2 の主材料)
- 関連 task: e-624 (member CLI), e-625 (owner/assignee), e-626 (Active Members UI)
- 既存 Skill: `/beacon-session-start` (再訪時のコンテキスト復元), `/beacon-init` (新規プロジェクト作成、これとは別)
