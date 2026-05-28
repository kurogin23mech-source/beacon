# Retrospection 方法論

> `/beacon-retrospect` Skill の companion ドキュメント。プロジェクト史を自然言語で問い合わせ、関連記録を統一検索 API で集めて要約するための作法を集約する。

## 設計原則

### 0. 「AI に聞けば、AI が引っ張ってきて要約する」体験を作る

CORE doc `Beacon の本質: Agentic AI のメモリ` の通り、Beacon は **AI が書く・AI が読む・人間は監査** が core value。  
retrospection は AI 主導の機能であり、ユーザーは Web UI をブラウズしなくても、Claude Code に話しかけるだけで過去を辿れる体験を作る。

UC10 SPEC では「Web UI は補助、Claude Code が主軸」と framing が明示されている。

### 1. 単一エンドポイントを使う

CORE doc `Beacon 検索基盤の原則` に従い、`beacon search` (= `lib/search.search_project`) を唯一の検索経路として使う。  
複数 CLI を組み合わせて自前で結合検索する実装はしない。AI が API を **重ね呼び** することはあっても、独自検索アルゴリズムは書かない。

### 2. 戦略 A 単発 sweep を当面採用

ms-40 SPEC §3 で戦略を 4 案比較した結果、初期実装は **戦略 A 単発 sweep**:

- 1 回だけ `beacon search` を呼ぶ
- 結果から要約する
- 戦略 B (再帰展開) / C (クエリ意図分類) / D (広く拾って深掘り) は将来検討

理由: シンプルさ優先、まず動くものを出す。ユースケース拡大に応じて戦略を進化させる。

### 3. 引用が必須

要約だけで終わらせると、ユーザーが裏を取れない。
出力には **必ず引用元** (エントリ ID + 種別 + 日付 + Web UI deep link) を付ける。

「○○は実装済みです」だけでなく「e-XXX (commit, 2026-05-27, hash abc1234) で実装」と書く。

---

## クエリ解釈のパターン

### 「○○の機能って実装したっけ？」型

- **意図**: 機能の実装有無を知りたい
- **抽出**: `q="○○"` `--type task,commit,document`
- **件数分岐**:
  - 0 件 → 「実装記録なし」
  - 1-5 件 → 「これらの記録があります、実装済みのようです」
  - 6+ → 候補確認

### 「○○ってどう実装したっけ？」型

- **意図**: 実装方法・設計判断を知りたい
- **抽出**: `q="○○"` `--type task,commit,document` (document scope: spec, memo, retro)
- **追加 fetch**: ヒットした SPEC document の full content を `beacon doc show` で取得して要約

### 「ms-XX の判断軌跡を教えて」型

- **意図**: 特定 MS の design rationale が知りたい
- **抽出**: `--ms ms-XX` (q なし) `--type document,task,commit`
- **追加 fetch**: ms-XX に紐づく SPEC document を必ず読む

### 「○月の××」型

- **意図**: 特定期間のテーマ
- **抽出**: `q="××"` `--from YYYY-MM-01 --to YYYY-MM-31`
- **件数注意**: 期間が広いと件数膨らみがち

### 「あの時の auth エラー」型

- **意図**: 過去のインシデント / バグ
- **抽出**: `q="auth エラー"` `--type incident,commit,task` `--include-closed` (Incident 履歴 e-619 CLI)
- **時間表現が曖昧** → 件数次第で聞き返し

### 「最近どう？」「あれの件」型

- **意図**: 曖昧、要追加情報
- **アクション**: クエリ解釈不能として **聞き返す**
  - 「最近 = 過去 1 週間 / 1 ヶ月 / 全期間 のどれですか？」
  - 「あれの件 = どのトピックですか？ MS-id / 機能名 / 期間 など」

---

## 件数による分岐のルール

| 状況 | 件数閾値 | アクション |
|---|---|---|
| 識別子が明確 (ms-X, e-X, op-X が含まれる) | - | 自律的に取得 → 要約 |
| 通常クエリ、ヒット少 | 1-5 | 自律的に要約 + 引用 |
| 通常クエリ、ヒット中 | 6-15 | 候補リスト提示 → ユーザーが選択 → 深掘り |
| 通常クエリ、ヒット多 | 16+ | 絞り込み条件を聞き返し |
| 時間表現が曖昧 | - | 「期間は？」を聞き返し |
| ヒット 0 | - | 「見つからない、別キーワードや期間で再試行を提案」 |

これは固定ルールではなく、AI 判断の **目安**。クエリの性質によって調整して構わない (例: 「全 MS のステータス教えて」のような meta-query は 16+ でも自律的に答える)。

---

## 出力フォーマット

### 通常ケース (1-5 件、自律的回答)

```
**結論**: ○○は実装済みです (commit b482c53 / 2026-05-27)。

**根拠**:
  - **e-559** (commit, 2026-05-27, hash b482c53)
    feat(skill): auto-open Web UI on init / session-start (A-method)
    🔗 https://beacon-ai.dev/?project=<id>#ms-37#e-559

  - **e-557** (task, done, 2026-05-27)
    UC-F26: Web UI 自動オープン (A 方式)
    motivation: ターミナル+Web UI 並列表示前提を成立させる
    🔗 https://beacon-ai.dev/?project=<id>#ms-37#e-557

**関連**: ms-37 e-555 で /beacon-log Skill 完了報告に Web UI リンクを含める拡張が todo です。
```

### 0 件ケース

```
○○に関する記録は見つかりませんでした。

検索したスコープ: type=task,commit,document / 期間=全期間
- 別キーワードで再試行: 「△△」「××」など類似語があれば
- 関連 MS を直接見る: `beacon search "" --ms ms-XX`
```

### 16+ ヒットケース

```
○○で 23 件ヒット。絞り込みが必要です:

候補:
  - 期間: 過去 1 ヶ月 (推奨) / 過去 1 週間 / 全期間
  - タイプ: task のみ / commit のみ / document も含むか
  - MS: 特定 MS に絞る (active な ms-XX / ms-YY / ...)

どう絞りますか？
```

---

## アンチパターン

### NG: 要約だけで引用が無い
> 「○○は実装済みです」

→ どこで実装されたか辿れない、信頼性ゼロ

### NG: コミットメッセージそのまま転記
> 「commit b482c53: feat(skill): auto-open Web UI on init / session-start (A-method)」

→ 開発者視点 (生コミットメッセージ) のまま。ユーザー視点で「何ができるようになったか」を 1 文で説明する

### NG: 検索結果を全件貼り付け
> 「e-559, e-557, e-554, e-553, ..." (30 件並べる)

→ 要約せず転記しただけ。AI の解釈価値ゼロ

### NG: 識別子が曖昧なまま深掘り
> 「auth エラー」→ 8 件ヒット → 全部を要約

→ 6-15 件は **候補確認** すべき

---

## エッジケース

### 検索 API が失敗した場合

`beacon search` が non-zero で終了 or 不正な JSON を返した場合:
- ユーザーに「検索 API が応答しません」と素直に報告
- 代替で `beacon status --json` などで簡易フォールバック検索を試みても良いが、その旨を明示する

### 古いプロジェクトで type フィールドが欠落している場合

`entity_type` が不明なエントリは type icon を `?` で表示し、内容のみ伝える。type 推測はしない (誤った分類は混乱の元)。

### Cloud mode でローカルキャッシュが古い場合

ローカル `cmd_search` は project.json のキャッシュを読む。最新でない可能性。  
クエリ結果に違和感があれば、ユーザーに「クラウドから最新を取り直すには Web UI を確認してください」と添える。

---

## /beacon-retrospect の将来拡張

戦略 A 単発 sweep からの進化候補:

| 戦略 | いつ採用するか |
|---|---|
| **B** 再帰展開 | 「ms-X の全関連」のように深く辿りたい用途が増えたら |
| **C** クエリ意図分類 | クエリ分類モデルが精度高く回るようになったら |
| **D** 2-pass (広く拾って深掘り) | 通常クエリの取りこぼしが目立つようになったら |

判断基準は **「ユースケースが広がってきたら」** (ms-40 SPEC §3 通り)。

---

## 関連

- CORE doc: `Beacon 検索基盤の原則` (`ZkH0vYu9QMMeGRZpW3dq`)
- SPEC: `検索基盤の実装方針` (`3ne57ccZegYQXDQA03op`)
- 関連 Skill: `/beacon-vision` (Skill + companion の構造パターン)
- 関連 CLI: `beacon search`, `beacon doc show`, `beacon incident list` (`--include-closed`)
- 関連 MS: ms-40 (本 Skill 出自), ms-43 e-616/e-631 (Web UI 側の検索拡張、AI と人間で同じ API を使う設計)
