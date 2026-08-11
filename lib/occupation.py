"""Beacon occupation adapter registry — the single ③ shared-frame dispatch
point (ms-108 e-3269).

The shared frame (③ = session-start / status / operation) has a skeleton that
is occupation-invariant ("what am I working on, what's the next move"), but the
*thing* it projects changes per occupation: development drives Milestones/Tasks,
sales drives Opportunities/Activities. Before this module those two projections
were selected by ``if profession == "sales"`` branches scattered across
``commands.py`` (~40 sites). This registry replaces that scatter with ONE
dispatch: each occupation contributes a ``project_targets(data)`` adapter, and
the frame asks the registry for the projection without knowing which occupation
it is looking at.

Import layering: ``core`` (development) and ``sales_entities`` (sales) are the
occupation adapters; both depend on ``work_model`` (the occupation-agnostic
canonical Target/WorkItem accessors). This module sits ABOVE both adapters and
is imported by ``commands.py``. Nothing the adapters import reaches back here,
so there is no import cycle.

Only ③ shared-frame surfaces use this. L1 (occupation-invariant: DM / Trek /
auth / doc / session) needs no adapter, and pure L2/L3 surfaces (development
task/milestone, sales opportunity/activity) call their own occupation's code
directly. See SPEC ``XOaDpSaFITVkZKKgPvPT`` 設計方針 4 / 6 and the reuse map
``E42bCsD7eQSrtGWX0JOF``.

⚠ TERMINOLOGY: the "L1 / L2 / L3" labels in THIS docstring are the OLDER
occupation-coupling scheme (L1 = occupation-invariant, L2/L3 = occupation-
specific). They are a DIFFERENT scheme from ms-134's L0..L4 *sharing-scope*
ledger in ``capability_ledger.py`` — do not conflate. Notably ``doc`` is "L1
occupation-invariant" here but "L2 class-abstraction" under the ms-134 scheme
(that finer distinction is what caught the milestone leak e-4720). See CORE doc
``37Svg6nD2FccJM27yBjq`` and ``capability_ledger.py``'s terminology note.
"""

from __future__ import annotations

import core
import sales_entities
import work_base
import work_model as _wm
import target_descriptor as _td   # ms-122 e-3957: data 定義 target-class 記述子
import target_engine as _te       # ms-122 e-3957: 記述子駆動 target の投影
import target_state as _tstate    # ms-142 e-5157: 宣言的 state model + phase_ball 導出


DEFAULT_PROFESSION = "dev"


# ---------------------------------------------------------------------------
# Descriptor-derived registry augmentation (ms-122 e-3957).
#
# The six registries below (PROJECTION_ADAPTERS / OWNED_TARGET_CLASSES /
# TARGET_COLLECTIONS / TARGET_DECOMPOSITION / NARROWING_KINDS /
# NARROWING_ID_PREFIXES) each hardcode the two built-in occupations (dev /
# sales). Before ms-122, adding a third occupation meant editing all six. Now a
# data-defined target-class (a descriptor under project.json ``target_classes``)
# contributes to each registry at read time: the accessor functions MERGE the
# built-in seed with descriptor-derived entries computed from ``data``. dev /
# sales behaviour is unchanged (a project with no descriptors merges nothing),
# and a new occupation (e.g. back-office: 契約 / 評価 / 月次決算) becomes visible
# in the shared frame + storage registries WITHOUT editing this file.
#
# Scope note (honest limitation, not silent): the shared-frame path
# (project_targets / iter_target_records / owned_target_classes) and the
# storage decomposition / narrowing LOOKUPS are descriptor-aware here. The
# server MySQL child-table DDL (server/mysql_client.py) and Trek scope
# narrowing (trek.py) still consult the built-in seed at import time; wiring
# THOSE to descriptors for a new occupation is a follow-up (descriptor targets
# ride additively in the document store meanwhile, per the compat contract).
# ---------------------------------------------------------------------------

def _descriptors_owned_by(data: dict, profession: str) -> list:
    """Return the well-formed descriptors whose ``profession`` matches (lower-
    cased). Empty for a dev / sales project (they declare no descriptors)."""
    want = (profession or "").strip().lower()
    out = []
    for desc in _td.load_descriptors(data):
        if isinstance(desc, dict) \
                and (desc.get("profession") or "").strip().lower() == want:
            out.append(desc)
    return out


def resolve_profession(data: dict) -> str:
    """Return the project's profession (e.g. ``"dev"`` / ``"sales"``),
    normalised to lower case. Missing / blank defaults to ``"dev"`` so legacy
    projects (written before the profession field existed) keep the
    development projection."""
    return (data.get("profession") or DEFAULT_PROFESSION).strip().lower() \
        or DEFAULT_PROFESSION


# The registry: profession -> the adapter's Target projection. Adding a new
# occupation means adding one entry here plus its ``project_targets`` adapter,
# with no change to the shared-frame callers.
PROJECTION_ADAPTERS = {
    "dev": core.project_targets,
    "sales": sales_entities.project_targets,
}


def project_targets(data: dict) -> list:
    """Return the project's Targets in the occupation-agnostic shape the shared
    frame consumes, dispatched by profession, PLUS any data-defined target-class
    instances owned by that profession (ms-122 e-3957).

    A dev / sales project resolves to its built-in adapter exactly as before. A
    profession whose targets are descriptor-defined (e.g. back-office) has no
    built-in adapter, so its rows come entirely from the descriptor projection.
    An unknown profession with no descriptors still falls back to the
    development projection (fail-open: show *something*). Item shape matches
    ``core.project_targets`` (id / label / status / kind / work_items_* /
    detail)."""
    prof = resolve_profession(data)
    rows: list = []
    adapter = PROJECTION_ADAPTERS.get(prof)
    if adapter is not None:
        rows.extend(adapter(data))
    for desc in _descriptors_owned_by(data, prof):
        for rec in _te.list_targets(data, desc):
            if _wm.is_cancelled(rec):   # match the default status view
                continue
            rows.append(_te.project_target(desc, rec))
    if not rows and adapter is None:
        rows = core.project_targets(data)
    return rows


# ---------------------------------------------------------------------------
# Target entry recording — the class-abstraction (L2) side-effect seam
# (ms-134 e-4720).
#
# A profession-SHARED capability (a document create/update) needs to record
# "something happened to this Target" without knowing whether the Target is a
# development milestone or a sales opportunity. Before this, the doc write paths
# called ``core.save_entry(ms_id=…)`` directly — the DEV concrete — which auto-
# picks the active milestone and RAISES "No active milestone" in a project that
# has none (any sales project). So a customer-document write succeeded but the
# command exited non-zero (bug e-4710), and the same raised error shadowed
# ``--account`` linkage (e-4711). The doc capability was designed shared but its
# recording subsystem was hardcoded dev-specific — an abstraction-boundary leak.
#
# ``record_target_entry`` is that missing seam: it dispatches by the Target's
# KIND (derived from its id prefix via ``work_model.target_kind``) and, crucially,
# NO-OPS when there is nothing to record onto — a Target class with no dev-era
# changelog (opportunity / account / acquisition / trek) or a project with no
# milestone at all. It never forces a milestone into existence. Development
# behaviour is unchanged (1 active milestone → recorded exactly as before; a
# bad explicit id or an ambiguous multi-active project still errors through
# ``core.resolve_recordable_milestone`` → ``find_target_milestone``).
#
# This is the ONE place allowed to reach the dev concrete (``core.save_entry``)
# on behalf of a shared capability; the shared callers depend on THIS abstraction
# instead. ``scripts/check-capability-scope.py`` enforces that no L1/L2 capability
# calls the dev concrete directly.
# ---------------------------------------------------------------------------

def _record_operation_entry(data: dict, op_id: str, *, description: str,
                            source: str, date: str, revision_id: str) -> dict:
    """Append a ``save`` entry to operation ``op_id``'s entries, matching the
    shape ``core.save_entry`` produces for a milestone. Returns a result dict;
    ``{"recorded": False, "reason": "operation-not-found"}`` when the id is
    unknown (the doc still wrote; only the side-effect log is skipped)."""
    now = date or work_base.now_iso()
    for op in data.get("operations", []):
        if op.get("id") == op_id:
            meta = {"source": source}
            if revision_id:
                meta["revision_id"] = revision_id
            op.setdefault("entries", []).append({
                "id": core.next_entry_id(data),
                "type": "save",
                "description": description,
                "status": "done",
                "created_at": now,
                "done_at": now,
                "meta": meta,
            })
            return {"recorded": True, "target": op_id}
    return {"recorded": False, "reason": "operation-not-found"}


def record_target_entry(data: dict, target_id: str = "", *, description: str,
                        source: str = "auto", date: str = "",
                        revision_id: str = "", url: str = "", hash: str = "",
                        progress: str = "") -> dict:
    """Record a side-effect changelog entry against a Target, dispatched by the
    Target's kind, profession-agnostically (ms-134 e-4720).

    Behaviour by target kind (derived from ``target_id``'s prefix):
      - empty ``target_id`` → record onto the project's single active milestone
        if one exists (development's historical auto-pick), else NO-OP (a project
        with no milestone — e.g. sales — records nothing rather than erroring).
      - ``operation`` → append a ``save`` entry to that operation's entries.
      - ``milestone`` → record onto that milestone via ``core.save_entry``.
      - ANY OTHER kind — a sales Target (opportunity / account / acquisition), a
        trek, or an unrecognised / descriptor-defined prefix — → NO-OP. It never
        falls through to the active milestone, so a doc explicitly linked to a
        non-dev target never silently records onto a different one.

    Returns ``{"recorded": bool, ...}``. NEVER raises for the "no milestone to
    record onto" case; a bad explicit id / multi-active ambiguity still raises
    through ``core.resolve_recordable_milestone`` (those are real user errors).
    The caller decides whether to persist based on ``recorded``.
    """
    # No explicit target → development's historical auto-pick onto the single
    # active milestone. Records only if one exists; a milestone-less project
    # (sales / back-office) no-ops (the structural fix for e-4710).
    if not target_id:
        if core.resolve_recordable_milestone(data, "") is None:
            return {"recorded": False, "reason": "no-milestone"}
        result = core.save_entry(data, ms_id="", description=description,
                                 source=source, date=date, url=url,
                                 revision_id=revision_id, hash=hash,
                                 progress=progress)
        return {"recorded": True, "target": result.get("milestone", ""),
                "result": result}
    kind = _wm.target_kind(target_id)
    if kind == "operation":
        return _record_operation_entry(data, target_id, description=description,
                                       source=source, date=date,
                                       revision_id=revision_id)
    if kind == "milestone":
        if core.resolve_recordable_milestone(data, target_id) is None:
            return {"recorded": False, "reason": "no-milestone"}
        result = core.save_entry(data, ms_id=target_id, description=description,
                                 source=source, date=date, url=url,
                                 revision_id=revision_id, hash=hash,
                                 progress=progress)
        return {"recorded": True, "target": result.get("milestone", target_id),
                "result": result}
    # Any OTHER kind — a sales Target (opportunity / account / acquisition), a
    # trek, OR an unrecognised / descriptor-defined prefix (ms-122 data-defined
    # target-class) — has no dev-era changelog to record onto. Crucially this
    # NEVER falls through to the active milestone: a doc EXPLICITLY linked to some
    # target must not silently record onto a *different* one. So a new descriptor
    # Target class is safe-by-default (no-op) without having to be enumerated
    # anywhere (maintainability review 2026-08-02, Maint#2).
    return {"recorded": False, "reason": f"{kind or 'unknown'}-no-changelog"}


# Target classes whose existence is hard-validated before a shared capability
# (doc) links to them: a sales account / opportunity / acquisition id must refer
# to a real record. Dev targets (milestone / operation), trek, and unknown /
# descriptor-defined prefixes keep the lenient round-trip. Located HERE (the
# occupation dispatch layer, which is allowed to know sales collections) so a
# profession-SHARED capability validates a link target WITHOUT branching on sales
# collections itself (ms-134, philosophy review 2026-08-02 #1).
_HARD_VALIDATED_COLLECTION = {
    "account": "accounts",
    "opportunity": "opportunities",
    "acquisition": "acquisitions",
}


def is_valid_link_target(data: dict, target_id: str) -> bool:
    """True when ``target_id`` is SAFE to link a doc to — profession-agnostic
    (ms-134; named for its contract, not "does it exist", per AX review
    2026-08-02 #1).

    Returns ``True`` when the target exists OR when its kind is not hard-validated
    (a dev milestone / operation, a trek, or an unknown / descriptor-defined
    prefix → lenient pass, matching the pre-ms-134 round-trip). Returns ``False``
    ONLY for a hard-validated sales class (account / opportunity / acquisition)
    whose id has no record. NOTE the lenient side: a non-existent ``ms-999`` returns
    ``True`` (dev is not hard-validated here) — this is a "safe to link" check, not
    a general existence check. This is the seam that lets a profession-SHARED (L2)
    capability such as ``doc`` validate a link target without reaching into sales
    collections directly."""
    kind = _wm.target_kind(target_id or "")
    coll = _HARD_VALIDATED_COLLECTION.get(kind)
    if not coll:
        return True
    return (target_id or "") in {x.get("id") for x in data.get(coll, [])}


# ---------------------------------------------------------------------------
# Onboarding plan — WHAT init asks + the ROLE of the project's objective/vision,
# per occupation (ms-133 e-4648 / e-4408).
#
# The front-door problem: `beacon init --profession sales` used to run the
# DEVELOPMENT onboarding (大目的 / ターゲット / やらないこと) because the
# /beacon-init + /beacon-vision Skills hardcoded the dev questions. Every
# occupation HAS a "why / where are we headed" (it is ③ shared-frame), but WHAT
# you ask and what the answer is FOR differs. Encoding that difference as
# `if profession == "sales"` inside the Skill markdown is exactly what the SPEC
# review flagged (high#1): a reviewer cannot structurally trust the Skill, and
# the next occupation re-implements the branch. So the plan is emitted HERE from
# the occupation and the Skill RENDERS it verbatim (CLI/lib decides, Skill draws).
#
# A plan is a dict:
#   vision_role : one line — what the project's objective/vision MEANS for this
#                 occupation. /beacon-vision uses it to frame its deep-dive so a
#                 sales project isn't asked to write a product vision.
#   ask         : ordered onboarding fields the Skill collects at init, each
#                 {"key", "label", "help", "required"}. `objective` is shared
#                 (every occupation needs a north star) but its label/help is
#                 occupation-specific; occupation-only fields ride after it.
#   next_hint   : the first real action after init (mirrors cmd_init's "Next:").
#
# dev's plan reproduces the existing dev onboarding verbatim (AC2: dev init
# 不変). Adding an occupation = adding one entry here (or, for a data-defined
# occupation, the GENERIC fallback below already yields a sane render-only plan
# with no code change — same descriptor-fallback contract as project_targets).

_ONBOARDING_PLANS: dict = {
    "dev": {
        "vision_role": "何を作り、誰をどんな状態にするか — プロダクトの北極星"
                       "（セッションをまたいで判断の基準になるゴール宣言）",
        "ask": [
            {
                "key": "objective",
                "label": "プロジェクトの大目的",
                "help": "このプロジェクトで最終的に何を実現したいですか？"
                        "「何を作るか」ではなく「誰がどんな状態になるか」で。",
                "required": True,
            },
            {
                "key": "target",
                "label": "ターゲット（任意）",
                "help": "誰のためのプロダクトか。空でも OK、後で /beacon-vision で深掘りできます。",
                "required": False,
            },
            {
                "key": "non_goals",
                "label": "やらないこと（任意）",
                "help": "スコープ外を先に決めておくと迷子になりにくい。空でも OK。",
                "required": False,
            },
        ],
        "next_hint": "beacon milestone add",
    },
    "sales": {
        "vision_role": "営業の狙い — 対象顧客・注力領域・達成したい成果"
                       "（商談を前に進める判断の基準）",
        "ask": [
            {
                "key": "objective",
                "label": "この営業活動のゴール",
                "help": "何を達成したいですか？（例: 対象市場での売上・獲得件数・"
                        "開拓したい顧客層）。プロダクトの作り込みではなく営業成果で。",
                "required": True,
            },
            {
                "key": "focus",
                "label": "注力領域（任意）",
                "help": "どの顧客層・商材・地域に注力するか。空でも OK。",
                "required": False,
            },
        ],
        "next_hint": "beacon account add / beacon opportunity add",
    },
    "backoffice": {
        "vision_role": "担当業務の範囲と目的 — 何を回し、何を守るか",
        "ask": [
            {
                "key": "objective",
                "label": "この業務の目的",
                "help": "担当する業務で何を成立させたいか（例: 契約・評価・月次決算を"
                        "滞りなく回す）。プロダクト開発ではなく業務運営の言葉で。",
                "required": True,
            },
            {
                "key": "scope",
                "label": "対象業務（任意）",
                "help": "扱う業務領域（契約 / 評価 / 月次決算 / 勤怠 等）。空でも OK。",
                "required": False,
            },
        ],
        "next_hint": "beacon target create --class <種類> --label <名前>",
    },
}

# The plan a data-defined occupation (legal / hr / …) gets when it has no
# built-in entry: a single occupation-neutral objective + a generic vision role.
# This keeps the front door working for occupations added purely by descriptor
# (no Beacon code change), matching project_targets' fail-open contract.
_GENERIC_ONBOARDING_PLAN: dict = {
    "vision_role": "この職種の目的 — 何を前に進めるための場か",
    "ask": [
        {
            "key": "objective",
            "label": "この職種のゴール",
            "help": "この職種で何を達成したいか（あなたの言葉で 1 行）。",
            "required": True,
        },
    ],
    "next_hint": "beacon target-class add",
}


def onboarding_plan(profession: str) -> dict:
    """Return the onboarding plan for ``profession`` (WHAT init asks + the role
    of the project's objective/vision).

    Built-in occupations (dev / sales / backoffice) return their curated plan;
    any other (data-defined) occupation returns the GENERIC plan carrying its
    own name, so the front door works for descriptor-only occupations with no
    code change here. ``objective`` is always present and required — every
    occupation needs a north star — so callers can rely on it existing."""
    prof = (profession or DEFAULT_PROFESSION).strip().lower() or DEFAULT_PROFESSION
    plan = _ONBOARDING_PLANS.get(prof)
    if plan is None:
        plan = dict(_GENERIC_ONBOARDING_PLAN)
    out = {
        "profession": prof,
        "vision_role": plan["vision_role"],
        "ask": [dict(f) for f in plan["ask"]],
        "next_hint": plan["next_hint"],
    }
    return out


# ---------------------------------------------------------------------------
# Profession ⊃ Target-class containment (ms-115 e-3785).
#
# The data model is "profession OWNS its set of target-classes": development
# owns Milestone / Operation, sales owns Opportunity / Account (顧客獲得ターゲット
# lands here in e-3786). Before this, the *projection* honored that ownership
# but *mutation* did not — `beacon milestone add` ran unchecked in a sales
# project and `beacon account add` only warned in a dev project, so a target of
# the wrong occupation could be created and then never appear in its frame (a
# "ghost"). This is the ONE place that knows which occupation owns which
# target-class; the CLI entry points ask here before creating a target so the
# containment is enforced structurally, not by prompt convention.
# ---------------------------------------------------------------------------

OWNED_TARGET_CLASSES = {
    "dev": ("milestone", "operation"),
    "sales": ("opportunity", "account", "acquisition"),
}

# The user-facing command that creates each target-class — surfaced in the block
# message so a wrong-profession call names the right command instead of a bare
# refusal (ms-115 方針5: 予防と発見性を block に添える).
_TARGET_CLASS_ADD_HINT = {
    "milestone": "beacon milestone add",
    "operation": "beacon operation open",
    "opportunity": "beacon opportunity add",
    "account": "beacon account add",
    "acquisition": "beacon acquisition add",
}


class TargetClassProfessionError(ValueError):
    """Raised when a command would create a target-class the project's
    profession does not own (ms-115). Carries a human-facing, guidance-rich
    message; CLI callers print it and exit non-zero."""


def owned_target_classes(data: dict, profession: str) -> tuple:
    """Return every target-class the ``profession`` owns in THIS project: the
    built-in seed for dev / sales PLUS the kinds of any descriptors declared for
    that profession (ms-122 e-3957). A dev / sales project (no descriptors)
    returns exactly the built-in tuple, so existing behaviour is unchanged."""
    prof = (profession or "").strip().lower()
    builtin = OWNED_TARGET_CLASSES.get(prof, ())
    out = list(builtin)
    for desc in _descriptors_owned_by(data, prof):
        kind = (desc.get("kind") or "").strip()
        if kind and kind not in out:
            out.append(kind)
    return tuple(out)


def target_class_owner(kind: str, data: dict | None = None) -> str:
    """Return the profession that owns ``kind`` (e.g. ``"milestone"`` → ``"dev"``),
    or ``""`` when no occupation claims it. When ``data`` is given, a descriptor-
    defined class also resolves to its declared ``profession`` (ms-122 e-3957);
    without ``data`` only the built-in dev / sales classes are known (keeps the
    old single-argument call sites working)."""
    for prof, kinds in OWNED_TARGET_CLASSES.items():
        if kind in kinds:
            return prof
    if data is not None:
        for desc in _td.load_descriptors(data):
            if isinstance(desc, dict) and (desc.get("kind") or "").strip() == kind:
                return (desc.get("profession") or "").strip().lower()
    return ""


def assert_target_class_owned(data: dict, kind: str) -> None:
    """Raise ``TargetClassProfessionError`` when this project's profession does
    NOT own the target-class ``kind``; return None (allowed) otherwise.

    This is the containment gate the target-creating CLI commands call before
    mutating (ms-115 e-3785). The message names what the project's profession
    CAN create and the exact command, so the user is guided to the right target
    rather than merely blocked. Descriptor-defined classes are owned by their
    declared profession, so a data-defined class in the right project passes
    (ms-122 e-3957)."""
    prof = resolve_profession(data)
    owned = owned_target_classes(data, prof)
    if kind in owned:
        return
    owner = target_class_owner(kind, data)
    owned_hints = " / ".join(
        _TARGET_CLASS_ADD_HINT.get(k, f"beacon target create --class {k}")
        for k in owned)
    owner_note = f"{owner} 職種の対象" if owner else "別職種の対象"
    raise TargetClassProfessionError(
        f"'{kind}' は{owner_note}です。このプロジェクトの職種は '{prof}' なので作成できません "
        f"(職種はそれぞれ自分の対象だけを持ちます)。\n"
        f"  '{prof}' で作れる対象: {', '.join(owned) if owned else '(なし)'}\n"
        f"  使うコマンド: {owned_hints or '(なし)'}")


# The project.json keys under which each occupation stores its AGGREGATABLE Target
# records — the set the shared-frame aggregators (session_log, deadline, the
# manifest) walk. Occupation-agnostic base code (work_model / work_base) must NOT
# carry these names; shared-frame code asks here instead of hardcoding the
# collection names (ms-108 e-3701 / fable review B-1: keep occupation knowledge in
# the registry layer).
#
# ⚠ NOT the only "which collections are Targets" registry (ms-142 T1 e-5156 review,
# fable AX/Maint): there are THREE, each a different lens on "Target", and they must
# stay in subset-sync:
#   - ``TARGET_COLLECTIONS`` (here) = AGGREGATABLE Targets (session_log / manifest);
#   - ``TARGET_DECOMPOSITION``      = physically DECOMPOSED Targets (row backend +
#     ``target_disclosure`` scan) — adds accounts / acquisitions;
#   - ``claim_target_collections`` = CLAIMABLE Targets (claim view) = the manifest
#     set ∪ {accounts, acquisitions}.
# The honest invariant (pinned by ``test_operation_target_class_e5156`` /
# ``claim_target_collections ⊇ TARGET_DECOMPOSITION ∪ target_collections``) keeps
# them from silently diverging while T1 has them split; unifying the three into one
# master registry is the deferred deep fix (follow-up, touched by T2–T7).
#
# ms-142 e-5156 (T1): ``operations`` joins the seed so a development Operation is
# enumerated as a first-class Target beside a Milestone — the same abstraction a
# Milestone rides for projection / claim / deadline enumeration, closing the gap
# where Operation was known only to the record path (occupation.record_target_entry)
# while列挙・クレーム経路 stayed a separate system. Operation is a PERSISTENT
# (recurring) dev Target with a status lifecycle (todo→in_progress→open→closed),
# NOT a funnel — so its state model derives no phase/ball (ms-142 T2) and,
# per the T1 裁定, no ``work_item_arm`` yet (its OperationTasks keep their own
# ``operation task done`` L3 path; folding them into the shared work-item CRUD
# spine is deferred to avoid a silent ``beacon task done`` regression). Consumers
# that walk this seed were each confirmed non-breaking before landing: session_log
# aggregation filters to commit/pr entries (operations carry none), deadline
# enumeration reads a ``deadline``/``target_date`` operations lack (→ UNSET,
# filtered), and the backup integrity count already documents "all Targets"
# semantics (the operations sub-count overlap is tracked debt, e-5115).
TARGET_COLLECTIONS = ("milestones", "opportunities", "operations")


def target_collections(data: dict | None = None) -> tuple:
    """Return the project.json keys that hold Target records. Without ``data``,
    the built-in seed (milestones / opportunities). With ``data``, the seed PLUS
    each descriptor's ``collection`` (ms-122 e-3957), so a data-defined
    occupation's records are walked by the same aggregators without editing this
    registry."""
    if not data:
        return TARGET_COLLECTIONS
    out = list(TARGET_COLLECTIONS)
    for desc in _td.load_descriptors(data):
        coll = (desc.get("collection") or "").strip() \
            if isinstance(desc, dict) else ""
        if coll and coll not in out:
            out.append(coll)
    return tuple(out)


def iter_target_records(data: dict) -> list:
    """Return every raw Target record across occupations (development
    Milestones + sales Opportunities + any data-defined target-class records,
    ms-122 e-3957). A project only ever populates the collections of its own
    occupation, so callers get exactly that occupation's Targets without
    branching on profession. Used by shared-frame aggregators that walk Target
    entries (session log). Unlike ``project_targets`` this returns the records
    verbatim (with their nested ``entries``), not the projected shape."""
    records = []
    for coll in target_collections(data):
        records.extend(data.get(coll, []) or [])
    return records


# Sales secondary Target collections that are CLAIMABLE but are not manifest
# Target collections (they ride a different persistence path and are not walked
# by ``target_collections`` / the session-log aggregator). A 顧客 = account and a
# 顧客獲得ターゲット = acquisition can be claimed / worked, so the claim view must
# cover them (SPEC AC1 names account explicitly) even though they are not in the
# manifest.
_CLAIM_SECONDARY_COLLECTIONS = ("accounts", "acquisitions")


def claim_target_collections(data: dict | None = None) -> tuple:
    """Return every project.json collection whose records are CLAIMABLE Targets
    across occupations (ms-142 e-5156 / T1).

    Sourced from ``profession_manifest`` (the DDL-decoupled Target set —
    milestones, opportunities, the ms-142 operations addition, and any descriptor
    collections) PLUS the sales secondary Target collections (accounts /
    acquisitions) that are claimable but ride a separate persistence path. This is
    the single source ``claim_view.build_claim_views`` consumes so its enumeration
    is NOT coupled to the physical decomposition / DDL registry
    (``TARGET_DECOMPOSITION``) — the T1 裁定 goal — while still covering 顧客 =
    account. Adding operations to the manifest therefore lights up the 2-layer
    claim filter for Operations with no edit in ``claim_view`` (T7 も同時前進)."""
    cols = [tc["collection"]
            for tc in profession_manifest(data or {})["target_classes"]]
    for extra in _CLAIM_SECONDARY_COLLECTIONS:
        if extra not in cols:
            cols.append(extra)
    return tuple(cols)


# ---------------------------------------------------------------------------
# Physical decomposition spec for row-oriented backends (ms-109 e-3591 / SPEC
# F7mdrDA4djd3byyDbZAv). For a backend that stores each record as its own row
# (MySQL v3), this declares — per Target collection — the id field and which
# nested "arms" (child arrays) are FAT (unbounded growth → split into their own
# rows) vs left inline in the Target row. This is the ONE place that knows the
# physical decomposition shape, so the storage layer stays occupation-agnostic
# (it reads this registry instead of hardcoding milestones/entries).
#
# sk rule (SPEC 方針 D2): a fat arm's child rows use sk = "{target_id}#{child_id}"
# when the Target has ONE fat arm (arm name implicit), else
# "{target_id}#{arm}#{child_id}". Development milestones have one arm (entries)
# so they keep the legacy 2-segment sk unchanged; sales Targets have several so
# they are arm-qualified. That the segment count differs by occupation is not an
# inconsistency to iron out but the honest consequence of "a Target's shape
# varies by occupation" (SPEC 方針 D2).
#
# Bounded arms (opportunity.gates, account.contacts / phase_history) are NOT
# listed → they ride inline in the Target row. Children nested under a fat-arm
# item (a communication under an activity) also stay inline in that item's row,
# exactly as a development commit nested under a task stays inline in the task's
# entry row. The unified attach-point model (SPEC 方針: Target↓Evidence /
# WorkItem↓Evidence / WorkItem↓WorkItem) is expressed by ``linked_id`` + this
# inline nesting, identically for both occupations.
# ---------------------------------------------------------------------------

TARGET_DECOMPOSITION = {
    "milestones":    {"id_field": "id", "arms": ("entries",)},
    "opportunities": {"id_field": "id", "arms": ("activities", "communications")},
    "accounts":      {"id_field": "id", "arms": ("nurturings", "communications")},
    # ms-115 e-3786: 顧客獲得ターゲット。work_items は少量なので fat arm にせず inline
    # に残す (arms=() → 新しい子テーブルを増やさない)。Target 行そのものは独立させ、
    # 忙しい獲得作業リストが projects 行を膨らませないようにする。
    "acquisitions":  {"id_field": "id", "arms": ()},
}


def target_decomposition(data: dict | None = None) -> dict:
    """Return the physical decomposition spec per Target collection. Without
    ``data``, the built-in seed. With ``data``, the seed merged with each
    descriptor's ``decomposition`` (``{id_field, arms}``) keyed by its
    ``collection`` (ms-122 e-3957). A descriptor with no ``decomposition``
    defaults to ``{id_field: "id", arms: ()}`` (its records ride inline, no new
    child table). Built-in collections are never overridden by a descriptor."""
    if not data:
        return dict(TARGET_DECOMPOSITION)
    merged = dict(TARGET_DECOMPOSITION)
    for desc in _td.load_descriptors(data):
        if not isinstance(desc, dict):
            continue
        coll = (desc.get("collection") or "").strip()
        if not coll or coll in merged:
            continue
        dec = desc.get("decomposition") or {}
        merged[coll] = {
            "id_field": dec.get("id_field", "id"),
            "arms": tuple(dec.get("arms") or ()),
        }
    return merged


def target_child_tables(data: dict | None = None) -> tuple:
    """Return the distinct child-table names across all Target collections (= the
    union of fat arm names, de-duplicated in declaration order). ``communications``
    is shared by the sales opportunity and account collections, so it appears
    once. A row-oriented backend creates one child table per name here. With
    ``data``, descriptor collections' arms are included too (ms-122 e-3957)."""
    seen: list = []
    for spec in target_decomposition(data).values():
        for arm in spec["arms"]:
            if arm not in seen:
                seen.append(arm)
    return tuple(seen)


# ---------------------------------------------------------------------------
# Instantiation manifest — the single read-path for "what slots a profession
# fills" (ms-142 e-5008 / SPEC BcQ0OUTjOrwTnUltRqmb 設計方針 1).
#
# The six registries above already gather a profession's slots (projection /
# owned classes / collections / decomposition / narrowing), but ONE thing was
# nowhere declared: for each Target collection, WHICH nested arm holds planned
# *work items* vs *evidence*, and HOW a work item is identified inside a shared
# arm. ``TARGET_DECOMPOSITION`` lists a collection's arms but treats them all
# alike — it cannot tell that a development ``entries`` arm mixes tasks (work
# items) and commits (evidence) discriminated by ``type``, while a sales
# ``activities`` arm is ALL work items and ``communications`` is a separate
# evidence arm. That missing classification is exactly what an occupation-
# agnostic work-item walk (deadline enumeration, the e-5009 iterator) needs.
#
# ``profession_manifest`` is that single read-path. It is COMPOSED from the
# existing registries (which stay the source of truth — nothing here overrides
# them) PLUS the ``_ARM_ROLES`` seed below, and it carries ms-122 descriptor
# collections for free. It is a non-breaking VIEW, not a physical merge: adding
# it changes no existing caller (AC1 = "引ける／集約" — a read-path, not a
# rewrite of the six well-tested registries).
#
# CRITICAL (ms-142 の芯 / leader 承認条件 1): the arm classification is
# DECLARATIVE DATA (``_ARM_ROLES`` keyed by collection; the phase/ball slot is
# derived from each class's declared state model in ``target_state``, ms-142 T2),
# never an ``if profession == …`` branch. ms-142's whole promise is "declare a
# new occupation's manifest and every arm-walking L2 capability lights up with
# ZERO wiring" (e-5014). A classification that leaked into code branches would
# break "declare ⇒ light up" — the same class of hole ms-133's independent
# review caught (high#: 記述子駆動でなく Skill md に if 分岐が漏れる). A data-
# defined occupation's arms come from its descriptor (``_DEFAULT_ARMS`` =
# work_items / evidence), so it, too, lights up without editing this file.
#
# CONTRACT (leader 承認条件 2): ``profession_manifest`` is the canonical CONSUME
# contract. When e-5013 migrates the ``KNOWN_COLLECTION_COUPLING`` debt into the
# coverage matrix, "consult profession_manifest instead of reading
# data['milestones']" is the fix it points each capability to. Enforcement lands
# later (e-5012 matrix / e-5013 migration); the shape is fixed HERE so the
# downstream forcing function loads cleanly onto it.
# ---------------------------------------------------------------------------

# Per-collection arm classification. ``work_item_arm`` names the arm holding
# planned work + how a work item is identified inside it: ``arm`` is the nested
# list, ``item_type`` is ``None`` when every item in the arm is a work item
# (sales activities) or an entry ``type`` string when the arm is shared (dev
# ``entries`` hold tasks AND commits → work items are ``type == "task"``), and
# ``kind`` is the occupation-agnostic label for a work item of that arm
# (dev ``task`` / sales ``activity``) — what a shared capability stamps instead of
# re-deriving it from a collection name (ms-142 e-5010: the deadline reminder's
# ``work_kind``). ``evidence_arms`` names where proof/changelog records live (dev
# commits ride the SAME entries arm; sales evidence is its own communications arm).
#
# ⚠ DECLARED-ONLY / UNCONSUMED (ms-143 PR#4, 思想レビュー finding e-5151): unlike
# ``work_item_arm`` (which ``add_work_item`` reads from the manifest so a new
# occupation lights up by DECLARING it), ``evidence_arms`` currently has NO
# consumer — ``occupation.add_evidence`` resolves the sales evidence grain directly
# (option A, human-approved interim) and does not read this declaration. So
# ``evidence_arms`` is a declared-but-unwired slot: a new occupation declaring it
# will NOT light up an evidence path yet. In the INTERIM, record sales evidence by
# calling ``occupation.add_evidence`` directly (option A) — do not rely on this
# ``evidence_arms`` declaration to route anything. This is recorded honestly here
# (not silently left as false "wired" advertising) until e-5151 makes add_evidence
# manifest-driven and recovers the declared→wired contract.
#
# REACHABILITY (ms-142 e-5011 review, Maint#5; extended e-5156): the ONLY consumer
# of these two ``_ARM_ROLES`` / ``_COLLECTION_KIND`` dicts (``_ARM_PHASE_BALL`` was
# dissolved into the state model in T2, e-5157)
# is ``profession_manifest``, which walks ``target_collections(data)`` — whose seed
# is milestones + opportunities + operations (accounts / acquisitions ride a
# different persistence path and are NOT Target collections here, see
# ``TARGET_COLLECTIONS``). So an entry keyed by any collection NOT returned by
# ``target_collections`` would be dead data — a silent no-op if edited. Keep these
# keyed to exactly those seed collections; a descriptor occupation's roles come
# from its descriptor (``target_descriptor.arm_roles``), not from here.
#
# operations declares a work_item_arm of ``None`` EXPLICITLY (below), not by
# omission: per the ms-142 T1 裁定 an Operation carries no shared work-item arm
# yet, but its absence is written as DATA so the two sibling dicts (_ARM_ROLES /
# _COLLECTION_KIND) share ONE key set (milestones + opportunities + operations) —
# "均一に宣言" per the class-engine ideal, not a prose-only claim a reader must
# trust. Its phase/ball slot is likewise a DECLARED absence, now sourced from the
# operation state model (``target_state``, T2) which derives ``None``.
# ``_arm_roles_for`` maps a ``None`` work_item_arm to the
# empty classification, so behaviour is identical to omitting it (the pin tests
# hold). Folding OperationTasks into the shared work-item spine (find_target_entry
# / set_entry_state / iter_work_items) is the deferred part — that would silently
# change ``beacon task done`` — not the declaration.
_ARM_ROLES = {
    "milestones": {
        # ms-143: ``id_prefix`` is the declarative work-item id prefix — a dev
        # task is ``e-`` (shared with operation entries, see next_entry_id). It
        # lets add_work_item allocate the next work-item id from the manifest
        # alone, so a new occupation lights up by DECLARING its prefix.
        "work_item_arm": {"arm": "entries", "item_type": "task", "kind": "task",
                          "id_prefix": "e-"},
        "evidence_arms": [{"arm": "entries", "item_type": "commit"}],
    },
    "opportunities": {
        "work_item_arm": {"arm": "activities", "item_type": None,
                          "kind": "activity", "id_prefix": "act-"},
        "evidence_arms": [{"arm": "communications", "item_type": None}],
    },
    # operations: work_item_arm is a DECLARED None (see the note above) so the
    # three sibling dicts share one key set. OperationTasks keep their own
    # ``operation task done`` L3 path in PR#1 (folding them into the shared
    # work-item spine is deferred, ms-142 T1 裁定).
    "operations": {"work_item_arm": None, "evidence_arms": []},
}

# The exclusive phase + who-has-the-ball model per collection (SPEC 方針 1 lists
# "phase・ball" among the slots). Sales Targets advance through a phase funnel and
# carry the ball; development milestones do not (their progress is task ratios /
# evidence), so dev's phase_ball is ``None`` — a declared absence, not a gap.
# operations likewise has no funnel: it moves through a STATUS lifecycle
# (todo→in_progress→open→closed), not a phase/ball, so its phase_ball is ``None`` too.
#
# ms-142 T2 (e-5157): the standalone ``_ARM_PHASE_BALL`` hardwire is DISSOLVED.
# phase_ball is no longer a hand-maintained dict keyed by collection; it is
# DERIVED from each class's declared state model (``target_state``) via
# ``target_state.derive_phase_ball``. A class has a phase/ball pair exactly when
# it advances through a non-``status`` field (a funnel/descriptor phase) AND
# carries a ball, so milestone/operation derive ``None`` and opportunity derives
# ``{"phase": "who_has_the_ball"}`` — byte-identical to the old literals (pinned
# by test_occupation_manifest), but now sourced from the state model rather than
# a second place that could drift from it.

# collection -> target-class kind for the built-in occupations. Bridges the
# collection-keyed registries to the kind-keyed ones (NARROWING_ID_PREFIXES).
# Descriptor collections resolve their kind from the descriptor itself, so this
# only needs the built-ins reachable via ``target_collections`` (milestones +
# opportunities + operations); see the reachability note above.
_COLLECTION_KIND = {
    "milestones": "milestone",
    "opportunities": "opportunity",
    "operations": "operation",
}


def _collection_kind(data: dict | None, collection: str) -> str:
    """Return the target-class ``kind`` for a collection: the built-in map, else
    a descriptor whose ``collection`` matches (ms-122), else ``""``."""
    kind = _COLLECTION_KIND.get(collection, "")
    if kind:
        return kind
    if data:
        for desc in _td.load_descriptors(data):
            if isinstance(desc, dict) \
                    and (desc.get("collection") or "").strip() == collection:
                return (desc.get("kind") or "").strip()
    return ""


def _arm_roles_for(data: dict | None, collection: str) -> dict:
    """Return ``{work_item_arm, evidence_arms}`` for a collection. Built-in
    collections use the ``_ARM_ROLES`` seed. A descriptor-defined collection asks
    its descriptor (``target_descriptor.arm_roles``) which arm holds work items vs
    evidence — an EXPLICIT declaration if the descriptor carries one (so a new
    occupation may name its arms anything and still light up arm-walking
    capabilities — the true "declare, don't wire" contract, ms-142 e-5011), else
    the thick-frame name convention (that fallback lives ONCE in
    ``target_descriptor.arm_roles`` — single source of truth, e-5011 review
    Maint#7). This is what lets a NEW occupation light up arm-walking capabilities
    by DECLARING its manifest, with no edit here (ms-142 の芯)."""
    seed = _ARM_ROLES.get(collection)
    if seed is not None:
        return {
            "work_item_arm": dict(seed["work_item_arm"])
            if seed["work_item_arm"] else None,
            "evidence_arms": [dict(a) for a in seed["evidence_arms"]],
        }
    if data:
        for desc in _td.load_descriptors(data):
            if isinstance(desc, dict) \
                    and (desc.get("collection") or "").strip() == collection:
                return _td.arm_roles(desc)
    # A collection with no seed and no descriptor is not reachable from
    # ``profession_manifest`` (it only walks ``target_collections``, whose members
    # each have a seed or a descriptor). Return an empty classification defensively
    # rather than re-implementing the name convention (that lives in
    # ``target_descriptor.arm_roles``).
    return {"work_item_arm": None, "evidence_arms": []}


def profession_manifest(data: dict, profession: str | None = None) -> dict:
    """Return the project's instantiation manifest — the SINGLE read-path for a
    profession's Target-class slots as declarative data (ms-142 e-5008).

    Shape::

        {
          "profession": "dev",
          "target_classes": [
            {
              "kind": "milestone",
              "collection": "milestones",
              "id_field": "id",
              "id_prefix": "ms-",
              "narrowing": True,          # sliceable in a Trek scope
              "arms": ("entries",),       # all fat arms (from decomposition)
              "work_item_arm": {"arm": "entries", "item_type": "task"},
              "evidence_arms": [{"arm": "entries", "item_type": "commit"}],
              "phase_ball": None,        # derived from state_model (ms-142 T2)
              "state_model": {"shape": "status_enum", "state_field": "status",
                              # states set_target_state won't write (verb-gated):
                              "gated_states": ["approved", "cancelled",
                                               "done", "in_review",
                                               "observing"],
                              "ball_field": None},
            },
            ...
          ],
        }

    Composed from the existing registries (``target_collections`` /
    ``target_decomposition`` / ``narrowing_id_prefixes`` / ``all_narrowing_kinds``
    — all untouched source of truth) plus the ``_ARM_ROLES`` classification seed
    and each class's declared state model (``target_state``, from which
    ``phase_ball`` is derived — ms-142 T2), and it carries ms-122 descriptor
    collections for free.
    Both a dev project and a sales project resolve to the SAME shape (a list of
    identically-keyed dicts) — the occupation-agnostic contract that arm-walking
    L2 capabilities (deadline enumeration, the e-5009 work-item iterator) consume
    instead of reading concrete collections directly. ``profession`` defaults to
    the project's own; pass it only to override for tests."""
    prof = (profession or resolve_profession(data)) if data is not None \
        else DEFAULT_PROFESSION
    decomposition = target_decomposition(data)
    narrowing = set(all_narrowing_kinds(data))
    prefixes = narrowing_id_prefixes(data)
    target_classes = []
    for collection in target_collections(data):
        spec = decomposition.get(collection, {"id_field": "id", "arms": ()})
        arms = tuple(spec.get("arms") or ())
        kind = _collection_kind(data, collection)
        roles = _arm_roles_for(data, collection)
        model = _tstate.state_model_for(data, kind)
        target_classes.append({
            "kind": kind,
            "collection": collection,
            "id_field": spec.get("id_field", "id"),
            "id_prefix": prefixes.get(kind, ""),
            "narrowing": kind in narrowing,
            "arms": arms,
            "work_item_arm": roles["work_item_arm"],
            "evidence_arms": roles["evidence_arms"],
            # ms-142 T2: derived from the declared state model, not a hardcoded map.
            "phase_ball": _tstate.derive_phase_ball(model),
            # ms-142 T2: the class's state model (shape / state_field / terminal /
            # ball) as a first-class manifest slot — "状態モデルを引ける読み取り経路".
            "state_model": _tstate.public_state_model(model),
        })
    return {"profession": prof, "target_classes": target_classes}


def target_class(data: dict, kind: str) -> dict:
    """Return the ``profession_manifest`` target-class descriptor for ``kind``
    (ms-143). Raises ``ValueError`` if this project's profession has no such
    class, so a caller minting / locating a Target of an unknown kind fails
    loudly instead of silently writing to the wrong collection."""
    classes = profession_manifest(data)["target_classes"]
    for tc in classes:
        if tc["kind"] == kind:
            return tc
    # review finding #3: name the valid kinds so a caller with a typo / wrong
    # profession sees what IS available, not just what isn't.
    valid = [tc["kind"] for tc in classes]
    raise ValueError(
        f"No target-class {kind!r} in this project's profession "
        f"(valid kinds: {valid})")


def next_target_id(data: dict, kind: str) -> str:
    """Allocate the next id for a Target of ``kind``, profession-generically
    (ms-143, 設計判断 ii). The collection and ``id_prefix`` come from
    ``profession_manifest`` so each target-class keeps an INDEPENDENT id space
    (milestones count ``ms-``, opportunities count ``opp-``), collision-safe and
    deterministic via ``work_base.next_suffixed_id`` (max integer suffix + 1).

    This is the single generic allocator the hand-rolled per-collection ones
    (``core.next_milestone_id`` / ``sales_entities.next_opportunity_id``) collapse
    into; those stay as thin back-compat shims delegating here."""
    tc = target_class(data, kind)
    id_field = tc.get("id_field", "id")
    ids = [rec.get(id_field, "") for rec in data.get(tc["collection"], []) or []]
    return work_base.next_suffixed_id(ids, tc["id_prefix"])


def create_target(data: dict, kind: str, *, label: str,
                  status: str = "", created_at: str = "", created_by: str = "",
                  assignee: str = "", stamp_created_by: bool = True,
                  **extra) -> dict:
    """Create a new Target of ``kind`` and append it to its collection,
    profession-generically (ms-143, 設計判断 b 系統2 = target 作成). Resolves the
    collection + ``id_prefix`` from ``profession_manifest``, allocates the id via
    ``next_target_id``, builds the occupation-agnostic skeleton through
    ``work_model.new_target`` (id / label / status / created_at / created_by /
    assignee), and appends. Profession-specific fields (a milestone's
    ``target_date`` / ``commits``, an opportunity's ``phase`` / ``account_id``)
    ride via ``**extra``. Returns the new record.

    Both a dev milestone and a sales opportunity mint through THIS one path — the
    ``kind``-keyed manifest lookup is the only place profession enters, so the
    verb itself never names ``data['milestones']`` / ``data['opportunities']``.
    Workflow around creation (seeding a phase's anchor activities, the ms-81
    empty-assignee no-pollution rule) stays at the caller / CLI frontend, not in
    this primitive (leader 握り: primitive は create に専念、workflow は CLI).

    ``stamp_created_by`` (ms-143 parity-first): the base always mints a
    ``created_by``, but a sales Opportunity historically carries none
    (DEV_ONLY_SKELETON_KEYS). A frontend that must preserve that existing
    profession skeleton difference passes ``stamp_created_by=False`` so the
    refactor stays a pure abstraction (no behavior change), NOT an enrichment that
    silently unifies the difference (leader 握り: 差は surface して温存、混ぜない).
    Whether Opportunities SHOULD carry ``created_by`` is a separate product
    decision tracked outside this refactor."""
    tc = target_class(data, kind)
    collection = tc["collection"]
    target_id = next_target_id(data, kind)
    # Collision guard (review finding #1): create_target is now the SINGLE writer
    # for all target creation, so the ID-uniqueness invariant that
    # ``core.milestone_add`` used to enforce belongs here. ``next_target_id`` is
    # max-suffix+1 so this is normally unreachable, but a corrupted id space
    # (e.g. a hand-edited project.json with duplicate ids) would otherwise
    # silently append a duplicate — raise instead of corrupting further.
    id_field = tc.get("id_field", "id")
    if any(rec.get(id_field) == target_id
           for rec in data.get(collection, []) or []):
        raise ValueError(
            f"Target ID collision: {target_id} already exists in "
            f"{collection!r}. Corrupted ids — run `beacon doctor`.")
    rec = _wm.new_target(
        target_id, label, status=status or _wm.TODO_STATUS,
        created_at=created_at, created_by=created_by, assignee=assignee, **extra)
    if not stamp_created_by:
        rec.pop("created_by", None)
    data.setdefault(collection, []).append(rec)
    return rec


def _find_in_entry_list(entries: list, entry_id: str, target: dict):
    """Locate ``entry_id`` in ``entries``, recursing into each entry's nested
    ``entries`` children — the same recursion ``core.find_entry`` (_find_entry_in)
    does for dev subtasks. Returns (target, arm_list, entry, index) or None."""
    for i, entry in enumerate(entries):
        if entry.get("id") == entry_id:
            return (target, entries, entry, i)
        hit = _find_in_entry_list(entry.get("entries", []) or [], entry_id, target)
        if hit:
            return hit
    return None


def find_target_entry(data: dict, entry_id: str):
    """Locate a work-item entry by id across ALL Target collections' work-item
    arms, profession-generically (ms-143, 設計判断 b 系統3). Returns
    ``(target, arm_list, entry, index)`` or ``None`` — the same shape
    ``core.find_entry`` returns for the dev case, so ``set_entry_state`` can close
    a dev task (a milestone's ``entries``, incl. nested subtasks) and a sales
    activity (an opportunity's ``activities``) through ONE path. The work-item arm
    per target-class comes from ``profession_manifest`` so the locator never names
    ``data['milestones']`` / ``data['opportunities']`` itself.

    SCOPE: walks Target work-item arms only. Operation entries (operations are NOT
    Targets) stay ``core.find_entry``'s responsibility — out of the ms-143 'target
    CRUD' scope."""
    for tc in profession_manifest(data)["target_classes"]:
        arm = (tc.get("work_item_arm") or {}).get("arm")
        if not arm:
            continue
        for target in data.get(tc["collection"], []) or []:
            hit = _find_in_entry_list(target.get(arm, []) or [], entry_id, target)
            if hit:
                return hit
    return None


def set_entry_state(data: dict, entry_id: str, status: str, *,
                    at: str = "", actor: str = "",
                    reason: str = "") -> tuple[dict, dict]:
    """Transition a work-item's lifecycle state, profession-generically (ms-143,
    設計判断 b 系統3 = 状態変更). ``done`` routes through ``work_model.mark_done``
    (canonical ``status`` / ``done_at`` + ``done_by`` / ``done_reason`` completion
    attribution); ``todo`` is a plain status set. This unifies ``core.task_done``
    (done) and ``sales_entities.activity_set_status`` (todo/done) behind one path.

    ``cancelled`` is a legitimate terminal state but carries its own audit stamp
    (``work_base.stamp_cancel`` via each occupation's cancel verb), so it is
    rejected here rather than set bare — mirroring ``activity_set_status``'s
    ``_SETTABLE`` guard. Attribute patches (rename / amount / deadline) are a
    SEPARATE responsibility (a future ``update_entry``), NOT folded here, because
    the completion attribution ``mark_done`` writes must not leak onto plain field
    edits (設計判断 i = 分離).

    Returns ``(parent_target, work_item_entry)`` — the containing Target first,
    the work item second (review finding #4: the positional order is documented so
    callers don't swap them). Raises ``ValueError`` if the entry is not found or
    ``status`` is not settable here."""
    hit = find_target_entry(data, entry_id)
    if not hit:
        raise ValueError(f"Entry not found: {entry_id}")
    target, _arm_list, entry, _idx = hit
    if status == _wm.DONE_STATUS:
        _wm.mark_done(entry, at=at, actor=actor, reason=reason)
    elif status == _wm.TODO_STATUS:
        entry["status"] = status
    else:
        raise ValueError(
            f"status must be 'todo' or 'done' (cancel has its own audit-stamped "
            f"path), got {status!r}")
    return target, entry


def update_entry(data: dict, record_id: str, **fields) -> dict:
    """Patch attributes on a Target OR a work item, profession-generically (ms-143,
    設計判断 i = 更新). The attribute-patch sibling of ``set_entry_state`` (which
    owns the LIFECYCLE transition todo/done + its completion attribution) — kept
    SEPARATE so the ``mark_done`` completion stamps never leak onto a plain field
    edit (設計判断 i = 分離).

    ``record_id`` is a Target id (milestone / opportunity) OR a work-item id (task /
    activity) — the parameter and the not-found error say "record", not "entry",
    because this resolves BOTH planes (AX review PR #628: an "entry" conventionally
    means a sub-record, so naming it ``entry_id`` mis-signals that a Target id is
    not accepted). Locates via ``find_target`` first, then ``find_target_entry`` —
    so it never names ``data['milestones']`` / ``data['opportunities']`` /
    ``find_opportunity`` itself. Applies each keyword in ``fields`` verbatim
    (``record[key] = value``); a value of ``None`` is WRITTEN, not skipped, so a
    field can be CLEARED (a sales ``goal_amount=None`` clears the 商談金額). The
    caller passes only the keys it means to change.

    Per-field validation / normalization stays in the FRONTEND — this primitive is
    the generic locate + apply skeleton, mirroring how ``add_work_item`` leaves the
    sales validation to its caller. So the rich, profession-specific edits (dev
    ``milestone update``'s progress clamp / priority resolver / status-meta stamp /
    label dual-write; sales ``opportunity phase``'s funnel transition) are NOT folded
    here — they keep their own frontend path (leader 握り: primitive は plain patch
    に閉じる). Returns the located record. Raises ``ValueError`` when ``record_id``
    matches no Target or work item."""
    record = find_target(data, record_id)
    if record is None:
        hit = find_target_entry(data, record_id)
        record = hit[2] if hit else None
    if record is None:
        raise ValueError(f"Record not found: {record_id}")
    for key, value in fields.items():
        record[key] = value
    return record


def target_records(data: dict, kind: str) -> list:
    """Return the list of Target records for ``kind`` (manifest-resolved
    collection), profession-generically (ms-143). The concrete-literal-free way
    for a shared / to-be-shared verb to get 'all milestones' or 'all
    opportunities' without naming ``data['milestones']`` / ``data['opportunities']``
    itself. Returns the live list (callers may read it; mutation should go through
    the create/add primitives). ``[]`` if ``kind`` isn't in this profession."""
    try:
        tc = target_class(data, kind)
    except ValueError:
        return []
    return data.get(tc["collection"], []) or []


# Display labels (Japanese) per occupation-agnostic kind — the SINGLE declaration
# the deadline surfaces read, replacing the ``label_jp`` dict that was duplicated
# in commands.py (``beacon deadline due``) AND scripts/session-start-deadlines.py
# (ms-143 e-5047 / PR #623 maintainability review finding #6). A descriptor-defined
# occupation declares its own label (``display_label`` / ``label_jp`` on the target
# descriptor), so a NEW occupation's kinds get a display label with ZERO wiring at
# the call sites. Built-ins are the exact set the old ``label_jp`` carried
# (milestone / task / activity), so an unlisted kind (opportunity / account / a
# descriptor kind with no declared label) falls back to the kind string —
# byte-identical to the pre-refactor ``label_jp.get(kind, kind)``.
_KIND_DISPLAY_LABEL = {
    "milestone": "MS",
    "task": "タスク",
    "activity": "活動",
}


def kind_display_label(data: dict | None, kind: str) -> str:
    """Return the display label for an occupation-agnostic ``kind`` (a target kind
    like ``milestone`` / ``opportunity`` or a work-item kind like ``task`` /
    ``activity``), sourced from declarations rather than a hardcoded map at the call
    site (ms-143 e-5047).

    Resolution order: the built-in ``_KIND_DISPLAY_LABEL`` (dev / sales built-ins),
    then — when ``data`` is given — a descriptor-defined kind's own
    ``display_label`` / ``label_jp`` (ms-122 data-defined occupations), else the
    ``kind`` string itself. ``data`` may be ``None`` (a display consumer without the
    project loaded, e.g. scripts/session-start-deadlines.py); descriptor labels then
    resolve only where the caller passes the CLI-provided label, and built-in kinds
    still map. This is the seam that makes a new occupation's deadline labels light
    up from its manifest/descriptor with no edit at either surface."""
    label = _KIND_DISPLAY_LABEL.get(kind)
    if label:
        return label
    if data:
        for desc in _td.load_descriptors(data):
            if isinstance(desc, dict) and (desc.get("kind") or "").strip() == kind:
                lbl = (desc.get("display_label")
                       or desc.get("label_jp") or "").strip()
                if lbl:
                    return lbl
    return kind


def resolve_target(data: dict, target_id: str = "", *,
                   index: int | None = None) -> dict:
    """Resolve a Target by id, or auto-select the single active one,
    profession-generically (ms-143). The profession-AGNOSTIC generalization of
    ``core.find_target_milestone`` (which is a dev-concrete symbol): a shared /
    to-be-shared verb resolves its Target through this rather than calling the
    dev-specific resolver. Behaviour mirrors find_target_milestone but over the
    manifest-driven record set (all Target collections), so it never names
    ``data['milestones']`` itself:

      - ``target_id`` given → that Target. Duplicate ids (corruption) require an
        explicit ``index`` (1-based); out-of-range / count reported.
      - empty ``target_id`` → the single ``status == "in_progress"`` Target
        (0 → "no active", >1 → "specify -m").

    ``core.find_target_milestone`` stays for L1 dev callers (milestone commands /
    cmd_pr / task_update); this is the L2 path. Raises ``ValueError`` on
    not-found / ambiguous / none-active / multiple-active. This is a target-level
    resolver — the sibling of the entry-level ``find_target_entry``."""
    records = iter_target_records(data)
    if target_id:
        matches = [r for r in records if r.get("id") == target_id]
        if not matches:
            raise ValueError(f"Target not found: {target_id}")
        if len(matches) == 1:
            if index is not None and index != 1:
                raise ValueError(
                    f"Target '{target_id}' has only 1 record but "
                    f"--index {index} was given.")
            return matches[0]
        if index is None:
            raise ValueError(
                f"Ambiguous target '{target_id}': {len(matches)} records exist "
                f"(data corruption). Specify which with `--index <n>` where n is "
                f"1..{len(matches)}.")
        if index < 1 or index > len(matches):
            raise ValueError(
                f"--index {index} is out of range for '{target_id}' "
                f"(valid: 1..{len(matches)}).")
        return matches[index - 1]
    active = [r for r in records if r.get("status") == "in_progress"]
    if len(active) == 0:
        # AX review PR #628 (misleading): resolve_target is profession-GENERIC, so
        # the recovery hint must not hard-code the dev-only `beacon milestone start`
        # (which does nothing for a sales project whose targets are Opportunities).
        # Emit the activation command the project's own occupation uses.
        prof = resolve_profession(data)
        hint = ("beacon milestone start <ms-id>" if prof == "dev"
                else "activate an in-progress target for this project")
        raise ValueError(f"No active target. Run: {hint}")
    if len(active) > 1:
        ids = ", ".join(r.get("id", "") for r in active)
        raise ValueError(f"Multiple active targets. Specify with -m <id>: {ids}")
    return active[0]


def find_target(data: dict, target_id: str) -> dict | None:
    """Locate a Target record by id across all Target collections,
    profession-generically (ms-143). Returns the record dict or ``None`` — the
    manifest-driven replacement for ``core.find_target_milestone`` /
    ``sales_entities.find_opportunity`` when a profession-shared / to-be-shared
    verb needs the containing Target without naming ``data['milestones']`` /
    ``data['opportunities']`` itself."""
    kind = _wm.target_kind(target_id)
    try:
        tc = target_class(data, kind)
    except ValueError:
        return None
    id_field = tc.get("id_field", "id")
    for rec in data.get(tc["collection"], []) or []:
        if rec.get(id_field) == target_id:
            return rec
    return None


def _collect_item_ids(entries: list, out: list) -> None:
    """Append every id in ``entries``, recursing into nested ``entries`` children
    (dev subtasks)."""
    for it in entries:
        out.append(it.get("id", ""))
        _collect_item_ids(it.get("entries", []) or [], out)


def _all_work_item_ids(data: dict) -> list:
    """Every work-item id across all Target work-item arms (nested) PLUS operation
    entries — the GLOBAL id space ``add_work_item`` allocates within (ms-143 設計
    判断 a). Work-item prefixes are globally unique (dev ``e-`` lives only in
    milestone / operation entries, sales ``act-`` only in activities), so
    ``next_suffixed_id`` filtered by a prefix over this superset is collision-safe
    and preserves ``core.next_entry_id``'s cross-operations scope. Operations are
    dev infra (not a profession Target) but share the ``e-`` space, so they are
    scanned explicitly."""
    ids: list = []
    for tc in profession_manifest(data)["target_classes"]:
        arm = (tc.get("work_item_arm") or {}).get("arm")
        if not arm:
            continue
        for rec in data.get(tc["collection"], []) or []:
            _collect_item_ids(rec.get(arm, []) or [], ids)
    for op in data.get("operations", []) or []:
        _collect_item_ids(op.get("entries", []) or [], ids)
    return ids


_ARM_DEFAULT_ITEM_TYPE = object()  # sentinel: "use the arm's declared item_type"


def add_work_item(data: dict, target_id: str, *, description: str,
                  status: str = "", item_type=_ARM_DEFAULT_ITEM_TYPE,
                  **extra) -> dict:
    """Append a work item (dev task / sales activity) under a Target,
    profession-generically (ms-143 設計判断 b 系統1 = work-item 追加). Resolves the
    target's ``work_item_arm`` ``{arm, item_type, id_prefix}`` from
    ``profession_manifest`` and appends ``{id, [type], status, description,
    **extra}`` to that arm. The id is allocated GLOBALLY by prefix (設計判断 a) via
    ``_all_work_item_ids`` so it never collides with an existing id of the same
    prefix anywhere (incl. operation entries for ``e-``).

    Profession-specific fields — a task's ``priority`` / ``motivation``, an
    activity's ``deadline`` / ``who_has_the_ball`` — ride via ``**extra``; the
    frontend (``core.task_add`` / ``sales_entities.activity_add``) owns them, this
    primitive stays the generic skeleton (mirrors ``create_target``'s layering).
    ``type`` is stamped only when the arm declares an ``item_type`` (dev tasks
    carry ``type="task"``; sales activities declare ``None`` and carry no type).

    Returns the new work-item dict. Raises ``ValueError`` if the target kind has
    no work-item arm or the target id is not found."""
    kind = _wm.target_kind(target_id)
    tc = target_class(data, kind)
    wia = tc.get("work_item_arm") or {}
    arm = wia.get("arm")
    if not arm:
        raise ValueError(f"target-class {kind!r} has no work-item arm")
    id_field = tc.get("id_field", "id")
    target = next((r for r in data.get(tc["collection"], []) or []
                   if r.get(id_field) == target_id), None)
    if target is None:
        raise ValueError(f"Target not found: {target_id}")
    item_id = work_base.next_suffixed_id(
        _all_work_item_ids(data), wia.get("id_prefix", ""))
    # ``type`` is the arm's declared item_type by default; a caller may override
    # it for the arm's polymorphic entries (dev's ``entries`` arm holds
    # task / commit / note — task_add passes its entry_type). A falsy resolved
    # type stamps no ``type`` field (sales activities declare None).
    resolved_type = (wia.get("item_type")
                     if item_type is _ARM_DEFAULT_ITEM_TYPE else item_type)
    item: dict = {"id": item_id, "description": description}
    if resolved_type:
        item["type"] = resolved_type
    item["status"] = status or _wm.TODO_STATUS
    item.update(extra)
    target.setdefault(arm, []).append(item)
    return item


def _resolve_evidence_parent(data: dict, parent_id: str):
    """Resolve where a piece of evidence (a sales Communication / 事後記録型の証跡)
    is stored and which planned work item it fulfilled, occupation-generically
    (ms-143 設計判断 b 系統4). Returns ``(node, arm, linked_id, container)`` — or
    ``(None, None, None, None)`` when unresolvable:

      - ``node``      the dict whose evidence arm physically holds the record: the
                      Target itself (opp-/acc- grain), or the fulfilled work item
                      (act-/nrt- grain, nested — mirrors a dev commit nested under
                      its task).
      - ``arm``       the evidence arm name on ``node`` (``"communications"``).
      - ``linked_id`` the work item the evidence fulfilled (``""`` at target grain),
                      recorded so both grains stay traceable.
      - ``container`` the owning Target (opp/acc) — the source of the
                      ``created_in_phase`` set-once default even when ``node`` is a
                      nested work item.

    Accounts / nurturings are deliberately NOT ``profession_manifest`` Target-classes
    (that invariant stays milestones + opportunities, see ``TARGET_COLLECTIONS``), so
    this resolver reaches the sales resolvers directly rather than forcing accounts
    into the manifest and changing every Target projection (ms-143 option A, human-
    confirmed 2026-08-10). occupation.py is the layer allowed to know sales
    collections — the same seam as ``record_target_entry`` /
    ``_HARD_VALIDATED_COLLECTION`` — so the account-grain evidence path lives HERE,
    not in the profession-agnostic base."""
    container, linked_id = sales_entities.resolve_communication_target(
        data, parent_id)
    if container is None:
        return None, None, None, None
    if linked_id.startswith("act-"):
        _, node = sales_entities.find_activity(data, linked_id)
    elif linked_id.startswith("nrt-"):
        _, node = sales_entities.find_nurturing(data, linked_id)
    else:
        node = container
    return node, "communications", linked_id, container


def add_evidence(data: dict, parent_id: str, *, summary: str, direction: str,
                 channel: str = "other", body: str = "",
                 source: dict | None = None, occurred_at: str = "",
                 created_at: str = "", created_in_phase: str = "") -> dict:
    """Append a piece of evidence (a sales Communication / 事後記録型の証跡) under a
    Target or the work item it fulfilled, occupation-generically, and return the new
    record (ms-143 設計判断 b 系統4 = 証跡追加). Returns the record dict — symmetric
    with its siblings ``add_work_item`` and ``target_engine.add_evidence``, which
    both return the new record; a caller reads ``.get("id")`` for the id (AX review
    PR #628 finding #1: sibling primitives must not split their return type). The
    evidence-grain sibling of ``add_work_item`` (which adds a *planned* work item to
    a Target's work-item arm)
    and of ``record_target_entry`` (which carries the dev milestone changelog and
    NO-OPs on a sales Target, so it cannot record a Communication — the gap this
    fills).

    NESTING (mirrors the dev commit↔task model, ms-106 e-3503): a record that
    fulfills a work item (act-/nrt-) nests UNDER that work item's own evidence arm;
    one addressed to the Target (opp-/acc-) directly sits at Target level.
    ``linked_id`` records which work item it fulfilled so both grains stay
    traceable, and the container's current phase is stamped set-once as
    ``created_in_phase``.

    Profession-specific fields (``direction`` / ``channel`` / ``body`` / ``source`` /
    ``occurred_at``) are the sales evidence vocabulary — occupation.py carries them
    exactly as ``record_target_entry`` carries the dev changelog vocabulary
    (``revision_id`` / ``hash`` / …). The id is allocated GLOBALLY by prefix via the
    canonical ``sales_entities.next_communication_id`` (comm- space over every
    Opportunity + Account, incl. nested), so numbering is unchanged (parity).

    Raises ``ValueError`` on an unresolvable parent, an empty summary, or an invalid
    direction — same precedence and messages as the pre-refactor
    ``sales_entities.communication_add`` (pinned by
    ``tests/test_add_evidence_primitive_ms143.py``)."""
    node, arm, linked_id, container = _resolve_evidence_parent(data, parent_id)
    if node is None:
        raise ValueError(
            "Communication target not found (opp-…/acc-… target or "
            f"act-…/nrt-… work item): {parent_id}")
    if not summary or not summary.strip():
        raise ValueError("Communication summary is required")
    if direction not in sales_entities.VALID_COMM_DIRECTION:
        raise ValueError(
            f"direction must be one of "
            f"{sorted(sales_entities.VALID_COMM_DIRECTION)}, got {direction!r}")
    # channel is free-text (e-3454): normalize only; empty → "other".
    ch = (channel or "").strip().lower() or "other"
    ev_id = sales_entities.next_communication_id(data)
    record = {
        "id": ev_id,
        "direction": direction,
        "channel": ch,
        "summary": summary.strip(),
        "source": dict(source) if source else {},
        "linked_id": linked_id,
        "occurred_at": occurred_at,
        "created_at": created_at,
        # e-3555: 証跡が生まれた時点の商談/顧客のフェーズを set-once で刻む (container の
        # 現フェーズを既定に、retarget しても不変)。
        "created_in_phase": created_in_phase or container.get("phase", ""),
    }
    # e-3544: 本文欄は非空のときだけ書く (空なら key ごと省いて前方互換を保つ)。
    body_txt = (body or "").strip()
    if body_txt:
        record["body"] = body_txt
    node.setdefault(arm, []).append(record)
    return record


def iter_work_items(data: dict):
    """Yield ``(work_item, target, arm)`` for every planned work item across
    occupations, profession-agnostically (ms-142 e-5009).

    Consumes ``profession_manifest``'s ``work_item_arm`` so a caller walks
    development tasks (a milestone's ``entries`` filtered to ``type == "task"``)
    AND sales activities (an opportunity's whole ``activities`` arm) through ONE
    read path, with no ``if profession`` branch. This is the occupation-agnostic
    work-item spine the deadline enumeration (e-5010) and the completeness harness
    build on, so an L2 capability never has to name ``data['milestones']`` /
    ``entries`` / ``activities`` itself.

    Per yielded tuple:
      - ``work_item`` — the raw work-item record (a dev task entry / a sales
        activity), verbatim (with its own nested fields).
      - ``target`` — the parent Target record it lives under (the milestone /
        opportunity). Yielded so a caller can resolve target-scoped context (the
        claiming session for a deadline reminder = ``target['occupation']
        ['session_id']``) without a second lookup.
      - ``arm`` — the arm name the item came from (``"entries"`` / ``"activities"``),
        for labelling.

    The manifest's ``item_type`` discriminates a SHARED arm: dev ``entries`` hold
    tasks AND commits, so only ``type == "task"`` items are work items (commits
    are evidence, never yielded here); a ``None`` item_type means every item in
    the arm is a work item (sales activities). Target classes with no
    ``work_item_arm`` (acquisitions) contribute nothing. Scope follows
    ``profession_manifest`` (milestones + opportunities), matching the deadline
    enumeration it will replace."""
    for tc in profession_manifest(data)["target_classes"]:
        wia = tc["work_item_arm"]
        if not wia:
            continue
        arm, item_type = wia["arm"], wia["item_type"]
        for target in data.get(tc["collection"], []) or []:
            for item in target.get(arm, []) or []:
                if not isinstance(item, dict):
                    continue
                if item_type is not None and item.get("type") != item_type:
                    continue
                yield item, target, arm


def iter_deadline_candidates(data: dict):
    """Yield every deadline-bearing candidate across occupations, each a dict
    (ms-142 e-5010 — the SINGLE occupation-agnostic enumeration both deadline
    call sites share)::

        {"item": <record>, "kind": "milestone"|"task"|"activity"|…,
         "label": <str>, "target_id": <parent target id>,
         "target_status": <parent target status>,
         "recipient": <claiming session_id or "">, "context": <breadcrumb>}

    Enumerates Target level (a milestone's ``target_date``) via
    ``iter_target_records`` PLUS work items (dev task / sales activity
    ``deadline``) via ``iter_work_items``. The server reminder
    (``server/app.py:_deadline_reminder_candidates``) and the session-start
    display (``beacon deadline due`` → ``scripts/session-start-deadlines.py``)
    both consume THIS, so neither names ``data['milestones']`` / ``entries`` /
    ``activities`` — a new occupation's deadlines light up with zero edit at
    either site. This only ENUMERATES; the L2 temporal rule
    (``deadline.work_item_temporal_status``) is applied by callers, keeping the
    ``deadline`` module collection-agnostic (capability 台帳 L2).

    ``recipient`` is the session claiming the item's Target (its
    ``occupation.session_id``): the milestone for a task, the opportunity for an
    activity; '' when unclaimed. ``kind`` is the occupation-agnostic label the
    reminder message stamps. ``target_status`` is the PARENT Target's status (a
    display can drop work items under a terminal Target without the enumerator
    imposing that policy). ``context`` is a human breadcrumb — the target id, or
    ``"<target_id> / <item_id>"`` for a work item."""
    for target in iter_target_records(data):
        recipient = (target.get("occupation") or {}).get("session_id", "") or ""
        tid = target.get("id", "")
        yield {
            "item": target,
            "kind": _wm.target_kind(tid) or "target",
            "label": target.get("title") or target.get("label") or tid,
            "target_id": tid,
            "target_status": target.get("status", ""),
            "recipient": recipient,
            "context": tid,
        }
    arm_kind = {tc["work_item_arm"]["arm"]: tc["work_item_arm"]["kind"]
                for tc in profession_manifest(data)["target_classes"]
                if tc["work_item_arm"]}
    for item, target, arm in iter_work_items(data):
        recipient = (target.get("occupation") or {}).get("session_id", "") or ""
        tid, iid = target.get("id", ""), item.get("id", "")
        yield {
            "item": item,
            "kind": arm_kind.get(arm, arm),
            "label": item.get("description") or iid,
            "target_id": tid,
            "target_status": target.get("status", ""),
            "recipient": recipient,
            "context": f"{tid} / {iid}" if iid else tid,
        }


# Trek scope narrowing vocabulary (ms-109 e-3699 / fable review B-2).
#
# A Trek scope entry narrows to a single target inside a project. Which target
# KINDS are sliceable is occupation-specific: development slices by
# milestone / operation / task; sales by opportunity / account. Trek is L1
# (project-vision: L1 — the coordination substrate including Trek — is domain-
# invariant), and a Trek can span a development and a sales project at once. A
# scope entry carries only ``{project, <kind>: ref}`` — not the occupation —
# so the recognised vocabulary is the UNION across occupations. Registering a
# new occupation's kinds HERE (not editing trek.py) is what keeps Trek from
# hardcoding development vocabulary — the exact L1 domain-leak fable B-2 caught.
NARROWING_KINDS = {
    "dev": ("milestone", "operation", "task"),
    "sales": ("opportunity", "account"),
}


def all_narrowing_kinds(data: dict | None = None) -> tuple:
    """Return the union of every occupation's Trek scope narrowing kinds,
    de-duplicated, in registration order (development first so the legacy
    identity/target_kind resolution order — milestone, operation, task — is
    unchanged for existing dev Treks; sales kinds append after). With ``data``,
    descriptor-defined kinds append after the built-ins (ms-122 e-3957); without
    it, only the built-in seed (keeps trek.py's import-time call unchanged)."""
    out: list = []
    for kinds in NARROWING_KINDS.values():
        for k in kinds:
            if k not in out:
                out.append(k)
    if data:
        for desc in _td.load_descriptors(data):
            k = (desc.get("kind") or "").strip() if isinstance(desc, dict) else ""
            if k and k not in out:
                out.append(k)
    return tuple(out)


# The id prefix each narrowing kind's target ids carry. Lets a CLI
# ``project:ref`` scope argument infer the narrowing kind from the ref alone,
# so the parser does not hardcode the vocabulary (ms-109 e-3699). Keeping this
# beside NARROWING_KINDS means a new occupation registers its kinds AND their
# id prefixes in one place.
NARROWING_ID_PREFIXES = {
    "milestone": "ms-",
    "operation": "op-",
    "task": "e-",
    "opportunity": "opp-",
    "account": "acc-",
}


def narrowing_id_prefixes(data: dict | None = None) -> dict:
    """Return the ``kind -> id prefix`` map. Without ``data``, the built-in
    seed; with ``data``, plus each descriptor's ``kind -> id_prefix`` (ms-122
    e-3957). Built-in kinds are never overridden."""
    if not data:
        return dict(NARROWING_ID_PREFIXES)
    merged = dict(NARROWING_ID_PREFIXES)
    for desc in _td.load_descriptors(data):
        if not isinstance(desc, dict):
            continue
        kind = (desc.get("kind") or "").strip()
        prefix = (desc.get("id_prefix") or "").strip()
        if kind and prefix and kind not in merged:
            merged[kind] = prefix
    return merged


def narrowing_kind_for_ref(ref: str, data: dict | None = None) -> str:
    """Return the narrowing kind whose id prefix ``ref`` matches, or ``""`` when
    none does. Longest matching prefix wins so ``opp-3`` resolves to
    ``opportunity`` rather than colliding with ``op-`` (operation) — though the
    current prefixes are disjoint, the longest-match rule keeps it robust if a
    future prefix nests inside another. With ``data``, descriptor id prefixes
    are considered too (ms-122 e-3957)."""
    ref = (ref or "").strip()
    best_kind, best_len = "", -1
    for kind, prefix in narrowing_id_prefixes(data).items():
        if ref.startswith(prefix) and len(prefix) > best_len:
            best_kind, best_len = kind, len(prefix)
    return best_kind
