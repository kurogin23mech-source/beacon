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

# The additive top-level project.json key holding the project's ADOPTED
# target-class kinds (ms-147 e-5397). This is the project's own copy of which
# built-in-as-data classes it enumerates — seeded from the profession manifest
# at init and thereafter the truth, so changing a profession's defaults later
# does NOT retro-alter an already-created project (SPEC 方針3 = 複写, not live
# inheritance). ABSENT (key not present) means "written before this feature" and
# read-paths fall back to the live profession-default derivation (tolerant
# compat); PRESENT-but-empty means "adopts no built-in class" and is honoured.
ADOPTED_TARGET_CLASSES_KEY = "adopted_target_classes"

# Child-arm field declarations (ms-146 e-5344). A descriptor may declare the
# fields its WORK ITEMS / EVIDENCE carry, exactly as ``fields`` declares the
# target's own. Both are OPTIONAL and read tolerantly: a descriptor written
# before this feature (backoffice's 契約 / 評価, an authored class) reads as
# "no child fields" and behaves exactly as before — the additive-only /
# tolerant-read compat contract (memo pnhATs37xgIxEkpFI8uR) holds by
# construction, so nothing needs migrating.
WORK_ITEM_FIELDS_KEY = "work_item_fields"
EVIDENCE_FIELDS_KEY = "evidence_fields"

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
    """Return the RAW user-declared target-class descriptors (the list under
    ``target_classes``), or ``[]`` when the key is absent / not a list. A
    project.json written before this feature reads as no descriptors, so
    occupation code that consults this gets exactly the built-in (code) classes
    and no error — the additive/tolerant compat contract in action.

    ⚠ RAW = authoring-only (ms-142 e-5161). This returns ONLY what the user wrote;
    it does NOT include profession-DEFAULT descriptors (dev's built-in ``release``).
    Use it for authoring / validation (``target-class list``, ``append_descriptor``,
    ``validate_target_classes``) where the user's declared set is the subject. For
    REGISTRY / RESOLUTION reads (projection, claim, state model, phase advance) call
    ``occupation.effective_descriptors`` / ``occupation.effective_get_descriptor``
    instead — those union in the profession defaults, so a built-in class like
    release resolves. Calling THIS for a registry read silently misses release."""
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
    """Return the RAW user-declared descriptor whose ``kind`` matches, or ``None``.
    First match wins (a duplicate kind is a validation error surfaced elsewhere;
    readers stay deterministic).

    ⚠ RAW = user list only (ms-142 e-5161): this returns ``None`` for a profession-
    DEFAULT class (dev's built-in ``release``) because the user never declared it.
    A registry / resolution caller that must find a built-in-as-data class MUST use
    ``occupation.effective_get_descriptor`` instead (it unions the profession
    defaults). Reserve this raw accessor for authoring paths that operate on exactly
    what the user wrote."""
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


def field_choices(field: dict) -> list:
    """Return the fixed set of values a field declaration allows, or ``[]`` when
    it allows any value (ms-146 e-5338).

    WHY a field needs this: "効いたか" is only useful as 効いた / 効いてない /
    わからない. As free text it is three spellings of the same thing plus typos,
    and a MECHANISM cannot count "効いてない twice in a row" over prose. A
    declared choice list is what turns a note into something the engine can
    reason about — which is the whole difference between a diary and a signal."""
    got = field.get("choices") if isinstance(field, dict) else None
    if not isinstance(got, list):
        return []
    return [c for c in got if isinstance(c, str) and c.strip()]


def check_field_value(field: dict, value) -> str:
    """Return a human-facing problem string when ``value`` is not allowed for
    this field declaration, or ``""`` when it is fine.

    Today the only constraint is ``choices``; the function exists so every write
    path (target create, phase advance, work item, evidence) asks ONE place
    rather than each re-implementing the check — the seam a future constraint
    (range, pattern) lands in without a four-site edit."""
    allowed = field_choices(field)
    if not allowed:
        return ""
    if value in (None, ""):
        return ""          # presence is the `required` check's job, not this one
    if str(value) not in allowed:
        key = (field.get("key") or "").strip()
        return (f"field '{key}' の値 '{value}' は選べません "
                f"(選択肢: {' / '.join(allowed)})")
    return ""


def base_fields(desc: dict) -> list:
    """Return the target-level fields a descriptor declares (present regardless
    of phase). Empty when none. Malformed field entries are skipped."""
    return [f for f in (desc.get("fields") or []) if isinstance(f, dict)]


def work_item_fields(desc: dict) -> list:
    """Return the fields a descriptor declares for its WORK ITEMS — the unit of
    doing (a development task / a sales activity equivalent). Empty when none.

    WHY this exists (ms-146 e-5344): ``occupation.add_work_item`` — the generic
    entry point a dev task / sales activity rides — accepts ``**extra`` so a
    profession can carry its own per-item fields (a task's ``priority``, an
    activity's ``deadline``). The descriptor path (``target_engine.add_work_item``)
    took a description and nothing else, so a DATA-defined target-class was the one
    kind whose work items could carry no fields at all. That asymmetry made a
    per-item declaration (e.g. an executive class's 時間予算) impossible to express
    as data — the very thing descriptors exist for. Declaring the shape here keeps
    "機構は基底 / 語彙は記述子" intact: the engine enforces, the descriptor names."""
    return [f for f in (desc.get(WORK_ITEM_FIELDS_KEY) or [])
            if isinstance(f, dict)]


def evidence_fields(desc: dict) -> list:
    """Return the fields a descriptor declares for its EVIDENCE records — the
    append-only proof that something happened (a commit / communication
    equivalent). Empty when none. Same rationale as ``work_item_fields``; an
    evidence record that can only carry a free-text summary cannot express a
    structured observation (e.g. 効いたか = did this move the objective), which is
    what turns a pile of notes into something a mechanism can reason over."""
    return [f for f in (desc.get(EVIDENCE_FIELDS_KEY) or [])
            if isinstance(f, dict)]


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

# ms-147 e-5375: ``profession`` left OUT — it is a PROVENANCE tag now, not a
# required part of a well-formed descriptor. A class authored profession-neutrally
# (or with the field stripped) is valid; wiring never reads the stamp (SPEC 方針1).
_REQUIRED_STRING_KEYS = ("kind", "label", "id_prefix", "collection")


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

    # Child-arm field declarations (ms-146 e-5344) get the same structural check
    # as base fields — a duplicate / malformed key must surface at declaration
    # time, not as a confusing write-time rejection much later.
    problems.extend(
        _validate_fields(work_item_fields(desc), label, where="work_item"))
    problems.extend(
        _validate_fields(evidence_fields(desc), label, where="evidence"))

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

    # ms-146 e-5341 — no phase may be declared AFTER a terminal one.
    #
    # This is a shape rule, not a style preference. Beacon's行動原則 is "always
    # advance the target" (CORE doc 0drGJ9f3UaKO7p0RrKQD), and phases are an
    # ORDERED ladder, so whatever sits at the top is what the engine tells its
    # owner to climb toward. A class built to stop over-doing (ms-146) originally
    # proposed ...→十分やった→プラスアルファやった; that ordering would have made the
    # mechanism itself recommend the exact behaviour the class exists to curb.
    # The terminal is the top. Anything done beyond it is a RECORD kept on the
    # finished target (e.g. an overrun note declared as a terminal-phase field),
    # never a rung above it. See ms-146 SPEC 設計方針3.
    _phase_order = [p for p in (desc.get("phases") or []) if isinstance(p, dict)]
    _terminal_at = [i for i, p in enumerate(_phase_order)
                    if p.get("terminal") is True]
    if _terminal_at:
        _last_terminal = max(_terminal_at)
        for later in _phase_order[_last_terminal + 1:]:
            lkey = (later.get("key") or "?").strip() or "?"
            problems.append(
                f"[{label}] phase '{lkey}' が終端 phase より後に宣言されています。"
                f"フェイズは順序付きの梯子なので、終端の上に段を置くと機構が"
                f"『そこまで登れ』と指示することになります。終端を最後にし、"
                f"やり過ぎた分は終端 phase の field として記録してください")

    # changelog slot (ms-142 e-5255 AX review medium): if declared, it must be
    # {arm: str, recorder: str} with a DESCRIPTOR-SAFE recorder. Surfacing a bad
    # recorder HERE (load time) — rather than letting it degrade to a silent write-time
    # no-op indistinguishable from "no changelog declared" — gives the author a
    # diagnostic instead of a dropped write.
    cl = desc.get("changelog")
    if cl is not None:
        if not isinstance(cl, dict):
            problems.append(
                f"[{label}] 'changelog' は辞書である必要があります (現在: {cl!r})")
        else:
            arm = (cl.get("arm") or "").strip()
            recorder = (cl.get("recorder") or "").strip()
            if not arm:
                problems.append(f"[{label}] 'changelog.arm' が未設定です")
            if not recorder:
                problems.append(f"[{label}] 'changelog.recorder' が未設定です")
            elif recorder not in DESCRIPTOR_SAFE_CHANGELOG_RECORDERS:
                problems.append(
                    f"[{label}] 'changelog.recorder' は "
                    f"{' / '.join(sorted(DESCRIPTOR_SAFE_CHANGELOG_RECORDERS))} "
                    f"のいずれか必要です (現在: {recorder!r})。'milestone' は"
                    f"マイルストーン専用の組み込み戦略で、記述子からは使えません")

    return problems


def _validate_field_choices(field: dict, label: str, where: str) -> list:
    """Problems for a field's ``choices`` declaration (ms-146 e-5338). Absent is
    fine (unconstrained); present-but-empty is not — it would allow nothing and
    silently make the field unwritable."""
    if "choices" not in field:
        return []
    raw = field.get("choices")
    key = (field.get("key") or "?").strip() or "?"
    if not isinstance(raw, list) or not raw:
        return [f"[{label}] {where} の field '{key}' の choices は "
                f"1 つ以上の値を持つリストである必要があります"]
    bad = [c for c in raw if not isinstance(c, str) or not c.strip()]
    if bad:
        return [f"[{label}] {where} の field '{key}' の choices に "
                f"空または文字列でない値があります: {bad!r}"]
    if len(set(raw)) != len(raw):
        return [f"[{label}] {where} の field '{key}' の choices が重複しています"]
    return []


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
        problems.extend(_validate_field_choices(field, label, where))
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


# Recorder strategies a DESCRIPTOR class may declare for its changelog slot (ms-142
# e-5255 AX review high#2). Only ``"plain"`` (the generic append) is descriptor-safe:
# the built-in ``"milestone"`` strategy calls ``core.save_entry`` which resolves a real
# milestone, so a descriptor routing to it would CRASH at write time. Keeping this a
# named allowlist (not "anything in _CHANGELOG_RECORDERS") means a descriptor can only
# pick a strategy that works for a non-milestone Target. Kept HERE (not imported from
# occupation) to avoid a lib layering cycle; it is a small, stable set.
DESCRIPTOR_SAFE_CHANGELOG_RECORDERS = frozenset({"plain"})


def _changelog_role(desc: dict) -> dict | None:
    """Return a descriptor's declared CHANGELOG slot — ``{"arm": str, "recorder":
    str} | None`` (ms-142 e-5255). The changelog is the side-effect log
    ``occupation.record_target_entry`` appends onto a Target when a shared capability
    (a doc link) touches it. Built-in milestones/operations declare theirs in
    ``occupation._ARM_ROLES``; a descriptor occupation declares its own here so it,
    too, lights up the changelog write path by DECLARATION alone instead of falling
    to the historical no-op. ``recorder`` names the write strategy and MUST be one of
    ``DESCRIPTOR_SAFE_CHANGELOG_RECORDERS`` (``"plain"`` today) — the built-in-only
    ``"milestone"`` strategy is REJECTED here so a descriptor cannot route to
    save_entry and crash at write time (e-5255 AX review high#2). Absent / malformed /
    unsafe-recorder → ``None`` (no changelog → the class keeps no-oping, unchanged);
    ``validate_descriptor`` ALSO flags a bad recorder at load time so it is not only a
    silent miss."""
    raw = desc.get("changelog")
    if not isinstance(raw, dict):
        return None
    arm = (raw.get("arm") or "").strip()
    recorder = (raw.get("recorder") or "").strip()
    if arm and recorder in DESCRIPTOR_SAFE_CHANGELOG_RECORDERS:
        return {"arm": arm, "recorder": recorder}
    return None


def arm_roles(desc: dict) -> dict:
    """Return a descriptor's arm ROLES — ``{"work_item_arm": {arm, item_type,
    kind} | None, "evidence_arms": [{arm, item_type}, ...], "changelog": {arm,
    recorder} | None}`` (ms-142 e-5011 + e-5255).

    A descriptor declares its child ARMS (``decomposition.arms``, physical child
    lists) but, until ms-142, not which arm holds planned WORK ITEMS vs EVIDENCE vs
    the CHANGELOG side-log — the occupation-agnostic capabilities (deadline
    enumeration, the coverage harness, record_target_entry) need that role, not just
    the name. This reads an EXPLICIT declaration when present so a new occupation can
    name its arms ANYTHING (``duties`` / ``attestations``) and still light up every
    arm-walking capability — the true "declare, don't wire" contract (the
    alternative, matching hard-coded arm names ``work_items`` / ``evidence``, only lit
    up professions that used those magic names). Falls back to that name convention
    when no explicit roles are declared, so descriptors authored before this
    (``build_descriptor`` / ``backoffice_seed``, whose arms ARE ``work_items`` /
    ``evidence``) are unchanged. ``changelog`` is always read from the explicit slot
    (there is no name convention for it — a descriptor opts into a changelog only by
    declaring one). Tolerant: a malformed field degrades to the empty classification,
    never raises."""
    if not isinstance(desc, dict):
        return {"work_item_arm": None, "evidence_arms": [], "changelog": None}
    changelog = _changelog_role(desc)
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
        return {"work_item_arm": work_item_arm, "evidence_arms": evidence_arms,
                "changelog": changelog}
    # No explicit roles → thick-frame name convention (back-compat).
    arms = [a for a in ((desc.get("decomposition") or {}).get("arms") or [])
            if isinstance(a, str)]
    work_item_arm = {"arm": "work_items", "item_type": None, "kind": "work_item"} \
        if "work_items" in arms else None
    evidence_arms = [{"arm": "evidence", "item_type": None}] \
        if "evidence" in arms else []
    return {"work_item_arm": work_item_arm, "evidence_arms": evidence_arms,
            "changelog": changelog}


def build_descriptor(*, kind: str, label: str, dtype: str,
                     id_prefix: str, collection: str,
                     profession: str = "",
                     fields: Optional[list] = None,
                     phases: Optional[list] = None,
                     work_item_fields: Optional[list] = None,
                     evidence_fields: Optional[list] = None) -> dict:
    """Build a target-class descriptor dict from its parts (pure, no I/O). The
    shape matches what ``backoffice_seed`` hand-writes: kind / label / type /
    id_prefix / collection / decomposition / fields / phases. ``fields`` and
    ``phases`` are passed through verbatim (the caller built them from CLI flags
    or JSON). ``decomposition.arms`` defaults to the thick-frame arms so authored
    classes get WorkItems / Evidence like the built-in seed. ``work_item_fields``
    / ``evidence_fields`` (ms-146 e-5344) are emitted only when non-empty. This
    does NOT validate — the caller runs ``validate_*``.

    ms-147 e-5375: ``profession`` is OPTIONAL — a PROVENANCE tag, never a wiring
    input (SPEC 方針1). Omitting it builds a profession-neutral material; when
    given it is recorded verbatim (a shared class file's origin), but the stamp is
    still emitted only as data a reader can see, not a filter any read-path honours.
    Emitted only when non-empty so a neutral class carries no empty stamp key."""
    desc = {
        "kind": (kind or "").strip(),
        "label": (label or "").strip(),
        "type": (dtype or "").strip(),
        "id_prefix": (id_prefix or "").strip(),
        "collection": (collection or "").strip(),
        "decomposition": {"id_field": "id", "arms": list(_DEFAULT_ARMS)},
        "fields": list(fields or []),
        "phases": list(phases or []),
    }
    # ms-147 e-5375: the provenance stamp is emitted ONLY when the author gave one,
    # so a profession-neutral material carries no empty ``profession`` key (mirrors
    # the child-arm emission below — no empty keys in every project.json).
    prof = (profession or "").strip()
    if prof:
        desc["profession"] = prof
    # Child-arm declarations are emitted ONLY when the author declared some, so a
    # descriptor built without them is byte-identical to what this returned before
    # ms-146 e-5344 (no empty keys appearing in every project.json).
    if work_item_fields:
        desc[WORK_ITEM_FIELDS_KEY] = list(work_item_fields)
    if evidence_fields:
        desc[EVIDENCE_FIELDS_KEY] = list(evidence_fields)
    return desc


# ---------------------------------------------------------------------------
# Post-declaration edits (ms-146 e-5346) — ADDITIVE ONLY.
#
# WHY additive only: a declaration is not a schema in an empty database, it is a
# promise about records that ALREADY EXIST. Renaming a field key orphans every
# value written under the old name; removing one orphans the values themselves.
# Both are silent data loss dressed as an edit, which the
# ``data-immutability-principle`` CORE doc forbids. So this module can only ADD,
# and the CLI refuses remove/rename explicitly (with the reason) rather than
# leaving the author to guess why the flag does not exist.
#
# Retroactivity: adding a field marked ``required`` does NOT invalidate records
# created before it. Enforcement happens at WRITE time against the declaration
# current at that moment, so existing records keep their shape and stay readable.
# The CLI says how many existing records lack the new field, so "my old records
# are now non-conforming" is a stated fact rather than a discovery.
# ---------------------------------------------------------------------------

# Where a field declaration can live on a class. ``phase`` needs a phase key too.
FIELD_ARM_BASE = "base"
FIELD_ARM_WORK_ITEM = "work_item"
FIELD_ARM_EVIDENCE = "evidence"
FIELD_ARM_PHASE = "phase"
VALID_FIELD_ARMS = (FIELD_ARM_BASE, FIELD_ARM_WORK_ITEM, FIELD_ARM_EVIDENCE,
                    FIELD_ARM_PHASE)

# arm -> the descriptor key holding that arm's field list (phase is special:
# its list lives inside the matching phase entry).
_ARM_FIELD_KEY = {
    FIELD_ARM_BASE: "fields",
    FIELD_ARM_WORK_ITEM: WORK_ITEM_FIELDS_KEY,
    FIELD_ARM_EVIDENCE: EVIDENCE_FIELDS_KEY,
}


def add_field(desc: dict, field: dict, *, arm: str = FIELD_ARM_BASE,
              phase_key: str = "") -> list:
    """Append one field declaration to an existing descriptor, in place.

    Returns a list of problem strings; when it is non-empty NOTHING was written
    (validate-before-mutate, same contract as ``append_descriptor``). ``arm``
    selects which list the field joins — the target's own ``base`` fields, its
    ``work_item`` / ``evidence`` child arms, or a single ``phase`` (which also
    needs ``phase_key``).

    A key already declared on the SAME arm is refused: re-declaring is a redefine
    in disguise, and the second declaration would silently win for readers while
    records written under the first keep the old meaning. A phase field may not
    shadow a base field either, because ``fields_at_phase`` merges the two and the
    author would have no way to tell which one a value belongs to."""
    problems: list = []
    if not isinstance(desc, dict):
        return ["記述子が辞書ではありません"]
    if arm not in VALID_FIELD_ARMS:
        return [f"未知の arm '{arm}' です "
                f"(有効: {' / '.join(VALID_FIELD_ARMS)})"]
    label = (desc.get("kind") or "?").strip() or "?"

    if arm == FIELD_ARM_PHASE:
        pkey = (phase_key or "").strip()
        if not pkey:
            return [f"[{label}] phase の field を足すには phase key が必要です"]
        phase = get_phase(desc, pkey)
        if phase is None:
            return [f"[{label}] phase '{pkey}' は宣言されていません "
                    f"(宣言済: {' / '.join(phase_keys(desc)) or 'なし'})"]
        existing = [f for f in (phase.get("fields") or [])
                    if isinstance(f, dict)]
        # A phase field must not shadow a base field (fields_at_phase merges).
        base_keys = {(f.get("key") or "").strip() for f in base_fields(desc)}
        if (field.get("key") or "").strip() in base_keys:
            problems.append(
                f"[{label}] '{field.get('key')}' は基本 field と重複します "
                f"(phase field は基本 field を覆い隠せません)")
    else:
        existing = [f for f in (desc.get(_ARM_FIELD_KEY[arm]) or [])
                    if isinstance(f, dict)]
        phase = None

    where = arm if arm != FIELD_ARM_PHASE else f"phase '{phase_key}'"
    problems.extend(_validate_fields(existing + [field], label, where=where))
    if problems:
        return problems

    if arm == FIELD_ARM_PHASE:
        phase.setdefault("fields", []).append(field)
    else:
        desc.setdefault(_ARM_FIELD_KEY[arm], []).append(field)
    return []


def records_missing_field(data: dict, desc: dict, field_key: str, *,
                          arm: str = FIELD_ARM_BASE) -> int:
    """Count the ALREADY-EXISTING records of this class that carry no value for
    ``field_key`` — targets for ``base`` / ``phase``, child records for the
    ``work_item`` / ``evidence`` arms.

    This exists so the CLI can STATE the retroactivity consequence at the moment
    the author adds a required field, instead of leaving them to find out later
    that half their records predate the promise."""
    key = (field_key or "").strip()
    if not key:
        return 0
    coll = data.get((desc.get("collection") or "").strip())
    if not isinstance(coll, list):
        return 0
    missing = 0
    for rec in coll:
        if not isinstance(rec, dict):
            continue
        if arm in (FIELD_ARM_BASE, FIELD_ARM_PHASE):
            if not (rec.get(key) or "") and rec.get(key) not in (0, False):
                missing += 1
            continue
        child_key = "work_items" if arm == FIELD_ARM_WORK_ITEM else "evidence"
        for child in (rec.get(child_key) or []):
            if not isinstance(child, dict):
                continue
            if not (child.get(key) or "") and child.get(key) not in (0, False):
                missing += 1
    return missing


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
    # ms-147 e-5375: NO profession stamp. release lives in the global catalog
    # (BUILTIN_DESCRIPTOR_CATALOG), which SPEC 方針4 layer 1 defines as
    # profession-NEUTRAL material. Which profession adopts it by default is the
    # manifest's job (PROFESSION_DEFAULT_DESCRIPTORS maps dev → release), keyed
    # independently of this object — so the material carries no owner, and a
    # stamp here would contradict the catalog's neutrality (and be dead data now
    # that no read-path consults the stamp).
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


# Layer 1 (ms-147 e-5397 / SPEC 方針4) — the global catalog: every built-in-as-
# data descriptor, profession-NEUTRAL, indexed by kind. A material lives here,
# not on a profession, so more than one profession can adopt the same class.
# This is deliberately SEPARATE from the manifest below: which professions adopt
# a class by default (layer 2) can change without making an already-copied kind
# unresolvable (a project's copied set resolves against THIS, layer 1).
BUILTIN_DESCRIPTOR_CATALOG: dict = {
    "release": RELEASE_DESCRIPTOR,
}

# Layer 2 — the profession manifest: which catalog kinds a profession adopts by
# DEFAULT (built-in, modelled as data). Only dev has one today (release). A
# profession absent from this map contributes no defaults, so sales / a data-
# defined occupation are unchanged. This is the seed `beacon init` COPIES into a
# project's adopted set; after that the project's copy — not this map — is read.
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


# ---------------------------------------------------------------------------
# Target-class as material: the 3-layer model (ms-147 e-5397 / SPEC 方針4).
#
#   (1) global catalog  — every built-in-as-data descriptor, profession-neutral
#   (2) profession manifest — the kinds a profession adopts by DEFAULT (a named
#       subset of the catalog; PROFESSION_DEFAULT_DESCRIPTORS above IS this)
#   (3) project adopted set — copied from (2) at init, thereafter the truth
#
# The inversion this MS makes: read-paths stop deriving a project's built-in
# classes LIVE from its profession, and read the project's own COPIED adopted
# set instead. These helpers expose (1) and (2) as the seam the copy is made
# from; the copy itself lives in the project (ADOPTED_TARGET_CLASSES_KEY) and is
# resolved back to descriptors via ``resolve_adopted_descriptors``.
# ---------------------------------------------------------------------------

def builtin_descriptor_catalog() -> dict:
    """Return the global catalog of built-in-as-data descriptors as
    ``{kind: descriptor}`` (ms-147 e-5397, SPEC 方針4 layer 1). Profession-neutral:
    every built-in class a project could adopt, indexed by kind. Today the only
    member is ``release`` (dev's built-in), but a class is added here — not on a
    profession — so a second profession can adopt the same material. Independent
    of the manifest: a project's copied kind resolves here even if no profession
    adopts it by default any more (that is exactly the survive-a-manifest-change
    guarantee, SPEC 受入条件4)."""
    return dict(BUILTIN_DESCRIPTOR_CATALOG)


def profession_adopted_kinds(profession: str) -> list:
    """Return the target-class KINDS a profession adopts by default (ms-147
    e-5397, SPEC 方針4 layer 2) — the manifest's named subset of the catalog,
    e.g. dev → ``["release"]``, a profession with no built-in default → ``[]``.
    This is what ``beacon init`` COPIES into the project's adopted set."""
    return [d["kind"] for d in profession_default_descriptors(profession)
            if isinstance(d, dict) and (d.get("kind") or "").strip()]


def load_adopted_kinds(data: dict):
    """Return the project's COPIED adopted target-class kinds (list of strings),
    or ``None`` when the key is ABSENT (ms-147 e-5397). The None-vs-empty
    distinction is load-bearing: ``None`` = "project predates this feature, fall
    back to live profession-default derivation" (tolerant compat); ``[]`` =
    "adopts no built-in class, honour that". Malformed entries are skipped."""
    raw = (data or {}).get(ADOPTED_TARGET_CLASSES_KEY)
    if not isinstance(raw, list):
        return None
    return [k.strip() for k in raw if isinstance(k, str) and k.strip()]


def resolve_adopted_descriptors(data: dict) -> list:
    """Resolve the project's adopted kinds into built-in descriptors from the
    global catalog (ms-147 e-5397). Returns ``[]`` when the project adopts none.
    An adopted kind with no catalog entry is skipped (a data-defined class lives
    in ``target_classes``, not the built-in catalog, and is unioned separately by
    ``occupation.effective_descriptors``)."""
    kinds = load_adopted_kinds(data)
    if not kinds:
        return []
    catalog = builtin_descriptor_catalog()
    out: list = []
    for kind in kinds:
        desc = catalog.get(kind)
        if desc is not None:
            out.append(desc)
    return out


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
