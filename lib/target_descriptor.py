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
                label, where=f"phase '{pkey}'", allow_required=False))

    return problems


def _validate_fields(fields: list, label: str, where: str,
                     *, allow_required: bool = True) -> list:
    """Return problems for a field list: each field needs a non-empty ``key``,
    field keys must be unique within their scope, and a declared ``type`` must
    be in ``ALLOWED_FIELD_TYPES``.

    ``allow_required=False`` additionally rejects ``required: true`` on a field.
    Used for PHASE fields (ms-122 AX finding): a base field's ``required`` is
    enforced at ``create_target``, but there is no mechanism yet to SET or
    enforce a phase field on ``advance`` — so declaring ``required`` on a phase
    field would be a silent no-op (the descriptor author believes it is
    enforced, nothing enforces it). Rejecting it in validation turns that false
    promise into an explicit, discoverable error until an advance-time field
    path exists (follow-up)."""
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
        if not allow_required and field.get("required"):
            problems.append(
                f"[{label}] {where} の field '{fkey}' に required は指定できません "
                f"(phase field を必須にする充足経路が未実装 — base field で必須化するか "
                f"advance --field 対応を待ってください)")
    return problems


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
