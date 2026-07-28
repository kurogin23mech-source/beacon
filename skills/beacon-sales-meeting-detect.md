---
name: beacon-sales-meeting-detect
profession: sales
description: 終了予定を過ぎた面談を Google カレンダーで突合し、実際に終わったものを「終了」に落として次の終了ワークフロー(議事録→判定→次活動)へ渡す。定期発火(server tick)で自律実行、または「面談の終了チェック」等で手動起動。営業 (profession=sales) 専用。
version: 1.0.0
triggers:
  - /beacon-sales-meeting-detect
  - 面談の終了チェック
  - 終了した面談
  - ミーティング終了検知
---

# Beacon Sales Meeting Detect (終了検知 C)

> ms-107 e-3434。B (`/beacon-sales-schedule`) が面談予定に埋め込んだ Beacon 識別 ID
> (`beacon-meeting-id: mtg-N`) を使って、**終了予定を過ぎた面談が実際に終わったか**を
> Google カレンダーで突合し、終わったものを「終了」に落とす。落ちた面談は次段の
> 終了ワークフロー A (議事録取得→フェーズ遷移判定→次活動提案、e-3435) の入力になる。
>
> 検知は機械、判断境界は人 — この Skill は「終わった」を検知して記録するところまで。
> フェーズを実際に進める確定は A(人間確認付き) が担う。
>
> 実行形態: 実際の実行は client セッションの Skill。定期発火は server tick が
> operation-trigger を投げて起こす (別 task e-3434 の配線側)。手動起動でも同じ動作。

## 文章の書き方 (Beacon 全体の哲学)

記録・報告は非開発者が 1 行で状況を掴める自然な日本語で。社内略語を持ち込まない。

## 前提条件チェック

Bash ツールで営業プロジェクトかを確認:

```bash
ROOT=$(beacon-find-root) && \
  test "$(python3 -c "import json;print(json.load(open('$ROOT/.beacon/project.json')).get('profession',''))" 2>/dev/null)" = "sales" \
  && echo "SALES_OK" || echo "NOT_SALES"
```

`NOT_SALES` なら「営業プロジェクトでのみ使えます」と伝えて終了。以降 `$ROOT` は
`beacon-find-root` の出力。内部コマンドは `python3 "$(beacon _lib-path)/commands.py" <cmd>` で呼ぶ。

## Step 1: 現在時刻の取得 + 終了候補の洗い出し

まず基準時刻を取る (カレンダー MCP の `get-current-time`、無ければシステム時刻でよい)。
続いて Beacon 側で「終了予定を過ぎたのにまだ scheduled の面談」を洗い出す:

```bash
beacon meeting ended --now "<現在時刻 ISO8601>" --json
```

`ended[]` が空なら **Step 4 (記録)** に飛び、「終了検知対象なし」で終わる。返る各面談には、
突合に使う情報 (面談 ID・商談 ID・予定日時・カレンダー情報・識別タグ) と、下記の
**「この面談がフェーズを進める判定の起点か」の印**が付く。[^detect-fields] この一覧が
**突合の候補**。

**この面談がフェーズを進める判定の起点か (= 大事な商談ステップか) (e-3581/e-3583)**:
各面談には、それが**その商談のフェーズを次へ進める判定の起点になっている面談 (= 大事な
商談ステップ)** か、それとも**関連の打ち合わせ**かの印が付いている。判定の起点になっている
面談が終わると、その商談のフェーズ判定 (次段 A) を促す。関連の打ち合わせ (事前すり合わせ等) は
終わっても議事録の取り込みはしてよいが、**フェーズは動かさない** (商談ステップに紐づけていない
面談でフェーズが誤って進む事故を構造で防ぐ)。この振り分けは手順書の判断でなく、面談ごとに
付いたこの印 (データ側の真値) に従う。

## Step 2: 各候補をカレンダーで突合 (実際に終わったか)

候補は「Beacon の予定表では終わっているはず」だが、カレンダー側で直接リスケ/キャンセル
されている可能性がある (二重管理の綻び)。真値はカレンダー。各候補について:

### Step 2a: 使うカレンダーの解決

候補の `calendar_namespace` / `calendar_account` があればそれを使う。無い (旧レコード等)
場合は送信アカウント台帳から calendar route を解決する (`/beacon-sales-schedule` Step 2
と同じ経路、手書きしない):

```bash
BEACON_SEND_SERVICE="calendar" BEACON_SEND_LABEL="" \
  python3 "$(beacon _lib-path)/commands.py" sales_account_resolve
```

解決した namespace の MCP ツール群を `$CALNS`、account を `$CALACCT` とする。

### Step 2b: 予定を引いて状態を判定

`calendar_event_id` があれば `$CALNS` の `get-event` でその予定を引く。無ければ
`$CALNS` の `list-events` で対象期間を引き、説明文に `tag` (`beacon-meeting-id: mtg-N`)
を含む予定を探す (突合の handshake)。判定:

| カレンダー側の状態 | 意味 | アクション |
|---|---|---|
| 予定が存在し、終了時刻が過去 | 実際に終わった | **終了**に落とす (Step 3-ended) |
| 予定が存在するが、開始が未来にずれている | カレンダーで直接リスケされた | **Beacon を追従**させる (Step 3-reschedule)、終了にしない |
| 予定が見つからない / 削除されている | キャンセルされた | **取消**にする (Step 3-cancel)、終了にしない |
| カレンダー linkage が無い候補 (`calendar_event_id` 空) | 突合できない | Beacon の end_at を信頼して**終了**に落とす (Step 3-ended) |

先方都合の欠席/延期などで「予定はあるが実施されなかった」判断が要る場合は、機械では
決めきれないので終了に落とさず、その旨を Step 4 の報告に含めて人間判断に委ねる。

## Step 3: Beacon 側を確定

判定に応じて内部コマンドで確定する。すべて冪等 (二重起動しても壊れない)。

- **終了 (ended)**:
  ```bash
  BEACON_MTG_ID="<mtg-id>" python3 "$(beacon _lib-path)/commands.py" meeting_end
  ```
  → status=ended。これが終了ワークフロー A の入力キューになる。

- **追従 (reschedule)**: カレンダーの新しい日時に Beacon と遷移日を揃える:
  ```bash
  BEACON_MTG_ID="<mtg-id>" BEACON_MTG_AT="<新 ISO8601>" BEACON_MTG_END="<新終了>" \
    BEACON_MTG_SET_TRANSITION=1 \
    python3 "$(beacon _lib-path)/commands.py" meeting_reschedule
  ```

- **取消 (cancel)**:
  ```bash
  BEACON_MTG_ID="<mtg-id>" python3 "$(beacon _lib-path)/commands.py" meeting_cancel
  ```

## Step 3.5: 終了ワークフロー A への引き渡し

終了に落とした面談は、次段 A (議事録取得→フェーズ遷移判定→次活動提案、e-3435 =
`/beacon-sales-meeting-wrap`) の入力。ただし **A のフェーズ判定に進むのは
「フェーズを進める判定の起点」の面談だけ**:

- **フェーズを進める判定の起点 (= 大事な商談ステップ)** → 議事録取得 + **フェーズ判定案** の
  A フローへ。
- **関連の打ち合わせ** → 議事録を証跡として残すのはよいが、**フェーズ判定は促さない**。
  報告では「議事録のみ (フェーズ非対象)」と区別して見せる。
- status=ended の面談が A の処理キュー (= A は `beacon meeting ended` ではなく status=ended
  を対象に走る)。この Skill は二重に A を起動しない (終了済みは Step 1 の候補から外れる)。

## Step 4: 記録と報告

処理結果を簡潔に報告:

```
🔎 面談終了検知 (基準 <現在時刻>)
  終了に確定:   [mtg-N] <商談名> <日時>  → 終了ワークフロー待ち
  カレンダー追従: [mtg-M] <商談名> 旧<日時> → 新<日時>
  取消:         [mtg-K] <商談名>
  判断保留:     [mtg-L] <商談名> (理由: 予定はあるが実施未確認)
  対象なし:     終了検知対象の面談はありませんでした
```

自律実行 (server tick 由来) の場合、この結果を run record として残す配線は e-3434 の
Operation 側が担う (この Skill は検知と Beacon 確定に徹する)。異常 (カレンダー MCP 認証
切れ等) は incident として起票する余地を残す。

## 制約

- **終了/取消/追従の確定は冪等コマンド経由のみ** (`meeting_end` / `meeting_cancel` /
  `meeting_reschedule`)。project.json を直接書き換えない。
- **フェーズを進めない** — この Skill は「終わった」の検知まで。フェーズ遷移の確定は
  A(人間確認付き) の役割 (検知は機械 / 判断境界は人)。**関連の打ち合わせ (フェーズを進める
  判定の起点でない面談) が終わってもフェーズ判定は促さない** (紐づけていない面談で誤って
  前進する事故を構造で防ぐ)。
- **使うカレンダー (namespace/account) は候補レコードか台帳解決から取る**。手書きしない
  (会社用/個人用の取り違え防止、e-3365)。
- 二重起動しない: 終了済み (status=ended) は Step 1 の候補に出ないので、同じ面談を
  二度終了ワークフローに乗せない (SPEC AC)。

## 関連

- `/beacon-sales-schedule` — B: 面談予約 + 識別 ID 埋め込み (この Skill の突合相手)
- (未実装) A: 終了ワークフロー e-3435 — この Skill が終了に落とした面談を処理する次段
- SPEC `o83GEljD8xeFMr95wLTh` §5 (ms-107 実運用 engine、e-3374 の検知側)

[^detect-fields]: 実装上、`ended[]` の各要素は `meeting_id` / `opportunity_id` /
`scheduled_at` / `end_at` / `calendar_event_id` / `calendar_namespace` /
`calendar_account` / `tag` / `is_gate_anchor` を持つ。「フェーズを進める判定の起点か」の印は
`is_gate_anchor` (bool) で、`true` = 判定の起点 (大事な商談ステップ) / `false` = 関連の
打ち合わせ。
