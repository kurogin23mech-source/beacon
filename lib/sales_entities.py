"""Sales job-template entities — ms-106 ① 営業エンティティ構築.

This is the *sales* profession's ② domain model (see ms-106 SPEC 設計方針 0):
new code, deliberately NOT reusing the milestone/task functions. It lives on
the same shared storage substrate (Store → project.json), which is the L1/③
infrastructure; only the entity shapes and their CRUD are sales-specific.

Entity model (SPEC §0 ER, minimum data layer only — no phase-transition
behaviour, deadline scan, pipeline rollup or UI; those belong to ms-107/108):

  Account       対象・継続 (never closes; keeps a health value + lifecycle phase)
    ├─ phase       exclusive field over the *account* funnel (リード → …)
    └─ contacts[]   sub-entity, nested (SPEC: "Account のフィールドでもいい")
  Opportunity   対象・有限 (closes at a terminal phase: 成約/失注/不成立)
    ├─ account_id  参照 association (N:1 → Account, independent lifecycle)
    ├─ phase       exclusive field over the *opportunity* funnel
    ├─ phase_history[]  append-only transition log (= 進捗の真値/証拠列,
    │                   the sales analogue of the dev commit series)
    └─ activities[]  従属 composition (N:1 → Opportunity), 業務・事前計画型

The phase *funnel itself* is per-company configuration, not a library constant
(案A, 2026-07-13): each target-type carries its own ordered phase vocabulary in
project.json (``account_phases`` / ``opportunity_phases``). This is the essence
of a sales project — the stages + their win-rates ARE that company's sales
methodology (SPEC §6: probability is a manual per-phase parameter). The model
reads the vocabulary from ``data`` rather than hard-coding it.

Master / source-of-truth (SPEC §5): phase transitions are declared by the
human (master). Declaration is permissive — declaring a phase outside the
vocabulary, or a terminal outside a stage's ``allowed_terminals`` rule, yields
a *warning* (via ``phase_warnings``) but is never blocked. The rule lives in
config so it can be tightened later without changing this module.
"""

from __future__ import annotations

from typing import Optional

import work_base
import work_model  # ms-109 e-3559: 職種非依存の Target/WorkItem 正準アクセサ
import deadline  # ms-139 e-4948: L2 締切エンジン (temporal core をここへ抽出)
import disclosure  # ms-113 e-3734: project 参加ベースの開示プリミティブ
import org  # ms-113 e-3734: personal/team org id 導出
import master_projection  # ms-111 e-3621: Account/Contact 生成時の master link seam
import attack_list  # ms-132 e-4501/e-4502: 打診フェーズ funnel の既定語彙 source of truth

# 取消 (cancelled) 状態は基底 work_base の語彙に揃える (ms-109 e-3558)。営業の
# activity / communication の「誤起票の訂正」(e-3537) と Meeting の cancelled が
# 同じ status 値を使うことで、UI・判定の語彙が職種横断で一致する。
CANCELLED_STATUS = work_base.CANCELLED_STATUS

# --- default funnel seeds (first-user 実フロー, 2026-07-13) -----------------
# Seeded into a fresh sales project.json by build_sales_project. Editable per
# company afterwards (that's the point of storing them as config). These are
# SEEDS, not enforced constants — the live vocabulary is always read from data.

DEFAULT_ACCOUNT_PHASES = [
    {"name": "未接触"},          # ms-115 e-3787: 生リスト。まだ接触していない顧客候補
                                 #   (= ターゲットリスト)。新規 Account の既定入口。
    {"name": "リード"},          # 接触して関係が始まった見込み客
    {"name": "未成約顧客"},      # 商談はあるがまだ成約なし
    {"name": "成約顧客"},        # 成約実績のある継続顧客
]

# 各進行フェーズは面談で区切られる (商談準備→初回面談→提案準備→提案面談→先方
# 検討中→先方合意→合意済み、最低2回・同期合意なら3回の面談)。methodology
# (goal / activity_template / default_lead) は ms-107 e-3375 でユーザー実務から
# 確定した基本4フェーズの型。判定手段は e-3581 で撤去 (transition_signal 廃止):
# 「どう判定するか」はフェーズ固定ではなく前進ゲートに紐づけた work-item で決まる。
# default_lead は面談未定時のフォールバック日数 (本命の遷移日は実際の面談日 =
# カレンダー由来)。詳細見積の提出は「提案段階の時も検討段階の時もある」状況依存の
# ためテンプレに入れず、AI の文脈生成 (e-3373) が必要な時だけ足す。これらは SEED
# (編集可能な出発点) であり enforce される定数ではない。
DEFAULT_OPPORTUNITY_PHASES = [
    # 進行フェーズ: allowed_terminals = ここから宣言できる決着。
    {"name": "商談準備", "probability": 10, "terminal": False,
     "allowed_terminals": ["不成立"],
     "goal": "初回面談の実施により、商談として進行可能な状態にする",
     "activity_template": ["初回面談を打診",
                           {"desc": "初回面談を実施", "kind": "meeting"},
                           "提案の方向性を確定"],
     "default_lead": 7},
    {"name": "提案準備", "probability": 20, "terminal": False,
     "allowed_terminals": ["成約", "失注"],
     "goal": "企画を作り提案を終え、先方が検討フェーズに入った状態にする",
     # ms-106 e-3527: 提案準備に進む時点で提案の規模 = 想定金額が定まっているべき。
     # require_amount は phase 定義側のフラグにして「どのフェーズから金額を課すか」を
     # per-company に設定可能にする (block でなく警告、master = 人間)。
     "require_amount": True,
     "activity_template": ["提案面談を打診",
                           {"desc": "提案面談を実施", "kind": "meeting"},
                           "提案内容を準備"],
     "default_lead": 14},
    {"name": "先方検討中", "probability": 40, "terminal": False,
     "allowed_terminals": ["成約", "失注"],
     "goal": "先方の実行合意を取る",
     "require_amount": True,
     "activity_template": ["合意確認日を確定（必要なら面談設定）", "合意の確認を取る"],
     "default_lead": 14},
    {"name": "合意済み", "probability": 80, "terminal": False,
     "allowed_terminals": ["成約", "失注"],
     "goal": "契約を締結する",
     "require_amount": True,
     "activity_template": ["契約書を送付", "締結"],
     "default_lead": 7},
    # 決着フェーズ (terminal): outcome は有限ターゲットの結末種別。
    {"name": "成約",       "probability": 100,  "terminal": True,  "outcome": "won"},
    {"name": "失注",       "probability": 0,    "terminal": True,  "outcome": "lost"},
    {"name": "不成立",     "probability": 0,    "terminal": True,  "outcome": "abandoned"},
]

# 打診フェーズ funnel (ms-132 e-4502): インサイドセールスの attack-list (= 一括打診
# リスト) の各行が辿る状態 (未接触 → 連絡済 → 返信あり → アポ)。account / opportunity
# funnel と同じく per-company config として project.json (``prospect_phases``) に保存され
# ``beacon phase prospect ...`` で編集できる。既定語彙の source of truth は
# ``attack_list.DEFAULT_PROSPECT_PHASES`` (依存の無い pure leaf module) だけ — ここに同名の
# 派生定数は置かず、seed が要る唯一の場所 (``build_sales_project``) で funnel-def 形式
# ({"name": ...}) に包む。attack-list の行 phase は table-doc の enum 列がこの語彙で検査する。


def default_prospect_phase_defs() -> list:
    """The prospect funnel seed in funnel-def form (``[{"name": ...}]``), wrapping
    the single source-of-truth vocabulary ``attack_list.DEFAULT_PROSPECT_PHASES``.
    One place builds this shape so there is no second ``DEFAULT_PROSPECT_PHASES``
    constant to drift (ms-132 e-4502 保守性レビュー)."""
    return [{"name": n} for n in attack_list.DEFAULT_PROSPECT_PHASES]

# who_has_the_ball (SPEC §4): whose court the deal is in. Data only in ms-106.
BALL_SELF = "self"
BALL_COUNTERPART = "counterpart"
VALID_BALL = {BALL_SELF, BALL_COUNTERPART}


# ---------------------------------------------------------------------------
# ID allocation. The ``max(existing)+1`` algorithm now lives in the shared
# ``work_base.next_suffixed_id`` (ms-109 e-3558) — the same one the development
# allocator uses — instead of being re-implemented here. Only the sales-specific
# part (which id list to hand it, per prefix) stays below.
# ---------------------------------------------------------------------------

def _next_prefixed_id(ids: list, prefix: str) -> str:
    return work_base.next_suffixed_id(ids, prefix)


def next_account_id(data: dict) -> str:
    return _next_prefixed_id([a.get("id", "") for a in data.get("accounts", [])], "acc-")


def next_opportunity_id(data: dict) -> str:
    return _next_prefixed_id([o.get("id", "") for o in data.get("opportunities", [])], "opp-")


def next_activity_id(data: dict) -> str:
    ids = []
    for opp in data.get("opportunities", []):
        ids.extend(act.get("id", "") for act in opp.get("activities", []))
    return _next_prefixed_id(ids, "act-")


# ---------------------------------------------------------------------------
# Phase vocabulary (per-company config, read from data)
# ---------------------------------------------------------------------------

def account_phases(data: dict) -> list:
    return data.get("account_phases", [])


def opportunity_phases(data: dict) -> list:
    return data.get("opportunity_phases", [])


def prospect_phases(data: dict) -> list:
    """The打診フェーズ funnel (ms-132 e-4502): the per-company vocabulary an
    attack-list's row phase advances through. Read from saved config, like the
    account / opportunity funnels."""
    return data.get("prospect_phases", [])


# Source labels for effective_phases (a funnel came from saved config, from the
# shipped built-in seed, or the project has no funnel concept at all).
PHASE_SOURCE_CONFIGURED = "configured"
PHASE_SOURCE_DEFAULT = "builtin_default"
PHASE_SOURCE_NONE = "none"


def effective_phases(data: dict) -> dict:
    """The funnels a *sales* project actually runs on, resolved once.

    Each funnel is the saved per-company vocabulary if configured, else the
    shipped ``DEFAULT_*`` seed — the "configured or default" rule. This is the
    ONE authoritative place for that rule (ms-134 e-4638): the account-phase
    derivation helpers (``_account_phase_idx`` / ``derive_account_phase``) now
    read ``effective_phases(data)["account"]`` instead of each re-applying
    ``account_phases(data) or DEFAULT_ACCOUNT_PHASES``, so the fallback — and its
    profession gate (non-sales → empty, no default leak) — lives here alone. It
    returns a **per-funnel source** so a caller can tell configured from default
    *per funnel* (a project may have custom account phases but be on the default
    opportunity funnel). Non-sales projects have no funnel → empty lists, source
    ``none``. (ms-129 e-4405 + AX/保守性 review 2026-07-30; ms-134 e-4638.)

    Returns ``{"account": [...], "opportunity": [...], "prospect": [...],
    "source": {"account": <label>, "opportunity": ..., "prospect": ...},
    "using_builtin_default": bool}``.
    """
    profession = (data.get("profession") or "").strip().lower()
    if profession != "sales":
        none = {"account": PHASE_SOURCE_NONE, "opportunity": PHASE_SOURCE_NONE,
                "prospect": PHASE_SOURCE_NONE}
        return {"account": [], "opportunity": [], "prospect": [],
                "source": none, "using_builtin_default": False}

    def _resolve(configured, default_list):
        if configured:
            return list(configured), PHASE_SOURCE_CONFIGURED
        return [dict(p) for p in default_list], PHASE_SOURCE_DEFAULT

    acc, acc_src = _resolve(account_phases(data), DEFAULT_ACCOUNT_PHASES)
    opp, opp_src = _resolve(opportunity_phases(data), DEFAULT_OPPORTUNITY_PHASES)
    pros_cfg = prospect_phases(data)
    if pros_cfg:
        pros, pros_src = list(pros_cfg), PHASE_SOURCE_CONFIGURED
    else:
        pros, pros_src = default_prospect_phase_defs(), PHASE_SOURCE_DEFAULT
    source = {"account": acc_src, "opportunity": opp_src, "prospect": pros_src}
    return {"account": acc, "opportunity": opp, "prospect": pros,
            "source": source,
            "using_builtin_default": PHASE_SOURCE_DEFAULT in source.values()}


# Macro-frame (前進の枠組み) — e-3582, SPEC 方針5(A) / AC6. A funnel-level
# natural-language framing that teaches the AI how to *read* the phases: a phase
# is not a bucket to sit in but a stage to be pushed *out of*. "仕事を進める" =
# advancing the deal's open phase through its前進ゲート to the next. Names carry
# the per-object frame (前進ゲート / 空・確定); this carries the whole-funnel one.
# The concept ("a target-class carries a macro-frame") is class-layer; the *text*
# is the sales instance's, so it lives in config (seeded, editable per company).
OPPORTUNITY_PHASE_MACRO_FRAME = (
    "各フェーズは、そこに留まるための箱ではなく『次へ抜けさせるためのステージ』です。"
    "仕事を進めるとは、いま進行中(open)のフェーズを前進ゲート(= 次へ進めてよいかを"
    "判定する関門)を通して次のフェーズへ抜けさせること。前進ゲートに work-item(面談/"
    "活動)を紐づけ、その完了で判定を促し、人が 前進 / やり直し / 決着 を決めます。"
    "滞留は『放置してよい』ではなく『まだ抜けていない』状態を意味します。"
)


def opportunity_phase_frame(data: dict) -> str:
    """The opportunity funnel's macro-frame text (e-3582): how the AI should read
    the phases — as stages to advance *out of*, not buckets to rest in. Reads a
    project's configured ``opportunity_phase_frame`` (editable per company), else
    the shipped default ``OPPORTUNITY_PHASE_MACRO_FRAME``."""
    return (data.get("opportunity_phase_frame") or "").strip() or \
        OPPORTUNITY_PHASE_MACRO_FRAME


def _phase_names(phases: list) -> list:
    return [p.get("name") for p in phases]


def _find_phase_def(phases: list, name: str) -> Optional[dict]:
    for p in phases:
        if p.get("name") == name:
            return p
    return None


def default_account_phase(data: dict) -> str:
    ps = account_phases(data)
    return ps[0]["name"] if ps else DEFAULT_ACCOUNT_PHASES[0]["name"]


def default_opportunity_phase(data: dict) -> str:
    """First non-terminal opportunity phase (= funnel entry). Falls back to
    the first phase, then to the seed's first stage."""
    ps = opportunity_phases(data)
    for p in ps:
        if not p.get("terminal"):
            return p["name"]
    if ps:
        return ps[0]["name"]
    return DEFAULT_OPPORTUNITY_PHASES[0]["name"]


def default_prospect_phase(data: dict) -> str:
    """First stage of the prospect funnel (= 生リストの未接触 entry). Falls back to
    the shipped seed's first name when a project has no saved prospect funnel
    (ms-132 e-4502)."""
    ps = prospect_phases(data)
    return ps[0]["name"] if ps else attack_list.INITIAL_PROSPECT_PHASE


# ---------------------------------------------------------------------------
# Attack-list bulk-send batch (ms-132 e-4504) — the structural anchor for the
# human 1-confirm gate on external outreach.
#
# A bulk external send goes: plan (pending) → authorize (confirm) → record per
# recipient (requires authorized). The invariants this model enforces:
#   - a recipient's send is *recordable* only inside an AUTHORIZED batch;
#   - authorization flips pending→authorized only via the CLI verb that refuses a
#     bus / DM / auto-execute origin AND an *armed* (autonomous) session — so no
#     no-human-in-loop context authorizes. (This does NOT prove a human typed the
#     command: in an agentic system the AI operates the CLI on the human's behalf.
#     A dialog AI acting on its human's instruction is the intended operator; the
#     Skill's explicit confirm step + the ``authorized_by`` audit cover it.)
#   - a record's message must digest to the confirmed 文面 (bind confirmed↔sent).
# So a prospect row's 未接触→連絡済 transition and its outbound 証跡 are reachable
# ONLY through an authorized batch and only for the confirmed content — confirm 前
# は 1 通も『記録』されない (SPEC 方針4). The transport itself (MCP Gmail) is
# Skill-level and uncontrollable from here; a rogue direct send leaves ZERO
# batch/証跡/phase record — silent in Beacon's ledger, detectable only by
# reconciling the Gmail sent-box (a follow-up detection Operation, out of scope).
# ---------------------------------------------------------------------------

SEND_BATCH_PENDING = "pending"
SEND_BATCH_AUTHORIZED = "authorized"
SEND_BATCH_SENT = "sent"
SEND_BATCH_CANCELLED = "cancelled"
_SEND_BATCHES_KEY = "attack_list_send_batches"


def _send_batches(data: dict) -> list:
    return data.setdefault(_SEND_BATCHES_KEY, [])


def next_send_batch_id(data: dict) -> str:
    return _next_prefixed_id(
        [b.get("id", "") for b in data.get(_SEND_BATCHES_KEY, [])], "sendb-")


def find_send_batch(data: dict, batch_id: str) -> Optional[dict]:
    for b in data.get(_SEND_BATCHES_KEY, []):
        if b.get("id") == batch_id:
            return b
    return None


def pending_send_batch_for_doc(data: dict, doc_id: str) -> Optional[dict]:
    for b in data.get(_SEND_BATCHES_KEY, []):
        if b.get("doc_id") == doc_id and b.get("status") == SEND_BATCH_PENDING:
            return b
    return None


def authorized_send_batch_for_doc(data: dict, doc_id: str) -> Optional[dict]:
    for b in data.get(_SEND_BATCHES_KEY, []):
        if b.get("doc_id") == doc_id and b.get("status") in (
                SEND_BATCH_AUTHORIZED, SEND_BATCH_SENT):
            return b
    return None


def create_send_batch(data: dict, *, doc_id: str, recipients: list,
                      message_digest: str, message_preview: str,
                      created_at: str, created_by: str = "") -> dict:
    """Create the pending send batch for ``doc_id``, superseding any earlier
    pending one (re-planning always yields exactly one fresh pending batch).
    ``recipients`` = list of ``{acc_id, row_id, email}``."""
    for b in _send_batches(data):
        if b.get("doc_id") == doc_id and b.get("status") == SEND_BATCH_PENDING:
            b["status"] = SEND_BATCH_CANCELLED  # superseded by the re-plan
    batch = {
        "id": next_send_batch_id(data),
        "doc_id": doc_id,
        "status": SEND_BATCH_PENDING,
        "message_digest": message_digest,
        "message_preview": message_preview,
        "recipients": [{"acc_id": r["acc_id"], "row_id": r.get("row_id", ""),
                        "email": r.get("email", ""), "sent_at": None,
                        "message_id": "", "comm_id": ""} for r in recipients],
        "created_at": created_at,
        "created_by": created_by,
        "authorized_at": None,
        "authorized_by": "",
    }
    _send_batches(data).append(batch)
    return batch


def authorize_send_batch(data: dict, batch_id: str, *, at: str, by: str = "") -> dict:
    """Flip a pending batch to authorized (the single human confirm). Idempotent
    on an already-authorized batch; refuses a sent/cancelled one."""
    b = find_send_batch(data, batch_id)
    if b is None:
        raise ValueError(f"send batch not found: {batch_id}")
    if b.get("status") == SEND_BATCH_AUTHORIZED:
        return b
    if b.get("status") != SEND_BATCH_PENDING:
        raise ValueError(
            f"batch {batch_id} is {b.get('status')}; only a pending batch can be "
            f"authorized")
    b["status"] = SEND_BATCH_AUTHORIZED
    b["authorized_at"] = at
    b["authorized_by"] = by
    return b


def batch_recipient(batch: dict, acc_id: str) -> Optional[dict]:
    for r in batch.get("recipients", []):
        if r.get("acc_id") == acc_id:
            return r
    return None


def record_batch_send(data: dict, doc_id: str, acc_id: str, *, at: str,
                      message_id: str = "") -> dict:
    """Mark one recipient sent within the doc's AUTHORIZED batch.

    The structural gate: refuses unless an authorized batch exists for the doc,
    the recipient is in it, and it was not already sent (idempotency = no
    double-send). Returns the recipient dict so the caller can attach the
    Communication id after recording the 証跡. Never fabricates authorization —
    a send can only be recorded against a human-authorized batch."""
    b = authorized_send_batch_for_doc(data, doc_id)
    if b is None:
        raise ValueError(
            f"{doc_id} に authorized な送信バッチがありません "
            f"(先に `attack-list-send <doc> --confirm` で人間が承認する必要があります)")
    r = batch_recipient(b, acc_id)
    if r is None:
        raise ValueError(f"{acc_id} は承認済みバッチ {b['id']} の宛先ではありません")
    if r.get("sent_at"):
        raise ValueError(f"{acc_id} は既にバッチ {b['id']} で送信記録済みです")
    r["sent_at"] = at
    r["message_id"] = message_id
    if all(x.get("sent_at") for x in b.get("recipients", [])):
        b["status"] = SEND_BATCH_SENT
    return r


def sent_message_ids_for_doc(data: dict, doc_id: str) -> dict:
    """Map acc_id -> the message-id we sent that Account for this attack-list,
    read from the send batches (ms-132 e-4505). Keeps the send-batch schema
    knowledge here (the model owns it) instead of letting a read command reach
    into ``attack_list_send_batches`` internals directly (保守性レビュー)."""
    out = {}
    for b in data.get(_SEND_BATCHES_KEY, []):
        if b.get("doc_id") != doc_id:
            continue
        for r in b.get("recipients", []):
            if r.get("message_id"):
                out[r.get("acc_id")] = r["message_id"]
    return out


def filter_accounts(data: dict, *, phase=None, assignee=None,
                    name_contains=None) -> list:
    """Return the Accounts matching a condition query (ms-132 e-4503).

    Used to assemble an attack list from untouched (= 未接触) Accounts: pass
    ``phase='未接触'`` (the生リスト entry phase, ms-115) for the raw prospect pool,
    optionally narrowing by owner (``assignee``) or a name substring. Predicates
    are AND-combined; a ``None`` predicate is ignored. Cancelled (tombstoned)
    Accounts never match — a corrected mis-entry is not a prospect. Kept as a
    reusable query so the bulk-register path (e-4503) and later lead conversion
    (e-4506) read the same filter instead of re-deriving one each."""
    out = []
    for a in data.get("accounts", []):
        if a.get("status") == CANCELLED_STATUS:
            continue
        if phase is not None and a.get("phase") != phase:
            continue
        if assignee is not None and a.get("assignee") != assignee:
            continue
        if name_contains is not None and name_contains not in (a.get("name") or ""):
            continue
        out.append(a)
    return out


def _opportunity_status_for_phase(data: dict, phase: str) -> str:
    """Mirror status from an opportunity's phase. Terminal phases close the
    (有限) Opportunity, projecting to their configured outcome."""
    pdef = _find_phase_def(opportunity_phases(data), phase)
    if pdef and pdef.get("terminal"):
        return pdef.get("outcome") or "closed"
    return "open"


def advance_funnel_phase(data: dict, opp: dict, new_phase: str) -> str:
    """The opportunity funnel's SINGLE low-level phase writer (ms-142 e-5169):
    set the exclusive ``phase`` and its mirrored ``status`` on ``opp``. Returns
    the phase left behind.

    This is the sales L3 seam ``target_state.set_target_state`` delegates to for a
    SHAPE_FUNNEL class (the twin of ``target_engine.advance_target`` for descriptor
    phases). It owns ONLY the phase↔status mirror — NOT the account rollup or the
    ``at`` audit stamp. Neither this function nor ``set_target_state`` stamps
    ``at``; whichever caller drives the transition (``phase_set`` today, a generic
    advance verb tomorrow) is responsible for threading its own ``at`` into the
    rollup, so routing a transition through ``set_target_state`` cannot shift the
    rollup's timestamp. The mirror uses the same ``_opportunity_status_for_phase``
    the raw write always did, so the status projection is byte-identical (status
    同期不変; pinned by test_sales_entities + test_target_state)."""
    old = opp.get("phase", "")
    opp["phase"] = new_phase
    opp["status"] = _opportunity_status_for_phase(data, new_phase)
    return old


def opportunity_phase_warnings(data: dict, current_phase: str, new_phase: str,
                               *, goal_amount=None, amount=None) -> list:
    """Non-blocking checks for an opportunity phase transition (SPEC §5:
    master=人間 declares; we surface, never block). Returns warning strings:

    * new_phase not in the configured vocabulary,
    * declaring a terminal that the current stage's ``allowed_terminals`` rule
      does not permit (e.g. 商談準備 → 成約, which skips 提案), and
    * (ms-106 e-3527) moving into a phase whose ``require_amount`` flag is set
      while the deal has no 想定金額 — the proposal's size should be known by
      提案準備. Either the goal (``goal_amount``, set at 起票 via --goal) OR the
      deal amount (``amount``, set later via ``opportunity amount``) satisfies
      it, so the rep can clear the warning on an existing deal. Both are passed
      by the caller (which holds the opportunity); when omitted the check skips.
    """
    warnings: list = []
    phases = opportunity_phases(data)
    if not phases:
        return warnings  # no vocabulary configured → nothing to check against
    names = _phase_names(phases)
    if new_phase not in names:
        warnings.append(
            f"'{new_phase}' は opportunity_phases の語彙にありません "
            f"(既知: {', '.join(names)})")
        return warnings
    new_def = _find_phase_def(phases, new_phase)
    cur_def = _find_phase_def(phases, current_phase)
    if new_def and new_def.get("terminal") and cur_def is not None:
        allowed = cur_def.get("allowed_terminals")
        if allowed is not None and new_phase not in allowed:
            warnings.append(
                f"'{current_phase}' から決着できるのは {allowed} のみです "
                f"('{new_phase}' はルール外)")
    # e-3527: 想定金額が要るフェーズへ進むのに未設定なら warning (block しない)。
    # goal_amount (起票時の目標) か amount (後から設定する取引額) のどちらかがあれば可。
    if _phase_requires_amount(new_def, new_phase) and \
            _amount_is_unset(goal_amount) and _amount_is_unset(amount):
        warnings.append(
            f"'{new_phase}' に進むには想定金額 (goal_amount) を設定すべきです "
            f"(提案の規模が未確定)。`beacon opportunity add --goal <円>` または "
            f"既存商談は金額設定で入れてください")
    return warnings


# Default phase names that carry require_amount (ms-106 e-3527). Used as a
# tolerant backfill so projects whose opportunity_phases were seeded from the
# shipped default BEFORE this flag existed still get the warning — no data
# migration needed (same expand pattern as the target-advancement frame).
_DEFAULT_REQUIRE_AMOUNT_PHASES = {
    p["name"] for p in DEFAULT_OPPORTUNITY_PHASES if p.get("require_amount")
}


def _phase_requires_amount(new_def, new_phase: str) -> bool:
    """Whether entering ``new_phase`` should have a 想定金額 set. Honors an
    explicit ``require_amount`` on the configured phase def (True OR False —
    a company can opt out); otherwise backfills from the shipped default by
    phase name so pre-existing projects are covered without migration."""
    if new_def is not None and "require_amount" in new_def:
        return bool(new_def["require_amount"])
    return new_phase in _DEFAULT_REQUIRE_AMOUNT_PHASES


def _amount_is_unset(goal_amount) -> bool:
    """想定金額が「未設定」か。None / 空文字 / 0 以下を未設定とみなす (0 円の提案は
    無いので 0 も未設定扱い)。"""
    if goal_amount is None or goal_amount == "":
        return True
    try:
        return float(goal_amount) <= 0
    except (TypeError, ValueError):
        return True


def opportunity_set_description(data: dict, opportunity_id: str,
                               description: str) -> dict:
    """Set an Opportunity's free-text 背景 / 経緯 / メモ (ms-106 e-3526). Empty
    string clears it. Returns the updated opportunity dict. Raises ValueError
    when the opportunity is unknown.

    ms-143: patches through the profession-generic occupation.update_entry
    (locate + plain field patch), so the sales 更新 path no longer names
    data['opportunities'] / find_opportunity itself. Lazy import — occupation
    imports sales_entities at module load."""
    import occupation
    return occupation.update_entry(
        data, opportunity_id, description=(description or "").strip())


def opportunity_set_title(data: dict, opportunity_id: str, title: str) -> dict:
    """Rename an Opportunity (ms-120 e-3909). Before this there was no path to
    fix a title after creation — `describe` only sets the free-text 背景, not the
    name (parallel to `milestone rename`). Returns the updated opportunity dict.
    Raises ValueError when the opportunity is unknown or the title is empty
    (unlike description, a title cannot be cleared — every opportunity needs a
    name).

    ms-143: the title validation (non-empty) stays here as the sales frontend
    concern; the locate + patch routes through occupation.update_entry so the 更新
    path no longer names data['opportunities'] itself. Lazy import."""
    import occupation
    new_title = (title or "").strip()
    if not new_title:
        raise ValueError("title must not be empty (an opportunity always needs a name)")
    return occupation.update_entry(data, opportunity_id, title=new_title)


def account_phase_warnings(data: dict, new_phase: str) -> list:
    warnings: list = []
    phases = account_phases(data)
    if not phases:
        return warnings
    names = _phase_names(phases)
    if new_phase not in names:
        warnings.append(
            f"'{new_phase}' は account_phases の語彙にありません "
            f"(既知: {', '.join(names)})")
    return warnings


# ---------------------------------------------------------------------------
# Phase methodology (config schema) — ms-107 e-3371, SPEC §1/§7.
# ---------------------------------------------------------------------------
# A phase definition (a dict in a target-type's phase vocabulary) may carry the
# *methodology* of that phase, on top of the ms-106 fields (name / probability /
# terminal / allowed_terminals / outcome):
#
#   goal              str   — このフェーズが達成したいゴール (1 行)
#   activity_template list  — ゴールへ向かう期待活動のテンプレ (固定でなく「期待」,
#                             SPEC §4: 面談 outcome で reconcile する)
#   on_fail           dict  — 判定が失敗した時の分岐設定 (下記 on_fail schema)
#   default_lead      int   — フェーズ入場時に遷移日を置く既定リード日数
#
# 判定手段 (transition_signal) は e-3581 で撤去した: 「どう判定するか」はフェーズ
# 固定の分岐ではなく、前進ゲートに紐づけた work-item (面談 / 活動) が何かで決まる。
# 面談を紐づければ面談終了が、日程付き活動を紐づければその done が判定を促す。何も
# 紐づけなければ遷移日が来たら人が判定する (旧 manual の縮退形)。判定の発火は
# ``gate_judgement_ready`` / ``work_item_completed`` が一手に引き受ける (SPEC §2)。
#
# These are the *target-class generic* vocabulary (SPEC §7: engine core は
# "商談" を語に焼き込まない). The accessors below read a phase_def dict without
# knowing whether the target is an opportunity — so ms-109 can drive the same
# engine over milestones/tasks by supplying a different phase vocabulary.
#
# All fields are OPTIONAL: the ms-106 seed does not carry them yet (the 営業
# アダプタ の初期 methodology は e-3375 で seed する). Accessors default
# gracefully so live projects created before this task keep working.

PHASE_METHODOLOGY_FIELDS = (
    "goal", "activity_template", "on_fail", "default_lead")


def phase_goal(phase_def: Optional[dict]) -> str:
    """The phase's goal string, or '' when unset."""
    return (phase_def or {}).get("goal", "") or ""


# Anchor "kind" markers (e-3548). A bare-string anchor is a plain activity; a
# {"desc","kind":"meeting"} anchor also co-seeds a 予定未定 Meeting on phase entry
# (see instantiate_phase_activities). This encodes the operating rule in the data
# structure rather than asking a Skill to remember it (機構の硬さ = ルールの硬さ).
ANCHOR_KIND_MEETING = "meeting"


def _anchor_desc(anchor) -> str:
    """The description text of a template anchor — tolerant of both the legacy
    bare-string form and the structured {"desc","kind"} form (e-3548)."""
    if isinstance(anchor, dict):
        return str(anchor.get("desc", "")).strip()
    return str(anchor or "").strip()


def _anchor_kind(anchor) -> str:
    """The kind marker of a template anchor ("meeting" etc.), or "" for a plain
    bare-string anchor (e-3548)."""
    if isinstance(anchor, dict):
        return str(anchor.get("kind", "")).strip()
    return ""


def phase_activity_anchors(phase_def: Optional[dict]) -> list:
    """The phase's anchors normalized to ``[{"desc","kind"}, ...]``, dropping
    empty ones. Tolerant read (e-3548): accepts both the legacy bare-string
    template and the structured form, so existing projects keep working while
    new ones can mark meeting-type anchors."""
    out = []
    for a in (phase_def or {}).get("activity_template") or []:
        desc = _anchor_desc(a)
        if desc:
            out.append({"desc": desc, "kind": _anchor_kind(a)})
    return out


def phase_activity_template(phase_def: Optional[dict]) -> list:
    """The phase's expected-activity template as a list of description strings,
    or [] when unset.

    SPEC §4: this is an *expectation*, not a fixed pipeline — the engine
    reconciles it against real meeting outcomes rather than enforcing it.

    e-3548: structured anchors ({"desc","kind"}) are normalized to their ``desc``
    here so existing consumers (status JSON / UI) keep receiving a string list;
    the ``kind`` marker is read via ``phase_activity_anchors`` by the seeder.
    """
    return [a["desc"] for a in phase_activity_anchors(phase_def)]


def phase_on_fail(phase_def: Optional[dict]) -> Optional[dict]:
    """The failure-branch config for this phase, or None.

    Shape (all optional): ``{"terminals": [<phase-name>...], "retry": bool}``.
    ``terminals`` narrows the決着 choices on failure (falls back to the phase's
    ``allowed_terminals`` when absent); ``retry`` marks that staying in-phase
    with a new transition_date is allowed (SPEC §3: advance / retry / terminal).
    The 3-way judgement itself is human-confirmed and lives in e-3372.
    """
    of = (phase_def or {}).get("on_fail")
    return of if isinstance(of, dict) else None


def phase_default_lead(phase_def: Optional[dict]) -> Optional[int]:
    """Default lead time (in days) for placing this phase's transition_date on
    entry, or None when unset. Kept as a plain int-of-days; date arithmetic is
    the caller's job (the engine, e-3372)."""
    lead = (phase_def or {}).get("default_lead")
    if lead is None:
        return None
    try:
        return int(lead)
    except (TypeError, ValueError):
        return None


def phase_methodology(phase_def: Optional[dict]) -> dict:
    """Normalized methodology view of a phase_def (target-class generic).

    Always returns every PHASE_METHODOLOGY_FIELDS key with a graceful default,
    so the engine and callers never KeyError on a phase that predates this
    schema (or on the ms-106 seed, which does not carry methodology yet).
    """
    return {
        "goal": phase_goal(phase_def),
        "activity_template": phase_activity_template(phase_def),
        "on_fail": phase_on_fail(phase_def),
        "default_lead": phase_default_lead(phase_def),
    }


def opportunity_phase_methodology(data: dict, phase_name: str) -> dict:
    """Methodology for a named opportunity phase (looked up in the configured
    vocabulary). Returns the same defaulted shape as ``phase_methodology`` even
    when the phase name is unknown, so callers get a stable dict."""
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    return phase_methodology(pdef)


def opportunity_phase_is_terminal(data: dict, phase_name: str) -> bool:
    """True when the named opportunity phase is a 決着 (terminal) stage."""
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    return bool(pdef and pdef.get("terminal"))


# ---------------------------------------------------------------------------
# Finders
# ---------------------------------------------------------------------------

def find_account(data: dict, account_id: str) -> Optional[dict]:
    for a in data.get("accounts", []):
        if a.get("id") == account_id:
            return a
    return None


def find_opportunity(data: dict, opportunity_id: str) -> Optional[dict]:
    for o in data.get("opportunities", []):
        if o.get("id") == opportunity_id:
            return o
    return None


# ---------------------------------------------------------------------------
# Account (対象・継続) + nested Contact
# ---------------------------------------------------------------------------

def account_add(data: dict, name: str, *, health: str = "", phase: str = "",
                assignee: str = "", created_at: str = "",
                owner_org_id: str = "", master_adapter=None) -> str:
    """Append a new Account (対象・継続) and return its id.

    ms-111 e-3621 chunk2b (additive seam): ``master_adapter`` を渡すと、生成した
    投影 Account を共有マスター (= 顧客 identity の真値源) に link する
    (``master_projection.link_new_account_to_master``)。既定 (None) では link せず
    従来動作のまま = 投影のみ。実 linking (write-through) を駆動する adapter の配線は
    e-3622 (bus 変更イベント同期) が担い、本 seam は「渡されたら link する」層だけを
    先に用意する (呼び元は現状 None のみ = inert、SPEC A 採用の staged migration)。

    Account carries a lifecycle ``phase`` (リード → 未成約顧客 → 成約顧客) with
    an append-only ``phase_history``, plus the ``health`` relationship-value
    slot (SPEC §0 reified 候補, 枠のみ). It never reaches a terminal (継続).

    ``assignee`` (担当ユーザー) is a target-class-generic slot mirroring
    Milestone.assignee (ms-81); it names the project member who owns this
    account. ``nurturings`` は 商談 (Opportunity) を持たない継続関係に対する
    ナーチャリング業務 (業務・事前計画型) を Account に直接ぶら下げる入れ物
    (ms-106 fb3、Opportunity.activities の継続 target 版)。

    ms-113 e-3734 (顧客DBの cross-project 参照): Account に所有 org
    (``owner_org_id``) と開示先 project 集合 (``project_links``) を持たせる。
    ``owner_org_id`` 未指定なら project の org (``org.project_org_id``) から
    決定的に導出する。``project_links`` は空で始まる = 生成時点では **home
    project 内でのみ見える** (home project の read は既存の project 認可が
    ゲートするので、空でも従来通り見える)。別 project から参照させたい時は
    ``beacon disclose <acc-id> --to-project <P>`` (汎用 target_disclosure 層) で
    明示リンクする (= opt-in の cross-project 開示)。
    """
    if not name or not name.strip():
        raise ValueError("Account name is required")
    data.setdefault("accounts", [])
    acc_id = next_account_id(data)
    initial_phase = phase or default_account_phase(data)
    # ms-109 e-3698 (fable A-4): apply the base default for ``created_at`` — a
    # blank timestamps to now — so a direct/test caller that omits it does not
    # persist an empty ``created_at`` (and an empty ``phase_history`` anchor).
    # Account does not yet route through ``work_model.new_target`` because it
    # has no generic ``status`` (継続 / never-terminal, tracked by phase); that
    # extraction is staged pending the sales model stabilising (SPEC 方針3).
    created_at = created_at or work_base.now_iso()
    # ms-113 e-3734: 所有 org を project から導出 (明示指定が優先)。
    owner_org = owner_org_id or org.project_org_id(data)
    data["accounts"].append({
        "id": acc_id,
        "name": name.strip(),
        "label": name.strip(),  # ms-109 e-3625: canonical Target label (dual-write during skew)
        "phase": initial_phase,
        "phase_history": [{"phase": initial_phase, "at": created_at, "note": "initial"}],
        "health": health,
        "assignee": assignee,
        "contacts": [],
        "nurturings": [],
        "created_at": created_at,
        # ms-113 e-3734: org 所有 identity + 開示先 project リンク集合。
        disclosure.OWNER_ORG_FIELD: owner_org,
        disclosure.PROJECT_LINKS_FIELD: [],
    })
    # ms-111 e-3621 chunk2b: adapter があればマスターへ link (無ければ inert)。
    if master_adapter is not None:
        master_projection.link_new_account_to_master(
            data["accounts"][-1], master_adapter, org_id=owner_org, now=created_at)
    return acc_id


# ms-113 generalization: Account 専用だった link/unlink は、任意の Target を開示する
# 汎用層 `target_disclosure.disclose_target` / `undisclose_target` に持ち上げた
# (開示は project_links への add/remove で どの Target でも同一のため)。ここには
# 読み出し側の listing フィルタ (accounts_disclosable_from) だけ残す。


def accounts_disclosable_from(accounts: list, requesting_project_id: str,
                              *, is_home: bool = False) -> list:
    """別 project から参照したとき、開示してよい Account だけに絞る (ms-113 e-3734)。

    - ``is_home=True`` (= 自 project の Account を自 project から読む) の場合は
      **絞らない** (= home read は既存 project 認可がゲート、従来通り全件見える)。
    - ``is_home=False`` (= cross-project 参照) の場合は fail-closed で、
      ``project_links`` に ``requesting_project_id`` を含む Account だけ返す。
      link されていない Account は「どうやっても見えない」(= あなたの要求どおり)。

    判定は呼び出し時の現在の ``project_links`` で行う (= link 剥奪が即反映)。
    """
    if is_home:
        return list(accounts or [])
    scope = {requesting_project_id} if requesting_project_id else set()
    return disclosure.filter_disclosable(accounts or [], scope)


def contact_add(data: dict, account_id: str, name: str, *,
                role: str = "", email: str = "", phone: str = "",
                master_adapter=None, now: str = "") -> dict:
    """Append a Contact under an Account (nested sub-entity) and return it.

    ms-111 e-3621 chunk2b (additive seam): ``master_adapter`` を渡すと、生成した
    投影 Contact を親会社のマスター担当者に link する
    (``master_projection.link_new_contact_to_master``)。親 Account が既に master に
    link 済ならその master_account_id に紐付く。既定 (None) では link せず従来動作。
    実 linking の駆動は e-3622 が担う (本 seam は inert、SPEC A 採用)。

    ``now`` : link 時刻 (ISO8601)。空なら現在時刻を補う。``link_new_contact_to_master``
    の ``now`` 引数にそのまま渡す (master 側の書き込み時刻の単一入力)。
    """
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    if not name or not name.strip():
        raise ValueError("Contact name is required")
    contact = {"name": name.strip(), "role": role, "email": email, "phone": phone}
    acc.setdefault("contacts", []).append(contact)
    # ms-111 e-3621 chunk2b: adapter があればマスターへ link (無ければ inert)。
    # org 束縛軸は親会社の owner org に揃える (親 Account が link された org と一致、
    # SPEC §8)。未設定なら project の org へ fallback。
    if master_adapter is not None:
        contact_org = acc.get(disclosure.OWNER_ORG_FIELD) or org.project_org_id(data)
        master_projection.link_new_contact_to_master(
            contact, master_adapter,
            org_id=contact_org,
            master_account_id=master_projection.linked_master_account_id(acc),
            now=now or work_base.now_iso())
    return contact


def account_rename(data: dict, account_id: str, new_name: str) -> dict:
    """Rename an Account (対象・継続). Returns the mutated account. The name is
    a plain label (not history-tracked like phase), so this is an in-place edit."""
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    if not new_name or not new_name.strip():
        raise ValueError("Account name is required")
    work_model.set_target_label(acc, new_name.strip(), dual_write=True)  # ms-109 e-3625
    return acc


# ---------------------------------------------------------------------------
# Account.transcript_source — 議事録取得元の宣言的 override (e-3552, テナント層)
# ---------------------------------------------------------------------------
# 議事録 (文字起こし) のソースと置き場は顧客ごとに違う: Meet+Workspace は自動
# 生成物がカレンダーイベント添付 / Drive の Meet Recordings に入るが、無料 Google
# や Zoom/tl;dv/Otter は任意フォルダ・任意命名。meeting-wrap が『Drive の Meet
# Recordings にある』を前提化すると壊れる。そこで **顧客別に最小の宣言** を持たせ、
# skill は宣言から解決する (汎用 config エンジンは作らない = 過汎用化回避, rule of
# three)。structured field 採用理由: skill が決定論的に読んで動くには NL でなく
# 構造データが要る (人間向け narrative の dossier とは別物)。
VALID_TRANSCRIPT_SOURCE_TYPES = (
    "meet_calendar",  # Meet+Workspace: mtg- の calendar_event_id から添付を辿る (第一候補)
    "drive_folder",   # 任意の Drive フォルダに置かれる (folder_id で場所を宣言)
    "external",       # Zoom/tl;dv/Otter 等の外部ツール (tool/naming で手掛かりを宣言)
    "manual",         # 自動生成物なし、毎回人に Drive リンクを聞く (無料アカウント等)
)


def set_account_transcript_source(data: dict, account_id: str, source_type: str, *,
                                  folder_id: str = "", naming: str = "",
                                  tool: str = "", clear: bool = False) -> dict:
    """Declare where an Account's 議事録 (文字起こし) comes from (e-3552, テナント層).

    ``source_type`` must be one of ``VALID_TRANSCRIPT_SOURCE_TYPES``. Optional
    ``folder_id`` (drive_folder 用), ``naming`` (命名規則の手掛かり), ``tool``
    (external の道具名) are stored only when 非空. The declaration is written
    atomically (one unit — the skill passes all its fields together, not a partial
    merge). Returns the mutated account.

    #498 review fixes:
    - **AX high — 空 type での暗黙 clear を禁止**。渡し忘れ/typo が既存宣言を silent に
      消す事故を防ぐ。空 type かつ ``clear`` でない → ``ValueError`` (既存は保持)。
    - **AX high (再レビュー) — clear と設定値の共存を拒否**。``clear=True`` かつ type/
      folder_id/naming/tool のいずれかが非空 → ``ValueError``。stale な ``clear`` フラグが
      設定意図を silent に delete へ化けさせる (『set のつもりが残留 clear で delete』)
      経路を塞ぐ。clear と設定はどちらか一方。
    - **type 組合せ検証**: ``drive_folder`` は ``folder_id`` 必須、``external`` は ``tool``
      必須 (無ければ手掛かりが無く ``manual`` と同挙動になる別名なので、``manual`` に倒す)。
      場所/道具を特定できない脆い宣言を作らせない。
    - **VALID_TRANSCRIPT_SOURCE_TYPES が type 集合の唯一の真値源**。docstring/skill md に
      列挙をコピーしない (drift ガード test が双方向で一致を固定する)。"""
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    st = (source_type or "").strip()
    fid = (folder_id or "").strip()
    nm = (naming or "").strip()
    tl = (tool or "").strip()
    if clear:
        if st or fid or nm or tl:
            raise ValueError(
                "clear と設定値が同時に渡されています — どちらか一方にしてください "
                "(clear は宣言を消すだけ、設定は clear なしで)")
        acc.pop("transcript_source", None)
        return acc
    if not st:
        raise ValueError(
            "transcript_source type が空です — 宣言するなら type を渡してください。"
            "宣言を消すのは明示 clear のときだけです (空 type では消しません)")
    if st not in VALID_TRANSCRIPT_SOURCE_TYPES:
        raise ValueError(
            f"transcript_source type must be one of "
            f"{list(VALID_TRANSCRIPT_SOURCE_TYPES)}, got {st!r}")
    if st == "drive_folder" and not fid:
        raise ValueError(
            "transcript_source type='drive_folder' には folder_id が必須です "
            "(場所を特定できない宣言は脆い Drive 総当たりに落ちるため)")
    if st == "external" and not tl:
        raise ValueError(
            "transcript_source type='external' には tool が必須です "
            "(道具を特定できない external は manual と同挙動なので manual を使ってください)")
    ts = {"type": st}
    if fid:
        ts["folder_id"] = fid
    if nm:
        ts["naming"] = nm
    if tl:
        ts["tool"] = tl
    acc["transcript_source"] = ts
    return acc


def get_account_transcript_source(data: dict, account_id: str) -> Optional[dict]:
    """Return an Account's transcript_source declaration, or None if unset (e-3552)."""
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    ts = acc.get("transcript_source")
    return dict(ts) if ts else None


def set_assignee(data: dict, target_id: str, assignee: str) -> dict:
    """Set the 担当ユーザー (assignee) on an Opportunity (``opp-``) or Account
    (``acc-``), dispatched by id prefix. Target-class-generic mutation that
    mirrors Milestone.assignee (ms-81); ms-109 folds all three into one engine.

    ``assignee`` is a free string (project member id / name); "" clears it.
    Returns the mutated target.
    """
    if target_id.startswith("opp-"):
        target = find_opportunity(data, target_id)
    elif target_id.startswith("acc-"):
        target = find_account(data, target_id)
    else:
        raise ValueError(
            f"target id must start with 'opp-' or 'acc-', got {target_id!r}")
    if target is None:
        raise ValueError(f"Target not found: {target_id}")
    target["assignee"] = assignee
    return target


# ---------------------------------------------------------------------------
# Opportunity (対象・有限) — 参照 association → Account
# ---------------------------------------------------------------------------

def opportunity_add(data: dict, title: str, *, account_id: str = "",
                    phase: str = "", goal_amount=None, probability=None,
                    deadline: str = "", who_has_the_ball: str = BALL_SELF,
                    transition_date: str = "", assignee: str = "",
                    description: str = "", created_at: str = "") -> str:
    """Append a new Opportunity (対象・有限) and return its id.

    ``account_id`` is a 参照 association (N:1 → Account) validated when given.
    ``phase`` defaults to the configured funnel entry. Entering a non-terminal
    phase opens the phase's advance gate (前進ゲート, e-3580 fold): the 遷移日
    and the phase-change history now live on the gate列, not on the Opportunity
    (SPEC AC5 — the deal no longer holds transition_date / phase_history直接).
    ``transition_date`` (= 遷移日, SPEC §2) is the planned judgement date; when
    given it is placed on the initial gate, else the engine prompts for it via
    ``needs_transition_date``.
    """
    if not title or not title.strip():
        raise ValueError("Opportunity title is required")
    if account_id:
        if find_account(data, account_id) is None:
            raise ValueError(f"Account not found: {account_id}")
    if who_has_the_ball not in VALID_BALL:
        raise ValueError(
            f"who_has_the_ball must be one of {sorted(VALID_BALL)}, got {who_has_the_ball!r}")
    initial_phase = phase or default_opportunity_phase(data)
    created_at = created_at or work_base.now_iso()
    # ms-143 sales-mirror (系統2 = target 作成): the id allocation + collection
    # append are now the ONE generic writer occupation.create_target — the SAME
    # path a dev milestone mints through — with sales enrichment (account_id /
    # phase / goal / ball / gates …) riding via **extra. This is the ms-143 core
    # proof that one abstraction serves both professions' create. Two parity-first
    # points (leader 握り): ``status`` stays phase-derived (NOT the base "todo"),
    # and ``stamp_created_by=False`` preserves the Opportunity's existing lack of
    # ``created_by`` (DEV_ONLY_SKELETON_KEYS) — a pure abstraction, no enrichment.
    # Lazy import avoids the occupation→sales_entities cycle.
    import occupation
    opp = occupation.create_target(
        data, "opportunity", label=title.strip(),
        status=_opportunity_status_for_phase(data, initial_phase),
        created_at=created_at, assignee=assignee, stamp_created_by=False,
        # ms-109 e-3625: canonical Target label dual-write during skew.
        title=title.strip(),
        account_id=account_id or None,
        phase=initial_phase,
        goal_amount=goal_amount,
        probability=probability,
        deadline=deadline,
        who_has_the_ball=who_has_the_ball,
        # ms-106 e-3526: free-text 背景 / 経緯 / メモ。
        description=(description or "").strip(),
        activities=[],
        gates=[],
    )
    opp_id = opp["id"]
    # 入場で空の前進ゲートを1つ置く (SPEC 実装順ヒント1) — non-terminal のみ。
    # 遷移日はゲートが持つ (fold): 与えられていれば初期ゲートに載せる。
    if not opportunity_phase_is_terminal(data, initial_phase):
        open_advance_gate(data, opp_id, phase=initial_phase,
                          transition_date=transition_date or "", at=created_at)
    # ms-115 e-3787: 商談を起票した = その顧客に接触が始まった。Account を
    # 未接触 → リード へ引き上げる (only-up; derive_account_phase の has_opp 信号)。
    if account_id:
        _auto_advance_account_phase(data, account_id, at=created_at)
    return opp_id


def phase_set(data: dict, target_id: str, new_phase: str, *,
              note: str = "", at: str = "") -> dict:
    """Declare a phase transition on an Opportunity (``opp-``) or Account
    (``acc-``), dispatched by id prefix (master = human, SPEC §5).

    Updates the target's exclusive ``phase`` (+ mirrored ``status`` for
    opportunities) and returns the transition record. For opportunities the
    phase-change *history* now lives on the advance-gate列 (e-3580 fold), so this
    only sets the exclusive phase — the gate lifecycle (advance/retry/terminal_
    transition) records the history + evidence. Accounts still keep their own
    append-only ``phase_history`` (they are outside the gate fold). Permissive:
    use ``opportunity_phase_warnings`` / ``account_phase_warnings`` to surface
    vocabulary / terminal-rule violations to the caller.
    """
    if not new_phase or not new_phase.strip():
        raise ValueError("new_phase is required")
    new_phase = new_phase.strip()
    if target_id.startswith("opp-"):
        opp = find_opportunity(data, target_id)
        if opp is None:
            raise ValueError(f"Opportunity not found: {target_id}")
        record = {"phase": new_phase, "at": at, "note": note}
        # ms-142 e-5169 — the funnel's non-terminal advance now rides the ONE
        # generic path (``target_state.set_target_state``), so an opportunity moves
        # its phase through the same primitive every other target-class does. A
        # terminal (決着) phase is the sales judge gate's completion claim, which
        # set_target_state refuses by contract, so it stays on the direct writer
        # here (permissive: master=人間, SPEC §5/§6). Both branches converge on the
        # SAME single writer ``advance_funnel_phase`` (terminal directly, non-
        # terminal via set_target_state), so the phase↔status projection has one
        # source — a future editor adding a write-side effect adds it THERE, not in
        # either branch, and it applies to both routes uniformly.
        if opportunity_phase_is_terminal(data, new_phase):
            advance_funnel_phase(data, opp, new_phase)
        else:
            import target_state
            target_state.set_target_state(data, target_id, new_phase, reason=note)
        # ms-106 fb3 / e-3350 — project the linked Account's lifecycle phase from
        # its opportunities' furthest progress. Advance UP only (a customer who
        # once closed stays 成約顧客); humans can still override via acc- phase_set.
        # Kept HERE (not in the seam) so the rollup keeps this caller's ``at``.
        if opp.get("account_id"):
            _auto_advance_account_phase(data, opp["account_id"], at=at)
        return record
    if target_id.startswith("acc-"):
        acc = find_account(data, target_id)
        if acc is None:
            raise ValueError(f"Account not found: {target_id}")
        record = {"phase": new_phase, "at": at, "note": note}
        acc.setdefault("phase_history", []).append(record)
        acc["phase"] = new_phase
        return record
    raise ValueError(
        f"target id must start with 'opp-' or 'acc-', got {target_id!r}")


# ---------------------------------------------------------------------------
# Account phase projection (顧客フェーズ ← 商談フェーズ, ms-106 fb3 / e-3350)
# ---------------------------------------------------------------------------
# The account lifecycle (リード → 未成約顧客 → 成約顧客) is NOT independent state:
# it is a *rollup* of the furthest progress across the account's opportunities.
# This is sales-adapter-specific (the vocabulary + mapping are 営業固有); the
# generic form ("継続 target の phase を子の有限 target 群の rollup で更新する")
# is what ms-109 hoists into the target-class engine.

def _opp_progress_signals(data: dict, opp: dict) -> tuple:
    """Return ``(won, lost, max_nonterminal_idx)`` an opportunity ever reached,
    scanning the phases its advance-gate列 covered + its current phase
    (e-3580 fold: the phase timeline now lives on the gates, not on a
    ``phase_history`` on the deal). ``max_nonterminal_idx`` is the furthest
    funnel position (0-based among non-terminal phases), -1 if none. 不成立
    (abandoned) counts as neither won nor lost — the seed only allows it from the
    商談準備 entry, so it is not "progress"."""
    _migrate_opp_gates(data, opp)
    phases = opportunity_phases(data)
    nonterm = [p.get("name") for p in phases if not p.get("terminal")]
    won = lost = False
    max_nt = -1
    names = [g.get("phase") for g in opp.get("gates", [])]
    if opp.get("phase"):
        names.append(opp.get("phase"))
    for name in names:
        pdef = _find_phase_def(phases, name)
        if pdef and pdef.get("terminal"):
            outcome = pdef.get("outcome")
            if outcome == "won":
                won = True
            elif outcome == "lost":
                lost = True
        elif name in nonterm:
            max_nt = max(max_nt, nonterm.index(name))
    return won, lost, max_nt


def _account_phase_idx(data: dict, phase_name: str) -> int:
    aps = effective_phases(data)["account"]  # e-4638: fallback を effective_phases に一本化
    for i, p in enumerate(aps):
        if p.get("name") == phase_name:
            return i
    return -1


def derive_account_phase(data: dict, account_id: str) -> Optional[str]:
    """Project an Account's lifecycle phase from its opportunities' furthest
    progress. Mapping counts *from the end* of ``account_phases`` (成約 = 末尾)
    so it stays config-generic AND survives adding a front phase (ms-115 e-3787
    「未接触」). The same formula lands on the right stage for both the 3-stage
    legacy funnel (リード/未成約顧客/成約顧客) and the 4-stage one
    (未接触/リード/未成約顧客/成約顧客):

      末尾   (成約顧客):   成約 (won) 商談が1つ以上
      末尾-1 (未成約顧客): 提案準備以降へ進んだ or 失注した商談が1つ以上、成約なし
      末尾-2 (リード):     商談はあるが未進行 (= 接触済みだが deal はまだ)
      先頭   (未接触):     商談が1つも無い (生リスト)。3段構成では リード に一致する

    Contact-without-a-deal (Communication はあるが Opportunity 未起票) を 未接触→
    リード に上げるのは Opportunity 信号の外なので、ここでは扱わない (人手の
    phase-set / 将来の拡張)。Returns the phase NAME, or ``None`` when unconfigured.
    """
    aps = effective_phases(data)["account"]  # e-4638: fallback を effective_phases に一本化
    if not aps:
        return None
    n = len(aps)
    won = lost = progressed = has_opp = False
    for o in live_opportunities(data):  # e-3586: 取消済は顧客フェーズ導出に数えない
        if o.get("account_id") != account_id:
            continue
        has_opp = True
        w, l, max_nt = _opp_progress_signals(data, o)
        won = won or w
        lost = lost or l
        if max_nt >= 1:
            progressed = True
    if won:
        idx = n - 1
    elif lost or progressed:
        idx = n - 2
    elif has_opp:
        idx = n - 3
    else:
        idx = 0
    idx = max(0, min(idx, n - 1))
    return aps[idx].get("name")


def _auto_advance_account_phase(data: dict, account_id: str, *, at: str = "") -> Optional[dict]:
    """Advance the account's stored phase to its derived value, but only UP the
    funnel (never auto-downgrade). Records the transition in ``phase_history``
    with an auto-note so the projection stays auditable. Returns the appended
    record, or ``None`` if no advance happened."""
    acc = find_account(data, account_id)
    if acc is None:
        return None
    derived = derive_account_phase(data, account_id)
    if not derived:
        return None
    cur_idx = _account_phase_idx(data, acc.get("phase"))
    new_idx = _account_phase_idx(data, derived)
    if new_idx > cur_idx:
        record = {"phase": derived, "at": at,
                  "note": "商談の進行により自動昇格 (derive_account_phase)"}
        acc.setdefault("phase_history", []).append(record)
        acc["phase"] = derived
        return record
    return None


# ---------------------------------------------------------------------------
# Pipeline value & member targets (見込み売上 / メンバー別 目標売上, ms-106 fb4)
# ---------------------------------------------------------------------------
# 見込み売上 (weighted pipeline) = Σ over OPEN (non-terminal) opportunities of
# goal_amount × phase.probability. Member targets (目標売上) live in a *sparse*
# project-level map ``sales_targets`` {member: amount}: a member added later
# simply has no entry (= 目標未設定) until one is set — additive, no migration.
# This is L3 (sales-adapter) config; ms-109 generalizes it to "a target carries
# a quota / KPI".

def set_opportunity_amount(data: dict, opp_id: str, amount) -> dict:
    """Set an opportunity's goal_amount (商談金額). ``amount`` is a number (円)
    or None to clear. Returns the mutated opportunity.

    ms-143: patches through the profession-generic occupation.update_entry
    (locate + plain field patch; ``goal_amount=None`` clears — update_entry writes
    None rather than skipping it), so the 更新 path no longer names
    data['opportunities'] itself. Lazy import."""
    import occupation
    return occupation.update_entry(data, opp_id, goal_amount=amount)


def set_phase_probability(data: dict, phase_name: str, probability) -> dict:
    """Set a per-company win probability (成約率, 0-100) on an opportunity phase
    definition. Config-level edit (per-company funnel tuning). Returns the def."""
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    if pdef is None:
        raise ValueError(f"Opportunity phase not found: {phase_name}")
    pdef["probability"] = probability
    return pdef


# ---------------------------------------------------------------------------
# Phase funnel editing (ms-116) — edit a *running* project's saved phase funnel.
# ---------------------------------------------------------------------------
# The funnel (段の並び) is per-company config stored in project.json
# (``account_phases`` / ``opportunity_phases``). ms-115 added a "未接触" stage,
# but only to the shipped SEED — a project that started earlier keeps its own
# saved funnel and never receives it. These primitives let a running project's
# saved funnel be edited (add / insert / rename / reorder / delete) so a funnel
# can evolve without rebuilding the project (ms-116 SPEC 方針1: 保存書き換えに
# 一本化, read-time expand は不採用).
#
# Order matters: the funnel entry (既定フェーズ) is the first stage —
# ``default_account_phase`` returns ``ps[0]`` and ``default_opportunity_phase``
# the first non-terminal. Those getters read the saved list at call time, so a
# reorder/insert re-derives the entry with no extra step (SPEC 方針3 / AC4).
# Deletion is blocked while a stage still holds live targets (SPEC 方針4 / AC3):
# data must never go missing behind a vanished stage.

_FUNNEL_KEYS = {"account": "account_phases", "opportunity": "opportunity_phases",
                "prospect": "prospect_phases"}


def _funnel_key(kind: str) -> str:
    try:
        return _FUNNEL_KEYS[kind]
    except KeyError:
        raise ValueError(
            f"unknown funnel kind: {kind!r} "
            f"(expected 'account', 'opportunity' or 'prospect')")


def payload_funnels(data: dict) -> dict:
    """The resolved sales funnels to merge into a Web UI project payload
    (ms-108 e-5194).

    Returns ``{"opportunity_phases": [...], "account_phases": [...],
    "prospect_phases": [...]}`` for a sales project — configured funnels as-is,
    else the shipped defaults — and an EMPTY dict for a non-sales project (no
    funnel keys injected). Resolution + the sales gate both come from
    :func:`effective_phases` (the ONE authoritative resolver), and the key
    mapping from ``_FUNNEL_KEYS`` — so no caller re-defines "is this sales" or
    "kind → payload key". This is the single place that owns "which funnels a
    payload carries" so the CLI and the Web UI enrich layer resolve identically
    (a funnel-less sales project — not created via ``new_sales_project`` — still
    renders its board instead of a blank one). Non-sales resolves to all-empty
    funnels here, so the dict comprehension drops them and dev payloads are
    untouched."""
    eff = effective_phases(data)
    return {key: eff[kind] for kind, key in _FUNNEL_KEYS.items() if eff[kind]}


def _funnel_targets(data: dict, kind: str) -> list:
    """The target rows whose ``phase`` field points into this funnel.

    account / opportunity phases are live fields on project.json entities, so
    their occupants come from those collections. The *prospect* funnel is
    different (ms-132 e-4502): it is the default vocabulary a new attack-list is
    seeded from, and an attack-list is a self-describing ms-131 table-doc (it
    bakes its own enum column) rather than a project.json entity. So no
    project.json row's live phase reads against the prospect funnel — editing or
    deleting a prospect stage never orphans an existing list (each keeps its own
    columns). Hence an empty occupant set here."""
    if kind == "account":
        return data.get("accounts", [])
    if kind == "opportunity":
        return data.get("opportunities", [])
    return []  # prospect (and any future config-only funnel)


def phase_occupants(data: dict, kind: str, name: str) -> list:
    """IDs of *live* (non-cancelled) targets currently sitting in phase ``name``.
    Used to block deletion of a non-empty stage (SPEC AC3). Cancelled rows are
    tombstones (誤起票の訂正) — they don't count as data you'd lose."""
    return [t.get("id", "") for t in _funnel_targets(data, kind)
            if t.get("phase") == name and t.get("status") != CANCELLED_STATUS]


def insert_phase(data: dict, kind: str, name: str,
                 index: Optional[int] = None, **attrs) -> dict:
    """Insert a new stage ``name`` into the funnel at ``index`` (0 = 先頭 entry).
    ``index`` None or past the end appends. Extra keyword attrs (probability /
    terminal / goal … for opportunity stages) are stored on the stage def.
    Raises ValueError on an empty or duplicate name, or an out-of-range index."""
    key = _funnel_key(kind)
    nm = (name or "").strip()
    if not nm:
        raise ValueError("phase name is required")
    phases = data.setdefault(key, [])
    if _find_phase_def(phases, nm) is not None:
        raise ValueError(f"phase already exists: {nm}")
    pdef = {"name": nm}
    pdef.update({k: v for k, v in attrs.items() if v is not None})
    if index is None or index >= len(phases):
        phases.append(pdef)
    elif index < 0:
        raise ValueError(f"index out of range: {index}")
    else:
        phases.insert(index, pdef)
    return pdef


def add_phase(data: dict, kind: str, name: str, **attrs) -> dict:
    """Append a new stage to the end of the funnel (= ``insert_phase`` at end)."""
    return insert_phase(data, kind, name, index=None, **attrs)


def rename_phase(data: dict, kind: str, old: str, new: str) -> dict:
    """Rename stage ``old`` to ``new`` and follow every reference so no target
    keeps a dead phase pointer (SPEC 方針3 / AC5). Tombstoned (cancelled) rows
    are updated too, so stored data stays internally consistent. Returns the
    stage def. Raises ValueError if ``old`` is missing or ``new`` collides."""
    key = _funnel_key(kind)
    nm = (new or "").strip()
    if not nm:
        raise ValueError("new phase name is required")
    phases = data.get(key, [])
    pdef = _find_phase_def(phases, old)
    if pdef is None:
        raise ValueError(f"phase not found: {old}")
    if nm == old:
        return pdef
    if _find_phase_def(phases, nm) is not None:
        raise ValueError(f"phase already exists: {nm}")
    pdef["name"] = nm
    for t in _funnel_targets(data, kind):
        if t.get("phase") == old:
            t["phase"] = nm
    return pdef


def move_phase(data: dict, kind: str, name: str, to_index: int) -> list:
    """Move stage ``name`` to position ``to_index`` in the funnel and return the
    reordered list. ``to_index`` is 0-based over the current stages (0 = 先頭 =
    become the funnel entry). Raises ValueError if the stage is missing or the
    index is out of range."""
    key = _funnel_key(kind)
    phases = data.get(key, [])
    idx = next((i for i, p in enumerate(phases) if p.get("name") == name), None)
    if idx is None:
        raise ValueError(f"phase not found: {name}")
    if to_index < 0 or to_index >= len(phases):
        raise ValueError(f"index out of range: {to_index}")
    phases.insert(to_index, phases.pop(idx))
    return phases


def remove_phase(data: dict, kind: str, name: str) -> dict:
    """Delete stage ``name`` from the funnel. Blocked while live targets still
    sit in it (SPEC 方針4 / AC3): the error names how many and which, so the
    caller can move them first. Returns the removed stage def."""
    key = _funnel_key(kind)
    phases = data.get(key, [])
    pdef = _find_phase_def(phases, name)
    if pdef is None:
        raise ValueError(f"phase not found: {name}")
    occ = phase_occupants(data, kind, name)
    if occ:
        shown = ", ".join(occ[:8]) + ("…" if len(occ) > 8 else "")
        raise ValueError(
            f"cannot delete non-empty phase {name!r}: "
            f"{len(occ)} target(s) still there ({shown}). Move them first.")
    phases.remove(pdef)
    return pdef


def sales_targets(data: dict) -> dict:
    return data.get("sales_targets", {})


def set_sales_target(data: dict, member: str, amount) -> dict:
    """Set a member's 目標売上 (sales quota) in the sparse ``sales_targets`` map.
    ``amount`` None removes the entry (= 目標未設定 に戻す). Returns the map."""
    if not member or not str(member).strip():
        raise ValueError("member is required")
    key = str(member).strip()
    targets = data.setdefault("sales_targets", {})
    if amount is None:
        targets.pop(key, None)
    else:
        targets[key] = amount
    return targets


def get_sales_target(data: dict, member: str):
    return sales_targets(data).get(member)


def _phase_probability(data: dict, phase_name: str) -> float:
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    p = pdef.get("probability") if pdef else None
    return float(p) if isinstance(p, (int, float)) else 0.0


def weighted_pipeline(data: dict, *, assignee: Optional[str] = None) -> float:
    """見込み売上 = Σ (goal_amount × phase.probability / 100) over OPEN
    (non-terminal) opportunities. ``assignee`` scopes it to one member's
    pipeline. Deals with no amount or no phase probability contribute 0."""
    phases = opportunity_phases(data)
    term = {p.get("name") for p in phases if p.get("terminal")}
    total = 0.0
    for o in live_opportunities(data):  # e-3586: 取消済は見込みに数えない
        if o.get("phase") in term:
            continue
        if assignee is not None and (o.get("assignee") or "") != assignee:
            continue
        amt = o.get("goal_amount")
        if isinstance(amt, (int, float)):
            total += amt * _phase_probability(data, o.get("phase")) / 100.0
    return total


# ---------------------------------------------------------------------------
# Transition date (遷移日) — ms-107 e-3371, SPEC §2. Target-class primitive.
# ---------------------------------------------------------------------------
# 遷移日 = 「そのフェーズのゴール達成を判定する予定日」. It sits alongside ``ball``
# on a target's runtime state and is the pivot of the engine cycle
# (置く → 準備 → 判定 → advance/retry/terminal). Written like ``phase_history``:
# the current value is mirrored on the target, and every change is appended to
# an append-only ``transition_date_history`` (証跡, data-immutability-principle).
# Generic over targets that close (opp- today); accounts (対象・継続) never do.

def get_transition_date(data: dict, target_id: str) -> str:
    """Current 遷移日 of a target (opp-…), or '' when unset. The date now lives
    on the opportunity's open advance gate (e-3580 fold): this returns that
    gate's transition_date, or '' when no gate is open (= terminal / none)."""
    if target_id.startswith("opp-"):
        if find_opportunity(data, target_id) is None:
            raise ValueError(f"Opportunity not found: {target_id}")
        gate = current_gate(data, target_id)
        return (gate.get("transition_date", "") or "") if gate else ""
    raise ValueError(
        f"transition_date targets an opportunity (opp-…), got {target_id!r}")


def set_transition_date(data: dict, target_id: str, transition_date: str, *,
                        note: str = "", at: str = "") -> dict:
    """Set (or clear) the 遷移日 on the opportunity's open advance gate and log it
    append-only on the gate's history (e-3580 fold — the date is a gate field).

    Requires an open gate (a judgement date is meaningless without an open phase
    to judge); raises when none is open. Passing an empty date clears it (still
    logged, so the clearing is auditable). Returns the appended history record.
    """
    if target_id.startswith("opp-"):
        opp = find_opportunity(data, target_id)
        if opp is None:
            raise ValueError(f"Opportunity not found: {target_id}")
        gate = current_gate(data, target_id)
        if gate is None:
            raise ValueError(
                f"{target_id} has no open advance gate to place a 遷移日 on "
                "(terminal / not yet opened)")
        value = (transition_date or "").strip()
        record = {"at": at, "action": "transition-date",
                  "transition_date": value, "note": note}
        gate.setdefault("history", []).append(record)
        gate["transition_date"] = value
        return record
    raise ValueError(
        f"transition_date targets an opportunity (opp-…), got {target_id!r}")


def _open_advance_gate(data: dict, target_id: str) -> Optional[dict]:
    """The opportunity's open前進ゲート, or None — the shared guard chain behind the
    derived「open gate かつ …が空」twins (``needs_transition_date`` /
    ``gate_needs_anchor``). Non-opportunity id, unknown opportunity, terminal phase
    (defensive vs a stale gate), or no open gate all collapse to None so a caller
    can write its predicate in one line (ms-144 e-5178 maint-b — single guard, two
    derived facts)."""
    if not target_id.startswith("opp-"):
        return None
    opp = find_opportunity(data, target_id)
    if opp is None:
        return None
    if opportunity_phase_is_terminal(data, opp.get("phase", "")):
        return None  # 決着済み — no live gate (defensive vs stale gate)
    return current_gate(data, target_id)


def needs_transition_date(data: dict, target_id: str) -> bool:
    """True when the opportunity has an open advance gate with no 遷移日 placed
    (SPEC §2: placing the 遷移日 is the top-priority step on phase entry — without
    a judgement date, preparation and follow-up can't be driven).

    Terminal / gate-less opportunities never need one (no open gate), so False.
    """
    gate = _open_advance_gate(data, target_id)
    return gate is not None and not (gate.get("transition_date") or "").strip()


def gate_needs_anchor(data: dict, target_id: str) -> bool:
    """True when the opportunity has an open前進ゲート with no発火源 (anchor) bound
    yet (ms-144 e-5178). This is the derived fact behind the「空 (発火源 未紐づけ)」
    display — surfaced as a flag so the AI can *see* an unanchored gate instead of
    re-deriving it from the raw ``gates[]`` (SPEC §問題: the判定日-only path that
    left cairn opp-3/opp-4 stuck). Anchor it with ``beacon opportunity anchor``.

    Terminal / gate-less opportunities never need one (no open gate), so False —
    the same twin-shape guard as ``needs_transition_date`` (shared
    ``_open_advance_gate``)."""
    gate = _open_advance_gate(data, target_id)
    return gate is not None and not (gate.get("anchor") or "").strip()


# ---------------------------------------------------------------------------
# Transition judgement engine — ms-107 e-3372, SPEC §3. Target-class generic.
# ---------------------------------------------------------------------------
# 遷移日に達したら、そのフェーズのゴールを人間が判定する: advance (次へ) /
# retry (やり直し = 新しい遷移日) / terminal (決着). AI は候補を出すだけで状態は
# 変えない (master=人間, SPEC §6) — この層は「判定を適用する」プリミティブで、
# *いつ* 判定するか / *どの* 分岐かは呼び出し側 (Skill + 人間) が決める。
#
# transition_status は派生 (derived) — transition_date と「今日」から毎回導出し、
# 永続フラグを持たない。判定されないまま遷移日を過ぎた商談は自動で ``overdue``
# に分類され、非terminal な間はそこに居座る (= 人間が判定するまで催促し続ける)。
# 永続フラグにすると、日付が過ぎた瞬間にフラグを立て直す常駐処理が要り必ず
# stale になるが、派生なら常に正しい。``status`` (open/won/lost…) とは直交する
# 別次元 (overdue な商談も status は open のまま)。

# --- target-class temporal core → L2 engine (ms-139 e-4948) ----------------
# 「対象が期日を持つ → 時間的ステータスが派生する」は sales 固有でなく target-class
# 共通の関心事 (milestone.target_date / opportunity.transition_date / activity.deadline
# / task.deadline は同型)。ms-107 e-3271 の予告どおり、pure な汎用コアと temporal
# status 語彙は L2 engine (lib/deadline.py) へ抽出済み。ここは後方互換の re-export で、
# 既存の sales_entities.TRANSITION_* / temporal_status / scan_overdue 参照
# (commands.py 等) を保つ。営業固有の wrapper transition_status は下に残る (第一
# consumer)。
TRANSITION_UNSET = deadline.TRANSITION_UNSET
TRANSITION_SCHEDULED = deadline.TRANSITION_SCHEDULED
TRANSITION_DUE = deadline.TRANSITION_DUE
TRANSITION_OVERDUE = deadline.TRANSITION_OVERDUE
TRANSITION_SETTLED = deadline.TRANSITION_SETTLED
temporal_status = deadline.temporal_status
scan_overdue = deadline.scan_overdue


def transition_status(data: dict, target_id: str, today: str) -> str:
    """Derived judgement state of an opportunity relative to ``today`` — the
    sales wrapper over ``temporal_status`` (due date = the open gate's 遷移日,
    settled = terminal phase / no open gate). Returns a TRANSITION_* constant."""
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    return temporal_status(
        get_transition_date(data, target_id), today,
        settled=opportunity_phase_is_terminal(data, opp.get("phase", "")))


def next_opportunity_phase(data: dict, phase_name: str) -> Optional[str]:
    """The next *non-terminal* phase after ``phase_name`` in the configured
    funnel order, or None when ``phase_name`` is the last non-terminal stage,
    is unknown, or is itself terminal. Advance moves along this order; the
    success outcome after the final stage is a terminal (決着), which goes
    through the terminal path, not advance."""
    phases = opportunity_phases(data)
    names = _phase_names(phases)
    if phase_name not in names:
        return None
    idx = names.index(phase_name)
    for p in phases[idx + 1:]:
        if not p.get("terminal"):
            return p.get("name")
    return None


def advance_transition(data: dict, target_id: str, *,
                       next_transition_date: str = "", note: str = "",
                       at: str = "", actor: str = "") -> dict:
    """Advance a target to the next non-terminal phase (goal met, SPEC §3).

    Settles the current open advance gate with ``outcome=advance`` (its 判断証跡)
    and opens a fresh gate for the new phase carrying ``next_transition_date``
    (blank → ``needs_transition_date`` prompts for one, SPEC §2). The done gate
    joins the phase-change history列 (e-3580 fold). Returns ``{"phase": <new>,
    "transition_date": …}``. Raises ValueError when there is no next non-terminal
    phase (use the terminal path — 成約 等 — instead of advance).
    """
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    cur = opp.get("phase", "")
    nxt = next_opportunity_phase(data, cur)
    if nxt is None:
        raise ValueError(
            f"'{cur}' は最終ステージです (次の非terminalフェーズがありません)。"
            "advance ではなく terminal (決着) を宣言してください")
    gate = current_gate(data, target_id)
    if gate is not None:
        settle_gate(data, gate["id"], outcome=GATE_ADVANCE, reason=note, at=at, actor=actor)
    # e-3553: fold the phase we are leaving BEFORE the exclusive phase moves —
    # evidence-linked activities auto-close, the rest surface for a human call.
    fold = fold_phase_activities(data, target_id, cur, at=at, actor=actor)
    phase_set(data, target_id, nxt, note=note, at=at)
    open_advance_gate(data, target_id, phase=nxt,
                      transition_date=next_transition_date, at=at)
    # e-3270: フェーズ入場でそのフェーズの固定アンカー活動を起こす (提案準備→提案
    # 作成 等)。goal からの文脈生成は Skill 層が上乗せする (LLM 必要)。共通アンカー
    # 「遷移日を置く」は needs_transition_date 促しで担い、ここでは重複させない。
    created = instantiate_phase_activities(data, target_id, at=at)
    return {"phase": nxt, "transition_date": get_transition_date(data, target_id),
            "activities": created, "fold": fold}


def retry_transition(data: dict, target_id: str, new_transition_date: str, *,
                     note: str = "", at: str = "", actor: str = "") -> dict:
    """Stay in the same phase but re-place the 遷移日 (未達だが粘る, SPEC §3 +
    2026-07-17 改訂 = Option 3). Settles the current gate with ``outcome=retry``
    (its 判断証跡 — なぜやり直すか) and opens a *new* gate for the **same** phase
    carrying ``new_transition_date``, so every judgement — advance / retry /
    terminal — is a first-class done-gate record. ``new_transition_date`` is
    required (retry means 置き直す; empty would be a clear). Returns
    ``{"phase": <same>, "transition_date": …}``.
    """
    if not (new_transition_date or "").strip():
        raise ValueError("retry requires a new transition_date (置き直す日付)")
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    cur = opp.get("phase", "")
    gate = current_gate(data, target_id)
    if gate is not None:
        settle_gate(data, gate["id"], outcome=GATE_RETRY, reason=note, at=at, actor=actor)
    open_advance_gate(data, target_id, phase=cur,
                      transition_date=new_transition_date, at=at)
    return {"phase": cur, "transition_date": get_transition_date(data, target_id)}


def allowed_terminals_for(data: dict, target_id: str) -> list:
    """The terminal (決着) phases declarable from the target's current phase
    (its ``allowed_terminals`` rule), or [] when unknown/unset. This is the
    choice list a human picks from on a failed judgement (SPEC §3)."""
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    cur_def = _find_phase_def(opportunity_phases(data), opp.get("phase", ""))
    return list((cur_def or {}).get("allowed_terminals") or [])


def terminal_transition(data: dict, target_id: str, terminal_phase: str, *,
                        note: str = "", at: str = "", actor: str = "") -> dict:
    """Declare a terminal (決着) phase and consume the transition_date.

    Permissive (master=人間, SPEC §6): declaring a terminal outside the stage's
    ``allowed_terminals`` is surfaced by ``opportunity_phase_warnings`` (caller's
    job), never blocked here. This helper only enforces that the target phase is
    actually terminal (a typo'd stage name would silently leave the deal open).

    Settles the current open gate with ``outcome=terminal`` (its 判断証跡) and
    opens no new gate — the deal is 決着済み, so no phase is left in progress
    (e-3580 fold: the 遷移日 was the gate's, and it retires with the gate).
    Returns the phase_set record.
    """
    if not opportunity_phase_is_terminal(data, terminal_phase):
        raise ValueError(
            f"'{terminal_phase}' は terminal (決着) フェーズではありません "
            f"(既知の決着: {[p['name'] for p in opportunity_phases(data) if p.get('terminal')]})")
    opp = find_opportunity(data, target_id)
    cur = opp.get("phase", "") if opp else ""
    gate = current_gate(data, target_id)
    if gate is not None:
        settle_gate(data, gate["id"], outcome=GATE_TERMINAL, reason=note, at=at, actor=actor)
    # e-3553: fold the phase being decided out of before the terminal phase is
    # set. On a 決着 there is no "carry" (no next phase), but evidence-linked
    # activities still auto-close and the rest surface for a done/cancel call.
    fold = fold_phase_activities(data, target_id, cur, at=at, actor=actor)
    rec = phase_set(data, target_id, terminal_phase, note=note or "terminal", at=at)
    rec["fold"] = fold
    return rec


def jump_transition(data: dict, target_id: str, new_phase: str, *,
                    note: str = "", at: str = "", actor: str = "") -> dict:
    """Corrective manual phase jump for an Opportunity that still honors the
    advance-gate model (ms-106 e-3688 / fable review C-1).

    ``beacon opportunity phase <opp> <phase>`` used to call the low-level
    ``phase_set`` directly, which set the exclusive phase but **settled no gate
    and discarded the ``note``** — so the phase-change history and the "why"
    were lost (SPEC AC5 violated), and the previous phase's open gate stayed
    open and could mis-anchor the next phase's activities. This routes a manual
    declaration through the SAME gate lifecycle as advance / terminal: settle
    the current open gate with its outcome + note (its 判断証跡), set the phase,
    and open a fresh gate unless the target phase is terminal (決着 = no phase
    left in progress). Permissive on vocabulary (master = 人間, SPEC §5/§6) —
    the CLI surfaces warnings, this never blocks.

    ``phase_set`` stays the internal exclusive-phase primitive that
    advance/retry/terminal_transition call *after* settling their gate; only
    this corrective path is exposed to the raw CLI now, so no bypass remains.
    """
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    if not new_phase or not new_phase.strip():
        raise ValueError("new_phase is required")
    new_phase = new_phase.strip()
    terminal = opportunity_phase_is_terminal(data, new_phase)
    gate = current_gate(data, target_id)
    if gate is not None:
        settle_gate(data, gate["id"],
                    outcome=(GATE_TERMINAL if terminal else GATE_ADVANCE),
                    reason=note or "manual phase jump", at=at, actor=actor)
    rec = phase_set(data, target_id, new_phase, note=note, at=at)
    if not terminal:
        open_advance_gate(data, target_id, phase=new_phase, at=at)
    # No auto-fold here: jump is the *corrective* manual path (may go backward),
    # so leaving-phase work is not necessarily "done" — fold is only wired to the
    # forward judge outcomes (advance / terminal), see e-3553.
    return rec


def fold_phase_activities(data: dict, opportunity_id: str, leaving_phase: str, *,
                          at: str = "", actor: str = "") -> dict:
    """Fold (後始末) the activities of the phase an opportunity is leaving (e-3553).

    When a deal advances / jumps / decides-terminal, the phase it *leaves* often
    has activities still sitting ``todo`` — the初回面談 that already happened, the
    direction-fixing step whose outcome justified the advance, extras that were
    seeded but never needed. Left alone they pile up and the activity log stops
    being readable, which breaks the premise that the AI reads the trail to
    propose the next move (opp-6 dogfood #8).

    This does the ONE deterministic step (記帳=自動, SPEC の (a)): an activity that
    already carries evidence — a Communication linked to it (``linked_id``) — is
    closed as done (evidence-based close, no human needed, the sales twin of a
    commit closing a task). Activities in the leaving phase carrying NO evidence
    are returned as ``needs_decision`` for a human to resolve — done / cancel /
    carry to the next phase ((b)/(c) は判断=人). The fold never cancels or carries
    on its own; those are human calls surfaced to the Skill layer.

    ``leaving_phase`` is passed explicitly (the caller runs this *before* moving
    the exclusive phase, so it knows which phase is being left). An activity whose
    ``created_in_phase`` is blank is treated as belonging to the leaving phase
    (legacy / hand-added records stamped before e-3555). Returns
    ``{"auto_done": [ids], "needs_decision": [{"id","description","reason"}]}``.
    """
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    auto_done: list = []
    needs_decision: list = []
    for act in opp.get("activities", []):
        if not work_model.is_open(act):
            continue
        born = act.get("created_in_phase", "") or leaving_phase
        if born != leaving_phase:
            continue
        act_id = act.get("id", "")
        has_evidence = bool(communications_of(opp, linked_id=act_id,
                                              include_cancelled=False))
        if has_evidence:
            work_model.mark_done(
                act, at=at, actor=actor or work_base.current_actor(),
                reason="evidence-based close on phase fold (e-3553)")
            auto_done.append(act_id)
        else:
            needs_decision.append({
                "id": act_id,
                "description": act.get("description", ""),
                "reason": ("証跡が無い活動です。ゴール達成済みなら done、"
                           "余計なら cancel、次フェーズに要るなら carry を人が選ぶ"),
            })
    return {"auto_done": auto_done, "needs_decision": needs_decision}


def suggest_transition_date(data: dict, phase_name: str,
                            base_date: str) -> Optional[str]:
    """Suggest a transition_date for a phase = ``base_date`` + the phase's
    ``default_lead`` days, or None when the phase has no default_lead. Pure
    date arithmetic; the human still confirms (this is a suggestion, SPEC §6).
    ``base_date`` is ``YYYY-MM-DD``."""
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    lead = phase_default_lead(pdef)
    if lead is None:
        return None
    import datetime
    try:
        base = datetime.date.fromisoformat(base_date)
    except (TypeError, ValueError):
        return None
    return (base + datetime.timedelta(days=lead)).isoformat()


def opportunities_awaiting_judgement(data: dict, today: str) -> list:
    """締切精査 for opportunities: due/overdue deals as of ``today``, oldest
    first. Built on the generic ``scan_overdue`` (transition_date + terminal =
    settled). Each row carries ``who_has_the_ball`` so the sales view can split
    the overdue set into the two actions it implies (SPEC §3 + e-3271):

    * ball=self  → 自分が判定/対応する (判定待ち)
    * ball=counterpart → 相手待ちが期限超過 → 催促する (相手ボール timeout)

    Returns dicts with id / title / phase / transition_date / transition_status /
    who_has_the_ball. This is the AI's catch surface for the ``overdue`` state.
    The 遷移日 now comes from each deal's open advance gate (e-3580 fold)."""
    def due_of(o):
        return get_transition_date(data, o["id"])

    def settled_of(o):
        return opportunity_phase_is_terminal(data, o.get("phase", ""))

    # e-3586: 取消済商談は判定待ちに出さない。
    pairs = scan_overdue(live_opportunities(data), due_of, settled_of, today)
    return [{
        "id": o["id"],
        "title": work_model.target_label(o),
        "phase": o.get("phase", ""),
        "transition_date": get_transition_date(data, o["id"]),
        "transition_status": st,
        # C-3 (e-3692): the live ball is derived from Communications; the static
        # who_has_the_ball field is only the initial declaration and goes stale
        # after an email exchange. Read derived-first so the 判定/催促 split is
        # not a lie; fall back to the static field only when there is no comm.
        "who_has_the_ball": derive_ball(o) or o.get("who_has_the_ball", ""),
    } for o, st in pairs]


def overdue_activities(data: dict, today: str) -> list:
    """締切精査 for activities (準備活動: 会食・打診など): 商談横断で deadline が
    due/overdue な活動を古い順に返す — ms-139 e-4951 (SPEC P3)。

    これまで締切精査は商談の遷移日 (transition_date) しか見ておらず、活動の期日
    (activity.deadline) は WebUI しか surface していなかった。ここは L2 締切エンジン
    (``deadline.overdue_work_items``) に活動を流すだけ — done / cancelled は
    ``is_settled`` で terminal 扱いになり自動で外れる (催促は完了・取消で止まる)。

    取消商談 (``live_opportunities`` で除外) の活動は出さない。各 row に商談文脈
    (opp_id / opp_title) と who_has_the_ball (次に動く責任) を載せる。"""
    out = []
    for opp in live_opportunities(data):
        acts = opp.get("activities", []) or []
        for act, st in deadline.overdue_work_items(acts, today):
            out.append({
                "opp_id": opp.get("id", ""),
                "opp_title": work_model.target_label(opp),
                "act_id": act.get("id", ""),
                "description": act.get("description", ""),
                "deadline": act.get("deadline", ""),
                "activity_status": st,  # deadline.TRANSITION_DUE / _OVERDUE
                "who_has_the_ball": act.get("who_has_the_ball", ""),
            })
    out.sort(key=lambda r: r["deadline"] or "")
    return out


# ---------------------------------------------------------------------------
# Advance gate (前進ゲート) — ms-106 SPEC XG1yBugcb3GRnamRljhx, task e-3579
# ---------------------------------------------------------------------------
# The "関門" through which an Opportunity leaves one phase for the next. Today
# the判定 (advance / retry / terminal) is welded onto the Opportunity itself
# (phase / transition_date live on the deal) and onto meetings (a meeting
# ending drives a phase change). fable's independent review found "重複が構造的
# に不可能" was only a Skill-手順書 promise, not a data constraint — the same
# 思想ズレ we hit in Trek. This entity lifts the判定 into its own work item so
# the structure — not a手順書 — guarantees the invariant.
#
# Structural guarantee (e-3579):
#   * ≤ 1 *open* gate per Opportunity  (商談は同時に1フェーズだけ進行中)
#   * done gates accumulate            (= 全判定履歴。フェーズ変更履歴 = outcome
#                                        ∈ {advance,terminal} でフィルタした派生ビュー)
# The fold (e-3580): the 遷移日 and the phase-change history moved OFF the
# Opportunity ONTO this列 — the deal no longer holds transition_date /
# phase_history直接 (SPEC AC5). advance/retry/terminal_transition drive the gate
# lifecycle (settle current → open next); ``_migrate_opp_gates`` converts legacy
# deals on first touch (AC8). Firing判定 off an anchored work-item's completion +
# dropping transition_signal = e-3581; replacing the meeting-seed on phase entry
# with a gate = e-3583 — those ride on these primitives and are still ahead.
#
# The gate is the sales-instance shape of the class-layer WorkItem-subtype
# "ステージ前進のゲート" (SPEC §6, e-3560 evidence-close の一適用): occupation
# vocabulary (前進ゲート / advance / retry / terminal, anchor kinds mtg-/act-/
# nrt-) stays here, while the invariant floor (id 採番 / append-only 監査 /
# cancel) routes through ``work_base`` so ms-109 can hoist it cleanly.

GATE_OPEN = "open"          # 進行中 — その商談で唯一 (≤1 の制約対象)
GATE_DONE = "done"          # 判定済み — outcome + 判断証跡を刻んで積む (履歴)
GATE_CANCELLED = work_base.CANCELLED_STATUS  # 取消 (商談中止 等で発火せず畳む)

# Single spelling for「発火源が結ばれていない前進ゲート」across the list display,
# the transition-date warning, and the cockpit (ms-144 e-5178 maint-c — one label,
# no drift). Used to build 「空 (発火源 未紐づけ)」 and the anchor nudge.
GATE_UNANCHORED_LABEL = "発火源 未紐づけ"

# outcome vocabulary — mirrors the three judgement branches (advance_transition /
# retry_transition / terminal_transition). Stamped on a gate when it settles.
GATE_ADVANCE = "advance"    # 前進 → 次フェーズへ
GATE_RETRY = "retry"        # やり直し → 同フェーズで置き直す
GATE_TERMINAL = "terminal"  # 決着 → 成約/失注/不成立
VALID_GATE_OUTCOMES = {GATE_ADVANCE, GATE_RETRY, GATE_TERMINAL}


def next_gate_id(data: dict) -> str:
    """Allocate the next global ``adv-N`` id (unique across all opportunities,
    like ``mtg-``). Gathers existing gate ids from every opportunity and defers
    numbering to the shared ``work_base.next_suffixed_id`` (ms-109 e-3558)."""
    ids = []
    for opp in data.get("opportunities", []):
        ids.extend(g.get("id", "") for g in opp.get("gates", []))
    return work_base.next_suffixed_id(ids, "adv-")


def _migrate_opp_gates(data: dict, opp: dict) -> None:
    """Lazily convert a legacy Opportunity (holding ``transition_date`` /
    ``transition_date_history`` / ``phase_history`` directly) to the advance-gate
    列 (e-3580 fold, SPEC AC8). Idempotent: deals created after the fold already
    carry ``gates`` and return immediately. Migrate-on-touch — the gate read/
    write paths call this so existing data self-heals on first access and
    persists on the next save, with no global load hook.

    Reconstruction: each phase the deal passed through becomes a done gate with
    ``outcome=advance`` (it was left by advancing); the current *non-terminal*
    phase becomes the single open gate carrying the deal's live 遷移日; a terminal
    current phase instead turns the last non-terminal gate into ``outcome=
    terminal`` and opens nothing. Approximate but faithful — the phase timeline
    and the live judgement date survive on the列, and the raw legacy fields
    (with their notes / full transition_date_history) are **preserved verbatim
    under ``meta.legacy_pre_gate_fold``**, not deleted (data-immutability-
    principle, C-4 / e-3693); only the *active* top-level copies are cleared so
    the gate列 is the single live source.
    """
    if "gates" in opp:
        return
    ph = opp.get("phase_history") or []
    seq = [e.get("phase") for e in ph if e.get("phase")]
    times = [e.get("at", "") for e in ph]
    cur = opp.get("phase", "")
    if cur and (not seq or seq[-1] != cur):
        seq.append(cur)
        times.append("")
    cur_td = (opp.get("transition_date", "") or "").strip()
    cur_terminal = opportunity_phase_is_terminal(data, cur)
    existing_ids = []
    for o in data.get("opportunities", []):
        existing_ids.extend(g.get("id", "") for g in o.get("gates", []))

    def _alloc():
        gid = work_base.next_suffixed_id(existing_ids, "adv-")
        existing_ids.append(gid)
        return gid

    gates = []
    for i, phase in enumerate(seq):
        at = times[i] if i < len(times) else ""
        is_last = (i == len(seq) - 1)
        if is_last and not cur_terminal:
            gates.append({
                "id": _alloc(), "status": GATE_OPEN, "phase": phase, "anchor": "",
                "transition_date": cur_td, "outcome": None, "created_at": at,
                "history": [{"at": at, "action": "opened"},
                            {"at": at, "action": "migrated"}], "meta": {}})
        elif is_last and cur_terminal:
            # terminal current phase: no gate FOR the terminal phase — the last
            # non-terminal gate is where the deal was 決着 from.
            if gates:
                gates[-1]["status"] = GATE_DONE
                gates[-1]["outcome"] = GATE_TERMINAL
                gates[-1]["history"].append(
                    {"kind": "settled", "outcome": GATE_TERMINAL, "at": at})
        else:
            gates.append({
                "id": _alloc(), "status": GATE_DONE, "phase": phase, "anchor": "",
                "transition_date": "", "outcome": GATE_ADVANCE, "created_at": at,
                "history": [{"at": at, "action": "opened"},
                            {"kind": "settled", "outcome": GATE_ADVANCE, "at": at}],
                "meta": {}})
    opp["gates"] = gates
    # C-4 (e-3693): the reconstruction above approximates the timeline onto the
    # gate列 but loses the phase_history *notes* (なぜ) and the whole
    # transition_date_history. Physically deleting them violated
    # data-immutability-principle (and contradicted this function's own "no
    # evidence deleted" claim). Preserve the raw legacy fields under
    # meta.legacy_pre_gate_fold, then clear the active top-level fields so the
    # gate列 is the single live source.
    legacy = {k: opp[k] for k in ("phase_history", "transition_date",
                                  "transition_date_history") if k in opp}
    if legacy:
        opp.setdefault("meta", {})["legacy_pre_gate_fold"] = legacy
    for k in ("phase_history", "transition_date", "transition_date_history"):
        opp.pop(k, None)


def opportunity_gates(opp: dict) -> list:
    """The gate records nested under an Opportunity (append-only列), or []."""
    return opp.get("gates", []) or []


def find_gate(data: dict, gate_id: str):
    """Return ``(opportunity, gate)`` for a gate id, or ``(None, None)``."""
    for opp in data.get("opportunities", []):
        for g in opp.get("gates", []):
            if g.get("id") == gate_id:
                return opp, g
    return None, None


def current_gate(data: dict, opportunity_id: str) -> Optional[dict]:
    """The Opportunity's single *open* advance gate, or None when none is open.

    The ≤1-open invariant (enforced by ``open_advance_gate``) means this is
    unambiguous: there is at most one. Cancelled/done gates are ignored."""
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    _migrate_opp_gates(data, opp)
    for g in opp.get("gates", []):
        if g.get("status") == GATE_OPEN:
            return g
    return None


def open_advance_gate(data: dict, opportunity_id: str, *, phase: str = "",
                      anchor: str = "", transition_date: str = "",
                      at: str = "") -> str:
    """Open a fresh advance gate on an Opportunity and return its id (``adv-N``).

    Enforces the structural invariant this SPEC exists for: an Opportunity may
    hold **at most one open gate at a time** — a second ``open_advance_gate``
    while one is still open raises, so "商談内 open ≤1" is guaranteed by the
    data layer, not by a Skill手順書 (SPEC AC1). done/cancelled gates don't
    count, so a settled deal can open its next gate.

    ``phase`` defaults to the opportunity's current funnel phase — the stage
    this gate judges advancing *out of*. ``anchor`` (a work item mtg-/act-/nrt-
    whose completion will prompt判定, "" = 人手判定) and ``transition_date`` are
    optional at open time; the firing wiring (e-3581) and the date fold (e-3580)
    fill them. Append-only ``history`` opens with an ``opened`` row.
    """
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    _migrate_opp_gates(data, opp)
    if any(g.get("status") == GATE_OPEN for g in opp.get("gates", [])):
        raise ValueError(
            f"{opportunity_id} already has an open advance gate — a商談は同時に"
            "1フェーズだけ進行中 (settle or cancel it before opening another)")
    gate_id = next_gate_id(data)
    opp.setdefault("gates", []).append({
        "id": gate_id,
        "status": GATE_OPEN,
        "phase": phase or opp.get("phase", ""),
        "anchor": anchor or "",
        "transition_date": transition_date or "",
        "outcome": None,
        "created_at": at,
        "history": [{"at": at, "action": "opened"}],
        "meta": {},
    })
    return gate_id


def ensure_open_gate(data: dict, opportunity_id: str, *, phase: str = "",
                     at: str = "") -> dict:
    """Idempotently guarantee the Opportunity has an open gate; return it.

    Returns the existing open gate untouched when one is present, else opens a
    fresh empty one. This is the "phase入場で空の前進ゲートを1つ置く" primitive
    (SPEC 実装順ヒント1) that the phase-entry wiring (e-3583) will call — safe to
    invoke repeatedly without ever creating a second open gate."""
    existing = current_gate(data, opportunity_id)
    if existing is not None:
        return existing
    gate_id = open_advance_gate(data, opportunity_id, phase=phase, at=at)
    _, gate = find_gate(data, gate_id)
    return gate


# The anchor work-item id prefixes (ms-144 e-5191): a meeting / activity / nurturing.
# THE single source for "which prefixes name an anchor work item" + how each kind
# resolves — the prefix-dispatch that was copy-pasted across anchor_gate /
# anchor_opportunity_gate / _work_item_date / work_item_completed / find_work_item.
# Change the value domain HERE, not in 4 places (the maint debt e-5191 pays down).
WORK_ITEM_PREFIXES = ("mtg-", "act-", "nrt-")
# The narrower domain that may anchor an OPPORTUNITY gate: a nurturing (nrt-) lives
# under an Account, so only a meeting / activity can (anchor_opportunity_gate).
OPPORTUNITY_ANCHOR_PREFIXES = ("mtg-", "act-")


def is_work_item_id(work_item_id: str) -> bool:
    """True when ``work_item_id`` names an anchor work item — ``startswith`` any of
    ``WORK_ITEM_PREFIXES`` (mtg-/act-/nrt-, ms-144 e-5191). One predicate the anchor
    paths share instead of re-spelling the three-prefix ``or`` chain."""
    return (work_item_id or "").strip().startswith(WORK_ITEM_PREFIXES)


def resolve_work_item(data: dict, work_item_id: str):
    """Resolve any anchor work item id to ``(owner, work_item, kind)`` across its
    kind's store (mtg-→``find_meeting`` / act-→``find_activity`` / nrt-→
    ``find_nurturing``), or ``(None, None, "")`` for a non-work-item id (ms-144
    e-5191). ``kind`` is ``"meeting"`` / ``"activity"`` / ``"nurturing"``. THE single
    prefix-dispatch the anchor / firing / find paths share, so the value domain and
    its finders live in one place rather than being re-derived per call site."""
    wi = (work_item_id or "").strip()
    if wi.startswith("mtg-"):
        owner, found = find_meeting(data, wi)
        return owner, found, "meeting"
    if wi.startswith("act-"):
        owner, found = find_activity(data, wi)
        return owner, found, "activity"
    if wi.startswith("nrt-"):
        owner, found = find_nurturing(data, wi)
        return owner, found, "nurturing"
    return None, None, ""


def anchor_gate(data: dict, gate_id: str, work_item_id: str, *,
                at: str = "") -> dict:
    """Attach (or re-attach) the work item whose completion will prompt this
    gate's判定, and return the gate. ``work_item_id`` is a meeting (``mtg-``),
    activity (``act-``) or nurturing (``nrt-``) — the anchor whose done/ended
    state becomes the発火源 (SPEC §2). Re-anchoring an open gate corrects a
    mis-linked anchor before settlement (やり直し does NOT re-anchor — under the
    2026-07-17 Option-3 revision a retry settles this gate and opens a fresh one).

    Only the completion-fires-判定 behaviour is deferred to e-3581; here we just
    record the link (validated + append-only) so the entity carries the field.
    """
    opp, gate = find_gate(data, gate_id)
    if gate is None:
        raise ValueError(f"Advance gate not found: {gate_id}")
    if gate.get("status") != GATE_OPEN:
        raise ValueError(
            f"{gate_id} is {gate.get('status')!r}, only an open gate can be anchored")
    wi = (work_item_id or "").strip()
    if not is_work_item_id(wi):
        raise ValueError(
            f"anchor must be a work item (mtg-/act-/nrt-), got {work_item_id!r}")
    _, found, _ = resolve_work_item(data, wi)
    if found is None:
        raise ValueError(f"anchor work item not found: {work_item_id}")
    gate["anchor"] = wi
    # AC4: 遷移日は紐づく work-item の日時に同期する (予定日超過が gate から判る)。
    anchor_date = _work_item_date(data, wi)
    if anchor_date:
        gate["transition_date"] = anchor_date
    gate.setdefault("history", []).append(
        {"at": at, "action": "anchored", "anchor": wi,
         "transition_date": anchor_date} if anchor_date
        else {"at": at, "action": "anchored", "anchor": wi})
    return gate


def _anchor_open_gate_to_meeting(data: dict, opportunity_id: str,
                                 meeting_id: str, *, at: str = "") -> None:
    """Anchor the opportunity's open前進ゲート to ``meeting_id`` (its発火源) when a
    meeting is confirmed with set_transition (e-3583 fix, dogfood #8).

    ms-144 e-5192: this is the meeting-confirm AUTO path — it now DELEGATES the
    actual bind to the public verb ``anchor_opportunity_gate`` so that path's
    ownership invariant (the meeting must belong to *this* opportunity) and its
    idempotency both apply here too, instead of calling the lower-level
    ``anchor_gate`` (which skips ownership). The ONE difference kept from the public
    verb is deliberate: a missing open gate is a **silent no-op** here (this is a
    passive side-effect of confirming a meeting, not a user request), where
    ``anchor_opportunity_gate`` raises. Guarding that case before delegating keeps
    the two anchor entry points converged on a single bind path (e-5192)."""
    if current_gate(data, opportunity_id) is None:
        return  # no live gate (terminal / none) — silent for the auto path
    anchor_opportunity_gate(data, opportunity_id, meeting_id, at=at)


def anchor_opportunity_gate(data: dict, opportunity_id: str,
                            work_item_id: str, *, at: str = ""):
    """Bind ``work_item_id`` (a meeting or activity) as the発火源 of the
    opportunity's open前進ゲート — the public verb backing ``beacon opportunity
    anchor`` (ms-144 e-5177). This is the missing入口: gates open with
    ``anchor=''`` and, outside the two meeting side-effects, no verb let the AI
    attach a non-meeting work item, so 先方検討中 / 合意済み gates (no
    ``kind:meeting`` template) stayed empty (SPEC §問題).

    Returns ``(gate, changed, synced_date)`` so the caller can report only what
    actually happened (the verb's whole point is not to claim state it did not
    set): ``changed`` is False on an idempotent re-anchor, ``synced_date`` is the
    遷移日 only when *this* call moved it (leader review A3).

    Wraps ``anchor_gate`` (which records the link + syncs 遷移日 to the work
    item's date) and adds guards ``anchor_gate`` alone cannot express here:

    * **no open gate → ValueError** (not a silent no-op like the meeting path):
      the verb has no work to do — the phase is terminal or none is open — so
      the caller must hear it rather than believe it anchored (leader 裁定 3).
    * **meeting / activity only**: nurturing (``nrt-``) lives under an Account,
      never an Opportunity, so it can never own an opportunity gate — reject it
      with a recovery hint instead of a confusing ownership error (review A2).
    * **idempotent**: re-anchoring to the *same* work item leaves the gate
      unchanged (``changed=False``, no history row); a *different* work item
      re-links (an auditable mis-link correction, permissive — leader 裁定 2).
    * **ownership**: the work item must belong to *this* opportunity. A foreign
      (another opp's) or unknown id is rejected, so a "確定 (発火源 X)" display
      can never point at a work item this deal does not own (leader caution 4).
    """
    gate = current_gate(data, opportunity_id)
    if gate is None:
        raise ValueError(
            f"{opportunity_id} has no open advance gate to anchor "
            f"(the phase is terminal, or no gate is open)")
    wi = (work_item_id or "").strip()
    # A meeting or activity only (OPPORTUNITY_ANCHOR_PREFIXES). Ownership: the work
    # item must live on this opportunity — the finders search globally, so an
    # existing-but-foreign id would slip past a bare existence check. nurturing
    # (nrt-) gets a recovery-hint rejection before the domain guard because it IS a
    # work item but lives under an Account (review A2), not a plain "not a work item".
    if wi.startswith("nrt-"):
        raise ValueError(
            f"{work_item_id} is a nurturing (nrt-), which lives under an Account, "
            f"not an Opportunity — it cannot anchor a商談's前進ゲート. "
            f"Anchor a meeting (mtg-) or activity (act-) on {opportunity_id} instead")
    if not wi.startswith(OPPORTUNITY_ANCHOR_PREFIXES):
        raise ValueError(
            f"anchor must be a meeting or activity (mtg-/act-), got {work_item_id!r}")
    owner, found, _ = resolve_work_item(data, wi)
    if found is None:
        raise ValueError(f"anchor work item not found: {work_item_id}")
    if owner is None or owner.get("id") != opportunity_id:
        raise ValueError(
            f"{work_item_id} belongs to "
            f"{owner.get('id') if owner else 'another target'}, "
            f"not {opportunity_id} — a gate can only anchor its own商談's work item")
    if (gate.get("anchor") or "") == wi:
        return gate, False, ""  # idempotent: already this発火源, nothing changed
    prev_date = (gate.get("transition_date") or "").strip()
    anchor_gate(data, gate["id"], wi, at=at)
    new_date = (gate.get("transition_date") or "").strip()
    synced_date = new_date if new_date != prev_date else ""
    return gate, True, synced_date


def settle_gate(data: dict, gate_id: str, *, outcome: str, reason: str = "",
                actor: str = "", at: str = "") -> dict:
    """Settle an open advance gate with its判定 outcome and evidence; return it.

    ``outcome`` is one of advance / retry / terminal (the human's decision, SPEC
    §3 — the gate records it, it doesn't decide). Stamps ``outcome`` + a
    ``settled`` audit row (誰・いつ・なぜ via ``work_base.record_audit_event``,
    SPEC AC5) and flips status to ``done``, leaving the gate on the append-only
    列 as履歴. Applying the actual phase move (advance_transition 等) stays the
    caller's job — this only closes the judgement work item.
    """
    if outcome not in VALID_GATE_OUTCOMES:
        raise ValueError(
            f"outcome must be one of {sorted(VALID_GATE_OUTCOMES)}, got {outcome!r}")
    opp, gate = find_gate(data, gate_id)
    if gate is None:
        raise ValueError(f"Advance gate not found: {gate_id}")
    if gate.get("status") != GATE_OPEN:
        raise ValueError(
            f"{gate_id} is already {gate.get('status')!r} — only an open gate settles")
    gate["status"] = GATE_DONE
    gate["outcome"] = outcome
    work_base.record_audit_event(
        gate.setdefault("history", []), kind="settled",
        reason=reason, actor=actor, at=at, outcome=outcome)
    return gate


def cancel_gate(data: dict, gate_id: str, *, reason: str = "") -> dict:
    """Soft-cancel an advance gate (商談中止・誤起票 等) and return it. Routes
    through ``work_base.stamp_cancel`` (status=cancelled + audited meta,
    append-only — never deleted, data-immutability-principle). A cancelled gate
    frees the ≤1-open slot without pretending判定 happened."""
    _, gate = find_gate(data, gate_id)
    if gate is None:
        raise ValueError(f"Advance gate not found: {gate_id}")
    return work_base.stamp_cancel(gate, reason=reason)


def gate_history(data: dict, opportunity_id: str) -> list:
    """The settled (done) advance gates of an Opportunity, oldest first — the
    full judgement history carried by the gate列 (e-3580 fold). Each row exposes
    its phase / outcome / settled evidence. The *phase-change* history is this
    filtered by ``outcome ∈ {advance, terminal}`` (a retry stays in the same
    phase); use ``phase_change_history`` for that view (SPEC AC5, 2026-07-17
    Option-3 revision)."""
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    _migrate_opp_gates(data, opp)
    return [g for g in opp.get("gates", []) if g.get("status") == GATE_DONE]


def phase_change_history(data: dict, opportunity_id: str) -> list:
    """The phase-change history of an Opportunity: the done gates whose outcome
    actually moved the phase (advance / terminal), oldest first (e-3580 fold,
    SPEC AC5). Retries — same-phase 置き直し — are in ``gate_history`` but not
    here, so this is the funnel timeline the deal walked."""
    return [g for g in gate_history(data, opportunity_id)
            if g.get("outcome") in (GATE_ADVANCE, GATE_TERMINAL)]


# --- judgement firing (e-3581, SPEC §2) — anchored work-item completion ------
# The old model branched on a per-phase ``transition_signal`` (calendar_ended /
# manual). That is gone: how a phase is judged is now determined by *what* work
# item the open gate is anchored to — a meeting's ending, a dated activity's
# done, or (no anchor) the 遷移日 arriving. One firing surface, no phase branch.

def _work_item_date(data: dict, work_item_id: str) -> str:
    """The date (YYYY-MM-DD) a work item is planned for, or '' — a meeting's
    scheduled_at, an activity/nurturing's deadline. Used to sync a gate's 遷移日
    to its anchor (SPEC AC4)."""
    _, found, kind = resolve_work_item(data, work_item_id)
    if found is None:
        return ""
    if kind == "meeting":
        return (found.get("scheduled_at", "") or "")[:10]
    return (found.get("deadline", "") or "")  # activity / nurturing


def work_item_completed(data: dict, work_item_id: str, now: str = "") -> bool:
    """True when a work item (mtg-/act-/nrt-) has reached its completed state —
    the signal that prompts its前進ゲート's phase judgement (e-3581, SPEC §2/AC3).

    A meeting counts as complete when it is ``ended``, or still ``scheduled`` but
    its effective end has passed ``now`` (a full timestamp). An activity or
    nurturing counts when its status is ``done``. Any other id / state → False."""
    _, found, kind = resolve_work_item(data, work_item_id)
    if found is None:
        return False
    if kind == "meeting":
        if found.get("status") == MEETING_ENDED:
            return True
        if found.get("status") == MEETING_SCHEDULED and now:
            end = meeting_effective_end(found)
            now_dt = _parse_dt(now)
            return bool(end is not None and now_dt is not None and end <= now_dt)
        return False
    return work_model.is_done(found)  # activity / nurturing


def gate_judgement_ready(data: dict, opportunity_id: str, now: str = "") -> bool:
    """True when the opportunity's open前進ゲート is ready for a phase judgement
    (e-3581, SPEC §2). Firing is unified — no per-phase transition_signal:

    * anchored gate  → ready once the anchored work-item has completed; an
      unanchored臨時 meeting ending does NOT make it ready (AC2).
    * unanchored gate→ ready once its 遷移日 is due/overdue as of ``now`` (the
      縮退形 of old manual — a human judges on the date, AC3).

    False when no gate is open (terminal / none). ``now`` is a timestamp; the
    date comparison uses its date portion."""
    gate = current_gate(data, opportunity_id)
    if gate is None:
        return False
    anchor = (gate.get("anchor") or "").strip()
    if anchor:
        return work_item_completed(data, anchor, now)
    td = (gate.get("transition_date") or "").strip()
    if not td or not now:
        return False
    today = now[:10]
    return temporal_status(td, today, settled=False) in (
        TRANSITION_DUE, TRANSITION_OVERDUE)


def meeting_is_gate_anchor(data: dict, meeting_id: str) -> bool:
    """True when this meeting is the anchor of its opportunity's open前進ゲート
    (e-3581/e-3583). The meeting-end detector uses this to route structurally: an
    anchored meeting ending prompts the phase judgement; a stray (unanchored)
    meeting ending is minutes-only and never moves the phase (AC2) — the skill
    reads this flag instead of deciding by手順書."""
    opp, m = find_meeting(data, meeting_id)
    if m is None:
        return False
    gate = current_gate(data, opp["id"])
    return gate is not None and (gate.get("anchor") or "") == meeting_id


def opportunities_awaiting_gate_judgement(data: dict, now: str) -> list:
    """The unified firing surface (e-3581): opportunities whose open gate is
    ready to judge (anchor completed OR 遷移日 due/overdue), oldest 遷移日 first.
    Each row carries id / title / phase / anchor / transition_date /
    who_has_the_ball. This replaces the per-phase transition_signal branch as the
    single "what needs judging now" list the sales skills read (SPEC §2)."""
    out = []
    for opp in live_opportunities(data):  # e-3586: 取消済は判定 firing surface に出さない
        oid = opp["id"]
        if not gate_judgement_ready(data, oid, now):
            continue
        gate = current_gate(data, oid)
        out.append({
            "id": oid, "title": work_model.target_label(opp), "phase": opp.get("phase", ""),
            "anchor": (gate or {}).get("anchor", ""),
            "transition_date": (gate or {}).get("transition_date", ""),
            # C-3 (e-3692): derived-first ball (see opportunities_awaiting_judgement).
            "who_has_the_ball": derive_ball(opp) or opp.get("who_has_the_ball", ""),
        })
    out.sort(key=lambda r: (r["transition_date"] or "9999-99-99"))
    return out


# ---------------------------------------------------------------------------
# Activity (業務・事前計画型) — 従属 composition → Opportunity
# ---------------------------------------------------------------------------

def activity_add(data: dict, opportunity_id: str, description: str, *,
                 deadline: str = "", who_has_the_ball: str = BALL_SELF,
                 source: str = "", created_at: str = "",
                 created_in_phase: str = "") -> str:
    """Append an Activity (業務・事前計画型) under an Opportunity, return its id.

    ``source`` records where the activity came from (e.g. ``"template-anchor"``
    for a phase's fixed step, ``"ai"`` for an AI-generated one, "" for a hand
    -added one) so the origin stays auditable.

    ``created_in_phase`` (e-3555) stamps which funnel phase this activity was
    born in — set-once and never mutated afterwards. When left blank it defaults
    to the opportunity's *current* phase (the hand-added / manual case); a caller
    seeding activities for a specific phase ahead of time passes it explicitly
    (the seed case). This lets the UI filter activities by phase and lets the
    phase-fold step (e-3553) tell which phase's work an activity belongs to. It
    is a SALES-instance field only — the occupation-agnostic base stays phase
    -free (doc 5srt3fcamG59ljyRlNKn の層分界)."""
    # ms-143 sales-mirror (系統1 = work-item 追加): validations stay here
    # (sales-frontend concern), but the target lookup + id allocation + append
    # delegate to the shared occupation primitives — the SAME path a dev task is
    # added through. find_target / add_work_item are manifest-driven so this verb
    # no longer names data['opportunities'] / next_activity_id itself. Lazy import
    # avoids the occupation→sales_entities cycle.
    import occupation
    opp = occupation.find_target(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    if not description or not description.strip():
        raise ValueError("Activity description is required")
    if who_has_the_ball not in VALID_BALL:
        raise ValueError(
            f"who_has_the_ball must be one of {sorted(VALID_BALL)}, got {who_has_the_ball!r}")
    # created_in_phase defaults to the opportunity's current phase (sales-specific
    # enrichment, ms-106 e-3555) — computed here and passed as an extra field.
    act = occupation.add_work_item(
        data, opportunity_id, description=description.strip(),
        deadline=deadline, who_has_the_ball=who_has_the_ball, source=source,
        created_at=created_at,
        created_in_phase=created_in_phase or opp.get("phase", ""))
    return act["id"]


def next_nurturing_id(data: dict) -> str:
    ids = []
    for acc in data.get("accounts", []):
        ids.extend(n.get("id", "") for n in acc.get("nurturings", []))
    return _next_prefixed_id(ids, "nrt-")


def nurturing_add(data: dict, account_id: str, description: str, *,
                  deadline: str = "", who_has_the_ball: str = BALL_SELF,
                  source: str = "", created_at: str = "",
                  created_in_phase: str = "") -> str:
    """Append a Nurturing (継続関係の業務・事前計画型) under an Account, return
    its id. The continuous-target twin of ``activity_add``: an Opportunity
    carries 業務 as *activities*, an Account carries them as *nurturings*
    (ms-106 fb3). Same shape (description / deadline / status / ball / source)
    so the target-class engine (ms-109) can treat both uniformly; the naming
    just reflects that account work is 関係維持 (nurturing), not deal advance.
    """
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    if not description or not description.strip():
        raise ValueError("Nurturing description is required")
    if who_has_the_ball not in VALID_BALL:
        raise ValueError(
            f"who_has_the_ball must be one of {sorted(VALID_BALL)}, got {who_has_the_ball!r}")
    nrt_id = next_nurturing_id(data)
    acc.setdefault("nurturings", []).append({
        "id": nrt_id,
        "description": description.strip(),
        "deadline": deadline,
        "status": "todo",
        "who_has_the_ball": who_has_the_ball,
        "source": source,
        "created_at": created_at,
        # e-3555: 活動(activity)の双子として同じ set-once の phase 帰属を持つ。
        # 空なら Account の現フェーズを既定にする。
        "created_in_phase": created_in_phase or acc.get("phase", ""),
    })
    return nrt_id


def instantiate_phase_activities(data: dict, target_id: str, *,
                                 at: str = "") -> list:
    """Create the current phase's fixed anchor activities on a target (e-3270,
    SPEC §4). Anchors come from the phase's ``activity_template`` — the company's
    must-do steps for that phase (提案準備→提案作成, 合意済み→契約書送付 等). Returns
    the created activity ids.

    Idempotent by description: an anchor whose text already exists as a
    non-done activity on this target is skipped, so re-entering a phase (or
    calling twice) never duplicates. This layer is deterministic — the AI's
    goal-driven activities (contextual, per-deal) are added on top by the Skill.
    """
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    pdef = _find_phase_def(opportunity_phases(data), opp.get("phase", ""))
    anchors = phase_activity_anchors(pdef)
    existing = {(_norm(a.get("description")))
                for a in opp.get("activities", []) if not work_model.is_done(a)}
    created = []
    for anchor in anchors:
        text = anchor["desc"]
        if not text or _norm(text) in existing:
            continue
        act_id = activity_add(data, target_id, text,
                              source="template-anchor", created_at=at)
        created.append(act_id)
        existing.add(_norm(text))
        # e-3548: a meeting-type anchor co-seeds a 予定未定 Meeting linked to it,
        # so date-confirmation later *updates* that Meeting instead of spawning a
        # duplicate activity. Idempotency flows from the activity guard above —
        # a skipped anchor seeds no second Meeting.
        if anchor["kind"] == ANCHOR_KIND_MEETING:
            mtg_id = meeting_seed(data, target_id, linked_id=act_id, at=at)
            # e-3583: the seeded meeting is this phase's発火源 — anchor the open
            # 前進ゲート to it so its ending prompts the phase judgement (SPEC §2).
            # The 予定未定 meeting has no date yet, so the gate's 遷移日 stays unset
            # until the schedule Skill confirms the meeting (which syncs it).
            gate = current_gate(data, target_id)
            if gate is not None and not (gate.get("anchor") or "").strip():
                anchor_gate(data, gate["id"], mtg_id, at=at)
    return created


# ---------------------------------------------------------------------------
# Cancellation (取消 — soft, append-only per data-immutability-principle).
# e-3586: opportunities and accounts are never physically removed. Deleting a
# deal would take its 証跡 (activities / communications — the immutable record of
# what actually happened) with it, which is the largest remaining immutability
# gap. Cancel instead stamps status=cancelled + 誰・いつ・なぜ (via
# work_base.stamp_cancel) and leaves the record in the log; live_opportunities()
# and every aggregation drop it from the default view. This is the same cancel
# vocabulary Communications already route through (communication_cancel).
# ---------------------------------------------------------------------------

def live_opportunities(data: dict) -> list:
    """Opportunities excluding soft-cancelled (取消済) ones — the default view and
    the set every aggregation / attention derivation reads (e-3586). Cancelled
    deals stay in ``data['opportunities']`` (append-only) but drop out of
    pipeline value, ball / overdue derivation, and the default listing."""
    return [o for o in data.get("opportunities", [])
            if not work_model.is_cancelled(o)]


def opportunity_cancel(data: dict, opportunity_id: str, *, reason: str = "",
                       actor: str = "", at: str = "") -> dict:
    """Soft-cancel (取消) an Opportunity and return it (e-3586).

    Routes through ``work_base.stamp_cancel`` (status=cancelled + audited meta:
    誰・いつ・なぜ, append-only) so the deal — and its composition children
    (activities / communications, the 証跡) — are never physically deleted. Its
    open advance gate is cancelled too, freeing the ≤1-open slot and dropping the
    deal out of the judgement queue. Cancelled deals leave the default view
    (``live_opportunities``) but the record stays for audit / retrospection."""
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    gate = current_gate(data, opportunity_id)
    if gate is not None:
        work_base.stamp_cancel(gate, reason=reason or "商談取消",
                               actor=actor, at=at)
    return work_base.stamp_cancel(opp, reason=reason, actor=actor, at=at)


def account_cancel(data: dict, account_id: str, *, reason: str = "",
                   actor: str = "", at: str = "", force: bool = False) -> list:
    """Soft-cancel (取消) an Account and return the ids of live opportunities that
    referenced it (e-3586).

    Mirrors the old delete's referential integrity but without physical removal:
    Opportunity → Account is a 参照 association (independent lifecycle), so an
    Account still referenced by *live* deals is refused unless ``force`` — with
    ``force`` those deals are orphaned (``account_id`` → None) rather than
    cancelled along with it (a live deal shouldn't be 取消 just because its
    account was). Routes through ``work_base.stamp_cancel``."""
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    referencing = [o["id"] for o in live_opportunities(data)
                   if o.get("account_id") == account_id]
    if referencing and not force:
        raise ValueError(
            f"Account {account_id} is referenced by live opportunities "
            f"{referencing}; reassign/cancel those or pass force=True to orphan them")
    if referencing and force:
        for o in data.get("opportunities", []):
            if o.get("account_id") == account_id:
                o["account_id"] = None
    work_base.stamp_cancel(acc, reason=reason, actor=actor, at=at)
    return referencing


# ---------------------------------------------------------------------------
# Communication (証跡・事後記録型) — 従属 composition → Opportunity or Account
# ---------------------------------------------------------------------------
# ms-107 e-3432 / SPEC o83GEljD8xeFMr95wLTh 設計方針 1: 営業の Commit。
#
# 開発の Task→Commit と同型で、営業の Activity (事前計画型) と対をなす事後記録型。
# 「実際にこのメールを送り、こう返ってきた」を source を辿れる形 + AI 要約付きで
# 残す証跡。フェーズ遷移の判定は「予定 (Activity)」でなく「事実 (Communication)」
# を読むのが正しい (SPEC §1)。性質:
#   - source を辿れる (message-id / thread / permalink / 議事録 doc / event link)
#   - AI 要約付き (summary は 1 行)
#   - 不変・追記のみ (data-immutability-principle — 一度残した証跡は書き換えない)
#   - target (Opportunity 優先、商談が無ければ Account) の子。Activity と同じ子層。
#
# ms-109 含意: Commit ≡ Communication = target 横断で汎用な証跡 primitive。本 MS
# では営業アダプタとして実装し、汎用化は ms-109 に委ねる。

COMM_INBOUND = "inbound"        # 相手 → 自分 (受信): この後ボールは自分
COMM_OUTBOUND = "outbound"      # 自分 → 相手 (送信): この後ボールは相手
VALID_COMM_DIRECTION = {COMM_INBOUND, COMM_OUTBOUND}

# やり取りの媒体。e-3454: channel は自由記述 (現実の媒体は email/slack に収まらず
# Facebook Messenger / LINE / 対面 / 電話 等と開いている)。下は UI hint / 補完候補
# としての *既知* セットであって enforce される閉集合ではない。communication_add は
# 任意の非空文字列を受け付け、正規化 (strip + lowercase) のみ行う (空なら "other")。
KNOWN_COMM_CHANNELS = ("email", "slack", "meeting", "calendar", "phone",
                       "messenger", "line", "in-person", "sms", "other")
# 後方互換 alias (旧名参照コード用)。
COMM_CHANNELS = KNOWN_COMM_CHANNELS


def _gather_communications(target: dict) -> list:
    """All communications of a target (opp/acc): its own target-level records
    plus those nested under its work items (activities/nurturings). Insertion
    order across the two levels is preserved for the stable-sort fallback in
    ``communications_of``. e-3503."""
    out = list((target or {}).get("communications", []) or [])
    for child in (target or {}).get("activities", []) or []:
        out.extend(child.get("communications", []) or [])
    for child in (target or {}).get("nurturings", []) or []:
        out.extend(child.get("communications", []) or [])
    return out


def next_communication_id(data: dict) -> str:
    ids = []
    for opp in data.get("opportunities", []):
        ids.extend(c.get("id", "") for c in _gather_communications(opp))
    for acc in data.get("accounts", []):
        ids.extend(c.get("id", "") for c in _gather_communications(acc))
    return _next_prefixed_id(ids, "comm-")


def find_activity(data: dict, activity_id: str):
    """Return ``(opportunity, activity)`` for an activity id, or ``(None, None)``."""
    for opp in data.get("opportunities", []):
        for a in opp.get("activities", []):
            if a.get("id") == activity_id:
                return opp, a
    return None, None


# ms-139 e-4950: cancelled は Activity の正当な terminal 状態 (activity_cancel が
# 作る)。以前 valid 集合が {todo, done} だけで cancelled を欠いていたため、cockpit の
# 「status == cancelled は未消化に出さない」除外前提と齟齬していた (SPEC P6)。
# vocabulary としては cancelled を含める。ただし cancelled は監査印
# (cancelled_at/by/reason) を伴うので、直接 set できる状態は下の _SETTABLE のみに
# 絞り、cancel は activity_cancel を経由させる (= work_base.stamp_cancel)。
VALID_ACTIVITY_STATUS = {
    work_model.TODO_STATUS, work_model.DONE_STATUS, work_model.CANCELLED_STATUS}
_SETTABLE_ACTIVITY_STATUS = {work_model.TODO_STATUS, work_model.DONE_STATUS}


def activity_set_status(data: dict, activity_id: str, status: str, *,
                        at: str = "") -> dict:
    """Set an Activity's status (todo/done) and return it. Used when a send or
    other Communication *fulfills* a planned Activity (ms-106 e-3505): the plan
    is marked done rather than leaving a lingering todo sitting beside the
    Communication fact — the send is recorded once (as the Communication), and
    the plan it satisfied is closed, not duplicated as a second '[sent]' todo.

    Only todo/done are settable here; cancel goes through ``activity_cancel``
    (it stamps the cancel audit trail) — ms-139 e-4950."""
    # ms-143 sales-mirror (系統3): the cancelled guard stays here (sales-specific
    # audit message pointing at activity_cancel), but the find + lifecycle
    # transition delegate to occupation.set_entry_state — the SAME shared path a
    # dev task closes through (find_target_entry locates the activity in the
    # opportunity's ``activities`` arm; ``done`` routes through the same
    # work_model.mark_done base). This is the ms-143 core proof: ONE abstraction
    # serves both professions' done. Lazy import avoids the
    # occupation→sales_entities cycle.
    if status not in _SETTABLE_ACTIVITY_STATUS:
        hint = " (cancel via activity_cancel)" if status == work_model.CANCELLED_STATUS else ""
        raise ValueError(
            f"status must be one of {sorted(_SETTABLE_ACTIVITY_STATUS)}{hint}, got {status!r}")
    import occupation
    # review finding #2: detect not-found EXPLICITLY (to keep the sales-specific
    # message) instead of catching set_entry_state's ValueError. A bare
    # ``except ValueError`` would rewrite ANY ValueError — e.g. a manifest
    # misconfiguration — into a misleading "Activity not found". With the entry
    # pre-confirmed present, set_entry_state's own ValueErrors propagate un-masked.
    if occupation.find_target_entry(data, activity_id) is None:
        raise ValueError(f"Activity not found: {activity_id}")
    _opp, act = occupation.set_entry_state(
        data, activity_id, status, at=at, actor=work_base.current_actor())
    return act


def activity_cancel(data: dict, activity_id: str, *, reason: str = "") -> dict:
    """Cancel (取消) an Activity and return it — correcting a mis-recorded plan
    without deleting it (data-immutability-principle).

    Routes through the shared ``work_base.stamp_cancel`` (ms-109 e-3558): sets
    ``status="cancelled"`` and stamps ``meta.cancelled_at / cancelled_by /
    cancel_reason``, append-only. This is the sales side of the same cancel
    vocabulary development uses (``core.task_delete``), closing the historical
    gap where sales could only hard-delete (SPEC AC3).

    Scope note (e-3558): this lands the cancel primitive for the Activity
    work item. Turning the Account/Opportunity hard-deletes into soft-cancel,
    the Communication delete/retarget path, and the reader-side filtering of
    cancelled records belong to e-3537 (誤起票の訂正を primitive 化).
    """
    _, act = find_activity(data, activity_id)
    if act is None:
        raise ValueError(f"Activity not found: {activity_id}")
    return work_base.stamp_cancel(act, reason=reason)


def activity_update(data: dict, activity_id: str, *, description: str = "",
                    deadline: str = "", who_has_the_ball: str = "") -> dict:
    """Update an Activity's mutable plan fields (説明 / 締切 / ボール) after
    creation and return it — ms-139 e-4950 (SPEC 方針5: deadline / ball の後追い
    更新)。空文字は「変更なし」(task_update と同じ規約) なので、触りたいフィールド
    だけ渡す。``who_has_the_ball`` は self / counterpart を検証する。締切の変更は L2
    締切エンジンが次の overdue 判定に即反映する (状態は派生なので stale しない)."""
    _, act = find_activity(data, activity_id)
    if act is None:
        raise ValueError(f"Activity not found: {activity_id}")
    if description.strip():
        act["description"] = description.strip()
    if deadline.strip():
        act["deadline"] = deadline.strip()
    if who_has_the_ball.strip():
        ball = who_has_the_ball.strip()
        if ball not in (BALL_SELF, BALL_COUNTERPART):
            raise ValueError(
                f"ball must be {BALL_SELF!r} or {BALL_COUNTERPART!r}, got {ball!r}")
        act["who_has_the_ball"] = ball
    return act


def find_nurturing(data: dict, nurturing_id: str):
    """Return ``(account, nurturing)`` for a nurturing id, or ``(None, None)``."""
    for acc in data.get("accounts", []):
        for n in acc.get("nurturings", []):
            if n.get("id") == nurturing_id:
                return acc, n
    return None, None


def find_work_item(data: dict, work_item_id: str):
    """Resolve an Activity (act-) or Nurturing (nrt-) work item to
    ``(target, work_item)``, or ``(None, None)``. Used by the reply-watcher
    (E) which watches a specific 予定 for its counterpart's reply. A meeting
    (mtg-) resolves via ``find_meeting`` / ``resolve_work_item``, NOT here — this
    stays act-/nrt- only (ms-144 e-5191: shares the one prefix-dispatch, but keeps
    its narrower return so callers that pass a meeting still get ``(None, None)``)."""
    owner, found, kind = resolve_work_item(data, work_item_id)
    if kind in ("activity", "nurturing"):
        return owner, found
    return None, None


# ---------------------------------------------------------------------------
# Watch (返信待ちスレッドの見張りフラグ) — E (返信ウォッチャー) の状態
# ---------------------------------------------------------------------------
# ms-107 e-3437。時間に敏感なやり取り (日程打診など) を送った時、その予定
# (Activity / Nurturing) に watch を立てる。返信ウォッチャー (E) が hourly に
# 「watch あり かつ ball=相手 (= まだ返信待ち)」のスレッドだけ確認し、返信が
# 来たら inbound Communication を残して ball を自分に戻す (= derive_ball が flip)。
# 話題が完結したら watch を落とす。watch 自体は「どのスレッドを・どの媒体で
# 見るか」を持つだけで、定期起動の判定は tick_scheduler が担う (関心の分離)。

def set_watch(data: dict, work_item_id: str, *, channel: str,
              thread_ref: str = "", cadence_minutes: int = 60,
              at: str = "") -> dict:
    """Arm a reply-watch on a work item (act-/nrt-). ``channel`` is the medium
    to poll (email/slack…), ``thread_ref`` the thread/message id to look under
    (from the outbound Communication's source). Returns the watch dict."""
    target, wi = find_work_item(data, work_item_id)
    if wi is None:
        raise ValueError(f"Work item not found (act-/nrt-): {work_item_id}")
    if not channel or not channel.strip():
        raise ValueError("watch channel is required")
    wi["watch"] = {
        "enabled": True,
        "channel": channel.strip().lower(),
        "thread_ref": thread_ref,
        "cadence_minutes": int(cadence_minutes) if cadence_minutes else 60,
        "armed_at": at,
        "last_checked_at": "",
    }
    return wi["watch"]


def clear_watch(data: dict, work_item_id: str, *, at: str = "") -> None:
    """Disarm a reply-watch (話題が完結した時). Kept as ``enabled: False`` so
    the audit trail of "we were watching" survives (data-immutability-principle)."""
    target, wi = find_work_item(data, work_item_id)
    if wi is None:
        raise ValueError(f"Work item not found (act-/nrt-): {work_item_id}")
    w = wi.get("watch")
    if w:
        w["enabled"] = False
        w["cleared_at"] = at


def watched_work_items(data: dict, *, awaiting_reply_only: bool = False) -> list:
    """List armed reply-watches as ``(target, work_item)``. With
    ``awaiting_reply_only`` keep only those whose target's ball is the
    counterpart (= we're still waiting) — that's the set E actually polls
    ('ball=相手 かつ watch', SPEC §3). Others (ball already back to us) are
    skipped until we send again."""
    out = []
    for opp in data.get("opportunities", []):
        if work_model.is_cancelled(opp):  # e-3586: 取消済商談は監視しない
            continue
        for a in opp.get("activities", []):
            if a.get("status") == CANCELLED_STATUS:  # e-3537: 取消済は監視しない
                continue
            w = a.get("watch")
            if w and w.get("enabled"):
                if awaiting_reply_only and derive_ball(opp) != BALL_COUNTERPART:
                    continue
                out.append((opp, a))
    for acc in data.get("accounts", []):
        for n in acc.get("nurturings", []):
            if n.get("status") == CANCELLED_STATUS:  # e-3537: 取消済は監視しない
                continue
            w = n.get("watch")
            if w and w.get("enabled"):
                if awaiting_reply_only and derive_ball(acc) != BALL_COUNTERPART:
                    continue
                out.append((acc, n))
    return out


def resolve_communication_target(data: dict, target_id: str):
    """Resolve where a Communication is stored and what planned item it fulfills.

    Mirrors the dev model where a commit is *stored under* a milestone yet can
    *reference* a task (resolves). Here the "container" (where the record lives)
    is always an Opportunity or Account; the optional "linked_id" points at the
    specific Activity/Nurturing (= 予定) the communication fulfilled:

      opp-…  → (opportunity, "")          # target grain, no work-item link
      acc-…  → (account,     "")          # target grain, no work-item link
      act-…  → (parent opp,  "act-…")     # stored on the deal, links the activity
      nrt-…  → (parent acc,  "nrt-…")     # stored on the account, links the nurturing

    Returns ``(container, linked_id)`` or ``(None, None)`` when unresolvable."""
    if target_id.startswith("opp-"):
        opp = find_opportunity(data, target_id)
        return (opp, "") if opp is not None else (None, None)
    if target_id.startswith("acc-"):
        acc = find_account(data, target_id)
        return (acc, "") if acc is not None else (None, None)
    if target_id.startswith("act-"):
        opp, act = find_activity(data, target_id)
        return (opp, target_id) if act is not None else (None, None)
    if target_id.startswith("nrt-"):
        acc, nrt = find_nurturing(data, target_id)
        return (acc, target_id) if nrt is not None else (None, None)
    return None, None


def find_communication_target(data: dict, target_id: str) -> Optional[dict]:
    """The container (Opportunity/Account) that stores a Communication for the
    given id — accepts opp-/acc- (target grain) and act-/nrt- (work-item grain,
    resolved to the parent deal/account). Returns the container dict or None."""
    container, _ = resolve_communication_target(data, target_id)
    return container


def find_communication(data: dict, comm_id: str):
    """Locate a Communication *record* by its id. Returns
    ``(container, node, comm)`` where ``container`` is the owning Opportunity/
    Account, ``node`` is the dict whose ``communications`` list physically holds
    the record (the container itself, or a nested Activity/Nurturing), and
    ``comm`` is the record. ``(None, None, None)`` when not found.

    Unlike ``find_communication_target`` (which resolves a *target* id to its
    container), this resolves a *communication* id to the record itself so it
    can be cancelled or moved (e-3537)."""
    def _scan(container):
        for c in container.get("communications", []) or []:
            if c.get("id") == comm_id:
                return container, container, c
        for child_key in ("activities", "nurturings"):
            for child in container.get(child_key, []) or []:
                for c in child.get("communications", []) or []:
                    if c.get("id") == comm_id:
                        return container, child, c
        return None
    for opp in data.get("opportunities", []):
        hit = _scan(opp)
        if hit:
            return hit
    for acc in data.get("accounts", []):
        hit = _scan(acc)
        if hit:
            return hit
    return None, None, None


def communication_cancel(data: dict, comm_id: str, *, reason: str = "") -> dict:
    """Cancel (取消) a mis-recorded Communication and return it.

    Soft-cancel via the shared ``work_base.stamp_cancel`` (status=cancelled +
    reason, append-only — the 証跡 is never physically deleted, per
    data-immutability-principle). The record stays in the log (the UI shows it
    struck-through) but is excluded from ball/phase derivation and reply-watch
    (e-3537). Use this only for a communication that should not exist (recorded
    an exchange that never happened). To fix a communication filed under the
    *wrong* work item, use ``communication_retarget`` — that is a re-filing, not
    a cancel."""
    _, _, comm = find_communication(data, comm_id)
    if comm is None:
        raise ValueError(f"Communication not found: {comm_id}")
    return work_base.stamp_cancel(comm, reason=reason)


def communication_retarget(data: dict, comm_id: str, new_target_id: str, *,
                           reason: str = "", actor: str = "", at: str = "") -> dict:
    """Move a Communication to the correct target/work item and return it.

    Re-filing a mis-attached 証跡 is a plain edit of *where it is filed*
    (``linked_id`` + nesting), NOT a change to the evidence itself: the fact
    fields (summary / source / direction / occurred_at) are untouched. So this
    is a direct move, not a cancel + re-issue — per data-immutability the
    immutable part is *what happened*, not *which work item it was filed under*.

    e-3585: the move itself is audited on the record — an append-only
    ``meta.retarget_history`` row (元 linked_id → 先 + 誰・いつ・なぜ via
    ``work_base.record_audit_event``). The earlier "git に残る" rationale (e-3537)
    doesn't hold for sales tenants, whose project.json is cloud-managed and git-
    untracked, so the audit lives with the evidence rather than only in the log.

    ``new_target_id`` may be a target (opp-/acc-) or a work item (act-/nrt-);
    the record is re-nested to mirror ``communication_add``'s placement."""
    _, node, comm = find_communication(data, comm_id)
    if comm is None:
        raise ValueError(f"Communication not found: {comm_id}")
    new_container, new_linked = resolve_communication_target(data, new_target_id)
    if new_container is None:
        raise ValueError(
            "Communication target not found (opp-…/acc-… target or "
            f"act-…/nrt-… work item): {new_target_id}")
    prev_linked = comm.get("linked_id") or ""
    # Detach from its current holder.
    node["communications"] = [c for c in node.get("communications", [])
                              if c.get("id") != comm_id]
    # Re-file: mirror communication_add's nesting — act-/nrt- nests under that
    # work item, a plain opp-/acc- target sits at the container level.
    comm["linked_id"] = new_linked
    if new_linked.startswith("act-"):
        _, dest = find_activity(data, new_linked)
    elif new_linked.startswith("nrt-"):
        _, dest = find_nurturing(data, new_linked)
    else:
        dest = new_container
    dest.setdefault("communications", []).append(comm)
    # e-3585: stamp who/when/why of the re-filing onto the record (append-only),
    # keeping the fact fields untouched — only the move's provenance is recorded.
    work_base.record_audit_event(
        comm.setdefault("meta", {}).setdefault("retarget_history", []),
        kind="retarget", reason=reason, actor=actor, at=at,
        from_linked=prev_linked, to_linked=new_linked)
    return comm


def communication_add(data: dict, target_id: str, summary: str, *,
                      direction: str, channel: str = "other",
                      body: str = "",
                      source: Optional[dict] = None,
                      occurred_at: str = "", created_at: str = "",
                      created_in_phase: str = "") -> str:
    """Append a Communication (証跡・事後記録型) and return its id.

    ``target_id`` may be a target (opp-/acc-) or a planned work item
    (act-/nrt-). This mirrors the dev commit↔task model *including its nesting*
    (ms-106 e-3503): a commit that resolves a task is stored **under that task**
    (nested), and one that resolves nothing sits at milestone level. So here a
    communication that fulfills an Activity/Nurturing is nested **under that
    work item's own ``communications``**; one addressed to the deal/account
    directly sits at the opp/acc level. ``linked_id`` still records which work
    item it fulfilled, so the two grains stay traceable.

    ``direction`` (inbound/outbound) is required — it's what ball derivation and
    the reply-watcher (E) read. ``source`` is a free dict of trace pointers
    (typically ``{"ref": <message-id/thread>, "url": <permalink>}``) so the
    origin stays auditable. Append-only: never mutates an existing record.

    ``body`` (e-3544, 任意) is a fuller free-text 内容要約 — the骨子 of a mail /
    the 要点 of a議事録 — kept separate from ``summary`` (the 1-行 log line). It is
    stored only when non-empty so old records stay byte-identical (前方互換), and
    is shown in the UI's per-証跡 detail toggle (最上部) when present.

    ms-143: the record build + nesting + id allocation now live in the profession-
    generic ``occupation.add_evidence`` (the evidence-grain sibling of
    ``add_work_item``); this stays as the sales frontend signature so existing
    callers are byte-identical (pinned by test_add_evidence_primitive_ms143). Lazy
    import — occupation imports sales_entities at module load, so the reverse edge
    must not be import-time."""
    import occupation
    # add_evidence returns the record dict (AX review PR #628 finding #1, symmetric
    # with add_work_item); this frontend keeps its historical id-string return.
    return occupation.add_evidence(
        data, target_id, summary=summary, direction=direction, channel=channel,
        body=body, source=source, occurred_at=occurred_at,
        created_at=created_at, created_in_phase=created_in_phase)["id"]


def communications_of(target: dict, *, linked_id: Optional[str] = None,
                      include_cancelled: bool = True) -> list:
    """The target's communications in occurrence order (oldest → newest).
    Sort key is occurred_at, falling back to created_at then insertion order so
    records without a timestamp keep their append order (stable). Pass
    ``linked_id`` to keep only the communications that fulfilled that specific
    Activity/Nurturing (= work-item grain view).

    e-3503: gathers both the target-level records and those nested under the
    target's work items (activities/nurturings), so the chronological log is
    complete regardless of nesting. Old records stored flat at target level (pre
    -nesting) are still included — read stays backward-compatible.

    ``include_cancelled`` (e-3537): the default (True) keeps cancelled (取消済)
    records in the list so the display shows them faithfully (struck-through,
    per the UI principle — a cancelled 証跡 is not hidden). Semantic readers
    that ask "what is the *effective* state" (ball / phase derivation) pass
    False so a mis-recorded, cancelled communication does not drive the deal."""
    comms = _gather_communications(target)
    if not include_cancelled:
        comms = [c for c in comms if c.get("status") != CANCELLED_STATUS]
    if linked_id is not None:
        comms = [c for c in comms if c.get("linked_id") == linked_id]

    def key(pair):
        idx, c = pair
        t = c.get("occurred_at") or c.get("created_at") or ""
        # Untimed records (t == "") sort *after* all dated ones, then keep
        # insertion order — a freshly-added note without a timestamp belongs at
        # the tail of the chronological log, not the head.
        return (t == "", t, idx)

    ordered = sorted(enumerate(comms), key=key)
    return [c for _, c in ordered]


def derive_ball(target: dict) -> Optional[str]:
    """Whose court the deal is in, derived from the latest Communication
    (SPEC §6): the newest inbound means the counterpart just played → the ball
    is ours (BALL_SELF); the newest outbound means we played → theirs
    (BALL_COUNTERPART). Returns None when there's no communication to derive
    from (ball is unknown, not a default). ball was removed from the UI but the
    engine keeps it as the reply-watcher's (E) driver.

    e-3537: cancelled (取消済) communications are excluded — a mis-recorded
    exchange must not decide whose court the deal is in."""
    comms = communications_of(target, include_cancelled=False)
    if not comms:
        return None
    latest = comms[-1]
    if latest.get("direction") == COMM_INBOUND:
        return BALL_SELF
    if latest.get("direction") == COMM_OUTBOUND:
        return BALL_COUNTERPART
    return None


# ---------------------------------------------------------------------------
# Meeting (面談・運用状態型) — 従属 composition → Opportunity
# ---------------------------------------------------------------------------
# ms-107 e-3433 (B) / e-3374 の ID ハンドシェイク。予定確定側 (B) が生産し、
# 終了検知側 (C = e-3434) が消費する共有基盤。
#
# 予定を確定しても Beacon の遷移日 (= フェーズ達成を判定する予定日) と Google
# カレンダーが二重管理でズレる問題を、両者を 1 つの Meeting レコードで束ねて
# 解消する。Communication (不変・事後の証跡) と違い、Meeting は運用状態型 —
# scheduled → ended / cancelled と状態が動く。状態変化は history に追記して
# 監査可能にする (data-immutability-principle は「証跡は消さない」の意)。
#
# 識別 ID (mtg-N) がカレンダー予定の説明文に埋め込む handshake token になる。
# C はこの token でカレンダー予定 → 商談を突き合わせ、二重起動を status で防ぐ。

MEETING_UNSCHEDULED = "unscheduled"  # 予定未定: seeded ahead, awaiting a date (e-3548)
MEETING_SCHEDULED = "scheduled"
MEETING_ENDED = "ended"
MEETING_CANCELLED = "cancelled"
VALID_MEETING_STATUS = {MEETING_UNSCHEDULED, MEETING_SCHEDULED, MEETING_ENDED,
                        MEETING_CANCELLED}

# handshake token 書式。B が埋め込み、C が parse する — 両者がこの 1 箇所を
# 参照することで書式 drift を構造的に防ぐ (single source of truth)。
_MEETING_TAG_PREFIX = "beacon-meeting-id:"


def meeting_calendar_tag(meeting_id: str) -> str:
    """The handshake token embedded in a calendar event's description so the
    end-detector (C) can map the event back to this meeting/opportunity.
    B writes it, C parses it — both call this one helper (書式の単一真値源)."""
    return f"{_MEETING_TAG_PREFIX} {meeting_id}"


def parse_meeting_tag(text: str) -> Optional[str]:
    """Extract a meeting id (mtg-…) from a calendar event's description that was
    stamped by :func:`meeting_calendar_tag`, or None when absent."""
    if not text:
        return None
    marker = text.find(_MEETING_TAG_PREFIX)
    if marker < 0:
        return None
    rest = text[marker + len(_MEETING_TAG_PREFIX):].strip()
    token = rest.split()[0] if rest.split() else ""
    return token if token.startswith("mtg-") else None


def next_meeting_id(data: dict) -> str:
    ids = []
    for opp in data.get("opportunities", []):
        ids.extend(m.get("id", "") for m in opp.get("meetings", []))
    return _next_prefixed_id(ids, "mtg-")


def find_meeting(data: dict, meeting_id: str):
    """Return ``(opportunity, meeting)`` for a meeting id, or ``(None, None)``."""
    for opp in data.get("opportunities", []):
        for m in opp.get("meetings", []):
            if m.get("id") == meeting_id:
                return opp, m
    return None, None


def opportunity_meetings(opp: dict) -> list:
    """A target's meetings in scheduled-time order (earliest → latest); untimed
    records sort last, then insertion order (same rule as communications_of)."""
    meetings = (opp or {}).get("meetings", [])
    ordered = sorted(
        enumerate(meetings),
        key=lambda pair: (not (pair[1].get("scheduled_at") or ""),
                          pair[1].get("scheduled_at") or "", pair[0]),
    )
    return [m for _, m in ordered]


def _parse_dt(value: str):
    """Parse an ISO8601 datetime (offset or trailing Z) to an aware UTC
    datetime, or None when empty / date-only / unparseable. Meeting times carry
    a timezone offset, so string comparison is wrong (same instant, different
    offset sorts differently) — the end-detector must compare real instants."""
    if not value or not str(value).strip():
        return None
    import datetime
    txt = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:  # naive → assume UTC (best effort)
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def meeting_effective_end(meeting: dict):
    """When a meeting is considered over: its end_at, or (no end set) its
    scheduled_at treated as a point-in-time. Returns an aware UTC datetime or
    None when neither is a parseable timestamp."""
    return _parse_dt(meeting.get("end_at")) or _parse_dt(meeting.get("scheduled_at"))


def scan_ended_meetings(data: dict, now: str) -> list:
    """The end-detector's (C, e-3434) core: meetings whose scheduled end has
    passed but are still ``scheduled`` — candidates to confirm on the calendar
    and hand to the follow-up workflow (A). Pure and timezone-aware.

    Idempotent by status: a meeting already ``ended``/``cancelled`` drops out,
    so re-running the detector never fires the same meeting twice (SPEC AC:
    同じミーティングを二重起動しない). The engine still calls
    :func:`meeting_mark_ended` (also idempotent) to record the transition.

    Returns a list of ``(opportunity, meeting)`` for the ended candidates,
    oldest end first (so the earliest-finished meeting is handled first)."""
    now_dt = _parse_dt(now)
    if now_dt is None:
        return []
    hits = []
    for opp in data.get("opportunities", []):
        if work_model.is_cancelled(opp):  # e-3586: 取消済商談の面談は自動処理しない
            continue
        for m in opp.get("meetings", []):
            if m.get("status") != MEETING_SCHEDULED:
                continue
            end = meeting_effective_end(m)
            if end is not None and end <= now_dt:
                hits.append((opp, m, end))
    hits.sort(key=lambda t: t[2])
    return [(opp, m) for opp, m, _ in hits]


def meeting_schedule(data: dict, opportunity_id: str, scheduled_at: str, *,
                     end_at: str = "", location: str = "",
                     calendar_event_id: str = "", calendar_namespace: str = "",
                     calendar_account: str = "", set_transition: bool = False,
                     at: str = "") -> str:
    """Book a Meeting under an Opportunity and return its id (mtg-…).

    When ``set_transition`` is true the opportunity's 遷移日 (transition_date) is
    moved to the meeting date in the *same* call, so the calendar plan and the
    phase-judgement date never drift apart (AC: 遷移日とカレンダーが同時更新).
    The returned id is the handshake token B stamps into the calendar event."""
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    if not scheduled_at or not scheduled_at.strip():
        raise ValueError("Meeting scheduled_at is required")
    mtg_id = next_meeting_id(data)
    when = scheduled_at.strip()
    opp.setdefault("meetings", []).append({
        "id": mtg_id,
        "scheduled_at": when,
        "end_at": end_at,
        "location": location,
        "calendar_event_id": calendar_event_id,
        "calendar_namespace": calendar_namespace,
        "calendar_account": calendar_account,
        "status": MEETING_SCHEDULED,
        "linked_id": "",  # e-3548: set when a meeting fulfills a seeded anchor activity
        "created_at": at,
        "history": [{"at": at, "action": "scheduled", "scheduled_at": when}],
    })
    if set_transition:
        # meeting date drives the phase-judgement date (the open gate's 遷移日).
        # Store the date portion (YYYY-MM-DD). e-3581: firing off the anchored
        # meeting's *end* is gate_judgement_ready's job; this only pins the date.
        set_transition_date(data, opportunity_id, when[:10],
                            note=f"面談確定 ({mtg_id})", at=at)
        # e-3583 fix (dogfood #8): a set_transition meeting IS the phase's発火源,
        # so anchor the open gate to it here too — the seed path
        # (instantiate_phase_activities) only covers meetings seeded on phase
        # entry, leaving migrated / directly-scheduled gates unanchored.
        _anchor_open_gate_to_meeting(data, opportunity_id, mtg_id, at=at)
    return mtg_id


def meeting_seed(data: dict, opportunity_id: str, *, linked_id: str = "",
                 at: str = "") -> str:
    """Seed a 予定未定 (date-undecided) Meeting and return its id (mtg-…).

    Created when a phase seeds a meeting-type anchor activity (e-3548): the
    Meeting is made up-front with an empty ``scheduled_at`` and status
    ``unscheduled``, linked to the anchor activity via ``linked_id``. When the
    date is later confirmed the schedule Skill calls ``meeting_reschedule`` on
    *this same* record instead of creating a new one — so a confirmed meeting and
    a leftover "アポ確定" activity can no longer both exist (重複が構造的に不可能).

    An unscheduled Meeting has no date, so the end-detection Operation (which only
    looks at ``scheduled`` meetings) never fires on it prematurely."""
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    mtg_id = next_meeting_id(data)
    opp.setdefault("meetings", []).append({
        "id": mtg_id,
        "scheduled_at": "",
        "end_at": "",
        "location": "",
        "calendar_event_id": "",
        "calendar_namespace": "",
        "calendar_account": "",
        "status": MEETING_UNSCHEDULED,
        "linked_id": linked_id,
        "created_at": at,
        "history": [{"at": at, "action": "seeded", "linked_id": linked_id}],
    })
    return mtg_id


def find_meeting_by_linked(data: dict, activity_id: str):
    """Return ``(opportunity, meeting)`` for the Meeting whose ``linked_id`` is
    ``activity_id``, or ``(None, None)``. Lets the schedule Skill find the seeded
    予定未定 Meeting for an anchor activity so it updates (fills the date) rather
    than creating a duplicate (e-3548)."""
    for opp in data.get("opportunities", []):
        for m in opp.get("meetings", []) or []:
            if m.get("linked_id") == activity_id:
                return opp, m
    return None, None


def meeting_reschedule(data: dict, meeting_id: str, scheduled_at: str, *,
                       end_at: Optional[str] = None, location: Optional[str] = None,
                       calendar_event_id: Optional[str] = None,
                       calendar_namespace: Optional[str] = None,
                       calendar_account: Optional[str] = None,
                       set_transition: bool = False, at: str = "") -> dict:
    """Move an existing meeting to a new time (予定変更), keeping the same
    handshake id so the calendar event and Beacon stay linked. Logs the change
    to ``history`` and, when ``set_transition``, moves the 遷移日 too so both
    follow the reschedule (AC: 予定変更時も両者が追従).

    This is also the path that *confirms a seeded 予定未定 meeting* (e-3548): the
    unscheduled Meeting gets its date + calendar handshake (event id / namespace /
    account) filled in and flips to ``scheduled`` — updating the same record, so
    no duplicate meeting is created."""
    opp, m = find_meeting(data, meeting_id)
    if m is None:
        raise ValueError(f"Meeting not found: {meeting_id}")
    if not scheduled_at or not scheduled_at.strip():
        raise ValueError("Meeting scheduled_at is required")
    when = scheduled_at.strip()
    m["scheduled_at"] = when
    if end_at is not None:
        m["end_at"] = end_at
    if location is not None:
        m["location"] = location
    if calendar_event_id is not None:
        m["calendar_event_id"] = calendar_event_id
    if calendar_namespace is not None:
        m["calendar_namespace"] = calendar_namespace
    if calendar_account is not None:
        m["calendar_account"] = calendar_account
    m["status"] = MEETING_SCHEDULED  # revive an unscheduled/cancelled slot
    m.setdefault("history", []).append(
        {"at": at, "action": "rescheduled", "scheduled_at": when})
    if set_transition:
        set_transition_date(data, opp["id"], when[:10],
                            note=f"面談再調整 ({meeting_id})", at=at)
        # e-3583 fix (dogfood #8): the rescheduled meeting is the開火源, anchor it.
        _anchor_open_gate_to_meeting(data, opp["id"], meeting_id, at=at)
    return m


def meeting_mark_ended(data: dict, meeting_id: str, *, at: str = "") -> dict:
    """Mark a meeting ended (C calls this after detecting the calendar event
    finished). Idempotent: a meeting already ended stays ended and no duplicate
    history row is appended — this is the double-trigger guard for C's Operation
    (e-3434: 同じミーティングを二重起動しない)."""
    opp, m = find_meeting(data, meeting_id)
    if m is None:
        raise ValueError(f"Meeting not found: {meeting_id}")
    if m.get("status") == MEETING_ENDED:
        return m  # already ended — no-op, keeps C idempotent
    m["status"] = MEETING_ENDED
    m.setdefault("history", []).append({"at": at, "action": "ended"})
    return m


def meeting_cancel(data: dict, meeting_id: str, *, at: str = "",
                   reason: str = "") -> dict:
    """Cancel a scheduled meeting (予定取消). Logged to history; the calendar
    event removal is the Skill's job (this is the Beacon-side state change).

    ms-120 e-3906 danger-class: cancelling a meeting is destructive to the
    schedule/trail, so the CLI now requires an audit entry. ``reason`` is
    recorded on both the meeting and its history line so the cancellation is
    never a silent state flip."""
    opp, m = find_meeting(data, meeting_id)
    if m is None:
        raise ValueError(f"Meeting not found: {meeting_id}")
    m["status"] = MEETING_CANCELLED
    if reason:
        m["cancel_reason"] = reason
    m.setdefault("history", []).append(
        {"at": at, "action": "cancelled", "reason": reason})
    return m


# ---------------------------------------------------------------------------
# Send identity pin (ms-107 e-3353 — 複数 Google アカウントの取り違え防止)
# ---------------------------------------------------------------------------
# 会社用 / 個人用など複数の Google アカウントがあり、取り違え送信は顧客との
# 関係を壊す一発事故。プロジェクト (将来は商談単位でも) に「この identity で
# 送る」を pin し、送信系 Skill は送信前に必ず from を pin と照合する。ここは
# データと照合ロジックのみ (照合を呼ぶのは各送信 Skill の責務)。

SEND_IDENTITY_KEY = "send_identity"    # the *default* send label (or, legacy, a bare email)
SEND_ACCOUNTS_KEY = "send_accounts"    # 送信アカウント台帳: 自分の Google アカウント群

# Services the ledger routes to concrete MCP tools. Kept small and explicit —
# each送信/操作 Skill maps one of these to its MCP tool family. slack は
# namespace=workspace 切替 (gmail と同型、account 引数なし)。
SEND_SERVICES = ("gmail", "calendar", "drive", "slack")


def _norm(value) -> str:
    """Case/space-insensitive key for label/email matching."""
    return (value or "").strip().lower()


def get_send_identity(data: dict) -> str:
    """Return the default send label (or legacy bare email), or ''."""
    return data.get(SEND_IDENTITY_KEY, "") or ""


def set_send_identity(data: dict, identity: str) -> str:
    """Pin the *default* send label for this project. Returns the stored value.

    This is now a pointer into the ledger (``send_accounts``): the value should
    be a ledger *label*. For backward compat a bare email is still accepted —
    ``check_send_from`` falls back to a plain string compare when no ledger
    entry resolves for the value.
    """
    if not identity or not identity.strip():
        raise ValueError("send identity is required (email address or account label)")
    data[SEND_IDENTITY_KEY] = identity.strip()
    return data[SEND_IDENTITY_KEY]


# ---------------------------------------------------------------------------
# Send-account ledger (ms-107 e-3365 — label → {email, per-service MCP route})
# ---------------------------------------------------------------------------
# 「どのアカウントで送るか」を bare email から台帳に格上げする。bare email では
# ① calendar/drive は同一 namespace 内で account=alias 切替、gmail は namespace
# 切替、と service ごとに切替機構が非対称なこと、② 同じ人物が複数 namespace で
# 到達可能なこと、を表せない。台帳は label → {email, routes{service:{namespace,
# alias}}} を持ち、送信/操作 Skill は namespace を必ず ``resolve_route`` 経由で
# 引く。台帳を通らない送信経路が存在しない = 取り違えが物理的に起きない (SPEC §2)。
# 注意: ここでの "account" は自分の送信アカウント。顧客 ``accounts`` (Account
# 顧客エンティティ) とは別物なので別キー ``send_accounts`` に格納する。


def list_send_accounts(data: dict) -> list:
    """Return the send-account ledger (list of entries), possibly empty."""
    return list(data.get(SEND_ACCOUNTS_KEY, []) or [])


def get_send_account(data: dict, label_or_email: str):
    """Return the ledger entry matching a label OR email (case-insensitive), or None."""
    key = _norm(label_or_email)
    if not key:
        return None
    for a in list_send_accounts(data):
        if _norm(a.get("label")) == key or _norm(a.get("email")) == key:
            return a
    return None


def _clean_routes(routes) -> dict:
    """Keep only known services with a non-empty namespace; normalise shape."""
    out = {}
    for svc, r in (routes or {}).items():
        if svc not in SEND_SERVICES or not isinstance(r, dict):
            continue
        ns = (r.get("namespace") or "").strip()
        if not ns:
            continue
        entry = {"namespace": ns}
        alias = (r.get("alias") or "").strip()
        if alias:
            entry["alias"] = alias
        out[svc] = entry
    return out


def add_send_account(data: dict, label: str, email: str, routes=None) -> dict:
    """Add (or idempotently update) a send account in the ledger.

    Matching an existing label updates email/routes in place so a Skill can
    re-run without creating duplicates. Returns the stored entry.
    """
    label = (label or "").strip()
    email = (email or "").strip()
    if not label:
        raise ValueError("account label is required")
    if not email:
        raise ValueError("account email is required")
    ledger = data.setdefault(SEND_ACCOUNTS_KEY, [])
    for a in ledger:
        if _norm(a.get("label")) == _norm(label):
            a["email"] = email
            if routes is not None:
                a["routes"] = _clean_routes(routes)
            return a
    entry = {"label": label, "email": email, "routes": _clean_routes(routes or {})}
    ledger.append(entry)
    return entry


def set_account_route(data: dict, label: str, service: str,
                      namespace: str, alias: str = "") -> dict:
    """Set one service's MCP route (namespace [+ alias]) on a ledger entry."""
    if service not in SEND_SERVICES:
        raise ValueError(f"unknown service '{service}' (expected one of {list(SEND_SERVICES)})")
    a = get_send_account(data, label)
    if a is None:
        raise ValueError(f"send account not found: {label}")
    ns = (namespace or "").strip()
    if not ns:
        raise ValueError("namespace is required")
    route = {"namespace": ns}
    alias = (alias or "").strip()
    if alias:
        route["alias"] = alias
    a.setdefault("routes", {})[service] = route
    return route


def set_account_signature(data: dict, label: str, signature: str, *,
                          clear: bool = False) -> dict:
    """Set the mail signature on a send-account ledger entry (e-3529).

    署名は送信 identity ごとに違う (会社用 / 個人用 …) ので、台帳の各エントリに
    ``signature`` として持たせる。email skill が解決した identity の署名を本文末尾に
    付ける。中身は user が運用しながら育てる soft guidance で、システムは置き場と
    読み込みだけを持つ。

    #496 review (AX high): **空文字での暗黙 clear を禁止する**。以前は空 signature が
    署名を消していたため、渡し忘れ/typo/quoting ミス (= 最頻の失敗) が『設定するつもり』
    で既存署名を silent に消していた。今は:
    - ``clear=True`` → 署名を消す (key ごと省いて前方互換)。**消すのは明示意図のときだけ**。
    - 非空 ``signature`` → 設定 (trim)。
    - 空 ``signature`` かつ ``clear`` でない → ``ValueError`` (既存署名は保持)。

    #496 再レビュー (AX high): **clear と設定値の共存を拒否**。``clear=True`` かつ非空
    ``signature`` → ``ValueError``。stale な clear フラグ (前回 export した CLEAR=1 が次の
    呼び出しに残る) が『set のつもりを delete に化けさせる』経路を塞ぐ。clear と設定は
    どちらか一方。"""
    a = get_send_account(data, label)
    if a is None:
        raise ValueError(f"send account not found: {label}")
    sig = (signature or "").strip()
    if clear:
        if sig:
            raise ValueError(
                "clear と署名の値が同時に渡されています — どちらか一方にしてください "
                "(clear は署名を消すだけ、設定は clear なしで)")
        a.pop("signature", None)
        return a
    if not sig:
        raise ValueError(
            "signature が空です — 設定するなら値を渡してください。"
            "署名を消すのは明示 clear のときだけです (空文字では消しません)")
    a["signature"] = sig
    return a


def remove_send_account(data: dict, label: str) -> None:
    """Remove a ledger entry by label (or email). Raises if not found."""
    key = _norm(label)
    ledger = list_send_accounts(data)
    kept = [a for a in ledger
            if _norm(a.get("label")) != key and _norm(a.get("email")) != key]
    if len(kept) == len(ledger):
        raise ValueError(f"send account not found: {label}")
    data[SEND_ACCOUNTS_KEY] = kept


def resolve_route(data: dict, service: str, label: str = "") -> Optional[dict]:
    """Resolve the concrete MCP routing for ``service`` for a given label
    (or the default send label when ``label`` is empty).

    Returns ``{label, email, service, namespace, alias, signature}`` or ``None``
    when the account or its route for that service is not configured. **This is
    the only sanctioned way a send/操作 Skill obtains a namespace** — a Skill must
    never free-hand one (that would reopen the取り違え hole, SPEC §2). ``signature``
    (e-3529) is the identity's mail signature ("" when unset) so the email skill
    can append it without a second lookup.
    """
    if service not in SEND_SERVICES:
        raise ValueError(f"unknown service '{service}' (expected one of {list(SEND_SERVICES)})")
    target = label.strip() if (label and label.strip()) else get_send_identity(data)
    a = get_send_account(data, target)
    if a is None:
        return None
    route = (a.get("routes") or {}).get(service)
    if not route or not route.get("namespace"):
        return None
    return {
        "label": a.get("label"),
        "email": a.get("email"),
        "service": service,
        "namespace": route.get("namespace"),
        "alias": route.get("alias"),
        "signature": a.get("signature", ""),
    }


class PermalinkIdentityMissing(ValueError):
    """``build_gmail_permalink`` was called with an empty ``from_addr`` (e-4185).

    This is a **wiring bug**, not a legitimate no-link: the send identity is
    always resolved (email skill Step 2) before an email trace is recorded, so an
    empty value means the caller failed to thread ``$FROM`` through. It must be
    surfaced (the command layer exits non-zero) rather than folded into the silent
    no-link path — otherwise a mis-wired caller would drop permalinks forever with
    no signal."""


def build_gmail_permalink(from_addr: str, message_id: str) -> str:
    """Canonical builder for a followable Gmail permalink from a sent mail's
    rfc822 Message-ID (e-3542, PR #495 review fix).

    **This is the single source of truth** for the permalink recipe — the email
    skill passes only ``from_addr`` (resolved send identity) and ``message_id``
    and never hand-assembles the URL in prose (which produced broken/ambiguous
    links). The UI's ``_salesRefToHref`` (server/static/index.html) is the legacy
    ref-only fallback for records that predate a stored ``source.url``; keep the
    two algorithmically consistent (strip surrounding ``<>`` + URL-encode the
    ``rfc822msgid:…`` query).

    e-4185 — the three "cannot build" causes are **not** collapsed into one silent
    empty return; they are told apart so a wiring bug cannot hide:
    - ``from_addr`` empty → **raises ``PermalinkIdentityMissing``** (wiring bug;
      an empty value would yield a broken ``u//`` URL, review AX-high). The caller
      must surface it, not swallow it.
    - ``message_id`` empty, or not an rfc822 Message-ID (no ``@`` — e.g. a Gmail
      thread-id / API id, which would 0-hit an rfc822msgid search, review
      AX-medium) → returns ``""`` (a *legitimate* no-link; caller records the ref
      only). Mirrors the UI's ``@`` guard.

    Form: ``https://mail.google.com/mail/u/<from_addr>/#search/<enc>`` where
    ``<from_addr>`` (the identity email, not a login-order ``u/0`` index) makes
    Gmail open the right mailbox across accounts, and ``<enc>`` is the
    URL-encoded ``rfc822msgid:<message-id-sans-brackets>``.
    """
    frm = (from_addr or "").strip()
    if not frm:
        raise PermalinkIdentityMissing(
            "from_addr (送信 identity) が空です — 送信元は証跡記録前に台帳解決済みのはず。"
            "呼び出し側の配線ミスの可能性があります")
    mid = (message_id or "").strip().strip("<>").strip()
    if not mid or "@" not in mid:
        return ""
    from urllib.parse import quote
    query = quote("rfc822msgid:" + mid, safe="")
    return f"https://mail.google.com/mail/u/{frm}/#search/{query}"


def check_send_from(data: dict, from_value: str, label: str = "") -> tuple:
    """Compare a proposed send ``from`` against the resolved send identity.

    Returns ``(ok, message)``. The send Skill calls this immediately before
    sending and surfaces the message; on ``ok == False`` it must stop and let
    the human resolve (取り違え防止は仕組みで閉じる、SPEC §2)。

    Resolution order:
    * ``label`` (or the default send label) resolves to a *ledger entry* →
      compare ``from`` against that entry's **email** (the台帳 is authoritative).
    * No ledger entry resolves → legacy bare-string pin compare (back-compat).

    Branches (either path):
    * Nothing pinned/configured → ok=False, ask to pin/register first.
    * from empty                → ok=False, from must be explicit.
    * matches (case/space-insensitive) → ok=True.
    * mismatch                  → ok=False, name both so the human sees it.
    """
    target = label.strip() if (label and label.strip()) else get_send_identity(data)
    entry = get_send_account(data, target) if target else None
    if entry is not None:
        expected = entry.get("email", "")
        who = entry.get("label", "")
        if not from_value or not from_value.strip():
            return (False, f"from が空です。台帳 '{who}' の identity は '{expected}'。"
                           "from を明示してください")
        if _norm(from_value) == _norm(expected):
            return (True, f"from='{from_value}' は台帳 '{who}' ({expected}) と一致")
        return (False, f"from='{from_value}' が台帳 '{who}' ({expected}) と"
                       "一致しません。取り違えの恐れ。送信を止めます")
    # Legacy path: bare-string pin (no ledger entry for the pinned value).
    pinned = get_send_identity(data)
    if not pinned:
        return (False, "送信 identity が未設定です。先に pin してください "
                       "(取り違え防止のため、from を明示せず送信しません)")
    if not from_value or not from_value.strip():
        return (False, f"from が空です。pin された identity は '{pinned}'。"
                       "from を明示してください")
    if from_value.strip().lower() == pinned.strip().lower():
        return (True, f"from='{from_value}' は pin された identity と一致")
    return (False, f"from='{from_value}' が pin された identity '{pinned}' と"
                   "一致しません。取り違えの恐れ。送信を止めます")


# ---------------------------------------------------------------------------
# Sales project template
# ---------------------------------------------------------------------------

def build_sales_project(name: str, objective: str, *, retro_day: str = "monday",
                        disclosure_policy: Optional[dict] = None) -> dict:
    """Return a fresh sales-profession project.json dict.

    Seeds the per-company phase funnels (``account_phases`` /
    ``opportunity_phases``) with the first-user default; these are editable
    config, not constants. Carries an empty ``milestones: []`` so the shared
    ``core.validate_project`` passes unchanged.
    """
    data = {
        "name": name,
        "objective": objective,
        "profession": "sales",
        "milestones": [],
        "opportunities": [],
        "accounts": [],
        # ms-115 e-3786: 顧客獲得ターゲット (取引先の無い有限の獲得・準備作業の器)。
        "acquisitions": [],
        "account_phases": [dict(p) for p in DEFAULT_ACCOUNT_PHASES],
        "opportunity_phases": [dict(p) for p in DEFAULT_OPPORTUNITY_PHASES],
        # ms-132 e-4502: attack-list の打診フェーズ funnel (未接触→連絡済→返信あり→アポ)。
        "prospect_phases": default_prospect_phase_defs(),
        # e-3582: 前進の macro-frame を config に seed する (企業ごと編集可)。AI が
        # フェーズを読むとき「フェーズ = 次へ抜けさせるもの」と理解する枠組み。
        "opportunity_phase_frame": OPPORTUNITY_PHASE_MACRO_FRAME,
        "retro_day": retro_day,
    }
    if disclosure_policy is not None:
        data["disclosure_policy"] = disclosure_policy
    return data


# ---------------------------------------------------------------------------
# 顧客獲得ターゲット (Acquisition) — ms-115 e-3786.
#
# 取引先の無い有限の獲得・準備作業 (X アカウント作成・自社サイト整備・打診 DM 文面・
# リスト精査・獲得施策 等) の器。営業が own する target-class で、開発の Milestone と
# 構造は同型 (finite・取引先なし・成約/失注なし) だが sales 所有なので dev の
# Milestone は借りない (職種 ⊃ 対象 の封じ込め)。
#
# 方針 (ms-115 SPEC 方針2 / ms-132 e-4507): opportunity のようなフェーズ funnel と
# 成約/失注 は持たず、素直な標準ライフサイクル (todo → in_progress → done) だけ持つ。
# 打ち切り (中止) は status でなく削除 (soft-cancel = acquisition_cancel) で表す
# ので、observing のような中間の「見守り」状態は置かない (ms-132 e-4507)。目標
# (例「20 社アタック → 5 社アポ」) は description の散文で表す。
# ---------------------------------------------------------------------------

# 標準ライフサイクル (開発 Milestone と同じ語彙)。フェーズ funnel ではない。
ACQUISITION_STATUSES = ("todo", "in_progress", "done")


def next_acquisition_id(data: dict) -> str:
    ids = [a.get("id", "") for a in data.get("acquisitions", [])]
    return _next_prefixed_id(ids, "acq-")


def find_acquisition(data: dict, acquisition_id: str) -> Optional[dict]:
    """Return the acquisition target for an id, or None."""
    for a in data.get("acquisitions", []):
        if a.get("id") == acquisition_id:
            return a
    return None


def acquisition_add(data: dict, title: str, *, description: str = "",
                    assignee: str = "", created_at: str = "",
                    created_by: str = "") -> str:
    """Append a 顧客獲得ターゲット (Acquisition) and return its id.

    Routes through ``work_model.new_target`` — the acquisition is a clean finite
    target that fits the generic skeleton (id / label / status / created_at /
    created_by / assignee), unlike Opportunity/Account whose status is
    phase-derived. Seeds ``description`` (the goal is prose here, not a field)
    and an inline ``work_items`` list (empty at creation; sub-items are a later
    step). Starts in ``todo``.
    """
    if not title or not title.strip():
        raise ValueError("Acquisition title is required")
    acq_id = next_acquisition_id(data)
    acq = work_model.new_target(
        acq_id, title.strip(), created_at=created_at, created_by=created_by,
        assignee=assignee, description=(description or "").strip(),
        work_items=[])
    data.setdefault("acquisitions", []).append(acq)
    return acq_id


def acquisition_set_status(data: dict, acquisition_id: str, status: str, *,
                           at: str = "") -> dict:
    """Move an Acquisition along its standard lifecycle (todo → in_progress →
    done) and return it. Occupation-agnostic target lifecycle — no phase funnel,
    no won/lost (ms-115 方針2), and no observing (ms-132 e-4507: 打ち切りは削除で表す)."""
    acq = find_acquisition(data, acquisition_id)
    if acq is None:
        raise ValueError(f"Acquisition not found: {acquisition_id}")
    if status not in ACQUISITION_STATUSES:
        # ms-132 e-4507 (AX misleading の是正): observing は廃止されたので、旧
        # 「様子見」相当を打とうとした利用者に正しい回復経路を名指しする。打ち切りは
        # status でなく削除 (soft-cancel) で表す — start/done では代替できない。
        hint = ""
        if status == "observing":
            hint = (" — observing は廃止されました。打ち切り (discontinuation) は "
                    "`beacon acquisition delete <acq-id>` を使ってください")
        raise ValueError(
            f"status must be one of {list(ACQUISITION_STATUSES)}, got {status!r}{hint}")
    # ms-120 e-3908: guard the from→to transition via the shared lifecycle
    # mechanism (illegal jumps like done→todo raise). Lazy import avoids a
    # module-load cycle. All named verbs (start/done) flow through here.
    import core
    core.validate_lifecycle_transition("acquisition", acq.get("status", ""), status)
    if status == work_model.DONE_STATUS:
        work_model.mark_done(acq, at=at, actor=work_base.current_actor())
    else:
        acq["status"] = status
    return acq


def acquisition_cancel(data: dict, acquisition_id: str, *, reason: str = "",
                       actor: str = "", at: str = "") -> dict:
    """Soft-cancel (打ち切り) an Acquisition and return it (ms-132 e-4507).

    Discontinuing a施策 is expressed as *deletion*, not a lifecycle status: the
    record is tombstoned via ``work_base.stamp_cancel`` (status → cancelled, with
    reason/actor/at) so the audit trail survives (data-immutability原則) and it
    drops out of the active list. An Acquisition is a standalone target (no live
    references to orphan, unlike an Account), so no force flag is needed."""
    acq = find_acquisition(data, acquisition_id)
    if acq is None:
        raise ValueError(f"Acquisition not found: {acquisition_id}")
    work_base.stamp_cancel(acq, reason=reason, actor=actor or work_base.current_actor(),
                           at=at)
    return acq


def backfill_target_labels(data: dict) -> int:
    """Backfill the canonical ``label`` on every sales Target — account and
    opportunity — that lacks it, copying from the legacy key (``name`` for an
    account, ``title`` for an opportunity). Returns the number of records newly
    stamped. Idempotent.

    Sales half of the ms-109 migrate step (e-3625); the development half is
    ``core.backfill_target_labels``. Per-record stamping is delegated to
    ``work_model.ensure_target_label`` so the occupation-agnostic base owns the
    definition of a canonical label.
    """
    n = 0
    for acc in data.get("accounts", []):
        if work_model.ensure_target_label(acc):
            n += 1
    for opp in data.get("opportunities", []):
        if work_model.ensure_target_label(opp):
            n += 1
    return n


def project_targets(data: dict) -> list:
    """③ shared-frame projection (ms-108 e-3269): map this sales project's
    Targets (Opportunities) into the same occupation-agnostic shape the
    development adapter (``core.project_targets``) emits, so the shared frame
    (status / session-start) can render either occupation without knowing
    which one it is looking at.

    The generic core is identical to development — ``id`` / ``label`` (via the
    tolerant ``work_model.target_label`` reader) / ``status`` / ``kind`` plus
    the WorkItem completion counts (here WorkItems are the Opportunity's
    activities/活動). Sales semantics — phase (フェーズ) / who_has_the_ball
    (誰にボールがあるか) / goal_amount (目標金額) / probability (成約率) /
    deadline / account link — live only in ``detail`` (SPEC AC4: the base stays
    occupation-neutral).

    Cancelled (取消) Opportunities are excluded, matching the default view.
    """
    targets = []
    for opp in data.get("opportunities", []):
        if opp.get("status") == work_model.CANCELLED_STATUS:
            continue
        activities = opp.get("activities", [])
        total = len(activities)
        done = sum(1 for a in activities
                   if a.get("status") == work_model.DONE_STATUS)
        targets.append({
            "id": opp.get("id", ""),
            "label": work_model.target_label(opp),
            "status": opp.get("status", ""),
            "kind": "opportunity",
            "work_items_total": total,
            "work_items_done": done,
            "detail": {
                "phase": opp.get("phase", ""),
                "who_has_the_ball": opp.get("who_has_the_ball", ""),
                "goal_amount": opp.get("goal_amount"),
                "probability": opp.get("probability"),
                "deadline": opp.get("deadline", ""),
                "account_id": opp.get("account_id"),
            },
        })
    # ms-115 e-3786: 顧客獲得ターゲット (Acquisition) を商談とは別レーンとして同じ
    # 投影 shape で並べる。phase funnel を持たないので detail は目標 (description) のみ。
    # 進捗は標準ライフサイクル (status) で表し、work_items があればその消化数も出す。
    for acq in data.get("acquisitions", []):
        if acq.get("status") == work_model.CANCELLED_STATUS:
            continue
        items = acq.get("work_items", [])
        total = len(items)
        done = sum(1 for w in items
                   if w.get("status") == work_model.DONE_STATUS)
        targets.append({
            "id": acq.get("id", ""),
            "label": work_model.target_label(acq),
            "status": acq.get("status", ""),
            "kind": "acquisition",
            "work_items_total": total,
            "work_items_done": done,
            "detail": {"description": acq.get("description", "")},
        })
    return targets
