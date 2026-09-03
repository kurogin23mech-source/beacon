"""decision-event の統一スキーマ builder / validator (ms-90 / e-3242 → ms-154 / e-5591)。

ms-90 「Trek リーダーの意思決定を構造化ログとして残す」の中核として生まれ、DM 発信 /
trek-review / scope 承認 / halt-resume の 4 経路が散在して記録していた「決定」を、
1 本の append-only ストリーム (= backend の ``decision_events`` collection) に
統一形で束ねてきた。将来ローカル LLM で PM 専用 AI を訓練する材料にする。

ms-154 (SPEC ``0iYyU79MEsxN4wGY7ADk``) でこの現物を **decision arm** へ一級化する。
AI 駆動開発では in-flight (= 実装中) の決定の多数派が AI 判断 (却下 / 先送り /
done 判定 / findings 採否) であり、これを「誰が (decided_by) / なにを (what) /
なぜ (why) / 何を根拠に (evidence)」で辿れるようにする。別 AI が rationale (= 根拠の
主張) を実コードに照合して独立検証できることが目的 (SPEC §設計方針4 / P4)。

論理スキーマの原型は e-3245 の doc ``OqqO02CUvsQzzDMyhhGf`` (spec, ms-90)。
このモジュールはその論理形を組み立てる純関数を提供する。物理永続化は
``mysql_client`` / ``firestore_client`` / ``dynamodb_client`` の
``append_decision_event`` / ``list_decision_events`` が担う (record dict を透過保存)。

設計上の要点:
- ``decision`` が **what** (= 何を選んだか)、``rationale`` が **why** (= なぜ) を
  兼ねる (= 新設せず既存 field に意味を載せる / SPEC「拡張する・新設しない」)。
- ``decided_by`` (= 誰が決めたか) は一級 enum (:data:`DECIDED_BY`)。
  ``autonomous-AI`` (= 人間未確認の AI 単独決定) が最も audit-critical。
- ``evidence`` (= 根拠への link) は **decided_by を立てたら必須** (= 一級 decision を
  宣言するなら、それを裏付ける証拠 link を構造的に強制する / SPEC「evidence-link 必須」)。
  commit hash / ``file:line`` / bus event_id / 会話 url 等の opaque な参照文字列の list。
- ``options`` (= 検討した他の選択肢) は任意。
- ``context`` (= 直面した問題 = 背景) は空でも組み立てを通す (= hard block しない)。
- ``outcome`` (= 結果) は **持たない**。相談 / 判断したこと自体を是としたいので、
  結果の良し悪しで判断行為を評価しない。誤って渡されたら ValueError で弾く。
- ``kind`` は **開いた語彙** (ms-154 §設計方針1「語彙開放」)。ms-90 の閉語彙 (Trek 由来
  5 経路のみ許可) から、decision arm が職種横断の汎用アームになったため開いた。
  空 kind だけ ValueError。既知の kind は :data:`KNOWN_DECISION_KINDS` に文書化する
  (= 参照用であって hard gate ではない)。
"""
from __future__ import annotations

import datetime
import secrets

# ms-154 e-5652: decided_by 語彙は lib/decision_vocab.py が単一ソース (CLI と server の
# 二重定義を廃止)。server は起動時に lib/ を path に載せる (server/app.py) ので import 可。
from decision_vocab import DECIDED_BY  # noqa: F401  (re-exported below)


# 既知の決定経路 (= 参照用の語彙リスト。ms-154 §設計方針1 で語彙を開いたので hard gate
# ではない = 未知の kind も build_decision_event は受け付ける)。新経路を足したらここに
# 文書化する。ms-90 の 5 経路 + ms-154 decision arm の捕獲対象。
KNOWN_DECISION_KINDS: frozenset[str] = frozenset(
    {
        # ms-90 Trek 由来の 4(+1) 経路
        "dm-send", "trek-review", "scope-approval", "halt", "resume",
        # ms-154 decision arm の捕獲対象 (e-5592 / e-5593 / e-5594)
        "task-done", "completion-verdict", "review-adjudication", "log-backstop",
    }
)

# 後方互換の別名 (= ms-90 期の import 名を壊さない)。閉語彙だった頃の意味ではなく、
# 「既知 kind の集合」を指す点に注意 (語彙自体は開いている)。
DECISION_KINDS = KNOWN_DECISION_KINDS

# decided_by (= 誰が決めたか) の一級 enum は decision_vocab.DECIDED_BY が単一ソース
# (上で import 済、ここから re-export)。旧: この module に重複定義していた (ms-154 e-5652)。

# related に載りうる参照キー (= 経路ごとに埋まる項目が違うが、shape は共通で固定)。
# ms-154 e-5592 で ``target_id`` を追加 (= milestone / opportunity 等の完遂判定が
# 指す対象。task 粒度の ``task_id`` より上位の target 粒度を表す)。
_RELATED_KEYS: tuple[str, ...] = (
    "event_id", "trek_id", "task_id", "target_id", "in_reply_to",
)

# who の shape (= 誰が判断したか)。agent は AI 識別子で、検出できなければ None。
_WHO_KEYS: tuple[str, ...] = ("session_id", "user_id", "agent")

# 構造的に持たせない項目 (= SPEC §設計方針2)。誤って渡されたら弾く。
_FORBIDDEN_FIELDS: frozenset[str] = frozenset({"outcome"})


def _now_iso() -> str:
    """ISO8601 (UTC, ミリ秒付き)。backend の _now_iso_utc と同じ書式。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def mint_decision_event_id() -> str:
    """decision_id を採番する (= trek_log の log- と同じ命名慣習)。"""
    return f"dec-{secrets.token_hex(8)}"


def _normalize_who(who: dict | None) -> dict:
    """who を {session_id, user_id, agent} の固定 shape に正規化する。

    session_id / user_id は空文字許容 (= 未検出でも組み立てを止めない)。
    agent は任意で、無ければ None。
    """
    src = dict(who or {})
    return {
        "session_id": str(src.get("session_id") or ""),
        "user_id": str(src.get("user_id") or ""),
        "agent": (src.get("agent") if src.get("agent") else None),
    }


def agent_from_claims(user: dict | None) -> str | None:
    """認証 claims (= require_auth が返す user dict) から decision の
    ``who.agent`` (= 誰が判断したか) を解決する。返すのは人間トークンの email。

    ``agent`` は「判断を下した主体の識別子」。decision には agent の解決元が
    2 つあり、どちらも正当:
      1. **認証 claims 経由** (この関数) — CLI 発の fire 経路 (task-done /
         completion-verdict / 手動 record / pr-intent / review 採否 / scope 承認 /
         trek halt・review)。email を claims から取る。
      2. **envelope issuer 経由** (app.py の dm-send、``agent=env_issuer``) —
         送信者は envelope から解決するので claims でなく issuer を使う。

    経緯 (e-6012): 経路 1 の全 fire site が claims に email があるのに ``who`` へ
    載せておらず ``who.agent`` が一律 None になっていた (回帰)。経路 1 の解決規則を
    この 1 関数に集約し、各 fire site が ``user.get("email")`` を直書きして 1 箇所
    忘れる事故を防ぐ。経路 2 (dm-send) はこの関数の対象外 (= envelope が真値源)。

    machine key は email を持たない (= backend agent) ので None (= backend
    decision は今日 agent 無しが正)。空文字 / 空白のみ / claims 無しも None。
    """
    if not user:
        return None
    email = (user.get("email") or "").strip()
    return email or None


def _normalize_related(related: dict | None) -> dict:
    """related を固定 4 キー shape に正規化する (= 未指定キーは None)。"""
    src = dict(related or {})
    return {key: (src.get(key) if src.get(key) else None) for key in _RELATED_KEYS}


def _normalize_decided_by(decided_by: str | None) -> str | None:
    """decided_by を検証して返す (= None 許容 / 語彙外は ValueError)。

    None は「未指定」(= legacy 経路 / decision arm を名乗らない記録)。値を渡す
    なら :data:`DECIDED_BY` の 4 語彙のいずれかでなければならない (= 一級 enum)。
    """
    if decided_by is None or decided_by == "":
        return None
    value = str(decided_by)
    if value not in DECIDED_BY:
        raise ValueError(
            f"unknown decided_by: {value!r} (allowed: {sorted(DECIDED_BY)})"
        )
    return value


def _normalize_link_list(value) -> list[str]:
    """evidence / options を link 文字列の list に正規化する。

    None → ``[]``、単一文字列 → ``[str]``、iterable → 各要素を str 化して
    空要素を落とした list。順序は保つ (= 証拠の提示順に意味があるため)。
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def build_decision_event(
    *,
    kind: str,
    decision: str,
    context: str = "",
    who: dict | None = None,
    rationale: str | None = None,
    related: dict | None = None,
    decided_by: str | None = None,
    evidence=None,
    options=None,
    created_at: str | None = None,
    decision_id: str | None = None,
) -> dict:
    """統一 decision-event レコードを組み立てて返す (純関数、副作用なし)。

    意味の対応 (ms-154 §設計方針): ``decision`` = what (= 何を選んだか)、
    ``rationale`` = why (= なぜ)、``decided_by`` = 誰が決めたか (一級 enum)、
    ``evidence`` = 何を根拠に (= link の list)、``options`` = 検討した他の選択肢。

    構造的な不変条件:
    - ``kind`` は空だと ValueError (= 語彙自体は開いている / ms-154 §設計方針1)。
    - ``decision`` (= what) は必須。空なら ValueError。
    - ``decided_by`` は :data:`DECIDED_BY` の語彙か None。語彙外は ValueError。
    - ``evidence`` は **実 link (commit / code / 会話) のみ** を積む。空でも通す
      (ms-154 e-5650): 自己参照 (``task:<id>`` / ``target:<id>``) を「evidence 非空」
      条件充足のために自動挿入する旧挙動 (= トートロジーで invariant を満たし検証
      材料ゼロを隠す) を廃止した。evidence が空 = 「物理的な裏付けが無い決定」という
      監査シグナルそのもの (= phantom done を隠さず露出する)。SPEC「evidence-link
      必須」は「実 link があるなら必須 (捏造で埋めない)」と読み替える。自己参照は
      ``related.task_id`` / ``related.target_id`` が既に運ぶ (= 冗長を排する)。
    - ``outcome`` を含む余計な引数はキーワード専用シグネチャなので構造的に混入しない。

    ``created_at`` / ``decision_id`` は未指定なら補完する。永続化層でも防御的に
    補完するので、ここでの補完は「呼び出し側が id を先に知りたい」ケース
    (= related.event_id を別レコードに書き戻す等) のためのもの。
    """
    if not kind or not str(kind).strip():
        raise ValueError("decision-event requires a non-empty 'kind'")
    if not decision or not str(decision).strip():
        raise ValueError("decision-event requires a non-empty 'decision'")

    decided_by = _normalize_decided_by(decided_by)
    evidence = _normalize_link_list(evidence)
    options = _normalize_link_list(options)

    return {
        "decision_id": decision_id or mint_decision_event_id(),
        "kind": str(kind),
        "decision": str(decision),
        "context": str(context or ""),
        "rationale": (str(rationale) if rationale else None),
        "decided_by": decided_by,
        "evidence": evidence,
        "options": options,
        "who": _normalize_who(who),
        "related": _normalize_related(related),
        "created_at": created_at or _now_iso(),
    }


def decision_event_from_dm_send(
    *,
    sender_session_id: str = "",
    sender_user_id: str = "",
    context: str = "",
    rationale: str | None = None,
    event_id: str = "",
    in_reply_to: str | None = None,
    agent: str | None = None,
) -> dict:
    """DM 発信 (= 主役経路 / e-3246) の decision-event を組み立てる純関数。

    「問題に直面して相談を始めた瞬間」を記録する。``context`` (= 直面した
    問題) が主役だが空でも組み立てる (= hard block しない、warning は送信側
    CLI の責務)。``related.event_id`` は発行済み bus event を指し、DM 本文は
    複製せず参照で繋ぐ (= approvals sidecar と同じ考え方)。``decision`` は
    経路内の選択で、DM 発信は ``"sent"`` 固定。
    """
    return build_decision_event(
        kind="dm-send",
        decision="sent",
        context=context,
        who={
            "session_id": sender_session_id,
            "user_id": sender_user_id,
            "agent": agent,
        },
        rationale=rationale,
        related={"event_id": event_id, "in_reply_to": in_reply_to},
    )


def decision_event_from_scope_approval(
    *,
    decision: str,
    decider_user_id: str = "",
    decider_session_id: str = "",
    event_id: str = "",
    context: str = "",
    rationale: str | None = None,
    agent: str | None = None,
) -> dict:
    """cross-user DM action の承認/却下 (= scope 承認経路) の decision-event。

    ``decision`` は ``"approve"`` / ``"deny"``。判断した人 (= 受信者) を who に、
    対象の bus event を related.event_id に置く。

    ``decided_by`` は ``human-delegated`` 固定 (ms-154 e-5651): この承認/却下は
    SPEC ms-70 方針3「terminal Claude Code 内での user 直接判断のみ」で、必ず人間が
    直接下す決定 = 人間が決定主体。これで dm_pending respond が「誰が決めたか」を持つ
    一級 decision として捕獲される (旧: decided_by 未設定で prompt 層格下げ扱いだった)。
    対象 envelope への参照は ``related.event_id`` が運ぶので evidence は積まない。
    """
    return build_decision_event(
        kind="scope-approval",
        decision=decision,
        context=context,
        who={
            "session_id": decider_session_id,
            "user_id": decider_user_id,
            "agent": agent,
        },
        rationale=rationale,
        decided_by="human-delegated",
        related={"event_id": event_id},
    )


def decision_event_from_task_done(
    *,
    entry_id: str,
    done_reason: str | None = None,
    decided_by: str = "autonomous-AI",
    evidence=None,
    decider_session_id: str = "",
    decider_user_id: str = "",
    agent: str | None = None,
    context: str = "",
) -> dict:
    """task の done 判定 (= 「このタスクは目的を果たした」) の decision-event (ms-154 e-5592)。

    what (= 何を選んだか) は ``"done"``、why (= なぜ) は done 判定の理由
    (``done_reason``)、根拠 (evidence) はこの done を裏付ける commit / 会話への link。
    decided_by の default は ``autonomous-AI`` (= CLI 経由の ``beacon task done`` は
    beacon-log Skill が駆動する AI 判断が主で、最も監査が要るため保守的に AI 側へ倒す。
    Web UI の人手 done 等は呼び出し側が明示指定して上書きする)。

    ``evidence`` は done を裏付ける **実 link (commit / code / 会話) のみ** (ms-154
    e-5650)。空でもそのまま通す = commit 照合が空振り (= phantom done) を隠さず
    「裏付け無し」として露出する。対象 task 自身への参照 (``task:<entry_id>``) は
    ``related.task_id`` が運ぶので evidence には積まない (= 自己参照でトートロジー的に
    invariant を満たす旧挙動を廃止)。
    """
    ev = list(_normalize_link_list(evidence))
    return build_decision_event(
        kind="task-done",
        decision="done",
        context=context,
        who={
            "session_id": decider_session_id,
            "user_id": decider_user_id,
            "agent": agent,
        },
        rationale=done_reason,
        decided_by=decided_by,
        evidence=ev,
        related={"task_id": entry_id},
    )


def decision_event_from_completion_verdict(
    *,
    target_id: str,
    verdict: str = "done",
    done_reason: str | None = None,
    decided_by: str = "AI-proposed-human-chose",
    evidence=None,
    decider_session_id: str = "",
    decider_user_id: str = "",
    agent: str | None = None,
    context: str = "",
) -> dict:
    """target (= milestone / opportunity 等) の完遂判定 (= 目的達成 verdict) の
    decision-event (ms-154 e-5592)。

    milestone を done / observing / closed へ倒す遷移は「この target は目的を果たした」
    という attainment claim を運ぶ (lib/transition_approval の目的達成レビュー)。その
    verdict を decision arm に記録する。what は verdict (``"done"`` 等)、why は
    ``done_reason``、根拠 (evidence) は達成を裏付ける link。

    decided_by の default は ``AI-proposed-human-chose`` (= milestone 完遂は ms-119 の
    目的達成レビューゲートで AI が根拠を組み立て人間が承認する形が原則のため)。純粋な
    AI 自律完遂なら呼び出し側が ``autonomous-AI`` を明示する。

    ``evidence`` は達成を裏付ける **実 link のみ** (ms-154 e-5650)。対象 target 自身への
    参照 (``target:<target_id>``) は ``related.target_id`` が運ぶので evidence には積まない
    (= 自己参照の自動挿入を廃止)。空なら「裏付け link 無し」として露出する。
    """
    ev = list(_normalize_link_list(evidence))
    return build_decision_event(
        kind="completion-verdict",
        decision=(verdict or "done"),
        context=context,
        who={
            "session_id": decider_session_id,
            "user_id": decider_user_id,
            "agent": agent,
        },
        rationale=done_reason,
        decided_by=decided_by,
        evidence=ev,
        related={"target_id": target_id},
    )


# review 採否 (approve / re-work / reject) は CLI 側 (cmd_pr) の判断で、専用の
# server route を持たない。汎用 decisions 書き込み口 (POST /api/projects/{id}/decisions,
# kind="review-adjudication") を通り build_decision_event で検証される。ゆえに専用 builder
# は置かない (= server 側に呼び出し元が無い vestigial 関数を作らない / ms-154 e-5593)。


# leader_review からの遷移先 → review 判断の対応 (= 閉じた mapping)。
def trek_review_decision_from_state(target_state: str) -> str:
    """leader_review 状態からの遷移先を review 判断語に写す。

    done → 承認 (approve)、user_review → user へ転送 (forward-to-user)、
    それ以外 (= working / todo へ差し戻し) → 再作業 (re-work)。
    """
    if target_state == "done":
        return "approve"
    if target_state == "user_review":
        return "forward-to-user"
    return "re-work"


def decision_event_from_trek_review(
    *,
    decision: str,
    trek_id: str = "",
    task_id: str = "",
    decider_session_id: str = "",
    decider_user_id: str = "",
    context: str = "",
    rationale: str | None = None,
    agent: str | None = None,
) -> dict:
    """Trek タスクのリーダー review (approve / re-work / forward-to-user) の
    decision-event。判断したリーダーを who に、対象の trek / task を related に置く。
    """
    return build_decision_event(
        kind="trek-review",
        decision=decision,
        context=context,
        who={
            "session_id": decider_session_id,
            "user_id": decider_user_id,
            "agent": agent,
        },
        rationale=rationale,
        related={"trek_id": trek_id, "task_id": task_id},
    )


def decision_event_from_halt(
    *,
    resumed: bool = False,
    trek_id: str = "",
    issuer_session_id: str = "",
    issuer_user_id: str = "",
    context: str = "",
    rationale: str | None = None,
    agent: str | None = None,
) -> dict:
    """Trek の中断 (halt) / 再開 (resume) の decision-event。

    ``resumed=False`` なら kind=halt / decision=halt、``resumed=True`` なら
    kind=resume / decision=resume。halt の理由 (= 直面した問題) は context に置く。
    """
    kind = "resume" if resumed else "halt"
    return build_decision_event(
        kind=kind,
        decision=kind,
        context=context,
        who={
            "session_id": issuer_session_id,
            "user_id": issuer_user_id,
            "agent": agent,
        },
        rationale=rationale,
        related={"trek_id": trek_id},
    )


def maybe_dm_send_record(
    *,
    channel: str,
    payload: dict | None,
    sender_session_id: str = "",
    sender_user_id: str = "",
    context: str = "",
    rationale: str = "",
    event_id: str = "",
    agent: str | None = None,
) -> dict | None:
    """DM 発信なら decision-event レコードを、そうでなければ None を返す。

    post_bus_event の配線を薄く保つための決定点 (= channel 判定 + payload から
    in_reply_to 抽出 + record 組み立て) を 1 箇所に集約し、server harness 無しで
    単体テストできるようにする。``channel != "dm"`` は None (= 記録しない)。
    """
    if channel != "dm":
        return None
    in_reply_to = (
        payload.get("in_reply_to") if isinstance(payload, dict) else None
    )
    return decision_event_from_dm_send(
        sender_session_id=sender_session_id,
        sender_user_id=sender_user_id,
        context=context,
        rationale=(rationale or None),
        event_id=event_id,
        in_reply_to=in_reply_to,
        agent=(agent or None),
    )


def assert_no_outcome(record: dict) -> None:
    """レコードに outcome 系の禁止フィールドが混入していないか検証する。

    永続化層 (append_decision_event) が書き込み直前に呼ぶ想定。SPEC の
    「outcome は持たない」不変条件を、builder を経由しない生 dict 書き込み
    経路でも構造的に守るための番人。
    """
    bad = _FORBIDDEN_FIELDS & set(record or {})
    if bad:
        raise ValueError(
            f"decision-event must not carry outcome-like fields: {sorted(bad)} "
            f"(SPEC §設計方針2 — 結果で相談行為を評価しない)"
        )


def _row_session_id(row: dict) -> str:
    """A decision event's originating session — ``who.session_id`` (ms-164 e-6030).

    The single place that knows WHERE the session lives on a row, so the filter and
    any future reader read it the same way."""
    return str((row.get("who") or {}).get("session_id") or "")


def _row_target_id(row: dict) -> str:
    """A decision event's worked Target — ``related.target_id`` with a top-level
    ``target_id`` fallback (ms-164 e-6030).

    ``related.target_id`` (ms-154 e-5592) is the canonical slot; the fallback keeps
    the filter honest for any producer that stamped ``target_id`` at the top level.

    NOTE — this layout is DECISION-EVENT specific. It is NOT the same shape as the
    worked-target attribution on project.json records (session log / note / push),
    which carry a top-level ``target_ids`` LIST (+ back-compat first ``target_id``).
    A decision event is single-target (the one judgment's target); the record types
    are multi. Do not copy this accessor onto those records — read their
    ``target_ids`` list instead."""
    related = row.get("related") or {}
    return str(related.get("target_id") or row.get("target_id") or "")


def window_decision_events(rows, *, kind: str = "", limit: int = 100,
                           since: str = "", session: str = "",
                           target: str = "") -> list[dict]:
    """decision_events の read 窓の**単一真実源** (ms-166 e-5970 / ms-164 e-6030).

    3 つの store backend (firestore / mysql / dynamodb) は「行の取得」だけを担い、
    窓のセマンティクス — ``kind`` / ``session`` / ``target`` で絞る → ``since``
    (created_at 下限) で絞る → ``(created_at, decision_id)`` 昇順に並べる → 直近
    ``limit`` 件 (``[-limit:]``) — はこの 1 関数に集約する。以前は同じロジックが
    3 backend に逐語コピーされ、1 箇所だけ直すと silent に drift した (= backend
    切替時に初めて発覚する穴)。

    なぜ最新側 (``[-limit:]``) か: append-only stream は無制限に伸び (dm-send だけで
    500+ 件)、最古 ``limit`` 件を返すと backlog が ``limit`` を超えた時点で新しい判断
    記録がすべて既定 read から不可視になる (= 永続化は成功しているのに「載らない」
    ように見える)。``kind`` / ``session`` / ``target`` は ``limit`` の *前* に絞るので、
    絞り込み指定の read は「最新 ``limit`` 件の中の一致」ではなく「一致するものの最新
    ``limit`` 件」を返す (ms-164 e-6030: session-end が『このセッション / この target の
    判断』を件数窓こぼれなく取れる = scale-contract-principle 準拠)。

    ``rows`` は各 backend が取得した decision dict の list (``decision_id`` / ``kind``
    / ``created_at`` / ``who`` / ``related`` を持つ)。純関数 — 副作用なし、入力 list は
    変更しない。
    """
    out = list(rows or [])
    if kind:
        out = [r for r in out if (r.get("kind") or "") == kind]
    if session:
        out = [r for r in out if _row_session_id(r) == session]
    if target:
        out = [r for r in out if _row_target_id(r) == target]
    if since:
        out = [r for r in out if (r.get("created_at") or "") > since]
    out.sort(key=lambda r: (r.get("created_at", ""), r.get("decision_id", "")))
    if limit and limit > 0:
        out = out[-limit:]
    return out
