"""Beacon target-class descriptors — data-defined Target classes (ms-122 e-3954).

A *target-class* is the kind of thing a project advances: development has
Milestone / Operation, sales has Opportunity / Account. Those two occupations
are wired in code (``core.py`` / ``sales_entities.py``). This module adds a
THIRD way to introduce a target-class: as **data** in ``project.json`` — a
descriptor — so a new occupation (e.g. back-office: 契約 / 評価 / 月次決算) can
be added without editing Beacon's code (ms-122 SPEC 3xu5R44xjMzvS9yLQO54 §3).

A descriptor declares only the STRUCTURE of a target-class — its name, whether
it is finite (single-shot, like a milestone) or ongoing (persistent, like an
operation), the fields it carries, and its ordered phases (each phase may add
fields that appear only once that phase is reached, SPEC §4). The occupation-
agnostic MECHANICS (advance a phase, mark done, stamp audit) live in
``work_base`` / ``work_model`` and are shared; a descriptor injects the
VOCABULARY those mechanics operate on. "機構は基底 / 語彙は記述子."

Descriptors live under the additive top-level key ``target_classes`` (a list).
Every read here goes through ``.get(key, default)`` so a project.json written
before this feature reads as "no descriptors" with no migration — the schema-
evolution compat contract (memo pnhATs37xgIxEkpFI8uR: additive-only / tolerant
read / full-dict write) is honoured by construction. Loading NEVER raises on a
malformed descriptor (best-effort, occupation code must not crash on bad data);
``validate_*`` is the separate explicit check that surfaces problems.

Like ``work_base`` / ``work_model`` this module performs no I/O: every function
is a pure transform over the values it is handed. Wiring the descriptors into
the occupation registry (``occupation.py`` OWNED_TARGET_CLASSES etc.) is a
separate task (e-3957); this module is the schema + loader + validator only.
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Schema vocabulary.
# ---------------------------------------------------------------------------

# The additive top-level project.json key holding the descriptor list.
TARGET_CLASSES_KEY = "target_classes"

# A target-class is either finite (single-shot, completes once — like a
# development milestone or a sales opportunity) or ongoing (persistent, recurs —
# like a development operation or a monthly close). SPEC §3.
TYPE_SINGLE_SHOT = "single-shot"
TYPE_PERSISTENT = "persistent"
VALID_TYPES = (TYPE_SINGLE_SHOT, TYPE_PERSISTENT)

# The field-value shapes a descriptor field may declare. Kept small on purpose:
# the descriptor describes a form, not a database. Unknown types are a
# validation warning, not a load failure (tolerant read).
ALLOWED_FIELD_TYPES = ("string", "text", "number", "date", "bool", "money")


# ---------------------------------------------------------------------------
# Tolerant loaders — never raise, missing keys read as empty.
# ---------------------------------------------------------------------------

def load_descriptors(data: dict) -> list:
    """Return the project's target-class descriptors (the raw list under
    ``target_classes``), or ``[]`` when the key is absent / not a list. A
    project.json written before this feature reads as no descriptors, so
    occupation code that consults this gets exactly the built-in (code) classes
    and no error — the additive/tolerant compat contract in action."""
    descriptors = data.get(TARGET_CLASSES_KEY)
    return descriptors if isinstance(descriptors, list) else []


def descriptor_kinds(data: dict) -> list:
    """Return the ``kind`` of every well-formed descriptor, in declaration
    order, skipping entries with no usable kind (tolerant read). Duplicates are
    preserved here (dedup / collision is a ``validate`` concern, not a read
    concern)."""
    out: list = []
    for desc in load_descriptors(data):
        if isinstance(desc, dict):
            kind = desc.get("kind")
            if isinstance(kind, str) and kind.strip():
                out.append(kind.strip())
    return out


def get_descriptor(data: dict, kind: str) -> Optional[dict]:
    """Return the descriptor whose ``kind`` matches, or ``None`` when no
    descriptor declares it. First match wins (a duplicate kind is a validation
    error surfaced elsewhere; readers stay deterministic)."""
    want = (kind or "").strip()
    if not want:
        return None
    for desc in load_descriptors(data):
        if isinstance(desc, dict) and (desc.get("kind") or "").strip() == want:
            return desc
    return None


# ---------------------------------------------------------------------------
# Phase accessors — the ordered phase list and the fields visible at a phase.
# ---------------------------------------------------------------------------

def phase_keys(desc: dict) -> list:
    """Return the ordered list of a descriptor's phase keys (empty when the
    class has no phases — a phase-less target-class is allowed). Skips malformed
    phase entries (tolerant read)."""
    out: list = []
    for phase in (desc.get("phases") or []):
        if isinstance(phase, dict):
            key = phase.get("key")
            if isinstance(key, str) and key.strip():
                out.append(key.strip())
    return out


def get_phase(desc: dict, phase_key: str) -> Optional[dict]:
    """Return the phase entry whose ``key`` matches within ``desc``, or
    ``None``."""
    want = (phase_key or "").strip()
    if not want:
        return None
    for phase in (desc.get("phases") or []):
        if isinstance(phase, dict) and (phase.get("key") or "").strip() == want:
            return phase
    return None


def terminal_phase_keys(desc: dict) -> list:
    """Return the phase keys flagged ``terminal: true`` — the phases that mean
    "this target is finished" (e.g. a contract's 締結). Used by the generic
    close mechanic (e-3956) to know which phase closes the target."""
    out: list = []
    for phase in (desc.get("phases") or []):
        if isinstance(phase, dict) and phase.get("terminal") is True:
            key = (phase.get("key") or "").strip()
            if key:
                out.append(key)
    return out


def base_fields(desc: dict) -> list:
    """Return the target-level fields a descriptor declares (present regardless
    of phase). Empty when none. Malformed field entries are skipped."""
    return [f for f in (desc.get("fields") or []) if isinstance(f, dict)]


def fields_at_phase(desc: dict, phase_key: str) -> list:
    """Return the fields visible once ``phase_key`` is reached: the class's base
    fields PLUS that phase's own field extension (SPEC §4 — a phase can surface
    fields that only appear there, e.g. a contract's 弁護士レビュー phase adds
    「レビュー依頼先」「想定リスク」). Base fields come first, then the phase's,
    in declaration order. An unknown phase returns just the base fields
    (tolerant)."""
    result = list(base_fields(desc))
    phase = get_phase(desc, phase_key)
    if phase is not None:
        result.extend(
            f for f in (phase.get("fields") or []) if isinstance(f, dict)
        )
    return result


# ---------------------------------------------------------------------------
# Validation — the explicit check (loaders never raise; this surfaces problems).
# ---------------------------------------------------------------------------

_REQUIRED_STRING_KEYS = ("kind", "label", "profession", "id_prefix", "collection")


def validate_descriptor(desc: dict) -> list:
    """Return a list of human-readable problem strings for one descriptor
    (empty list = valid). This is a STRUCTURE check, not a data-immutability
    check: it verifies a descriptor is well-enough-formed that the generic
    mechanics (create / advance / close) can operate on it. Never raises."""
    problems: list = []
    if not isinstance(desc, dict):
        return [f"記述子が辞書ではありません: {desc!r}"]

    label = (desc.get("kind") or "?").strip() or "?"

    for key in _REQUIRED_STRING_KEYS:
        val = desc.get(key)
        if not isinstance(val, str) or not val.strip():
            problems.append(f"[{label}] 必須項目 '{key}' が未設定です")

    dtype = desc.get("type")
    if dtype not in VALID_TYPES:
        problems.append(
            f"[{label}] 'type' は {' / '.join(VALID_TYPES)} のいずれか必要です "
            f"(現在: {dtype!r})")

    prefix = desc.get("id_prefix")
    if isinstance(prefix, str) and prefix.strip() and not prefix.endswith("-"):
        problems.append(
            f"[{label}] 'id_prefix' は '-' で終わるべきです (現在: {prefix!r})")

    # Field key uniqueness within the class (base fields).
    problems.extend(_validate_fields(base_fields(desc), label, where="base"))

    # Phase key uniqueness + per-phase field checks.
    seen_phases: set = set()
    for phase in (desc.get("phases") or []):
        if not isinstance(phase, dict):
            problems.append(f"[{label}] phase が辞書ではありません: {phase!r}")
            continue
        pkey = (phase.get("key") or "").strip()
        if not pkey:
            problems.append(f"[{label}] phase に 'key' がありません")
            continue
        if pkey in seen_phases:
            problems.append(f"[{label}] phase key '{pkey}' が重複しています")
        seen_phases.add(pkey)
        problems.extend(
            _validate_fields(
                [f for f in (phase.get("fields") or []) if isinstance(f, dict)],
                label, where=f"phase '{pkey}'"))

    return problems


def _validate_fields(fields: list, label: str, where: str) -> list:
    """Return problems for a field list: each field needs a non-empty ``key``,
    field keys must be unique within their scope, and a declared ``type`` must
    be in ``ALLOWED_FIELD_TYPES``.

    A phase field MAY be marked ``required`` (ms-124 e-4090): it is enforced at
    ``advance_target`` — advancing INTO a phase demands that phase's required
    fields be supplied (``advance --field``) or already set. This closes the
    ms-122 fence, where phase ``required`` was rejected because no advance-time
    field path existed; now that it does, the promise is real, not silent."""
    problems: list = []
    seen: set = set()
    for field in fields:
        fkey = (field.get("key") or "").strip()
        if not fkey:
            problems.append(f"[{label}] {where} の field に 'key' がありません")
            continue
        if fkey in seen:
            problems.append(
                f"[{label}] {where} の field key '{fkey}' が重複しています")
        seen.add(fkey)
        ftype = field.get("type")
        if ftype is not None and ftype not in ALLOWED_FIELD_TYPES:
            problems.append(
                f"[{label}] {where} の field '{fkey}' の type '{ftype}' は未知です "
                f"(許可: {' / '.join(ALLOWED_FIELD_TYPES)})")
    return problems


# ---------------------------------------------------------------------------
# Authoring — build a descriptor and append it to a project (ms-124 e-4091).
# The no-code onboarding path: a new target-class enters via this builder + the
# CLI that drives it, NOT by hand-editing project.json (which the project rules
# forbid). The builder is a pure transform; appending validates before writing.
# ---------------------------------------------------------------------------

# Default child arms a freshly-authored descriptor carries so its targets
# inherit the thick cognitive frame (WorkItems / Evidence, ms-124 e-4089).
_DEFAULT_ARMS = ["work_items", "evidence"]


def arm_roles(desc: dict) -> dict:
    """Return a descriptor's arm ROLES — ``{"work_item_arm": {arm, item_type,
    kind} | None, "evidence_arms": [{arm, item_type}, ...]}`` (ms-142 e-5011).

    A descriptor declares its child ARMS (``decomposition.arms``, physical child
    lists) but, until ms-142, not which arm holds planned WORK ITEMS vs EVIDENCE —
    the occupation-agnostic capabilities (deadline enumeration, the coverage
    harness) need that role, not just the name. This reads an EXPLICIT declaration
    when present so a new occupation can name its arms ANYTHING (``duties`` /
    ``attestations``) and still light up every arm-walking capability — the true
    "declare, don't wire" contract (the alternative, matching hard-coded arm
    names ``work_items`` / ``evidence``, only lit up professions that used those
    magic names). Falls back to that name convention when no explicit roles are
    declared, so descriptors authored before this (``build_descriptor`` /
    ``backoffice_seed``, whose arms ARE ``work_items`` / ``evidence``) are
    unchanged. Tolerant: a malformed field degrades to the empty classification,
    never raises."""
    if not isinstance(desc, dict):
        return {"work_item_arm": None, "evidence_arms": []}
    wia_raw = desc.get("work_item_arm")
    ev_raw = desc.get("evidence_arms")
    if wia_raw is not None or ev_raw is not None:
        work_item_arm = None
        if isinstance(wia_raw, dict) and (wia_raw.get("arm") or "").strip():
            work_item_arm = {
                "arm": wia_raw["arm"],
                "item_type": wia_raw.get("item_type"),
                "kind": (wia_raw.get("kind") or "").strip() or "work_item",
            }
        evidence_arms = []
        for e in (ev_raw or []):
            if isinstance(e, dict) and (e.get("arm") or "").strip():
                evidence_arms.append(
                    {"arm": e["arm"], "item_type": e.get("item_type")})
        return {"work_item_arm": work_item_arm, "evidence_arms": evidence_arms}
    # No explicit roles → thick-frame name convention (back-compat).
    arms = [a for a in ((desc.get("decomposition") or {}).get("arms") or [])
            if isinstance(a, str)]
    work_item_arm = {"arm": "work_items", "item_type": None, "kind": "work_item"} \
        if "work_items" in arms else None
    evidence_arms = [{"arm": "evidence", "item_type": None}] \
        if "evidence" in arms else []
    return {"work_item_arm": work_item_arm, "evidence_arms": evidence_arms}


def build_descriptor(*, kind: str, label: str, profession: str, dtype: str,
                     id_prefix: str, collection: str,
                     fields: Optional[list] = None,
                     phases: Optional[list] = None) -> dict:
    """Build a target-class descriptor dict from its parts (pure, no I/O). The
    shape matches what ``backoffice_seed`` hand-writes: kind / label /
    profession / type / id_prefix / collection / decomposition / fields /
    phases. ``fields`` and ``phases`` are passed through verbatim (the caller
    built them from CLI flags or JSON). ``decomposition.arms`` defaults to the
    thick-frame arms so authored classes get WorkItems / Evidence like the
    built-in seed. This does NOT validate — the caller runs ``validate_*``."""
    return {
        "kind": (kind or "").strip(),
        "label": (label or "").strip(),
        "profession": (profession or "").strip(),
        "type": (dtype or "").strip(),
        "id_prefix": (id_prefix or "").strip(),
        "collection": (collection or "").strip(),
        "decomposition": {"id_field": "id", "arms": list(_DEFAULT_ARMS)},
        "fields": list(fields or []),
        "phases": list(phases or []),
    }


def append_descriptor(data: dict, desc: dict) -> list:
    """Append ``desc`` to the project's ``target_classes`` and return the
    problem list (empty = appended OK). Validates the descriptor in isolation
    AND against the project's existing descriptors (a duplicate kind / id_prefix
    / collection would collide) BEFORE mutating; when problems are found nothing
    is written. The additive/tolerant compat contract holds: the key is created
    on first use, existing readers ignore it."""
    problems = validate_descriptor(desc)
    # Cross-descriptor collision against what's already declared.
    kind = (desc.get("kind") or "").strip()
    prefix = (desc.get("id_prefix") or "").strip()
    coll = (desc.get("collection") or "").strip()
    for existing in load_descriptors(data):
        if not isinstance(existing, dict):
            continue
        if kind and (existing.get("kind") or "").strip() == kind:
            problems.append(f"kind '{kind}' は既に宣言済みです")
        if prefix and (existing.get("id_prefix") or "").strip() == prefix:
            problems.append(f"id_prefix '{prefix}' は既に別の記述子が使用中です")
        if coll and (existing.get("collection") or "").strip() == coll:
            problems.append(f"collection '{coll}' は既に別の記述子が使用中です")
    if problems:
        return problems
    lst = data.get(TARGET_CLASSES_KEY)
    if not isinstance(lst, list):
        lst = []
        data[TARGET_CLASSES_KEY] = lst
    lst.append(desc)
    return []


# ---------------------------------------------------------------------------
# Profession-default descriptors — a target-class a profession ALWAYS has, even
# though no user declared it in ``target_classes`` (ms-142 e-5161 / T6).
#
# ``load_descriptors`` returns ONLY the user-declared raw list (uncontaminated —
# authoring / validation / ``target-class list`` operate on exactly what the user
# wrote). But some target-classes are a BUILT-IN part of an occupation that we
# choose to model AS a descriptor rather than hardcode a fourth registry branch
# for. Release is the first: it is dev's L3 "bundle several milestones' output,
# carry a version, publish→deploy" target-class (class-engine ideal §9). Modelling
# it as a descriptor means every L2 capability (projection / claim / phase advance /
# the coverage matrix) lights it up by the SAME "declare, don't wire" path a
# data-defined occupation rides — no ``if kind == "release"`` anywhere.
#
# These defaults are injected by ``occupation.effective_descriptors`` (defaults +
# the raw user list), which the registry read-paths consult; ``load_descriptors``
# stays raw so the user's declared set is never polluted with a built-in. sales
# declares no default here — "職種固有 target-class を宣言で足す" means release is
# dev's, not everyone's (§9).
# ---------------------------------------------------------------------------

# Release: dev's L3 target-class. It bundles milestones (a cross-target reference
# resolved by the generic ``occupation.bundled_targets``, not owned) and advances
# draft → published → deployed (terminal). It carries a ``version`` field and NO
# work-item / evidence arms — a release's "work" is the milestones it bundles and
# its "evidence" is the git tag / deploy record the existing release.yml path owns,
# so on the coverage matrix its deadline + 証跡 cells are DECLARED N/A (identical
# to operation's shape). ``work_item_arm`` / ``evidence_arms`` are declared
# EXPLICITLY (not left to the name convention) so the absence is data a reader can
# see, mirroring how ``occupation._ARM_ROLES`` writes operation's None as data.
#
# ⚠ collection is ``release_targets``, NOT ``releases`` (ms-142 e-5161 裁定 A):
# ``data["releases"]`` ALREADY holds the deploy-flow release-NOTE ledger
# (``release-YYYYMMDD-N`` records with semver / deploy_ids, written by
# ``cmd_deploy`` / read by ``cmd_trigger``'s release-due count — the release.yml
# path the design deliberately does NOT touch). Reusing ``releases`` would make
# ``project_targets`` / the claim view enumerate those release-notes as bogus
# release TARGETS. The L3 target-class is a DISTINCT thing (a release you push
# draft→published→deployed, bundling milestones) from a published release-note, so
# it gets its own collection; the two coexist without collision.
RELEASE_DESCRIPTOR: dict = {
    "kind": "release",
    "label": "リリース",
    "profession": "dev",
    "type": TYPE_SINGLE_SHOT,
    "id_prefix": "rel-",
    "collection": "release_targets",
    "decomposition": {"id_field": "id", "arms": []},
    "work_item_arm": None,
    "evidence_arms": [],
    "fields": [
        {"key": "version", "type": "string", "label": "バージョン"},
    ],
    "phases": [
        {"key": "draft"},
        {"key": "published"},
        {"key": "deployed", "terminal": True},
    ],
}


# profession -> the descriptors it ALWAYS has (built-in, modelled as data). Only
# dev has one today (release). A profession absent from this map contributes no
# defaults, so sales / a data-defined occupation are unchanged.
PROFESSION_DEFAULT_DESCRIPTORS: dict = {
    "dev": [RELEASE_DESCRIPTOR],
}


def profession_default_descriptors(profession: str) -> list:
    """Return the built-in-as-data descriptors a profession ALWAYS carries (e.g.
    dev's ``release``), or ``[]`` for a profession with none (ms-142 e-5161). These
    are NOT in ``load_descriptors`` (the raw user list): the registry read-paths
    union them in via ``occupation.effective_descriptors`` so authoring / the raw
    ``target_classes`` list stay uncontaminated by a built-in the user never wrote."""
    return list(PROFESSION_DEFAULT_DESCRIPTORS.get(
        (profession or "").strip().lower(), []))


def validate_target_classes(data: dict) -> dict:
    """Validate every descriptor in the project. Returns a dict mapping a
    problem-label → its list of problem strings. Two cross-descriptor checks are
    added under synthetic labels: duplicate ``kind`` and duplicate ``id_prefix``
    / ``collection`` (two classes must not share an id space or storage key, or
    their records would collide). An empty dict means all descriptors are
    valid. Never raises — callers (a future ``beacon doctor`` / CLI) decide how
    to surface."""
    result: dict = {}
    descriptors = load_descriptors(data)

    for desc in descriptors:
        problems = validate_descriptor(desc)
        if problems:
            key = ((desc.get("kind") if isinstance(desc, dict) else None)
                   or "(kind未設定)")
            result.setdefault(str(key), []).extend(problems)

    # Cross-descriptor uniqueness: kind / id_prefix / collection.
    for attr, human in (("kind", "kind"),
                        ("id_prefix", "id_prefix"),
                        ("collection", "collection")):
        seen: dict = {}
        for desc in descriptors:
            if not isinstance(desc, dict):
                continue
            val = (desc.get(attr) or "").strip()
            if not val:
                continue
            seen[val] = seen.get(val, 0) + 1
        for val, count in seen.items():
            if count > 1:
                result.setdefault("(重複)", []).append(
                    f"{human} '{val}' が {count} 個の記述子で重複しています")

    return result
