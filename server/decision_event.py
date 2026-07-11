"""decision-event の統一スキーマ builder / validator (ms-90 / e-3242)。

ms-90 「Trek リーダーの意思決定を構造化ログとして残す」の中核。DM 発信 /
trek-review / scope 承認 / halt-resume の 4 経路が散在して記録している「決定」を、
1 本の append-only ストリーム (= backend の ``decision_events`` collection) に
統一形で束ねる。将来ローカル LLM で PM 専用 AI を訓練する材料にする。

論理スキーマの確定は e-3245 の doc ``OqqO02CUvsQzzDMyhhGf`` (spec, ms-90)。
このモジュールはその論理形を組み立てる純関数を提供する。物理永続化は
``mysql_client`` / ``firestore_client`` / ``dynamodb_client`` の
``append_decision_event`` / ``list_decision_events`` が担う。

設計上の要点 (SPEC ``FrW0XunCgFNPjQcERivm`` §設計方針):
- ``context`` (= 直面した問題 = 背景) が主役。空でも組み立て自体は通す
  (= hard block しない、warning は呼び出し側の責務)。
- ``rationale`` (= 判断理由) は任意。None を許す。
- ``outcome`` (= 結果) は **持たない**。相談したこと自体を是としたいので、
  結果の良し悪しで相談行為を評価しない。誤って渡されたら ValueError で弾く。
- ``kind`` は閉じた語彙。語彙外は ValueError (= 経路が増えたらここを直す
  forcing function)。
"""
from __future__ import annotations

import datetime
import secrets


# 決定経路 (= 閉じた語彙)。増やす時はここと doc OqqO02CUvsQzzDMyhhGf を同時に直す。
DECISION_KINDS: frozenset[str] = frozenset(
    {"dm-send", "trek-review", "scope-approval", "halt", "resume"}
)

# related に載りうる参照キー (= 経路ごとに埋まる項目が違うが、shape は共通で固定)。
_RELATED_KEYS: tuple[str, ...] = ("event_id", "trek_id", "task_id", "in_reply_to")

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


def _normalize_related(related: dict | None) -> dict:
    """related を固定 4 キー shape に正規化する (= 未指定キーは None)。"""
    src = dict(related or {})
    return {key: (src.get(key) if src.get(key) else None) for key in _RELATED_KEYS}


def build_decision_event(
    *,
    kind: str,
    decision: str,
    context: str = "",
    who: dict | None = None,
    rationale: str | None = None,
    related: dict | None = None,
    created_at: str | None = None,
    decision_id: str | None = None,
) -> dict:
    """統一 decision-event レコードを組み立てて返す (純関数、副作用なし)。

    ``kind`` が語彙外なら ValueError。``outcome`` を含む余計な引数は
    シグネチャ上受け取れないので構造的に混入しない (= キーワード専用)。

    ``created_at`` / ``decision_id`` は未指定なら補完する。永続化層でも
    防御的に補完するので、ここでの補完は「呼び出し側が id を先に知りたい」
    ケース (= related.event_id を別レコードに書き戻す等) のためのもの。
    """
    if kind not in DECISION_KINDS:
        raise ValueError(
            f"unknown decision-event kind: {kind!r} "
            f"(allowed: {sorted(DECISION_KINDS)})"
        )
    if not decision or not str(decision).strip():
        raise ValueError("decision-event requires a non-empty 'decision'")

    return {
        "decision_id": decision_id or mint_decision_event_id(),
        "kind": kind,
        "decision": str(decision),
        "context": str(context or ""),
        "rationale": (str(rationale) if rationale else None),
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
