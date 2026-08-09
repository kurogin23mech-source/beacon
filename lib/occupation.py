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


# The project.json keys under which each occupation stores its Target records.
# This registry is the ONE place that knows "which collections are Targets"
# across occupations; occupation-agnostic base code (work_model / work_base)
# must NOT carry these names. Shared-frame code that needs the RAW Target
# records (not the projected shape) — e.g. session_log aggregation — asks here
# instead of hardcoding the collection names itself (ms-108 e-3701 / fable
# review B-1: keep occupation knowledge in the registry layer).
TARGET_COLLECTIONS = ("milestones", "opportunities")


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
# DECLARATIVE DATA (``_ARM_ROLES`` / ``_ARM_PHASE_BALL`` keyed by collection),
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

# Per-collection arm classification. Keyed by collection name (parallel to
# TARGET_DECOMPOSITION) so the two read together. ``work_item_arm`` names the arm
# holding planned work + how a work item is identified inside it; ``item_type``
# is ``None`` when every item in the arm is a work item (sales activities), or an
# entry ``type`` string when the arm is shared (dev ``entries`` hold tasks AND
# commits → work items are ``type == "task"``). ``evidence_arms`` names where
# proof/changelog records live (dev commits ride the SAME entries arm; sales
# evidence is its own communications arm).
_ARM_ROLES = {
    "milestones": {
        "work_item_arm": {"arm": "entries", "item_type": "task"},
        "evidence_arms": [{"arm": "entries", "item_type": "commit"}],
    },
    "opportunities": {
        "work_item_arm": {"arm": "activities", "item_type": None},
        "evidence_arms": [{"arm": "communications", "item_type": None}],
    },
    "accounts": {
        "work_item_arm": {"arm": "nurturings", "item_type": None},
        "evidence_arms": [{"arm": "communications", "item_type": None}],
    },
    # acquisitions carry no fat arms (inline work items, no child changelog) so
    # they declare no work-item / evidence arm — an honest empty classification.
    "acquisitions": {
        "work_item_arm": None,
        "evidence_arms": [],
    },
}

# The exclusive phase + who-has-the-ball model per collection (SPEC 方針 1 lists
# "phase・ball" among the slots). Sales Targets advance through a phase funnel and
# carry the ball; development milestones do not (their progress is task ratios /
# evidence), so dev's phase_ball is ``None`` — a declared absence, not a gap.
_ARM_PHASE_BALL = {
    "milestones": None,
    "opportunities": {"phase_field": "phase", "ball_field": "who_has_the_ball"},
    "accounts": {"phase_field": "phase", "ball_field": None},
    "acquisitions": None,
}

# collection -> target-class kind for the built-in occupations. Bridges the
# collection-keyed registries (TARGET_DECOMPOSITION / _ARM_ROLES) to the kind-
# keyed ones (NARROWING_ID_PREFIXES). Descriptor collections resolve their kind
# from the descriptor itself, so this only needs the built-ins.
_COLLECTION_KIND = {
    "milestones": "milestone",
    "opportunities": "opportunity",
    "accounts": "account",
    "acquisitions": "acquisition",
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


def _arm_roles_for(data: dict | None, collection: str, arms: tuple) -> dict:
    """Return ``{work_item_arm, evidence_arms}`` for a collection. Built-in
    collections use the ``_ARM_ROLES`` seed. A descriptor-defined collection has
    no seed entry, so its roles are derived from its declared arms following the
    thick-frame convention (``_DEFAULT_ARMS`` = work_items / evidence): the
    ``work_items`` arm is the work-item arm and ``evidence`` is an evidence arm,
    each with ``item_type=None`` (every item in the arm plays that role). This is
    what lets a NEW occupation light up arm-walking capabilities by DECLARING its
    manifest, with no edit here (ms-142 の芯)."""
    seed = _ARM_ROLES.get(collection)
    if seed is not None:
        return {
            "work_item_arm": dict(seed["work_item_arm"])
            if seed["work_item_arm"] else None,
            "evidence_arms": [dict(a) for a in seed["evidence_arms"]],
        }
    work_item_arm = {"arm": "work_items", "item_type": None} \
        if "work_items" in arms else None
    evidence_arms = [{"arm": "evidence", "item_type": None}] \
        if "evidence" in arms else []
    return {"work_item_arm": work_item_arm, "evidence_arms": evidence_arms}


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
              "phase_ball": None,
            },
            ...
          ],
        }

    Composed from the existing registries (``target_collections`` /
    ``target_decomposition`` / ``narrowing_id_prefixes`` / ``all_narrowing_kinds``
    — all untouched source of truth) plus the ``_ARM_ROLES`` / ``_ARM_PHASE_BALL``
    classification seed, and it carries ms-122 descriptor collections for free.
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
        roles = _arm_roles_for(data, collection, arms)
        target_classes.append({
            "kind": kind,
            "collection": collection,
            "id_field": spec.get("id_field", "id"),
            "id_prefix": prefixes.get(kind, ""),
            "narrowing": kind in narrowing,
            "arms": arms,
            "work_item_arm": roles["work_item_arm"],
            "evidence_arms": roles["evidence_arms"],
            "phase_ball": _ARM_PHASE_BALL.get(collection),
        })
    return {"profession": prof, "target_classes": target_classes}


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
