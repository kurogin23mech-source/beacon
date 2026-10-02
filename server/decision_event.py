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
# 文書化する。定義本体は ms-166 e-6633 で lib/decision_vocab.py に移した — CLI 側
# (beacon decision list) が「指定された kind が既知か」を判定するのに同じ集合を要し、
# server/ は CLI の import 経路に無いため (DECIDED_BY が e-5652 で同じ理由で移設済)。
from decision_vocab import KNOWN_DECISION_KINDS  # noqa: F401  (re-exported below)

# 後方互換の別名 (= ms-90 期の import 名を壊さない)。閉語彙だった頃の意味ではなく、
# 「既知 kind の集合」を指す点に注意 (語彙自体は開いている)。
DECISION_KINDS = KNOWN_DECISION_KINDS


# 「決定」ではない kind (ms-166 e-6603)。既定の read から外す。
#
# dm-send は ms-90 期に「DM 発信も決定の 1 経路」として同じストリームに束ねられたが、
# 実データで見ると**判断ではなく通信ログ**である: decided_by が None (= 誰の判断でもない)、
# related.target_id も None (= どの対象の話かも持たない)。直近 100 件で 3 件、時期に
# よっては半分を占め、session-start の「最近の決定」が送信ログで埋まって**本物の判断が
# 読めない** (= ms-166 が塞ぎたい silent 非機能そのもの)。
#
# **消すのではなく既定から外すだけ**。`kind=dm-send` を明示すれば従来どおり全件引ける
# (= データを到達不能にしない。既定を黙って狭めるのは silent scope narrowing で、
# 「送ったはずの記録が消えた」と読み手を誤らせる)。既定除外は応答の ``excluded_kinds``
# で開示する。
NON_DECISION_KINDS: frozenset[str] = frozenset({"dm-send"})

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
    """related を固定 shape (:data:`_RELATED_KEYS`) に正規化する (= 未指定キーは None)。

    許可キー外を渡されたら **ValueError で弾く** (ms-166 e-5996)。旧実装は未知キーを
    無言で drop していた = write は成功 (decision_id を返す) のにそのフィールドだけ
    消える「write accepted / field lost」の silent 非機能で、dogfood で
    review-adjudication に載せた ``pr_number`` が消えた (書いた側は成功と誤認)。
    POST /decisions ルートは build_decision_event の ValueError を 400 に写す
    (routers_projects.record_decision) ので、呼び出し元は「関連付けが保存されなかった」
    ことに即座に気付ける (CORE doc pzNKeE1 原則6: 破れは構造で塞ぐ、プロンプトで塞がない)。
    新しい参照キーが要るときは :data:`_RELATED_KEYS` を **意図的に** 拡張する
    (= schema を silent に増やさず、追加は 1 箇所の enum 変更として可視化する)。

    姉妹の ``_normalize_who`` は同じ「固定 shape へ正規化」パターンだが、未知キーを
    黙って drop する — これは **意図的な非対称**。related は client が body で渡す
    外部入力なので未知キーを loud に弾く必要があるが、who は server が token から
    組み立てる内部値 (client は who を渡さない) で、外部から未知キーが到達する経路が
    無い。ゆえに who 側に同じ guard を置いても発火しない dead guard になる。
    """
    src = dict(related or {})
    unknown = sorted(set(src) - set(_RELATED_KEYS))
    if unknown:
        # エラー文言は呼び出し元 (POST /decisions を叩いた側) が今すぐ取れる復旧策を
        # 先に出す: 未知キーを外して allowed キーだけで再送する。schema 拡張は
        # decision_event のコード変更を伴い呼び出し側では実行不能なので、それは後段の
        # 補足に留める (= source path を「今の一手」として提示しない / AX medium 反映)。
        raise ValueError(
            f"unknown related key(s): {unknown} — これらを related から外し、"
            f"allowed キー {sorted(_RELATED_KEYS)} だけで再送してください。"
            f"(related は固定 shape。新しい参照キーの追加は decision_event の "
            f"_RELATED_KEYS を拡張するコード変更が必要で、呼び出し側だけでは足せません)"
        )
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


def effective_exclude_kinds(exclude_kinds, kind: str = "") -> frozenset:
    """実際に適用する除外集合を返す — ``kind`` を明示したらその種別自身は引く。

    なぜ引くか: ``kind=dm-send`` のように「既定除外されている種別を明示して見たい」
    read を成立させるため (= 消すのではなく既定から外すだけ、という契約)。それ以外の
    組合せは素直な AND になる。

    **Python 側の窓 (:func:`window_decision_events`) と SQL 生成
    (:func:`mysql_window_sql`) の両方がこの 1 関数を呼ぶ** (ms-166 e-5986 独立レビュー
    保守性 M-1)。旧実装は同じ式を 2 箇所に逐語コピーしており、片方だけ直すと
    「一覧には出るのに SQL では落ちる (逆も)」という、このモジュールが警告している
    まさにその drift を再生産しうる状態だった。
    """
    if not exclude_kinds:
        return frozenset()
    return frozenset(exclude_kinds) - ({kind} if kind else frozenset())


def window_decision_events(rows, *, kind: str = "", limit: int = 100,
                           since: str = "", session: str = "",
                           target: str = "", exclude_kinds=None) -> list[dict]:
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

    ``exclude_kinds`` (ms-166 e-6603) は既定 read から外す kind 集合
    (:data:`NON_DECISION_KINDS` を渡す想定)。``limit`` の前に適用するので、除外した分
    だけ本物の判断が窓からこぼれることはない。``kind`` と同時に渡した場合は
    **AND 合成** で、除外集合から ``kind`` 自身を引いて適用する — つまり
    「``kind=dm-send`` を明示すれば dm-send は引ける」性質を保ったまま、残りの組合せは
    素直な AND になる (片方が黙って勝つ優先規則は置かない / 独立レビュー AX-4)。

    ``rows`` は各 backend が取得した decision dict の list (``decision_id`` / ``kind``
    / ``created_at`` / ``who`` / ``related`` を持つ)。純関数 — 副作用なし、入力 list は
    変更しない。
    """
    out = list(rows or [])
    if kind:
        out = [r for r in out if (r.get("kind") or "") == kind]
    if exclude_kinds:
        # ms-166 e-6603: 既定 read から「決定でない kind」を外す。**limit の前**に絞るのが
        # 要点 — 後で絞ると「最新 limit 件の中の残り」になって、除外した分だけ本物の判断が
        # 窓からこぼれる (e-5970 で直した filter-after-truncate と同じ穴を再生産する)。
        #
        # ``kind`` と同時に渡されたら **AND 合成** する: 除外集合から ``kind`` 自身を引いて
        # 適用する (独立レビュー AX-4)。旧実装は elif で ``kind`` 指定時に exclude_kinds を
        # 丸ごと無視しており、署名からは「両方渡すと片方が黙って勝つ」ことが読めなかった。
        # ``kind`` 自身を引くので「``kind=dm-send`` を明示したら dm-send が引ける」性質は
        # 保たれ、それ以外の組合せは素直な AND になる。
        _ex = effective_exclude_kinds(exclude_kinds, kind)
        if _ex:
            out = [r for r in out if (r.get("kind") or "") not in _ex]
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

# ── read 窓の絞り込み仕様 (ms-166 e-5986) ────────────────────────────────────
#
# 窓の意味論は :func:`window_decision_events` が持つが、**MySQL backend は同じ絞り込みを
# SQL へ押し下げる** 必要がある (append-only の流れは無制限に伸び、全件を Python に読むと
# 2026-08-20 の本番停止と同型の負荷になる / CORE doc scale-contract-principle)。
#
# そこで「どの項目を・どの JSON パスで・どう比べるか」を **この 1 つの仕様表** に置き、
# Python 述語と SQL 片の両方をここから導く。2 箇所に書くと、片方だけ直したときに
# 「一覧には出るのに SQL では落ちる (逆も)」という最悪の drift になる — しかも手元の
# 規模テストは偽カーソルが WHERE を解釈しないので **誤りが緑で通る**。
#
# 各項目: (名前, JSON パスの候補列, 比較の種類)
#   - パス候補が複数なら **先に見つかった非空** を使う (``target`` の related → top-level
#     fallback がこれ。:func:`_row_target_id` と同じ順序)
#   - 比較は "eq" (等値) / "gt" (より大きい = since) / "not_in" (除外集合)
#
# NULL / 欠損の扱い (SQL 側で明示的に揃える):
#   JSON に無い / JSON null の項目は Python では ``""`` になる。SQL では
#   ``JSON_UNQUOTE(JSON_EXTRACT(...))`` が NULL か文字列 ``'null'`` を返すので、
#   ``NULLIF(..., 'null')`` で潰してから ``COALESCE(..., '')`` で空文字に落とす。
#   これを忘れると ``NULL NOT IN (...)`` が NULL になり、種別を持たない行が **SQL でだけ
#   静かに消える**。
_WINDOW_FILTERS: tuple = (
    ("kind", ("$.kind",), "eq"),
    ("session", ("$.who.session_id",), "eq"),
    ("target", ("$.related.target_id", "$.target_id"), "eq"),
    ("since", ("$.created_at",), "gt"),
    ("exclude_kinds", ("$.kind",), "not_in"),
)

# 並び順の基準 (newest-limit を SQL に寄せるため)。Python 側の sort key と同じ順序。
_WINDOW_ORDER: tuple = ("$.created_at",)


def window_filter_spec() -> tuple:
    """絞り込み仕様表を返す (公開アクセサ)。SQL 生成側と検査テストが参照する。"""
    return _WINDOW_FILTERS


def _row_value(row: dict, paths) -> str:
    """``paths`` の候補を順に見て、最初の非空を文字列で返す (無ければ ``""``)。

    ``$.a.b`` 形式の JSON パスを dict 辿りに写す。SQL 側の
    ``COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(data, '$.a.b')), 'null'), …, '')``
    と同じ値を返すのが契約。
    """
    for path in paths:
        cur = row
        for part in path.lstrip("$.").split("."):
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(part)
        if cur:
            return str(cur)
    return ""


def mysql_window_sql(*, kind: str = "", limit: int = 100, since: str = "",
                     session: str = "", target: str = "", exclude_kinds=None):
    """絞り込み仕様表から MySQL の ``WHERE`` / ``ORDER BY`` / ``LIMIT`` を組む。

    返り値は ``(sql_tail, params)``。``sql_tail`` は ``WHERE pk=%s`` に続けて
    ``AND …`` を並べ、``ORDER BY … DESC`` + ``LIMIT %s`` までを含む断片。
    **newest-limit を SQL に寄せる**ので、呼び出し側は返ってきた行を昇順に並べ直して
    :func:`window_decision_events` に通す (意味論の最終判定はそちらが持つ)。

    純関数 — DB に触らない。SQL 文字列はテストで固定されるので、将来の変更が差分に出る。
    """
    values = {"kind": kind, "session": session, "target": target,
              "since": since, "exclude_kinds": exclude_kinds}
    # 除外集合の算出は窓ヘルパーと同じ 1 関数を通す (保守性 M-1: 式の逐語コピーを廃止)
    if exclude_kinds:
        values["exclude_kinds"] = sorted(effective_exclude_kinds(exclude_kinds, kind))
    clauses: list = []
    params: list = []
    for name, paths, op in _WINDOW_FILTERS:
        value = values.get(name)
        if not value:
            continue
        expr = _mysql_coalesced_expr(paths)
        if op == "eq":
            clauses.append(f"{expr} = %s")
            params.append(value)
        elif op == "gt":
            clauses.append(f"{expr} > %s")
            params.append(value)
        elif op == "not_in":
            holes = ", ".join(["%s"] * len(value))
            clauses.append(f"{expr} NOT IN ({holes})")
            params.extend(value)
    tail = "".join(f" AND {c}" for c in clauses)
    order = ", ".join(f"{_mysql_coalesced_expr((p,))} DESC" for p in _WINDOW_ORDER)
    tail += f" ORDER BY {order}, sk DESC"
    if limit and limit > 0:
        tail += " LIMIT %s"
        params.append(int(limit))
    return tail, params


def _mysql_coalesced_expr(paths) -> str:
    """JSON パス候補列を「最初の非空、無ければ空文字」の MySQL 式に写す。

    ``NULLIF(..., 'null')`` で JSON null (= MySQL では文字列 ``'null'``) を潰し、
    最後に ``COALESCE(..., '')`` で欠損を空文字に落とす。:func:`_row_value` と同じ値を
    返すのが契約。
    """
    inner = ", ".join(
        f"NULLIF(JSON_UNQUOTE(JSON_EXTRACT(data, '{p}')), 'null')" for p in paths)
    return f"COALESCE({inner}, '')"

def mysql_window_eval(rows, sql: str, params) -> list:
    """生成 SQL を **仕様表に基づいて** 評価し、MySQL が返すはずの行を再現する。

    テスト専用の評価器 (ms-166 e-5986)。SQL 文字列を手で parse するのではなく、
    :func:`mysql_window_sql` が仕様表から組んだ式そのものを ``sql`` の中から探して
    「どの項目が絞られているか」を復元し、パラメータを同じ順に消費する。手書きの
    parser を置くと SQL 生成側と評価側が別々に drift するが、この形なら **仕様表が
    変われば両方が同時に変わる**。

    これで検証できるのは「生成 SQL が仕様表どおりに評価されたら結果はどうなるか」
    まで。**本物の MySQL が仕様表どおりに評価するかは検証できない** — JSON 欠損の
    COALESCE / JSON null の NULLIF / ORDER BY DESC の挙動は、デプロイ後に実機で
    突合する前提 (規模テストの偽カーソルは WHERE を一切解釈しないので、そこでは
    この次元が測れない)。
    """
    out = list(rows or [])
    idx = 0
    for name, paths, op in _WINDOW_FILTERS:
        expr = _mysql_coalesced_expr(paths)
        if op == "not_in":
            marker = f"{expr} NOT IN ("
            if marker not in sql:
                continue
            holes = sql.split(marker, 1)[1].split(")", 1)[0].count("%s")
            excluded = {str(v) for v in params[idx:idx + holes]}  # 生成側が既に kind を引いている
            idx += holes
            out = [r for r in out if _row_value(r, paths) not in excluded]
            continue
        symbol = "=" if op == "eq" else ">"
        if f"{expr} {symbol} %s" not in sql:
            continue
        value = str(params[idx])
        idx += 1
        if op == "eq":
            out = [r for r in out if _row_value(r, paths) == value]
        else:
            out = [r for r in out if _row_value(r, paths) > value]
    # ORDER BY ... DESC, sk DESC + LIMIT n (= 最新側から n 件)
    order_expr = _mysql_coalesced_expr(_WINDOW_ORDER)
    if f"ORDER BY {order_expr} DESC" in sql:
        out.sort(key=lambda r: (_row_value(r, _WINDOW_ORDER),
                                str(r.get("decision_id") or "")), reverse=True)
    if "LIMIT %s" in sql:
        out = out[:int(params[-1])]
    return out

# ──────────────────────────────────────────────────────────────────────────
# 以下は #772 (e-6602 完遂の冪等) 由来。上の read 窓の絞り込み仕様 (e-5986) とは
# 独立した追記で、名前の衝突も無いため両方を残している (merge 合成)。
# ──────────────────────────────────────────────────────────────────────────

# ── 完遂 (= target が終端に到達した) decision の冪等規則 (ms-166 e-6602) ──────────
#
# 完遂を宣言できる入口は 7 経路ある (A: milestone done / target close、B: opportunity
# judge terminal、C: opportunity phase <terminal>、D: operation close、E: acquisition
# status <terminal>、F: target approve = review gate、G: server の done_milestone route)。
# どれも ``append_decision_event`` へ収束するが、そこに冪等制約が無く decision_id を
# 毎回新規 mint して無条件 append していたため、**同じ target が同じ verdict で二度
# 完遂を宣言されると同一内容の行が 2 本残る** 状態だった (例: ``opportunity phase 失注``
# で決着した後に ``judge terminal 失注``、``milestone done`` の後に ``target approve``)。
#
# 対になる deliverable 側は ``deliverable_capture.capture_target_completion`` が
# 「同じ target×category の active 行が在れば append しない」= first-write-wins の
# 冪等性を既に持っている。decision 側だけ非対称に開いていたのを閉じる (= 受入条件2
# 「deliverable と decision の冪等性が対称」)。
#
# 鍵を ``(target, kind, decision)`` の 3 つ組にして ``decision`` (= verdict) を含めるのは、
# **1 つの target が異なる verdict で段階的に完遂しうる**から。実データでも
# ``observing`` で完遂した後に ``done`` へ倒る target が在り、``(target, kind)`` だけを
# 鍵にすると後段の正当な状態変化が落ちる (さらに F の rich rationale を持つ記録が
# 失われる)。「同じ結論を二度書かない」だけを弾き、結論が変わった記録は残す。
#
# 窓 (``window_decision_events``) と同じ理由でこのモジュールに置く: 3 つの store
# backend に逐語コピーすると 1 箇所だけ直した時に silent に drift する。backend は
# 「行の取得」と「append の中止」だけを担い、**何を重複と見なすか**はここが決める。
COMPLETION_DECISION_KINDS: frozenset[str] = frozenset({"completion-verdict"})


def completion_dedup_key(row: dict) -> tuple[str, str, str] | None:
    """完遂 decision の冪等キー ``(target_id, kind, decision)`` — 対象外なら ``None``。

    ``None`` は「この行に冪等制約を課さない」の意 (= 従来どおり無条件 append)。
    対象外になるのは 2 つ:

    - ``kind`` が完遂族 (:data:`COMPLETION_DECISION_KINDS`) でない。dm-send /
      review-adjudication / task-done 等は同じ内容が正当に反復しうる (同じ PR を
      二度採否する、同じ相手に二度送る) ので、ここで止めてはならない。
    - 完遂族だが ``related.target_id`` が空。どの target の完遂かが判らない行は
      重複判定の基準を持てないので、落とさず残す (= 安全側: 記録を消さない)。

    ``target_id`` の読み出しは :func:`_row_target_id` (``related.target_id`` →
    top-level ``target_id`` fallback) に委ねる。窓の target 絞りと同じ規則で読む
    ことで、「list --target で引ける行」と「重複と見なされる行」がズレない。
    """
    kind = str((row or {}).get("kind") or "")
    if kind not in COMPLETION_DECISION_KINDS:
        return None
    target_id = _row_target_id(row or {})
    # 空白のみの target も「target 無し」として扱う (= 冪等対象外、記録を消さない)。
    # ただしキーに載せる値は _row_target_id が返したままにする: 窓の target 絞りは
    # 完全一致なので、重複判定だけ正規化すると「list --target で引けない行を重複と
    # 見なす」ズレが生まれる。読む側と同じ値で突き合わせる。
    if not target_id.strip():
        return None
    return (target_id, kind, str((row or {}).get("decision") or ""))


def find_duplicate_completion(rows, record: dict) -> dict | None:
    """``rows`` の中から ``record`` と同じ完遂キーを持つ既存行を返す (無ければ ``None``)。

    純関数。``record`` が完遂族でない / target を持たない場合は常に ``None`` を返す
    (= 冪等制約の対象外なので「重複は無い」と答える)。複数一致した場合は
    **最初の 1 件** を返す: first-write-wins なので、残すべきは最も古い記録。

    各 backend の ``append_decision_event`` が書き込み直前に呼ぶ。返り行が在れば
    append を中止し、その ``decision_id`` を呼び出し側へ返す (= 冪等な reject:
    「記録は 1 度だけ在る」という事実を正しく返しつつ、best-effort な完遂フローを
    例外で壊さない)。
    """
    key = completion_dedup_key(record or {})
    if key is None:
        return None
    for row in (rows or []):
        if completion_dedup_key(row) == key:
            return row
    return None
