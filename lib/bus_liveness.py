"""ms-165 / e-5965 — the *progress* dimension of session liveness.

Session liveness has three orthogonal dimensions (CORE doc
``liveness-three-dimensions``). Each answers a different question and is
computed from a different signal, so collapsing them into one field hides the
failure mode the others can't see:

  * **transport** — can a receive path reach this session at all?
    (``ws_live`` / ``poll_health.healthy`` → ``live``). The picker reads this.
  * **progress**  — is a live session actually CONSUMING (draining) its inbox,
    or receiving-but-not-consuming (wedged)?  (``draining`` → ``reachable``).
    Only the send-path strict check reads this. THIS MODULE.
  * **attention** — is a human/AI actively driving the session right now?
    (``heartbeat_fresh``). Informational only.

The wedge this dimension catches: a session whose bridge polls (so ``live`` is
true) but never actually takes the events addressed to it — DMs pile up
undelivered. The sender is fooled into "sent✓ delivered✗" because the transport
check only proves polling.

**何を "未消化" と数えるか (ms-173 / e-6799 で引き直した)**: 初版は recipient の
``bus_cursors`` をここの真値源にしていたが、**その cursor は実配信経路のものでない**。
cursor を進めるのは ``bin/beacon-bus-inbox-hook.py`` (= 人の打鍵ごとに走る hook) と
``beacon bus receive`` だけで、idle-wake を実際に配る MCP bridge は ms-140 で
**意図的に cursor から切り離されている** (自分の watermark を
``.beacon/bridges/<sid>.delivery.json`` に持ち、``bus_cursors`` を読み書きしない —
channel/bus.mjs の ms-140 コメント)。よって "cursor を越えていない" は
「配られていない」でなく「**人がそのセッションでまだ打鍵していない**」を意味していた。
結果、bridge が 1 秒で配った DM でも、人がその端末に戻るまで 5 分を超えれば
wedge と判定され、**DM が最も集まる本体セッションが恒久的に到達不能**になった。
そこで現在は per-event receipt (``delivered_at`` / ``delivered_by``) を消化の証拠に
使う (:func:`delivered_to_recipient`)。receipt は bridge が filter chain より前で全取得
event に刻む (e-1348) ので、「受信経路がこの event を取ったか」の網羅的な証拠になる。
cursor はここでは scan の起点 (= コスト境界) としてしか使わない。

Pure functions only — the impure store scan that produces
``oldest_unread_created_at`` lives on the server (``app._oldest_unread_addressed_created_at``).
Keeping the derivation pure lets "stale unread ⇒ not draining" be pinned
without a bus fixture.
"""
from __future__ import annotations

import datetime
from typing import Optional

# Send-path graded verdicts (ms-165 SPEC 方針 b). Named constants so callers
# compare against a symbol, not a bare string that could drift.
SEND_NORMAL = "normal"
SEND_WEDGED = "wedged"
SEND_NOT_LIVE = "not_live"

# ---------------------------------------------------------------------------
# ms-159 / e-6243 — the *work-unit state* projection (統合オペレーションUI).
#
# The 5 canonical work-unit states (作業単位状態モデル SPEC ``np2fSUqpE5LSIkOqHLuK``
# 判断1). A session/Operation run往復 between these; every UI / inbox / attention
#面 reads ONLY this small frozen set, never an executor's native vocabulary.
STATE_RUNNING = "running"                # 自律実行中 — no human attention needed
STATE_IDLE = "idle"                      # 生きて待機、判断要求なし — no attention
STATE_AWAITING_HUMAN = "awaiting_human"  # 具体的な判断要求が立っている — attention
STATE_BLOCKED = "blocked"                # 外部要因で停止 — conditional attention
STATE_TERMINATED = "terminated"          # 終了(completed/aborted/failed)
# ``unknown`` は状態集合の一員だが *宣言できない* 特別枠 (判断1補足 / 判断4):
# セッションは自分が unknown だと報告できない (死んだ executor は「私は死んだ」と
# 言えない)。server が不在検知 / 宣言不信で立てる、人間の注意を引く側の安全弁。
STATE_UNKNOWN = "unknown"
# ms-177 — ``interrupted`` (中断) is the OTHER server-raised state, and like
# ``unknown`` it cannot be declared: a session killed by a closed terminal / an
# API error / a context-limit exit does not get to file a report on its way out.
# It means "this WAS a real working session (it declared running/idle/
# awaiting_human/blocked) and then its receive path vanished WITHOUT going
# through ``beacon session end``" — i.e. it stopped by accident, not on purpose.
# Split out of ``unknown`` so the ops room can show an accidental death loudly
# instead of burying it in the same grey "stopped" bucket as a clean exit
# (ms-177 SPEC 方針1/2). ``unknown`` thereby returns to its one true meaning:
# live but never stated what it is doing.
STATE_INTERRUPTED = "interrupted"

# The states a session may self-declare (方針1: 自己宣言が正)。``unknown`` /
# ``interrupted`` は含まない — 宣言由来では決して現れない (判断4 / ms-177)。
DECLARABLE_STATES = frozenset({
    STATE_RUNNING, STATE_IDLE, STATE_AWAITING_HUMAN, STATE_BLOCKED,
    STATE_TERMINATED,
})

# The COMPLETE range of ``derive_state`` — every value a consumer (ops room,
# Go viewer, roster ordering, attention filter) may receive. Consumers that
# enumerate states MUST check exhaustiveness against THIS set, not against
# ``DECLARABLE_STATES | {STATE_UNKNOWN}``: the server-raised states are exactly
# the ones a consumer forgets, and a guard written from the declarable set stays
# green while a new state silently falls into some `.get(..., default)` bucket
# (ms-177 — that is how ``interrupted`` would have slipped past the existing
# ``_ROSTER_STATE_ORDER`` exhaustiveness test).
ALL_STATES = DECLARABLE_STATES | frozenset({STATE_UNKNOWN, STATE_INTERRUPTED})


# ms-173 独立レビュー AX-4 — **状態がその値になった「由来」**。
#
# ``unknown`` には 2 つの異なる経緯が畳まれている: (a) live だが一度も何をしているか
# 言っていない (ms-177 の本来の意味) と (b) ``running`` と宣言したのに declared_at が
# 止まった (e-6774 で足した投影先)。どちらも「生きているが何をしているか言えない」
# なので ``state`` としては同じ値が正しい — しかし **対応の仕方は違う**: (a) は宣言を
# 促す話、(b) は許可待ちで止まっている疑いが濃い (実測した 2 件はいずれも待機内容に
# 許可要求の文面が残っていた) ので人が見に行く話。名前 1 つに 2 つの原因を畳むと、
# consumer が (b) を (a) と読んで取り違える。
#
# 状態集合 (``ALL_STATES``) は増やさない: 全 UI / inbox / attention面 がこの小さな
# 凍結集合だけを読む契約 (ms-159 判断1) で、7 番目の状態を足すと全消費側に波及する。
# 代わりに **由来を別フィールドに並べて刻む** (先例: ``live_suppressed_reason`` =
# 「抑止を silent にしない。理由を行に刻む」)。
#
# **全ての導出経路に由来を付ける** のが要: 「由来が無い」と「由来が不明」が同じ空値に
# 畳まれると、また同じ取り違えが別の形で起きる (e-6777 の None/True、AX-2 の三値の
# truthy 畳み込みと同型の病理)。
STATE_ORIGIN_DECLARED = "declared"              # 宣言どおり (= 最も多い)
STATE_ORIGIN_STALE_RUNNING = "stale-running"    # running と言ったが止まった → unknown
STATE_ORIGIN_NEVER_DECLARED = "never-declared"  # live だが一度も言っていない → unknown
STATE_ORIGIN_TRANSPORT_LOST = "transport-lost"  # 受信経路が消え宣言も古い → interrupted
STATE_ORIGIN_NO_TRACE = "no-trace"              # 受信経路も宣言も無い → terminated

ALL_STATE_ORIGINS = frozenset({
    STATE_ORIGIN_DECLARED, STATE_ORIGIN_STALE_RUNNING,
    STATE_ORIGIN_NEVER_DECLARED, STATE_ORIGIN_TRANSPORT_LOST,
    STATE_ORIGIN_NO_TRACE,
})


# ms-173 / e-6775 — 「待機内容 (state_detail) を持つ状態」の正典。
#
# 不変条件: **state_detail は state に属する。両者は必ず一緒に動く。** 待ちでない状態
# (running / idle / terminated …) は待機内容を持たない。
#
# これを明文化する理由 (実害 2026-10-01 実測 6 行): session row は merge 保存なので、
# 宣言が awaiting_human → running に変わってもマーカーが state_detail を省くと **古い
# 待機内容が永久に残る**。`state=running` なのに「Claude needs your permission」が
# ぶら下がった行が現に 6 件あった。消費側 (lib/working_target) は待ち状態でしか detail を
# 読まないので画面には出にくいが、行の中身は嘘になっており、detail を読む別の consumer が
# 増えた瞬間に表に出る。
#
# この集合はここが唯一の定義。lib/working_target は別名で参照するだけ (以前は
# working_target 側に private な複製があり、server からは参照できなかった)。
WAIT_DETAIL_STATES = frozenset({STATE_AWAITING_HUMAN, STATE_BLOCKED})


def state_carries_wait_detail(state) -> bool:
    """``state`` が待機内容 (``state_detail``) を持つ状態か。

    持たない状態で detail が残っていたら、それは前の状態の残骸。保存側はここを見て
    空に揃える (:func:`state_detail_for_declaration`)。
    """
    return str(state or "") in WAIT_DETAIL_STATES


def state_detail_for_declaration(declared_state, state_detail):
    """宣言と一緒に保存すべき ``state_detail`` を返す。

    待ちでない状態なら ``""`` (= 空で上書きして古い残骸を消す)。待ち状態なら渡された値を
    そのまま (空なら空 — でっち上げない)。宣言が無い (= 状態を宣言していない heartbeat)
    なら ``None`` を返し、保存側は **何も触らない** (merge の既存値を保つ)。
    """
    if not declared_state:
        return None
    if not state_carries_wait_detail(declared_state):
        return ""
    return state_detail if isinstance(state_detail, str) else ""


def delivered_to_recipient(event, recipient_sid) -> bool:
    """``event`` を ``recipient_sid`` 自身の受信経路が実際に取得したか (ms-173 / e-6799)。

    真値源は per-event receipt (``delivered_at`` / ``delivered_by``)。これは
    ``set_bus_event_receipt`` が stage=``delivered`` で刻むもので、bridge は
    **filter chain より手前で全取得 event に刻む** (channel/bus.mjs e-1348 の
    「stamp delivered BEFORE the filter chain」)。よって「受信経路がこの event を
    取ったか」の網羅的な証拠になり、bridge が落とした event にも receipt が残る。

    ``delivered_by`` の一致を必須にする理由: receipt は stage ごとに
    first-write-wins で、**最初に取った 1 セッションの名前しか残らない**。
    broadcast event (= DM 以外の channel で宛先無指定) で「誰かが取った」を全員の
    消化と読むと、残り全員の本物の wedge をまとめて隠す。だから「自分が取った」
    以外は消化と数えない。

    ``delivered_at`` があるのに ``delivered_by`` が空の event は **帰属不明**なので
    消化と数えない (= 安全側。未消化として wedge 判定に残す)。
    """
    if not recipient_sid or not isinstance(event, dict):
        return False
    if not event.get("delivered_at"):
        return False
    return str(event.get("delivered_by") or "") == str(recipient_sid)


def derive_draining(oldest_unread_created_at, now, window_seconds) -> Optional[bool]:
    """Return whether a session is *draining* its inbox.

    - ``True``  — 未読のバックログがあり、その最古が ``window_seconds`` より新しい
      (= 実際に消化できていることを **観測した**)。
    - ``None``  — バックログが無い。消化できるかどうかの **証拠が無い** (下記参照)。
    - ``False`` — the oldest event still unread by the recipient is OLDER than
      ``window_seconds``: it is receiving (polling) but not consuming = wedged.
      ``None`` はほかに「判断する時刻が無い / 読めない」場合も返る。いずれも hard
      signal ではなく、呼び出し側は ``None`` を「確定的に wedge ではない」と扱うので、
      バックログの無い idle session が誤って落とされることはない。

    ms-173 / e-6777 — **バックログが無いときに ``True`` を返さない。**

    以前はここで ``True`` (健全) を返していた。そのため「誰からも送られないセッション」と
    「瞬時に消化しているセッション」が同じ最高評価になり、**受信していないセッションが
    最も健全に見える** 指標になっていた (実測 2026-10-01: live 14 行のうち 13 行が
    draining=True だが、その大半は送られた実績が無い。一方 DM が集まる本体だけが
    draining=False で到達不能と判定された)。指標が「受信しないこと」を報酬にしていた。

    消化の証拠が無いなら「不明」が正直な値。``True`` は **観測した** ときだけ出す。

    この変更が配信を壊さない理由: :func:`is_reachable` と
    :func:`classify_send_delivery` はどちらも ``False`` だけを悪い信号として扱い、
    ``None`` と ``True`` を同じく扱う (reachable / SEND_NORMAL)。よって送信経路の挙動は
    byte 単位で不変で、受信者を誤って落とすリスクなしに指標の嘘だけを消せる。

    ``oldest_unread_created_at`` is the ``created_at`` of the OLDEST event
    addressed to the recipient that **その recipient 自身の受信経路がまだ取って
    いない** もの、または backlog が無いときは falsy。ms-173 / e-6799 まで、この
    入力は「recipient の cursor を越えていない最古」だった = 実配信経路ではなく
    inbox-hook (人の打鍵) の進み具合を測っていた。取得の判定は
    :func:`delivered_to_recipient` が所管し、cursor は scan の起点にしか使わない。

    ``window_seconds`` reuses the attentiveness window (a healthy receive path
    fetches in seconds; a backlog older than the human-attention window is a
    wedge, not in-flight latency) — no new constant, per SPEC 方針 a.
    """
    if not oldest_unread_created_at:
        # e-6777: バックログが無い = 消化の証拠が無い → 不明。健全 (True) と言わない。
        # ここを True に戻すと「受信していないセッションが最も健全」に逆戻りする。
        return None
    try:
        oldest = datetime.datetime.fromisoformat(
            str(oldest_unread_created_at).replace("Z", "+00:00"))
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, AttributeError, TypeError):
        # Unparseable stamp — unknown, not dead. Fail toward reachable.
        return None
    age = (now - oldest).total_seconds()
    return age <= window_seconds


def is_reachable(live, draining) -> bool:
    """``reachable = live AND (draining is not False)``.

    Only a *definitive* wedge (``draining is False``) makes a live session
    unreachable. Unknown draining (``None``) keeps a live session reachable, so
    an idle fork with no backlog is never dropped from the picker (the SPEC 方針
    c no-regression guarantee: the ``live`` union is unchanged; ``reachable`` is
    an ADDITIONAL field only the send path reads strictly).
    """
    return bool(live) and (draining is not False)


def classify_send_delivery(live, draining) -> str:
    """3-tier graded send verdict for a DM recipient (SPEC 方針 b).

    - ``SEND_NORMAL``   — live and reachable: deliver as usual; the send
      response is byte-unchanged (the healthy common path must not regress).
    - ``SEND_WEDGED``   — live but NOT draining: still enqueued, but the send
      response carries loud ``recipient_wedged`` / ``delivery_uncertain`` flags
      so every client path (CLI --json, MCP reply, headless) surfaces it — not
      a soft field a caller can skip past.
    - ``SEND_NOT_LIVE`` — no transport liveness at all: existing behaviour
      unchanged (the picker / soft-warn path already covers a dead session).
    """
    if not live:
        return SEND_NOT_LIVE
    if draining is False:
        return SEND_WEDGED
    return SEND_NORMAL


def parse_iso8601(stamp) -> Optional[datetime.datetime]:
    """ISO8601 文字列を tz-aware な datetime にする。読めなければ ``None``。

    ms-173 / e-6729 独立レビュー (保守性 M-3): 「``Z`` を ``+00:00`` に置換 →
    ``fromisoformat`` → naive なら UTC を補う」という同じ手順が複数箇所に手書きで
    散っていた。パース規則 (Z 補完 / naive の扱い / 不正値の吸収) を直したいときに
    全箇所を見つけて揃えないと、読めない値の扱いが関数ごとに drift する。ここを
    唯一の定義にして、判定側 (:func:`_declaration_is_stale`) と経過秒側
    (:func:`iso_age_seconds`) はこれを呼ぶ薄い層にする。

    ``None`` は「読めなかった」であって「古い」ではない。古いと解釈するかは
    呼び出し側の文脈で決める (判定側は stale 扱い、経過秒側は「源が無い」扱い)。
    """
    if not stamp:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def iso_age_seconds(stamp, now) -> Optional[float]:
    """ISO8601 文字列の古さ (秒)。読めなければ ``None`` (= その源が無い)。

    ``None`` を「古い」ではなく「無い」として扱うのが要で、読めない源を古い扱いに
    すると、その源を 1 つ持たない正当な行を誤って not-live にしてしまう
    (:func:`ws_only_liveness_suppression` の裏付け判定で使う)。
    """
    parsed = parse_iso8601(stamp)
    if parsed is None:
        return None
    return (now - parsed).total_seconds()


def _declaration_is_stale(declared_at, now, stale_after_seconds) -> bool:
    """Return whether a state declaration is too old to trust.

    ``True`` when the declaration is older than ``stale_after_seconds`` — OR when
    its timestamp is missing/unparseable. A declaration we cannot date is treated
    as stale: safe side, since we would rather surface something for human
    attention than trust an undatable self-report. ``False`` only when the stamp
    parses AND is within the freshness window.

    This predicate answers only "is the stamp too old to trust". **What a stale
    declaration then projects to is NOT described here** — ``derive_state`` is the
    single source of truth for that mapping (it depends on liveness too). Naming
    the outcome in this docstring as well is how it went stale once already: it
    said ``unknown`` and kept saying so after ms-177 changed the outcome to
    ``interrupted``. Read ``derive_state`` for the mapping; keep this one about
    the stamp.
    """
    stamped = parse_iso8601(declared_at)
    if stamped is None:   # 欠落 / 読めない = 日付を付けられない宣言は信じない
        return True
    return (now - stamped).total_seconds() > stale_after_seconds


# ms-173 / e-6729 — WS 単独で立っている生存主張を、裏付けが無いときに取り下げる。
#
# 背景 (e-6583 の実測 2026-10-01): 親が死んで孤児になった bridge が 25 日間 WS を
# つないだまま ping を送り続け、死んだセッションが ws_live=true のまま居座った。真因は
# bridge 側で直したが (= ping を「生存報告が通っていること」に結び直した)、直したコードが
# 入っていない古い bridge には届かない。e-6583 の記述自身が「poll 履歴を持たない行には
# ガードが効かない設計なので、そこでリークすると永久ゾンビが再発しうる」と指摘していた穴。
#
# e-6563 の先行ガードは `last_poll_at` を一度でも書いた行だけを対象にしていた (= 古い
# bridge を誤って not-live にしないための意図的な除外)。その除外が穴そのものなので、
# 「poll 履歴があるか」で線を引くのをやめ、**生存の裏付けが何か一つでも新しいか** で引く。
#
# ここは e-6582 (汚染された『確認待ち』の降格) と違い、曖昧さが無い。「WS で生存を主張して
# いるのに、生存の痕跡がどれも 30 分以上古い (または一つも無い)」は、本物を隠す恐れのある
# 推定ではなく、嘘をついている証拠そのもの。だから倒す向きを人に問う必要がない。
#
# 誤爆しない側の安全弁が「裏付けを複数源から取る」こと: poll 報告が無い古い bridge でも、
# 人/AI が実際に動かしていれば CLI 由来の heartbeat (last_heartbeat_at) が新しい。
# だから「古い bridge かどうか」ではなく「生きている痕跡があるか」で救う。
#
# ms-173 / e-6776 の訂正: 当初この行は last_heartbeat_at を「PostToolUse hook 由来」と
# 書いていたが誤りで、実際は CLI の session 解決が /api/me/heartbeat を叩いた副産物。
# さらに throttle cache の時計が別の書き手 (受信プロセスの last_active) を見ていたため、
# この stamp は最初の mint 時刻で凍結し **安全弁として機能していなかった** (実測: 稼働中
# 14 セッション中 heartbeat_fresh=True は 1 件)。e-6776 で時計を付け替えて実効化した。
WS_SUPPRESS_POLL_STALE = "ws-zombie-poll-stale"          # e-6563 と同一の理由文字列 (互換)
WS_SUPPRESS_NO_EVIDENCE = "ws-zombie-no-liveness-evidence"


def ws_only_liveness_suppression(*, ws_live, poll_healthy, has_poll_history,
                                 evidence_ages_seconds, max_age_seconds,
                                 max_age_seconds_no_history=None,
                                 session_age_seconds=None):
    """WS 単独の生存主張を取り下げるべきか。取り下げる理由、または ``None``。

    Args:
        ws_live: 接続台帳の raw signal (True / False / None)。
        poll_healthy: poll 報告が健全か。
        has_poll_history: ``last_poll_at`` を一度でも書いたか (理由文字列の選択だけに使う。
            **抑止するか否かの判定には使わない** — そこが e-6563 の穴だった)。
        evidence_ages_seconds: 生存の裏付けの古さ (秒) を並べたもの。``None`` は
            「その源は無い / 読めない」。poll 報告・hook heartbeat・最終活動のように、
            互いに独立した源を渡す (1 源だけだとその源を持たない正当な行を誤爆する)。
        max_age_seconds: poll 履歴がある行で、この秒数以内の裏付けが 1 つでもあれば救う。
        max_age_seconds_no_history: poll 履歴が **無い** 行に使う、より長い猶予。
            省略時は ``max_age_seconds`` と同じ。履歴が無い行は「古い bridge が正当に
            待機しているだけ」の可能性を server 側から確かめる手段が無い (ping は
            ゾンビも送る) ため、確信度が低い側に長い猶予を与えて誤爆を減らす。
            ゾンビは日単位で居座るので、時間単位の猶予でも検知力は落ちない
            (実測されたゾンビは 25 日 / 500 時間)。
        session_age_seconds: セッション自身の年齢 (秒)。猶予より若ければ **判定しない**。
            繋いだ直後の bridge は、まだ一度も生存報告を出していないのが正常なので、
            「裏付けが無い」を嘘の証拠として扱ってはならない (これを入れないと、
            起動直後の数秒だけ not-live に見える窓ができる)。年齢は生存の証拠ではなく
            「まだ証拠を期待できない」ことの根拠なので、裏付けとは別の引数で受ける。

    Returns:
        ``WS_SUPPRESS_POLL_STALE`` / ``WS_SUPPRESS_NO_EVIDENCE`` / ``None``。

    引数が **すべて keyword-only** なのは意図的 (ms-173 / e-6729 独立レビュー AX-1)。
    先頭 3 つは ws_live (True/False/None) / poll_healthy (bool) / has_poll_history
    (bool) と型が同系で、位置で渡すと並びを入れ替えても TypeError にならず **判定が
    黙って逆になる**。ここは「WS ping を生存の証拠にしない」という契約を守る唯一の
    判定点なので、取り違えが静かに通ると 25 日ゾンビと同型のバグが再発する。
    keyword-only にすれば取り違えはその場で TypeError として顕在化する。

    契約:
      * ``ws_live`` が True でない、または poll が健全なら **何もしない** (``None``)。
        この関数は「WS 単独で立っている主張」だけを扱う。
      * 裏付けが 1 つでも新しければ救う。誤って not-live にすると DM が届かなくなり、
        嘘を残すより重い害になるので、判定は救う側に倒す。
      * 裏付けが全て古い / 一つも無い場合に取り下げる。
      * セッションが猶予より若ければ何もしない (まだ証拠を期待できない)。
      * 閾値が不正 (None / 0 以下) なら何もしない (= 設定ミスで健全な行を黙らせない)。
    """
    if ws_live is not True or poll_healthy:
        return None
    limit = max_age_seconds if has_poll_history else (
        max_age_seconds_no_history if max_age_seconds_no_history is not None
        else max_age_seconds)
    if limit is None or limit <= 0:
        return None
    if session_age_seconds is not None and session_age_seconds <= limit:
        return None          # 若すぎて判定できない (証拠の不在を嘘の証拠にしない)
    ages = [a for a in (evidence_ages_seconds or ()) if a is not None]
    if any(a <= limit for a in ages):
        return None
    return WS_SUPPRESS_POLL_STALE if has_poll_history else WS_SUPPRESS_NO_EVIDENCE


def derive_state(declared_state, declared_at, live, now,
                 stale_after_seconds, *,
                 running_stale_after_seconds=None) -> str:
    """Project a work unit's canonical ``state`` — 由来を要らない呼び出し側向けの薄い窓口。

    判定は :func:`derive_state_with_origin` が 1 箇所で行い、ここはその状態側だけを
    返す (= 2 箇所で分岐を書かない。独立レビュー AX-4 で由来を足すとき、同じ分岐を
    もう一度書くと必ず drift する)。既存の呼び出しと戻り値は不変。
    """
    return derive_state_with_origin(
        declared_state, declared_at, live, now, stale_after_seconds,
        running_stale_after_seconds=running_stale_after_seconds)[0]


def derive_state_with_origin(declared_state, declared_at, live, now,
                             stale_after_seconds, *,
                             running_stale_after_seconds=None):
    """Project a work unit's canonical ``state`` **と、その値になった由来** を返す
    (ms-159 / e-6243 + ms-173 独立レビュー AX-4)。

    Returns:
        ``(state, origin)`` — ``state`` は ``ALL_STATES`` の 1 つ、``origin`` は
        ``ALL_STATE_ORIGINS`` の 1 つ。**全経路が由来を返す** (「由来が無い」と
        「由来が不明」を同じ空値に畳まない)。とくに ``unknown`` は 2 経緯
        (``stale-running`` / ``never-declared``) を持つので、名前 1 つで区別できない
        のをここで区別する。

    The one place every value in ``ALL_STATES`` is decided. Pure: the
    impure liveness scan that produces ``live`` lives on the server; keeping the
    derivation pure lets every branch be pinned without a bus fixture (same
    discipline as ``derive_draining``).

    Authority model (作業単位状態モデル SPEC ``np2fSUqpE5LSIkOqHLuK`` 判断1/4 +
    slice SPEC ``Icb8zFtbnZZ1yXzMsLO6`` 方針1/4):

    Since ms-177 the two server-raised states divide cleanly: ``interrupted``
    means "it was working and its transport died" (not live), ``unknown`` means
    "it is live but has never said what it is doing". Neither is declarable.

    - **``terminated`` is terminal.** A session that reported SessionEnd stays
      ``terminated`` regardless of liveness or age — it legitimately stops
      emitting, so nothing flips it to ``unknown``.
    - **While LIVE, the latest declaration is authoritative — staleness does NOT
      apply, with ONE exception: a ``running`` declaration (see
      ``running_stale_after_seconds`` below and the branch comment in the body).**
      待ち状態の静止は正常なので経年で疑わないが、``running`` は継続的な主張なので
      静止は矛盾 — そこだけ疑う (ms-173 / e-6774)。この要約表が唯一の正とするため、
      例外は後付け段落でなくここに書く (独立レビュー 保守性 M-1)。
      The bridge heartbeats every few seconds; that live heartbeat
      continuously re-affirms the marker (the session would push a NEW marker if
      its state changed), so ``declared_at`` age is not evidence the state is
      wrong. This is the correction that lets a session parked in
      ``awaiting_human`` for hours stay visible at the top of the attention面
      (its ``declared_at`` freezes while it waits — no hook fires — but the
      heartbeat proves it is still genuinely waiting). Uniform staleness would
      instead hide exactly the long-waiting sessions this MS exists to surface.
      The one live exception: **no declaration at all ⇒ ``unknown``** (判断4:
      never silently assume ``running`` — the safe side toward human attention).
    - **Once NOT LIVE, staleness is what indicts the declaration.** With the
      heartbeat gone we can no longer confirm the state, so:
        * a *fresh* declaration is a very recent death — trust it through a short
          grace window (a just-crashed ``awaiting_human`` still reads
          ``awaiting_human`` for a moment, indistinguishable from a real pause);
        * a *stale* declaration ⇒ ``interrupted`` (中断) — transport gone AND the
          last report is old, so a lingering non-terminal state must be
          neutralized (判断4 固着 backstop: a dead session frozen in
          ``awaiting_human`` must not nag the inbox forever). ms-177 names this
          outcome instead of folding it into ``unknown``: it is precisely the
          accidental death (terminal closed / API error / context-limit exit) the
          ops room must show LOUDLY, because the human wants to resume it. Note
          this covers ``blocked`` too — a blocked session that then loses its
          transport also died without ending, so it takes the same branch rather
          than needing a second rule.
        * *no* declaration ⇒ ``terminated`` (no transport and nothing ever
          declared = gone). ms-177 SPEC 方針4 accepts that ``terminated`` stays a
          mixed bucket (clean exit + never-declared death) for now: separating
          those needs ``last_poll_at`` as a derivation input.

    So ``stale_after_seconds`` is a *post-death grace window*, not a general
    freshness clock — it is consulted only on the not-live path.

    ms-173 / e-6774 — その上で ``running`` **だけ** は live でも経年で疑う
    (``running_stale_after_seconds``)。「live-stuck な宣言は後回し」と書いていたのが
    この箇所で、実測 (2026-10-01) で live なのに running の declared_at が 38 分止まった
    行が 2 件見つかったため塞いだ。塞げる理由は非対称性にある: 待ち状態の静止は正常
    (hook が発火しないので当然凍る) だが、``running`` は継続的な主張なので静止は矛盾。
    だから待ち状態を隠すリスクを負わずに running だけを訂正できる。詳細は該当分岐の
    コメント参照。

    Args:
        declared_state: the session's last self-declared state, or a falsy /
            unrecognized value when it never declared one.
        declared_at: ISO-8601 timestamp of that declaration (``None`` if absent).
        live: transport liveness of the session (the ``live`` union used by the
            picker). Load-bearing: the primary axis here.
        now: current tz-aware ``datetime``.
        stale_after_seconds: post-death grace window (only consulted when
            ``live`` is false).

    State values: ``STATE_RUNNING`` / ``STATE_IDLE`` / ``STATE_AWAITING_HUMAN`` /
    ``STATE_BLOCKED`` / ``STATE_TERMINATED`` / ``STATE_INTERRUPTED`` /
    ``STATE_UNKNOWN`` (= ``ALL_STATES``)。由来は ``ALL_STATE_ORIGINS`` を参照。
    """
    # 1. Terminal declaration is authoritative forever — never let age or a
    #    dropped transport flip an ended session to unknown.
    if declared_state == STATE_TERMINATED:
        return STATE_TERMINATED, STATE_ORIGIN_DECLARED

    # 2. A recognized non-terminal declaration.
    if declared_state in DECLARABLE_STATES:  # non-terminal (terminated handled)
        if live:
            # ms-173 / e-6774 — ``running`` だけは経年で疑う。
            #
            # 下の原則 (live な宣言は経年で疑わない) には **非対称性** がある:
            #   * ``awaiting_human`` / ``blocked`` / ``idle`` は静止が正常。待っている間は
            #     hook が発火しないので declared_at は当然凍る。だから経年で疑ってはならず、
            #     疑うと「長く待っている行」= この MS が surface したい対象を隠してしまう。
            #   * ``running`` は **継続的な主張**。本当に動いていれば PreToolUse /
            #     PostToolUse が次々に発火して declared_at を進める。進まないなら、その
            #     「作業中」はもう本当ではない。
            #
            # 実害 (2026-10-01 実測): live なのに running の declared_at が 38 分止まった
            # 行が 2 件。いずれも待機内容に許可要求の文面が残っており、許可待ちで止まって
            # いるのに運用室では「作業中」に見えていた。止まった事実を誰も訂正しない。
            #
            # 投影先は ``unknown``: 止まったことは分かるが、何をしているかは分からない
            # (idle とも awaiting_human とも断定できない)。``unknown`` の意味
            # 「生きているが何をしているか言えない」をそのまま使う — でっち上げない。
            #
            # しきい値は「1 回の tool 呼び出しの最長」を上回る必要がある: PreToolUse と
            # PostToolUse の間 (= 長いテスト実行や CI 待ち) は declared_at が凍るので、
            # 短く取ると正常な長時間作業を unknown にしてしまう。既定は呼び出し側が渡す
            # (None = この判定を使わない = 従来どおり) ので、既存の呼び出しは不変。
            if (declared_state == STATE_RUNNING
                    and running_stale_after_seconds is not None
                    and running_stale_after_seconds > 0
                    and _declaration_is_stale(
                        declared_at, now, running_stale_after_seconds)):
                # 由来を残す: 「一度も言っていない」(never-declared) と読まれると、
                # 許可待ちで止まっている疑いという **対応の違う** 情報が消える。
                return STATE_UNKNOWN, STATE_ORIGIN_STALE_RUNNING
            # Live heartbeat re-affirms the marker → trust it regardless of age.
            # This keeps a long-waiting awaiting_human at the top of attention.
            return declared_state, STATE_ORIGIN_DECLARED
        # Not live: the heartbeat is gone, so staleness now decides.
        if not _declaration_is_stale(declared_at, now, stale_after_seconds):
            # very recent death: grace window — まだ宣言どおりに読む
            return declared_state, STATE_ORIGIN_DECLARED
        # Gone + stale, and it HAD declared real work ⇒ 中断 (ms-177). This is
        # the 固着 backstop as before — the frozen awaiting_human stops nagging —
        # but it is now named for what it actually is instead of being dumped in
        # ``unknown``: a session that was working and died without ending.
        return STATE_INTERRUPTED, STATE_ORIGIN_TRANSPORT_LOST

    # 3. No (or unrecognized) declaration ⇒ liveness fallback (判断4 safe side).
    #    live ⇒ up but unstated ⇒ unknown; not live ⇒ gone ⇒ terminated.
    if live:
        return STATE_UNKNOWN, STATE_ORIGIN_NEVER_DECLARED
    return STATE_TERMINATED, STATE_ORIGIN_NO_TRACE
