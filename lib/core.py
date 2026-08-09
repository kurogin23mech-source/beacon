"""Beacon Core - Pure business logic for project data manipulation.

All functions take a project data dict and explicit parameters,
returning results without performing I/O. This module is shared
between the CLI (commands.py) and the API (server/app.py).
"""

from __future__ import annotations

import datetime as _dt
import re

import work_base
import work_model  # ms-109 e-3559: 職種非依存の Target/WorkItem 正準アクセサ
import transition_approval as _ta  # ms-119 e-3912: 職種中立な目的達成レビュー primitive
import report_primitive  # ms-114 e-3742: report→server-authoring の継ぎ目


def _now_iso() -> str:
    """Return current UTC time as ISO8601 string with seconds precision.

    Thin wrapper over ``work_base.now_iso`` — the implementation moved to the
    occupation-agnostic base (ms-109 e-3558); the name is kept so existing
    callers and test monkeypatch targets in this module are unchanged.
    """
    return work_base.now_iso()


def _get_actor() -> str:
    """Return the current operator: 'claude' inside Claude Code, else git user email.

    Thin wrapper over ``work_base.current_actor`` (implementation moved to the
    shared base in ms-109 e-3558; name kept for backward compatibility).
    """
    return work_base.current_actor()


def _clean_author(author: dict | None) -> dict:
    """Strip an ``author`` dict to the canonical {user_id, email, display_name}.

    ms-78 / e-1909 — the server side resolves the *human* identity of the
    operator and threads it through to ``meta.author`` on commits / tasks
    so the Web UI can show a human-friendly label without walking the
    users collection on every render. We allowlist exactly three keys to
    keep the on-disk shape stable, and drop empty strings / Nones so a
    user who hasn't set a display_name yet stays absent from that field
    (= falsy fallback path in the UI).
    """
    if not isinstance(author, dict):
        return {}
    out: dict = {}
    for k in ("user_id", "email", "display_name"):
        v = author.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


VALID_STATUSES = {"todo", "in_progress", "in_review", "approved", "waiting", "done", "observing", "cancelled"}
VALID_ENTRY_TYPES = {"commit", "task", "note", "save", "pr", "run_record", "incident", "operation_task", "target-transition-approval"}

# PR lifecycle: in_review → approved → merged (or closed/rejected)
# "open" is reserved for Phase 2 auto-detection via GitHub API (external PRs not yet picked up by beacon)
VALID_PR_STATUSES = {"open", "in_review", "approved", "merged", "closed"}
VALID_REVIEW_STATUSES = {"pending", "approved", "changes_requested", "rejected"}
VALID_RUN_STATUSES = {"ok", "warning", "error"}
VALID_INCIDENT_STATUSES = {"open", "resolved"}
# ms-119 e-3912: 目的達成レビュー entry のライフサイクル (build=pending →
# approve=approved / reject=cancelled、lib/transition_approval.append_verdict)。
VALID_TRANSITION_APPROVAL_STATUSES = {"pending", "approved", "cancelled"}
VALID_PRIORITIES = {"highest", "high", "medium", "low", "lowest"}
# ms-120 / e-3895: `medium` is the canonical mid-tier priority — it matches
# every priority system an AI has a prior for (Jira / GitHub / monitoring all
# use medium/med). `middle` is a positional word, not a severity word, and was
# the old local name; it stays accepted as a deprecated alias so nothing breaks,
# but input normalizes to `medium` for storage and help/errors show `medium`.
_PRIORITY_ALIASES = {"middle": "medium"}
_ACCEPTED_PRIORITIES = VALID_PRIORITIES | set(_PRIORITY_ALIASES)

# ms-126: `untriaged` is a *sentinel*, not a severity. It records "a machine
# created this entry and no human has judged its priority yet". It is stored in
# the same ``priority`` field, but deliberately kept OUT of ``VALID_PRIORITIES``
# / ``_ACCEPTED_PRIORITIES`` so it can never be confused with a real severity
# and ``normalize_priority`` never maps it to ``medium``. The machine paths
# (issue import / review-apply / roadmap bulk / dispatch) pass this in via
# ``allow_untriaged=True`` when they cannot supply a human-judged severity, so
# the missing judgement becomes *visible debt* (surfaced by the
# ``untriaged-backlog`` trigger) instead of a silent empty field. The human
# entry paths (``beacon milestone add`` / ``beacon task add``) leave
# ``allow_untriaged=False`` and are hard-rejected on an empty priority — a
# forcing function that makes a person actually pick one of the 5 severities.
UNTRIAGED_PRIORITY = "untriaged"
# The set the core write-paths accept when a value is present. ``untriaged``
# joins here (so a machine may store it) but NOT in ``VALID_PRIORITIES`` (so it
# is never presented as a choosable severity in help / errors).
_STORABLE_PRIORITIES = _ACCEPTED_PRIORITIES | {UNTRIAGED_PRIORITY}

# ms-126 (AX#6): the canonical *scale* order for listing the 5 severities. Every
# surface (error / prompt / trigger / help) lists them in this order so the name
# alone carries "this is an ordered severity axis". Never list via ``sorted()``
# (alphabetical) — that destroys the scale signal.
_PRIORITY_SCALE_ORDER = ["highest", "high", "medium", "low", "lowest"]

# Shared message for the "you must pick a priority" rejection so the CLI human
# paths (milestone add / task add) speak with one voice and always list the 5.
_PRIORITY_REQUIRED_MSG = (
    "Priority is required. Choose one of: "
    + ", ".join(_PRIORITY_SCALE_ORDER)
    + " (highest = 大目的への寄与が最大 / lowest = 最小)."
)

# ms-126 (AX#3): a human passing the literal "untriaged" is NOT choosing a
# severity — untriaged is a machine sentinel. Say so precisely instead of the
# misleading "Priority is required" (which reads as "you passed nothing").
_UNTRIAGED_NOT_SEVERITY_MSG = (
    "'untriaged' is a sentinel, not a severity. Choose one of: "
    + ", ".join(_PRIORITY_SCALE_ORDER)
    + " (machine callers use --untriaged / allow_untriaged instead)."
)

# ms-126 (AX#2): --untriaged means "no priority judged yet"; an explicit
# severity means "judged". Passing both is contradictory — reject loudly instead
# of silently letting the severity win and dropping the untriaged intent (which
# would quietly remove the entry from untriaged-backlog tracking).
_PRIORITY_MUTUALLY_EXCLUSIVE_MSG = (
    "--untriaged and an explicit priority are mutually exclusive; pass one or the other."
)


def normalize_priority(priority: str) -> str:
    """Canonicalize a priority value, mapping deprecated aliases (middle→medium).

    Returns the canonical value unchanged if already canonical. Does NOT validate
    (callers guard against ``_ACCEPTED_PRIORITIES`` first); an unknown value is
    returned as-is so the caller's own "Invalid priority" error still fires.

    ms-126: ``untriaged`` is a sentinel, not an alias — it passes through
    unchanged and is NEVER mapped to a severity.
    """
    return _PRIORITY_ALIASES.get(priority, priority)


def _resolve_priority_for_write(priority: str, *, allow_untriaged: bool,
                                required: bool = True) -> str:
    """Resolve the ``priority`` value a write-path should persist (ms-126).

    Single source of truth so every write-path (``milestone_add`` / ``task_add``
    for tasks AND for non-task entries) shares one rule — no branch re-implements
    validation inline (Maint#1):

    * ``allow_untriaged`` + an explicit severity → ``ValueError`` (AX#2): the two
      are mutually exclusive, so the untriaged intent can never be silently
      dropped.
    * Non-empty value → validate against the real severities (unknown → the
      existing "Invalid priority" ``ValueError``); ``untriaged`` is accepted only
      from machine callers (``allow_untriaged``) and stored as-is. A human typing
      "untriaged" gets a precise sentinel-not-a-severity error (AX#3).
    * Empty value + ``allow_untriaged=True`` (machine path) → the ``untriaged``
      sentinel, so the un-judged priority becomes visible debt.
    * Empty value + ``allow_untriaged=False`` + ``required=True`` (human work
      item) → ``ValueError`` forcing the caller to pick one of the 5 severities.
    * Empty value + ``required=False`` (non-task entry: commit / note / …) →
      ``""`` (no severity stored — those entries carry none).

    Returns the string to store, or ``""`` meaning "store no priority".
    """
    # AX#2: reject contradictory "untriaged + explicit severity" up front, before
    # either could win. (untriaged + literal "untriaged" is not contradictory.)
    if allow_untriaged and priority and priority != UNTRIAGED_PRIORITY:
        raise ValueError(_PRIORITY_MUTUALLY_EXCLUSIVE_MSG)
    if priority:
        if priority == UNTRIAGED_PRIORITY:
            if not allow_untriaged:
                # AX#3: a human typing "untriaged" is naming the sentinel, not
                # choosing a severity — say exactly that.
                raise ValueError(_UNTRIAGED_NOT_SEVERITY_MSG)
            return UNTRIAGED_PRIORITY
        if priority not in _ACCEPTED_PRIORITIES:
            raise ValueError(
                f"Invalid priority: {priority}. Valid: {', '.join(_PRIORITY_SCALE_ORDER)}"
            )
        return normalize_priority(priority)
    # Empty priority.
    if allow_untriaged:
        return UNTRIAGED_PRIORITY
    if required:
        raise ValueError(_PRIORITY_REQUIRED_MSG)
    return ""
# Member roles (e-624): defines permissions in 2-5 person team context.
# - owner: project owner, all permissions including delete project
# - maintainer: can manage milestones / merge PRs / approve operations
# - contributor: can create tasks / PRs but not delete or merge
# - viewer: read-only (Web UI dashboard observer)
VALID_MEMBER_ROLES = {"owner", "maintainer", "contributor", "viewer"}
# Operation lifecycle states (mirrors Milestone for symmetry):
#   todo         — defined but not active (outline only, OperationTasks not finished)
#   in_progress  — actively preparing (filling OperationTasks)
#   open         — fully activated, run_records flow in
#   closed       — role finished
VALID_OPERATION_STATUSES = {"todo", "in_progress", "open", "closed"}
VALID_OP_STATUSES = VALID_OPERATION_STATUSES  # alias
MS_ID_RE = re.compile(r"^ms-\d+$")
ENTRY_ID_RE = re.compile(r"^e-\d+$")
OP_ID_RE = re.compile(r"^op-\d+$")

SCHEDULE_DAYS = {
    "daily":    ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "weekly":   ["fri"],
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_project(data: dict) -> None:
    """Validate project.json schema. Raises ValueError on invalid data.

    Issue #14: This also rejects duplicate milestone / entry / operation IDs.
    Allowing duplicates makes the entire CLI surface ambiguous — every
    "first match" operation silently picks the same target and the other
    record becomes unreachable. We fail fast at load time so the data
    corruption is surfaced before any "successful" no-op write happens.

    Recovery path when duplicates are found:
      1. `beacon doctor` lists which IDs are duplicated and how many times
      2. `beacon milestone purge <ms-id> --index <n> --reason "..."`
         physically removes one record at a time
      3. Allocator now uses max(existing ids) + 1 so future adds will not
         silently re-collide.
    """
    if not isinstance(data, dict):
        raise ValueError("project.json must be a JSON object")
    for key in ("name", "milestones"):
        if key not in data:
            raise ValueError(f"Missing required field: {key}")
    if not isinstance(data["milestones"], list):
        raise ValueError("milestones must be an array")

    seen_ms_ids: dict[str, int] = {}
    seen_entry_ids: dict[str, int] = {}
    for ms in data["milestones"]:
        ms_id = ms.get("id", "")
        if ms_id and not MS_ID_RE.match(ms_id):
            raise ValueError(
                f"Milestone ID '{ms_id}' does not match required format 'ms-{{N}}'. "
                "IDs must be ms-1, ms-2, etc."
            )
        if ms_id:
            seen_ms_ids[ms_id] = seen_ms_ids.get(ms_id, 0) + 1
        if "tasks" in ms:
            raise ValueError(
                f"Milestone '{ms_id or '?'}' uses 'tasks' field. "
                "Use 'entries' instead. Do NOT edit project.json directly — use beacon CLI."
            )
        if ms.get("status") and ms["status"] not in VALID_STATUSES:
            raise ValueError(
                f"Milestone '{ms.get('id', '?')}' has invalid status '{ms['status']}'. "
                f"Valid: {', '.join(sorted(VALID_STATUSES))}"
            )
        for entry in ms.get("entries", []):
            _validate_entry(entry, ms.get("id", "?"), seen_entry_ids)

    seen_op_ids: dict[str, int] = {}
    for op in data.get("operations", []):
        op_id = op.get("id", "")
        if op_id and not OP_ID_RE.match(op_id):
            raise ValueError(
                f"Operation ID '{op_id}' does not match required format 'op-{{N}}'. "
                "IDs must be op-1, op-2, etc."
            )
        if op_id:
            seen_op_ids[op_id] = seen_op_ids.get(op_id, 0) + 1
        if op.get("status") and op["status"] not in VALID_OPERATION_STATUSES:
            raise ValueError(
                f"Operation '{op_id or '?'}' has invalid status '{op['status']}'. "
                f"Valid: {', '.join(sorted(VALID_OPERATION_STATUSES))}"
            )
        for entry in op.get("entries", []):
            _validate_entry(entry, op_id or "?", seen_entry_ids)

    # Aggregate duplicate-ID report. Reporting all dupes at once is more
    # actionable than failing on the first one — the operator can run
    # `beacon milestone purge` for each ID in one pass.
    duplicates: list[str] = []
    for ms_id, n in seen_ms_ids.items():
        if n > 1:
            duplicates.append(f"milestone '{ms_id}' appears {n} times")
    for eid, n in seen_entry_ids.items():
        if n > 1:
            duplicates.append(f"entry '{eid}' appears {n} times")
    for oid, n in seen_op_ids.items():
        if n > 1:
            duplicates.append(f"operation '{oid}' appears {n} times")
    if duplicates:
        raise ValueError(
            "Duplicate IDs in project data — data corruption. "
            + "; ".join(duplicates)
            + ". Use `beacon doctor` to inspect and "
              "`beacon milestone purge <ms-id> --index <n> --reason \"...\"` "
              "to remove the duplicate record."
        )


def find_duplicate_ids(data: dict) -> dict[str, dict[str, int]]:
    """Return a report of duplicate IDs in the project data (no exception).

    Returns a dict shaped like:
      {
        "milestones": {"ms-13": 2, ...},
        "entries":    {"e-5": 2, ...},
        "operations": {"op-3": 2, ...},
      }
    Only IDs that appear more than once are included. Empty subdicts mean
    no duplicates for that category.

    This is the read-only counterpart of validate_project's duplicate
    detection — useful for `beacon doctor` which wants to *report* rather
    than refuse to load.
    """
    ms_counts: dict[str, int] = {}
    entry_counts: dict[str, int] = {}
    op_counts: dict[str, int] = {}

    def _walk_entry(entry: dict) -> None:
        eid = entry.get("id", "")
        if eid:
            entry_counts[eid] = entry_counts.get(eid, 0) + 1
        for child in entry.get("entries", []):
            _walk_entry(child)

    for ms in data.get("milestones", []):
        mid = ms.get("id", "")
        if mid:
            ms_counts[mid] = ms_counts.get(mid, 0) + 1
        for entry in ms.get("entries", []):
            _walk_entry(entry)
    for op in data.get("operations", []):
        oid = op.get("id", "")
        if oid:
            op_counts[oid] = op_counts.get(oid, 0) + 1
        for entry in op.get("entries", []):
            _walk_entry(entry)

    return {
        "milestones": {k: n for k, n in ms_counts.items() if n > 1},
        "entries":    {k: n for k, n in entry_counts.items() if n > 1},
        "operations": {k: n for k, n in op_counts.items() if n > 1},
    }


def _validate_entry(entry: dict, ms_id: str, seen_entry_ids: dict[str, int] | None = None) -> None:
    """Recursively validate an entry and its children.

    If `seen_entry_ids` is provided, increment the count for each entry ID
    seen — the caller uses this to detect duplicates across the whole
    project (validate_project). Left as `None` for legacy callers.
    """
    eid = entry.get("id", "")
    if eid and not ENTRY_ID_RE.match(eid):
        raise ValueError(
            f"Entry ID '{eid}' in {ms_id} does not match required format 'e-{{N}}'. "
            "IDs must be e-1, e-2, etc."
        )
    if eid and seen_entry_ids is not None:
        seen_entry_ids[eid] = seen_entry_ids.get(eid, 0) + 1
    if entry.get("type") and entry["type"] not in VALID_ENTRY_TYPES:
        raise ValueError(
            f"Entry '{entry.get('id', '?')}' in {ms_id} has invalid type '{entry['type']}'. "
            f"Valid: {', '.join(sorted(VALID_ENTRY_TYPES))}"
        )
    # Status validity is type-dependent: run_record uses ok/warning/error and
    # incident uses open/resolved, NOT the general task-style statuses. The
    # writers (run_record_add / incident_*) already enforce their own enum, so
    # validate must dispatch by type or it rejects data it just wrote — the
    # write-read inconsistency reported in Issue #29 (regression from the
    # operation-entry validation added for Issue #14).
    status = entry.get("status")
    if status:
        entry_type = entry.get("type")
        if entry_type == "run_record":
            valid_statuses = VALID_RUN_STATUSES
        elif entry_type == "incident":
            valid_statuses = VALID_INCIDENT_STATUSES
        elif entry_type == "target-transition-approval":
            valid_statuses = VALID_TRANSITION_APPROVAL_STATUSES
        else:
            valid_statuses = VALID_STATUSES
        if status not in valid_statuses:
            raise ValueError(
                f"Entry '{entry.get('id', '?')}' in {ms_id} has invalid status "
                f"'{status}' for type '{entry_type or 'unknown'}'. "
                f"Valid: {', '.join(sorted(valid_statuses))}"
            )
    for child in entry.get("entries", []):
        _validate_entry(child, ms_id, seen_entry_ids)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_milestones(data: dict, ms_id: str) -> list[dict]:
    """Return ALL milestone records whose id matches `ms_id`.

    Normally returns 0 or 1 records. Returns >1 only when the project data
    has duplicate IDs (Issue #14 — should now be prevented by
    validate_project, but the helper still works on raw/unvalidated data).
    """
    return [ms for ms in data.get("milestones", []) if ms.get("id") == ms_id]


def find_target_milestone(data: dict, ms_id: str = "", *, index: int | None = None) -> dict:
    """Find target milestone by id or auto-select if only one is active.

    Raises ValueError if not found, ambiguous, or duplicated.

    `index` (1-based) disambiguates duplicate IDs when validate_project is
    bypassed (e.g. doctor-style recovery flows). When duplicates exist and
    index is None, this raises with the count so the caller can prompt
    for --index <n>. CORE doc data-immutability-principle treats this as
    a refused write rather than a silent first-match write.
    """
    if ms_id:
        matches = find_milestones(data, ms_id)
        if not matches:
            raise ValueError(f"Milestone not found: {ms_id}")
        if len(matches) == 1:
            if index is not None and index != 1:
                raise ValueError(
                    f"Milestone '{ms_id}' has only 1 record but --index {index} was given."
                )
            return matches[0]
        # Duplicate IDs: require explicit --index <n>.
        if index is None:
            raise ValueError(
                f"Ambiguous milestone '{ms_id}': {len(matches)} records exist "
                "(data corruption — Issue #14). Specify which one with "
                f"`--index <n>` where n is 1..{len(matches)}. Use "
                "`beacon milestone purge <ms-id> --index <n> --reason \"...\"` "
                "to remove the duplicate."
            )
        if index < 1 or index > len(matches):
            raise ValueError(
                f"--index {index} is out of range for '{ms_id}' "
                f"(valid: 1..{len(matches)})."
            )
        return matches[index - 1]

    active_list = [ms for ms in data["milestones"] if ms["status"] == "in_progress"]
    if len(active_list) == 0:
        raise ValueError("No active milestone. Run: beacon milestone start <ms-id>")
    if len(active_list) > 1:
        ids = ", ".join(ms["id"] for ms in active_list)
        raise ValueError(f"Multiple active milestones. Specify with -m <ms-id>: {ids}")
    return active_list[0]


def resolve_recordable_milestone(data: dict, ms_id: str = "") -> dict | None:
    """Tolerant resolver for entry-recording *side effects* (ms-134 e-4720).

    Returns the milestone dict a side-effect entry (e.g. "doc add: …") should be
    recorded onto, or ``None`` when there is NO milestone to record onto — a
    project with zero milestones (a sales / back-office project) must not be
    forced to have one just so a shared capability can log a side effect.

    This is deliberately narrower than ``find_target_milestone``: it swallows ONLY
    the "no milestone exists at all" case (auto-pick over an empty list). An
    explicit ``ms_id`` that does not exist, and the "multiple active milestones,
    which one?" ambiguity, still RAISE through ``find_target_milestone`` — those
    are real user errors to surface, not the profession-shape difference the
    shared doc path must tolerate. So a dev project keeps recording exactly as
    before (1 active → record; bad id / ambiguous → error), while a sales project
    (0 milestones) yields ``None`` and the caller no-ops.

    Callers must go through the occupation layer (``occupation.record_target_entry``),
    not call ``save_entry`` / ``find_target_milestone`` directly, so that a
    profession-shared (L1/L2) capability never depends on this dev concrete —
    ``scripts/check-capability-scope.py`` enforces that boundary.
    """
    if not ms_id:
        active = [ms for ms in data.get("milestones", [])
                  if ms.get("status") == "in_progress"]
        if not active:
            return None
    return find_target_milestone(data, ms_id)


def next_entry_id(data: dict) -> str:
    """Generate next entry id across all milestones and operations (including nested).

    The ``max + 1`` allocation lives in ``work_base.next_suffixed_id`` (shared
    with the sales allocators, ms-109 e-3558). This function's remaining job is
    the development-specific part: gathering every entry id, walking the nested
    ``entries`` tree.
    """
    ids: list[str] = []
    for ms in data["milestones"]:
        for entry in ms.get("entries", []):
            _collect_entry_ids(entry, ids)
    for op in data.get("operations", []):
        for entry in op.get("entries", []):
            _collect_entry_ids(entry, ids)
    return work_base.next_suffixed_id(ids, "e-")


def next_op_id(data: dict) -> str:
    """Generate next operation id."""
    ids = [op.get("id", "") for op in data.get("operations", [])]
    return work_base.next_suffixed_id(ids, "op-")


def _collect_entry_ids(entry: dict, out: list[str]) -> list[str]:
    """Append this entry's id and all nested descendants' ids to ``out``."""
    eid = entry.get("id", "")
    if eid:
        out.append(eid)
    for child in entry.get("entries", []):
        _collect_entry_ids(child, out)
    return out


def find_entry(data: dict, entry_id: str):
    """Find an entry by ID across all milestones and operations (including nested).

    Returns (container, parent_entries_list, entry, index) or None.
    Container is either a milestone dict or an operation dict.
    """
    for ms in data["milestones"]:
        result = _find_entry_in(ms.get("entries", []), entry_id, ms)
        if result:
            return result
    for op in data.get("operations", []):
        result = _find_entry_in(op.get("entries", []), entry_id, op)
        if result:
            return result
    return None


def _find_entry_in(entries: list, entry_id: str, ms: dict):
    for i, entry in enumerate(entries):
        if entry.get("id") == entry_id:
            return (ms, entries, entry, i)
        children = entry.get("entries", [])
        result = _find_entry_in(children, entry_id, ms)
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# Milestone operations
# ---------------------------------------------------------------------------

def next_milestone_id(data: dict) -> str:
    """Generate the next milestone id using max(existing ids) + 1.

    Issue #14: the previous implementation used `len(milestones) + 1`, which
    silently re-issues IDs of physically-removed milestones (e.g. if a
    project.json hand-edit drops one). The max-id allocator is the only
    structural fix — cancelled / done / observing milestones all stay in
    the array and contribute to the max, so the new ID is monotonic
    forever (until the int counter literally rolls over).
    """
    max_id = 0
    for ms in data.get("milestones", []):
        mid = ms.get("id", "")
        if mid.startswith("ms-"):
            try:
                max_id = max(max_id, int(mid[3:]))
            except ValueError:
                pass
    return f"ms-{max_id + 1}"


def milestone_add(data: dict, title: str, target_date: str = "",
                   description: str = "", priority: str = "",
                   objective: str = "", acceptance_criteria: str = "",
                   owner: str = "", assignee: str = "",
                   author: dict | None = None,
                   allow_untriaged: bool = False) -> str:
    """Add a milestone. Returns the new ms_id.

    Issue #14: the ID is computed via `next_milestone_id` (max + 1) and we
    re-check for collisions before appending. The collision check is
    belt-and-suspenders: if validate_project ran during load and the data
    already has duplicate IDs, raising here prevents us from compounding
    the corruption.

    ``author`` (ms-43 / e-2246): optional ``{"user_id", "email",
    "display_name"}`` dict attached to ``meta.author`` — the *human*
    identity that created the milestone on the server side. Mirrors the
    ms-78 / e-1909 contract on tasks so the UI can surface a creator
    label in MS lists / detail. Empty fields are dropped via
    ``_clean_author``.
    """
    # ms-143 (leader 握り 設計判断 b/B): the profession-neutral skeleton + the
    # collection append are now the ONE generic writer ``occupation.create_target``
    # (dispatched by ``profession_manifest``), so this verb no longer names
    # ``data['milestones']`` itself. ``milestone_add`` stays as the dev-FRONTEND
    # helper: it owns the dev enrichment below (ms-81 empty-assignee no-pollution /
    # priority / description / objective / AC / owner / author) and delegates only
    # the skeleton. Imported lazily because ``occupation`` imports ``core`` — a
    # top-level import here would cycle (``create_target`` itself stays core-free
    # so a future ``core → occupation`` delegation stays clean). The legacy
    # ``title`` dual-write, ``target_date`` and ``commits`` ride via ``extra``.
    # ms-126 forcing function: resolve/validate the priority BEFORE any write so a
    # rejected value (human empty / untriaged+explicit conflict) raises before
    # create_target appends. ms-143 preserved this pre-write invariant when the
    # append moved into create_target (previously the raise happened before the
    # tail append; now it must happen before create_target's append).
    resolved_priority = _resolve_priority_for_write(
        priority, allow_untriaged=allow_untriaged
    )
    import occupation
    ms = occupation.create_target(
        data, "milestone", label=title,
        created_at=_now_iso(), created_by=_get_actor(), assignee=assignee,
        title=title, target_date=target_date, commits=[],
    )
    ms_id = ms["id"]
    # Preserve the milestone no-pollution rule (ms-81): an empty assignee is
    # dropped rather than persisted as ``""``. The base constructor always
    # emits the generic slot; milestones opt out of the empty case so a doc
    # only carries ``assignee`` once someone owns it.
    if not assignee:
        ms.pop("assignee", None)
    if description:
        ms["description"] = description
    # ms-126: priority is now mandatory. The human path leaves
    # allow_untriaged=False and an empty value is rejected here; machine paths
    # pass allow_untriaged=True and an empty value becomes the ``untriaged``
    # sentinel (visible debt), never a silent blank.
    ms["priority"] = resolved_priority
    if objective:
        ms["objective"] = objective
    if acceptance_criteria:
        ms["acceptance_criteria"] = acceptance_criteria
    # e-625: owner is recorded but NOT validated against members[] at this
    # layer — that check belongs in the CLI/API surface, where we can produce a
    # friendly "did you mean to `beacon member add` first?" message. Here we
    # just store whatever string the caller provided. (``assignee`` is now part
    # of the generic skeleton built by ``work_model.new_target`` above.)
    if owner:
        ms["owner"] = owner
    # ms-43 / e-2246 — stamp the human author on the milestone so the UI
    # can surface a creator label (= 起票者) in MS lists / detail. Same
    # contract as task creation (ms-78 / e-1909). Stored under ``meta``
    # to keep top-level fields stable and mirror the entry-side shape.
    author_clean = _clean_author(author)
    if author_clean:
        ms.setdefault("meta", {})["author"] = author_clean
    # NB: ``occupation.create_target`` already appended ``ms`` to the collection;
    # the enrichment above mutates that same record in place (ms-143).
    return ms_id


def milestone_start(data: dict, ms_id: str) -> dict:
    """Activate a milestone (multiple can be active simultaneously). Returns the activated ms."""
    found = None
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            ms["status"] = "in_progress"
            found = ms
    if not found:
        raise ValueError(f"Milestone not found: {ms_id}")
    return found


# ms-81 e-1918: session occupation model. Active MS may also be "occupied"
# by a session — meaning a single bclaude (or other agent) is currently
# sitting in front of it. Per CORE doc DqIvAVzDprcq6hsq0AuF §3, occupation
# is a separate layer from `status`: an active MS can be unoccupied (=
# nobody's sitting at it right now) and a session can release without
# changing status. Occupation is a soft claim; the SPEC explicitly chose
# warning-based over block, so callers always look up the existing claim
# and decide rather than ever rejecting.


def milestone_claim_occupation(
    data: dict,
    ms_id: str,
    *,
    session_id: str,
    machine: str = "",
    agent: str = "",
) -> tuple[dict, dict | None]:
    """Record an occupation claim on ``ms_id``.

    Returns ``(milestone, previous_claim)``. ``previous_claim`` is the
    occupation record that was in place *before* this call, or ``None`` if
    the MS was unoccupied. Callers (= CLI) decide whether to surface a
    warning based on whether ``previous_claim`` exists and whether its
    session is still live.

    The claim is stored directly on the milestone (``ms["occupation"]``)
    so the standard cloud-sync path propagates it without new
    subcollections; the event-log (= ``worktree_sessions``) lives at the
    project level (see ``milestone_record_occupation_event``).
    """
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            previous = ms.get("occupation")
            claim = {
                "session_id": session_id,
                "machine": machine,
                "agent": agent,
                "claimed_at": _now_iso(),
            }
            ms["occupation"] = claim
            return ms, previous
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_release_occupation(
    data: dict, ms_id: str, *, reason: str = "manual"
) -> tuple[dict, dict | None]:
    """Release the occupation claim on ``ms_id``.

    Returns ``(milestone, released_claim)``. ``released_claim`` is the
    occupation that was just cleared, or ``None`` if the MS wasn't
    occupied. The MS ``status`` is NOT touched — release leaves the MS
    in whatever phase it was in, per the SPEC's "active のまま専有解除
    できる" principle (CORE doc §3).
    """
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            released = ms.pop("occupation", None)
            return ms, released
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_record_occupation_event(
    data: dict,
    *,
    ms_id: str,
    event_type: str,
    session_id: str,
    machine: str = "",
    agent: str = "",
    reason: str = "",
) -> dict:
    """Append a row to the project-level ``worktree_sessions`` log.

    ``event_type`` is one of ``"claim"``, ``"release"``, ``"takeover"``.
    The log lives at ``data["worktree_sessions"]`` so cloud sync carries
    it without bespoke subcollection plumbing; consumers (= Web UI audit
    tab in e-1921) read this list directly.

    Returns the recorded event for convenience.
    """
    if event_type not in {"claim", "release", "takeover"}:
        raise ValueError(
            f"Invalid occupation event_type: {event_type}. "
            f"Expected one of claim / release / takeover."
        )
    log = data.setdefault("worktree_sessions", [])
    event = {
        "event_id": f"wts-{len(log) + 1}",
        "ms_id": ms_id,
        "event_type": event_type,
        "session_id": session_id,
        "machine": machine,
        "agent": agent,
        "at": _now_iso(),
    }
    if reason:
        event["reason"] = reason
    log.append(event)
    return event


def _split_assignees(value) -> list[str]:
    """Normalise the assignee field (str or list) into a list of names.

    Per ms-42 the field is historically a single string. ms-51 introduces
    multi-assignee semantics via "name1,name2" — we keep wire-compat with
    a list-aware split so legacy single-string projects don't need migration.

    Empty strings and whitespace-only tokens are dropped.
    """
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = str(value).split(",")
    return [s.strip() for s in items if s and s.strip()]


def milestone_assignee_add(data: dict, ms_id: str, actor: str) -> tuple[dict, bool]:
    """Add ``actor`` to the milestone's assignee list (ms-51 / e-932, e-933).

    Returns ``(milestone, added)`` where ``added`` is False if the actor was
    already present (caller treats as no-op per SPEC AC). The wire format
    stays string-shaped: a single assignee remains ``"alice"``, two become
    ``"alice,bob"``. This preserves backward compatibility with the Web UI
    badge code (ms-43 / e-767) which currently reads a single string.

    Raises ValueError if the MS doesn't exist or is done/cancelled (per
    e-933 AC-4 — joining a finished MS is rejected).
    """
    actor = (actor or "").strip()
    if not actor:
        raise ValueError("actor must be a non-empty string")
    for ms in data["milestones"]:
        if ms["id"] != ms_id:
            continue
        # SPEC e-933 AC-4: refuse to join a done/cancelled MS.
        status = ms.get("status", "todo")
        if status in ("done", "cancelled"):
            raise ValueError(
                f"Milestone {ms_id} is {status}; cannot add assignee."
            )
        current = _split_assignees(ms.get("assignee", ""))
        if actor in current:
            return ms, False
        current.append(actor)
        # Single-element list stays as a bare string for backward compat.
        ms["assignee"] = current[0] if len(current) == 1 else ",".join(current)
        return ms, True
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_done(data: dict, ms_id: str, *, reason: str = "") -> dict:
    """Mark a milestone as done. Returns the milestone."""
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            ms["status"] = "done"
            meta = ms.setdefault("meta", {})
            meta["done_at"] = _now_iso()
            meta["done_by"] = _get_actor()
            if reason:
                meta["done_reason"] = reason
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


# ms-81 e-1915: waiting = a milestone that was active/observing but is now
# paused, with the explicit intent of returning later. Distinct from todo
# (= never started) because commit/PR history is already attached and the
# work has accumulated context. Per CORE doc DqIvAVzDprcq6hsq0AuF §1, the
# legal source statuses are active (= in_progress) and observing; todo / done
# / cancelled cannot transition to waiting because their semantics conflict.
_WAITING_SOURCES = {"in_progress", "active", "observing"}


def milestone_wait(data: dict, ms_id: str, *, reason: str = "") -> dict:
    """Transition a milestone to ``waiting`` status (ms-81 e-1915).

    Rejects the transition if the current status is not in
    ``_WAITING_SOURCES``. Per the state-machine CORE doc, todo / done /
    cancelled cannot move to waiting — todo means "never started" (no
    history to preserve), done means "completed" (re-open via observe or
    active), cancelled is terminal.
    """
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            current = ms.get("status", "todo")
            if current not in _WAITING_SOURCES:
                raise ValueError(
                    f"Cannot wait milestone in status '{current}'. "
                    f"Only active (in_progress) or observing milestones can "
                    f"transition to waiting. "
                    f"(todo → use start; done → use start to re-open)"
                )
            ms["status"] = "waiting"
            meta = ms.setdefault("meta", {})
            meta["waiting_at"] = _now_iso()
            meta["waiting_by"] = _get_actor()
            if reason:
                meta["waiting_reason"] = reason
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_update(data: dict, ms_id: str, *,
                     title: str = "", progress: str = "",
                     target_date: str = "", status: str = "",
                     description: str = "", reason: str = "",
                     priority: str = "", objective: str = "",
                     acceptance_criteria: str = "",
                     owner: str = "", assignee: str = "") -> dict:
    """Update milestone fields. Returns the milestone.

    e-625: owner / assignee can be cleared by passing the literal string
    "-" (matches the convention used by other CLI clear-this-field tools);
    any other non-empty value sets the field.
    """
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            if title:
                work_model.set_target_label(ms, title, dual_write=True)  # ms-109 e-3625
            if description is not None and description != "":
                ms["description"] = description
            if progress:
                try:
                    ms["progress"] = max(0, min(100, int(progress)))
                except ValueError:
                    pass
            if target_date:
                ms["target_date"] = target_date
            if priority:
                # ms-126 (e-4224 + AX round-2): a provided priority always routes
                # through the single-source resolver; empty/omitted = no change.
                # ``untriaged`` is rejected state-independently (a client cannot
                # re-assert the machine sentinel). To leave an untriaged milestone
                # unchanged, omit the field rather than echo it. Mirrors
                # task_update above.
                ms["priority"] = _resolve_priority_for_write(
                    priority, allow_untriaged=False)
            if objective:
                ms["objective"] = objective
            if acceptance_criteria:
                ms["acceptance_criteria"] = acceptance_criteria
            if owner:
                ms["owner"] = "" if owner == "-" else owner
            if assignee:
                ms["assignee"] = "" if assignee == "-" else assignee
            if status:
                if status not in VALID_STATUSES:
                    raise ValueError(
                        f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_STATUSES))}"
                    )
                ms["status"] = status
                meta = ms.setdefault("meta", {})
                meta[f"{status}_at"] = _now_iso()
                meta[f"{status}_by"] = _get_actor()
                if reason:
                    meta[f"{status}_reason"] = reason
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_purge(data: dict, ms_id: str, *, reason: str,
                     index: int | None = None) -> dict:
    """Physically remove a milestone record. Returns the removed milestone.

    Issue #14: this is the recovery path for duplicate IDs and for hand-
    corrupted data — soft delete (`status="cancelled"`) cannot help here
    because the record remains in the array and every `first match` op
    continues to hit it.

    Constraints:
      • `reason` is required (CORE doc data-immutability-principle). The
        physical delete is recorded as a `milestone.purge` operation in
        the changelog so the trail isn't lost.
      • When duplicate IDs exist, `index` (1-based) is required to pick
        which copy to remove. With a single record, `index` may be omitted
        or set to 1.

    This is intentionally a different code path from `milestone_delete`
    (soft). The two are not interchangeable — soft delete preserves the
    record for retrospection; purge removes it because the record itself
    is the bug.
    """
    if not reason:
        raise ValueError("milestone_purge requires a reason")
    matches: list[tuple[int, dict]] = [
        (i, ms) for i, ms in enumerate(data.get("milestones", []))
        if ms.get("id") == ms_id
    ]
    if not matches:
        raise ValueError(f"Milestone not found: {ms_id}")
    if len(matches) == 1:
        if index is not None and index != 1:
            raise ValueError(
                f"Milestone '{ms_id}' has only 1 record but --index {index} was given."
            )
        target_pos, target_ms = matches[0]
    else:
        if index is None:
            raise ValueError(
                f"Milestone '{ms_id}' has {len(matches)} records. "
                f"Specify which to purge with --index <n> (1..{len(matches)})."
            )
        if index < 1 or index > len(matches):
            raise ValueError(
                f"--index {index} is out of range for '{ms_id}' (valid: 1..{len(matches)})."
            )
        target_pos, target_ms = matches[index - 1]
    # Annotate before removal — the returned dict reflects the purge
    # decision so callers / changelog writers see the reason context.
    meta = target_ms.setdefault("meta", {})
    meta["purged_at"] = _now_iso()
    meta["purged_by"] = _get_actor()
    meta["purge_reason"] = reason
    data["milestones"].pop(target_pos)
    return target_ms


# ---------------------------------------------------------------------------
# Entry / Operation purge — recovery for duplicate entry / operation IDs.
# These are the e-N / op-N analogues of milestone_purge (Issue #14 / e-863):
# validate_project rejects loading a project with duplicate IDs of ANY kind,
# but the only hard-delete recovery path was for milestones. Without these,
# a duplicate entry/operation ID would lock the whole CLI with no structural
# way out (doctor could only say "contact maintainers"). Allocators are
# already max-id based so duplicates shouldn't arise in normal use, but the
# recovery path must be symmetric or the asymmetry re-creates Issue #14 one
# level down.
# ---------------------------------------------------------------------------

def find_operations(data: dict, op_id: str) -> list[dict]:
    """Return ALL operation records whose id matches `op_id` (0, 1, or >1)."""
    return [op for op in data.get("operations", []) if op.get("id") == op_id]


def _collect_entry_matches(entries: list, entry_id: str, out: list) -> None:
    """Append (parent_list, index, entry) for every match, recursing into
    nested entries. parent_list + index is what a caller needs to pop()."""
    for i, entry in enumerate(entries):
        if entry.get("id") == entry_id:
            out.append((entries, i, entry))
        _collect_entry_matches(entry.get("entries", []), entry_id, out)


def find_entries(data: dict, entry_id: str) -> list[dict]:
    """Return ALL entry records matching `entry_id` across milestones and
    operations, including nested entries (normally 0 or 1)."""
    matches: list = []
    for ms in data.get("milestones", []):
        _collect_entry_matches(ms.get("entries", []), entry_id, matches)
    for op in data.get("operations", []):
        _collect_entry_matches(op.get("entries", []), entry_id, matches)
    return [entry for (_lst, _i, entry) in matches]


def entry_purge(data: dict, entry_id: str, *, reason: str,
                index: int | None = None) -> dict:
    """Physically remove an entry record (e-N). Returns the removed entry.

    The entry-level analogue of milestone_purge (e-863). Entries may be
    nested inside milestones or operations; this searches all of them.
    `reason` is required (data-immutability-principle); `index` (1-based)
    disambiguates duplicates. Soft delete (`status="cancelled"`) cannot help
    a duplicate-ID corruption because both records keep the same id.
    """
    if not reason:
        raise ValueError("entry_purge requires a reason")
    matches: list[tuple[list, int, dict]] = []
    for ms in data.get("milestones", []):
        _collect_entry_matches(ms.get("entries", []), entry_id, matches)
    for op in data.get("operations", []):
        _collect_entry_matches(op.get("entries", []), entry_id, matches)
    if not matches:
        raise ValueError(f"Entry not found: {entry_id}")
    if len(matches) == 1:
        if index is not None and index != 1:
            raise ValueError(
                f"Entry '{entry_id}' has only 1 record but --index {index} was given."
            )
        parent_list, pos, target = matches[0]
    else:
        if index is None:
            raise ValueError(
                f"Entry '{entry_id}' has {len(matches)} records. "
                f"Specify which to purge with --index <n> (1..{len(matches)})."
            )
        if index < 1 or index > len(matches):
            raise ValueError(
                f"--index {index} is out of range for '{entry_id}' (valid: 1..{len(matches)})."
            )
        parent_list, pos, target = matches[index - 1]
    meta = target.setdefault("meta", {})
    meta["purged_at"] = _now_iso()
    meta["purged_by"] = _get_actor()
    meta["purge_reason"] = reason
    parent_list.pop(pos)
    return target


def operation_purge(data: dict, op_id: str, *, reason: str,
                    index: int | None = None) -> dict:
    """Physically remove an operation record (op-N). Returns the removed op.

    The operation-level analogue of milestone_purge (e-863). `reason` is
    required; `index` (1-based) disambiguates duplicate op-ids.
    """
    if not reason:
        raise ValueError("operation_purge requires a reason")
    matches: list[tuple[int, dict]] = [
        (i, op) for i, op in enumerate(data.get("operations", []))
        if op.get("id") == op_id
    ]
    if not matches:
        raise ValueError(f"Operation not found: {op_id}")
    if len(matches) == 1:
        if index is not None and index != 1:
            raise ValueError(
                f"Operation '{op_id}' has only 1 record but --index {index} was given."
            )
        target_pos, target_op = matches[0]
    else:
        if index is None:
            raise ValueError(
                f"Operation '{op_id}' has {len(matches)} records. "
                f"Specify which to purge with --index <n> (1..{len(matches)})."
            )
        if index < 1 or index > len(matches):
            raise ValueError(
                f"--index {index} is out of range for '{op_id}' (valid: 1..{len(matches)})."
            )
        target_pos, target_op = matches[index - 1]
    meta = target_op.setdefault("meta", {})
    meta["purged_at"] = _now_iso()
    meta["purged_by"] = _get_actor()
    meta["purge_reason"] = reason
    data["operations"].pop(target_pos)
    return target_op


def milestone_delete(data: dict, ms_id: str, *, reason: str = "") -> dict:
    """Cancel a milestone (soft delete). Returns the milestone."""
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            ms["status"] = "cancelled"
            meta = ms.setdefault("meta", {})
            meta["cancelled_at"] = _now_iso()
            meta["cancelled_by"] = _get_actor()
            if reason:
                meta["cancel_reason"] = reason
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


# ---------------------------------------------------------------------------
# Member operations (e-624)
# ---------------------------------------------------------------------------
#
# `members` is a flat list at project root. Each member is:
#   {
#     "id":       "<handle>",          # required, unique within the project
#     "name":     "<display name>",     # optional, defaults to id
#     "email":    "<email>",            # optional
#     "role":     "owner|maintainer|contributor|viewer",
#     "added_at": "<iso-timestamp>",
#     "added_by": "<actor>",
#   }
#
# These functions are PURE — they mutate the project dict and return a value
# but never touch I/O. They are designed to be called from inside an
# apply_operation closure.

def members_list(data: dict) -> list:
    """Return the list of members (never None — empty list if absent)."""
    members = data.get("members")
    if not isinstance(members, list):
        return []
    return members


def member_add(data: dict, member_id: str, *, name: str = "", email: str = "",
               role: str = "contributor") -> dict:
    """Add a member to the project. Returns the new member dict.

    Raises ValueError if member_id is empty, role is invalid, or a member
    with the same id already exists.
    """
    if not member_id or not member_id.strip():
        raise ValueError("Member id is required")
    member_id = member_id.strip()
    if role not in VALID_MEMBER_ROLES:
        raise ValueError(
            f"Invalid role: {role}. Valid: {', '.join(sorted(VALID_MEMBER_ROLES))}"
        )
    members = data.setdefault("members", [])
    if not isinstance(members, list):
        # Heal a corrupt members field rather than raising — this only happens
        # if someone hand-edited project.json (which is explicitly banned).
        members = []
        data["members"] = members
    for m in members:
        if isinstance(m, dict) and m.get("id") == member_id:
            raise ValueError(f"Member already exists: {member_id}")
    new_member = {
        "id": member_id,
        "name": name or member_id,
        "email": email,
        "role": role,
        "added_at": _now_iso(),
        "added_by": _get_actor(),
    }
    members.append(new_member)
    return new_member


def member_remove(data: dict, member_id: str, *, reason: str = "") -> dict:
    """Remove a member from the project. Returns the removed member dict.

    Raises ValueError if no such member exists. owner role cannot be removed
    (escalate the new owner first with `member role`).
    """
    members = data.get("members", [])
    if not isinstance(members, list):
        raise ValueError(f"Member not found: {member_id}")
    for i, m in enumerate(members):
        if isinstance(m, dict) and m.get("id") == member_id:
            if m.get("role") == "owner":
                raise ValueError(
                    f"Cannot remove owner {member_id}. "
                    "Promote another member with `beacon member role <id> owner` first."
                )
            removed = members.pop(i)
            # Audit trail lives in changelog.jsonl via apply_operation; we
            # don't write that here (this function is pure data).
            return removed
    raise ValueError(f"Member not found: {member_id}")


def member_set_role(data: dict, member_id: str, role: str) -> dict:
    """Change a member's role. Returns the updated member dict.

    Raises ValueError on invalid role or unknown member.
    """
    if role not in VALID_MEMBER_ROLES:
        raise ValueError(
            f"Invalid role: {role}. Valid: {', '.join(sorted(VALID_MEMBER_ROLES))}"
        )
    members = data.get("members", [])
    if not isinstance(members, list):
        raise ValueError(f"Member not found: {member_id}")
    for m in members:
        if isinstance(m, dict) and m.get("id") == member_id:
            m["role"] = role
            return m
    raise ValueError(f"Member not found: {member_id}")


# ---------------------------------------------------------------------------
# Entry / Task operations
# ---------------------------------------------------------------------------

def task_add(data: dict, ms_id: str, description: str, *,
             entry_type: str = "task", date: str = "",
             detail: str = "", requested_by: str = "",
             priority: str = "", motivation: str = "",
             acceptance_criteria: str = "", deadline: str = "",
             author: dict | None = None,
             allow_untriaged: bool = False) -> str:
    """Add an entry to a milestone. Returns the new entry id.

    ``author`` (ms-78 / e-1909): optional ``{"user_id", "email",
    "display_name"}`` dict attached to ``meta.author`` — the *human*
    identity that created the task on the server side. Distinct from
    ``meta.created_by`` (= local-agent string for CLI / debug). Empty
    fields are dropped.
    """
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])
    eid = next_entry_id(data)
    meta = {}
    if requested_by:
        meta["requested_by"] = requested_by
    # ms-126: priority is mandatory — but ONLY for actual work items
    # (``task``). Other entry types (commit / note / …) carry no severity, so
    # forcing one on them would be nonsense and would break the Web UI's
    # commit/note create path. For a task: the human path (allow_untriaged=
    # False) rejects an empty value; machine paths (allow_untriaged=True) store
    # the ``untriaged`` sentinel as visible debt. Non-task entries keep the old
    # permissive rule: validate only when a value is present.
    # ms-126 (Maint#1): route BOTH the task path (priority required) and the
    # non-task path (commit / note — priority optional) through the one resolver,
    # so validation / sentinel-gating / mutual-exclusion live in a single place
    # and can never drift between branches.
    resolved_priority = _resolve_priority_for_write(
        priority, allow_untriaged=allow_untriaged,
        required=(entry_type == "task"),
    )
    if resolved_priority:
        meta["priority"] = resolved_priority
    now = _now_iso()
    meta["created_by"] = _get_actor()
    author_clean = _clean_author(author)
    if author_clean:
        meta["author"] = author_clean
    entry = {
        "id": eid,
        "type": entry_type,
        "description": description,
        "date": date or now,
        "created_at": now,
        "done_at": None,
        "status": "todo",
        "meta": meta,
    }
    if detail:
        entry["detail"] = detail
    if motivation:
        entry["motivation"] = motivation
    if acceptance_criteria:
        entry["acceptance_criteria"] = acceptance_criteria
    # ms-139 e-4949: 締切 (deadline, YYYY-MM-DD) は top-level フィールド。milestone の
    # target_date と同様、L2 締切エンジン (lib/deadline.py の deadline_of) が canonical
    # ``deadline`` として読む。フィールド名は職種ごとに残す (task/activity=deadline、
    # milestone=target_date)。任意なので値がある時だけ持たせる。
    if deadline:
        entry["deadline"] = deadline
    entries.append(entry)
    return eid


def task_done(data: dict, entry_id: str, *, date: str = "", reason: str = "",
              author: dict | None = None) -> tuple[dict, dict]:
    """Mark an entry as done. Returns (milestone, entry).

    ``author`` (ms-78 / e-1909): optional ``{"user_id", "email",
    "display_name"}`` dict attached to ``meta.done_by_user`` — the
    *human* identity that completed the task on the server side
    (distinct from ``meta.done_by`` which is the local-agent string).
    """
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    # ms-109 e-3696 (fable A-2): the generic done-stamp — status / done_at /
    # meta.done_by / meta.done_reason — goes through the occupation-agnostic
    # base so a development task and a sales activity close the same way. The
    # dev-specific bits (the ``date`` mirror and the *human* ``done_by_user``
    # identity) stay here.
    work_model.mark_done(entry, at=date or _now_iso(), actor=_get_actor(),
                         reason=reason)
    if not entry.get("date"):
        entry["date"] = entry["done_at"]
    author_clean = _clean_author(author)
    if author_clean:
        entry.setdefault("meta", {})["done_by_user"] = author_clean
    return ms, entry


def task_update(data: dict, entry_id: str, *,
                description: str = "", status: str = "",
                detail: str = "", date: str = "",
                motivation: str = "", acceptance_criteria: str = "",
                behavior: str = "", priority: str = "", deadline: str = "",
                author: dict | None = None) -> tuple[dict, dict]:
    """Update entry fields. Returns (milestone, entry).

    The MS-32 "必要十分フォーマット" fields (motivation / acceptance_criteria /
    behavior) and priority are now updatable here. Empty strings are treated as
    "no change" so callers can omit fields they don't want to touch.

    ``author`` (ms-78 / e-1909): optional ``{"user_id", "email",
    "display_name"}`` dict attached to ``meta.updated_by_user`` — the
    *human* identity that performed the update. Only stamped when at
    least one field actually changes, to avoid a "nothing happened"
    no-op call rewriting the audit field with the latest opener.
    """
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    changed = False
    if description:
        entry["description"] = description
        changed = True
    if status:
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_STATUSES))}"
            )
        entry["status"] = status
        if status == work_model.DONE_STATUS and not entry.get("done_at"):
            entry["done_at"] = date
        changed = True
    if detail:
        entry["detail"] = detail
        changed = True
    if motivation:
        entry["motivation"] = motivation
        changed = True
    if acceptance_criteria:
        entry["acceptance_criteria"] = acceptance_criteria
        changed = True
    if behavior:
        entry["behavior"] = behavior
        changed = True
    if deadline:
        # ms-139 e-4949: 締切の後追い設定/変更。空文字は「変更なし」(他フィールドと
        # 同じ規約)。L2 締切エンジンが canonical ``deadline`` として読む。
        entry["deadline"] = deadline
        changed = True
    if priority:
        # ms-126 (e-4224 + AX round-2): a provided priority always routes through
        # the single-source resolver (Maint#1). Empty/omitted = no change (the
        # ``if priority`` guard above); a provided value must be a real severity.
        # ``untriaged`` is rejected state-INDEPENDENTLY — a client cannot
        # re-assert the machine sentinel regardless of the current value. The
        # earlier idempotent-echo carve-out (accept priority==current, incl. the
        # sentinel) was dropped because it made the same input state-dependent
        # (echo untriaged = silent no-op vs a 400 otherwise), which an AI reading
        # the surface cannot predict. To leave an untriaged entry unchanged, omit
        # the field (Web: priority=None; CLI: no --priority) rather than echo it.
        entry.setdefault("meta", {})["priority"] = _resolve_priority_for_write(
            priority, allow_untriaged=False)
        changed = True
    if changed:
        author_clean = _clean_author(author)
        if author_clean:
            meta = entry.setdefault("meta", {})
            meta["updated_by_user"] = author_clean
    return ms, entry


def task_delete(data: dict, entry_id: str, *, reason: str = "") -> dict:
    """Cancel an entry (soft delete). Returns the entry.

    Routes through ``work_base.stamp_cancel`` — the shared cancel vocabulary
    (status=cancelled + reason/actor/timestamp, append-only) that sales
    correction (e-3537) also uses (ms-109 e-3558).
    """
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    _, _, entry, _ = result
    return work_base.stamp_cancel(entry, reason=reason)


def sweep_trashed_in_project(data: dict, *, days: int = 30,
                             apply: bool = True) -> dict:
    """Hard-delete cancelled milestones / tasks older than ``days`` (ms-14 e-826).

    Pure data transform — caller is responsible for persisting the result.
    When ``apply=False`` the data is left untouched but the would-be ids
    are still returned, supporting a dry-run preview from the sweep API.

    Returns ``{ms_purged_ids, task_purged_ids}`` so the caller can
    attach the ids to a changelog entry before the items vanish.

    Items missing ``cancelled_at`` (legacy soft-deletes from before the
    meta was standardized) are intentionally NOT swept — without the
    timestamp we can't prove they're past the window. They surface in
    the trash listing and an operator can purge them manually.
    """
    cutoff = _days_ago_iso(days)
    ms_purged: list[str] = []
    task_purged: list[str] = []

    # MS sweep: cancelled milestones with cancelled_at < cutoff.
    keep_ms: list[dict] = []
    for ms in data.get("milestones", []) or []:
        meta = ms.get("meta", {}) or {}
        cancelled_at = meta.get("cancelled_at", "")
        if (
            ms.get("status") == "cancelled"
            and cancelled_at
            and cancelled_at < cutoff
        ):
            ms_purged.append(ms.get("id", ""))
            continue
        keep_ms.append(ms)
    if apply:
        data["milestones"] = keep_ms

    # Task sweep: walk every milestone's entries (including nested) and
    # drop cancelled tasks past the window. We modify the *kept* MS
    # entries; the swept MS array doesn't have them anymore.
    def _filter_entries(entries: list[dict]) -> list[dict]:
        out: list[dict] = []
        for e in entries or []:
            meta = e.get("meta", {}) or {}
            cancelled_at = meta.get("cancelled_at", "")
            if (
                e.get("type") == "task"
                and work_model.is_cancelled(e)
                and cancelled_at
                and cancelled_at < cutoff
            ):
                task_purged.append(e.get("id", ""))
                continue
            # Recurse into nested entries; replace the children list with
            # the filtered version (in-place mutation under the kept entry).
            children = e.get("entries")
            if children:
                e_children = _filter_entries(children)
                if apply:
                    e["entries"] = e_children
            out.append(e)
        return out

    for ms in keep_ms:
        filtered = _filter_entries(ms.get("entries", []) or [])
        if apply:
            ms["entries"] = filtered

    return {
        "ms_purged_ids": [i for i in ms_purged if i],
        "task_purged_ids": [i for i in task_purged if i],
    }


def _days_ago_iso(days: int) -> str:
    """ISO timestamp for `days` days before now. Used as a string cutoff
    so the comparison stays a plain lexicographic check on ISO 8601."""
    import datetime
    cutoff = datetime.datetime.now() - datetime.timedelta(days=int(days))
    return cutoff.isoformat()


def _days_ago_from(iso_ts: str) -> int | None:
    """Return integer days between iso_ts and now (positive = in the past).
    Returns None on parse failure."""
    if not iso_ts:
        return None
    import datetime
    try:
        # Truncate any trailing tz info we don't handle here.
        clean = iso_ts.split("+")[0].split("Z")[0]
        when = datetime.datetime.fromisoformat(clean)
    except (ValueError, TypeError):
        return None
    delta = datetime.datetime.now() - when
    return max(0, delta.days)


def entry_move(data: dict, entry_id: str, *,
               task_id: str = "", ms_id: str = "") -> None:
    """Move an entry under a task or to another milestone's top level."""
    if not task_id and not ms_id:
        raise ValueError("Must specify task_id or ms_id")

    src = find_entry(data, entry_id)
    if not src:
        raise ValueError(f"Entry not found: {entry_id}")
    _, src_list, entry, src_idx = src

    if ms_id:
        target_ms = None
        for ms in data["milestones"]:
            if ms["id"] == ms_id:
                target_ms = ms
                break
        if not target_ms:
            raise ValueError(f"Milestone not found: {ms_id}")
        src_list.pop(src_idx)
        target_ms.setdefault("entries", []).append(entry)
    else:
        dst = find_entry(data, task_id)
        if not dst:
            raise ValueError(f"Task not found: {task_id}")
        _, _, task_entry, _ = dst
        if task_entry.get("id") == entry_id:
            raise ValueError("Cannot move entry under itself")
        src_list.pop(src_idx)
        task_entry.setdefault("entries", []).append(entry)


# ---------------------------------------------------------------------------
# Progress & Summary
# ---------------------------------------------------------------------------

def update_progress(milestone: dict, progress_str: str) -> None:
    """Update milestone progress if specified. Auto-transitions status."""
    if progress_str:
        try:
            p = int(progress_str)
            milestone["progress"] = max(0, min(100, p))
        except ValueError:
            return

    p = milestone.get("progress", 0)
    if p > 0 and milestone.get("status") == "todo":
        milestone["status"] = "in_progress"


def auto_update_summary(data: dict, max_entries: int = 5) -> None:
    """Auto-update summary with recent work trail across all milestones."""
    all_entries = []
    for ms in data.get("milestones", []):
        all_entries.extend(_collect_entries_flat(ms.get("entries", [])))

    all_entries.sort(key=lambda x: (x[0], x[1]), reverse=True)
    recent = all_entries[:max_entries]

    if not recent:
        return

    recent.reverse()
    trail = " → ".join(desc for _, _, desc in recent)
    data["summary"] = f"直近の流れ: {trail}"


def _collect_entries_flat(entries: list) -> list:
    """Recursively collect all entries with timestamps into a flat list."""
    result = []
    for entry in entries:
        created = entry.get("created_at") or entry.get("date") or ""
        eid = entry.get("id", "e-0")
        try:
            id_num = int(eid.split("-", 1)[1]) if "-" in eid else 0
        except (ValueError, IndexError):
            id_num = 0
        if created:
            result.append((created, id_num, entry.get("description", "")))
        result.extend(_collect_entries_flat(entry.get("entries", [])))
    return result


# ---------------------------------------------------------------------------
# Commit logging
# ---------------------------------------------------------------------------

def check_duplicate_commit(entries: list, commit_hash: str) -> bool:
    """Check if commit hash already exists in entries (including nested)."""
    for entry in entries:
        if entry.get("type") == "commit" and entry.get("meta", {}).get("hash", "").startswith(commit_hash):
            return True
        if check_duplicate_commit(entry.get("entries", []), commit_hash):
            return True
    return False


def log_commit(data: dict, *, ms_id: str = "", commit_hash: str,
               message: str, date: str, summary: str = "",
               progress: str = "", behavior: str = "",
               resolves: str = "", resolves_explicit: bool = False,
               actor: dict | None = None,
               session_id: str = "", source: str = "",
               author: dict | None = None) -> dict:
    """Record a commit to the target milestone. Returns result info dict.

    ``actor`` (ms-51 / e-934): optional ``{"machine": ..., "agent": ...}``
    dict attached to ``meta.actor``. Callers should pass the result of
    :func:`lib.agent.get_actor`. Kept as a parameter (not auto-fetched
    inside core) because ``core.py`` is meant to be pure I/O-free
    business logic; the agent identity lookup involves filesystem and
    env reads, which belongs in the CLI layer.

    ``session_id`` (ms-57 / e-1062): optional opaque session identifier
    persisted as ``meta.session_id``. Powers the session-log aggregation
    query (``WHERE meta.session_id == X``) used by session-end and rescue.
    Forward-only: past commits without it stay untagged. Empty string
    is the documented "no session" sentinel — those entries simply won't
    appear in aggregation results.

    ``source`` (ms-79 / e-1817): optional axis tag distinguishing AI
    auto-op commits from human dialog commits. Stored on
    ``meta.source``. Currently the recognised value is ``"auto-op"``;
    empty string keeps the historical behaviour (= human-untagged). The
    field powers the source-breakdown facet in retro_query.

    ``author`` (ms-78 / e-1909): optional ``{"user_id", "email",
    "display_name"}`` dict attached to ``meta.author``. This is the
    *human* identity that initiated the commit (= sign-in identity on
    the server, distinct from ``actor`` which is the machine/agent
    pair). Server-side callers should pass it whenever they can resolve
    the calling user record; the Web UI then has a 1-hop label for the
    author column without walking the users collection. Empty / unset
    fields are dropped so the persisted shape stays tight.

    ms-114 e-3742 — record-authoring seam: this function no longer authors the
    ledger mutations directly. It builds a ``kind="commit"`` *report* (the raw
    fact the client observed) and routes it through
    ``report_primitive.apply_report``, which dispatches to
    :func:`commit_authoring_rule` (the *author*). The client-facing signature,
    stdout contract, and local-mode behaviour are unchanged (SPEC 方針7
    expand/contract の expand step): all three callers (CLI ``cmd_log`` /
    ``cmd_log_finalize`` / the API ``project.log`` endpoint) keep passing the
    same kwargs. What changes is *who authors*: separating actor from author is
    the structural fix for "機構を書いた≠達成した" (ms-109 fable review). This
    slice moves the commit-entry construction and the task-resolution
    interpretation server-side; ``progress`` stays a client-reported value (its
    server-side derivation is a later slice — see :func:`commit_authoring_rule`).
    """
    report = report_primitive.make_report(
        kind="commit",
        actor=_commit_report_actor(actor, session_id),
        payload={
            "hash": commit_hash,
            "message": message,
            "date": date,
            "summary": summary,
            "progress": progress,
            "behavior": behavior,
            "resolves": resolves,
            "resolves_explicit": resolves_explicit,
            "ms_id": ms_id,
            # Rich provenance the commit rule persists but the generic report
            # schema doesn't model (actor = machine/agent pair, author = human
            # identity). They travel in the payload because only the commit
            # authoring rule understands them.
            "actor": actor or {},
            "author": author or {},
        },
        subject=ms_id,
        session_id=session_id,
        source=source,
        occurred_at=date or "",
        origin_verb="log",
        evidence=[commit_hash] if commit_hash else [],
    )
    rules = report_primitive.new_authoring_registry()
    report_primitive.register_authoring_rule(rules, "commit", commit_authoring_rule)
    # Two-layer dedup (SPEC 方針7 / e-3742 承認点3): the authoritative dedup is
    # the in-ledger hash scan inside the rule (persisted commits from prior
    # runs). ``seen`` is apply_report's *replay* idempotency ledger — fresh per
    # call here, so it never masks the hash-based check; it only guarantees a
    # single report object isn't authored twice within this call.
    outcome = report_primitive.apply_report(data, report, rules=rules, seen={})
    if outcome.get("status") == "authored":
        return outcome["result"]
    # Structurally unreachable: the report is always valid (fixed kind, non-empty
    # actor, dict payload) and the rule is always registered, and ``seen`` starts
    # empty. Refuse rather than return a malformed result (data-immutability).
    raise RuntimeError(f"commit report was not authored: {outcome}")


def _commit_report_actor(actor: dict | None, session_id: str) -> str:
    """Derive the report-level ``actor`` string (a required, non-empty field) for
    a commit report. The rich ``{machine, agent}`` pair travels in the payload;
    this is just a stable label for the report envelope. Falls back through
    machine → agent → session_id → ``"cli"`` so it is never blank."""
    label = ""
    if isinstance(actor, dict):
        label = actor.get("machine") or actor.get("agent") or ""
    return label or session_id or "cli"


def commit_authoring_rule(project_data: dict, report: dict) -> dict:
    """Server-side authoring rule for a ``kind="commit"`` report (ms-114 e-3742).

    The first concrete authoring rule re-homed from ``beacon log``'s internal
    logic behind the report/request seam (``lib/report_primitive.py``). The
    client emits a *report* of the raw commit fact; this rule — the *author* —
    fans it out to the canonical ledger mutations: build the commit entry,
    resolve which task the commit closes, derive progress, refresh the summary.

    Signature matches ``report_primitive.AuthoringRule`` = ``(project_data,
    report) -> result``. Pure business logic: mutates ``project_data`` in place
    and returns a result dict, no I/O. ``report["payload"]`` carries the commit
    facts + rich provenance (actor/author dicts); ``report["subject"]`` is the
    target milestone ref (empty → auto-pick, same as before); ``session_id`` /
    ``source`` are generic provenance.

    e-3742 scope boundary (leader-approved): the **task-resolution** here is the
    server-authored interpretation — the executor no longer authors which task
    its own commit closes (SPEC AC5, task-resolve dimension; AC3 "authoring 段が
    1 つ以上動く"). ``progress`` is still a *client-reported* value in
    ``payload["progress"]`` (the /beacon-log Skill's AI evaluation). Deriving
    progress server-side — which would close AC5 in the progress dimension too —
    is deliberately a later slice: it needs a backend interpreter, which ms-114
    scopes out ("やらない: backend LLM 基盤"). This rule re-homes the mechanism
    and moves task-resolution authoring server-side; progress stays reported.
    """
    payload = report.get("payload") or {}
    ms_id = report.get("subject", "") or payload.get("ms_id", "")
    commit_hash = payload.get("hash", "")
    message = payload.get("message", "")
    date = payload.get("date", "")
    summary = payload.get("summary", "")
    progress = payload.get("progress", "")
    behavior = payload.get("behavior", "")
    resolves = payload.get("resolves", "")
    resolves_explicit = bool(payload.get("resolves_explicit", False))
    actor = payload.get("actor") or None
    author = payload.get("author") or None
    session_id = report.get("session_id", "")
    source = report.get("source", "")

    target = find_target_milestone(project_data, ms_id)
    entries = target.setdefault("entries", [])

    if check_duplicate_commit(entries, commit_hash):
        if progress:
            update_progress(target, progress)
        return {"status": "duplicate", "hash": commit_hash,
                "milestone": target["id"], "progress": target.get("progress", 0)}

    now = _now_iso()
    meta = {"hash": commit_hash, "message": message}
    if resolves:
        meta["resolves"] = resolves
    if actor:
        # Defensive: only persist the expected keys, drop any extra fields
        # callers might tack on. Keeps the meta shape stable for the Web UI.
        clean = {}
        if actor.get("machine"):
            clean["machine"] = actor["machine"]
        if actor.get("agent"):
            clean["agent"] = actor["agent"]
        if clean:
            meta["actor"] = clean
    if session_id:
        meta["session_id"] = session_id
    if source:
        # ms-79 / e-1817: persist the source axis so retrospect / retro
        # can filter "AI 自律 commit だけ". No-op when source is empty
        # (the historical / human default).
        meta["source"] = source
    author_clean = _clean_author(author)
    if author_clean:
        # ms-78 / e-1909: persist the human identity of the commit author.
        # See `_clean_author` for the field allowlist.
        meta["author"] = author_clean
    commit_entry = {
        "id": next_entry_id(project_data),
        "type": "commit",
        "description": summary or message,
        "date": date or now,
        "created_at": now,
        "done_at": now,
        "status": "done",
        "meta": meta,
    }
    if behavior:
        commit_entry["behavior"] = behavior

    # Task binding precedence (fixes the auto-bind footgun where `--resolves`
    # was recorded to meta but never consulted for the actual nesting):
    #   1. resolves names a concrete e-XXX task  → authoritative, no fuzzy match.
    #      (specified-but-absent falls through to top-level, never a fuzzy pick.)
    #   2. resolves explicitly empty (--resolves "") → opt out of binding; the
    #      commit sits at milestone top level.
    #   3. resolves unset (auto-fire / post-commit hook) → legacy fuzzy match on
    #      the commit message so hook-driven logs still self-nest.
    resolve_ids = re.findall(r'e-\d+', resolves or "")
    if resolve_ids:
        matched_task = _find_task_by_ids(entries, resolve_ids)
    elif resolves_explicit:
        matched_task = None
    else:
        commit_text = (summary or "") + " " + (message or "")
        matched_task = _find_matching_task(entries, commit_text)

    if matched_task:
        # ms-109 e-3560: stamp the canonical evidence→work-item link so a dev
        # commit records which task it closes the same way a sales communication
        # records the activity it fulfilled (evidence_linked_id reads both). The
        # nesting under the task is kept — this is the additive unified field.
        work_model.link_evidence(commit_entry, matched_task["id"])
        matched_task.setdefault("entries", []).append(commit_entry)
    else:
        entries.append(commit_entry)

    update_progress(target, progress)
    auto_update_summary(project_data)

    result = {
        "status": "logged",
        "hash": commit_hash,
        "entry_id": commit_entry["id"],
        "milestone": target["id"],
        "milestone_title": work_model.target_label(target),
        "progress": target.get("progress", 0),
    }
    if matched_task:
        result["matched_task"] = matched_task["id"]
    return result


def _tokenize(text: str) -> set:
    """Split text into comparable tokens for Japanese and English."""
    en_words = re.findall(r'[a-zA-Z_]{2,}', text.lower())
    ja_text = re.sub(r'[a-zA-Z0-9_\s]+', ' ', text)
    ja_chunks = re.split(r'[のをにはがでとからまでもへや、。・\s]+', ja_text)
    ja_tokens = [c for c in ja_chunks if len(c) >= 2]
    return set(en_words + ja_tokens)


def _find_task_by_ids(entries: list, ids: list):
    """Return the first task entry whose id is in ``ids`` (recursive).

    Used by ``log_commit`` when the caller passed an explicit ``--resolves
    e-XXX``: that binding is authoritative and must win over the fuzzy
    message matcher. Recurses into nested entries so a resolves target that
    lives under a parent task still binds. Returns None if no id matches
    (specified-but-absent → commit lands at milestone top level, never a
    coincidental fuzzy pick)."""
    id_set = set(ids)
    for entry in entries:
        if entry.get("type") == "task" and entry.get("id") in id_set:
            return entry
        nested = _find_task_by_ids(entry.get("entries", []), ids)
        if nested is not None:
            return nested
    return None


def _find_matching_task(entries: list, commit_text: str):
    """Find the best matching task for a commit based on content."""
    id_matches = re.findall(r'e-\d+', commit_text)
    if id_matches:
        for entry in entries:
            if entry.get("id") in id_matches and entry.get("type") == "task":
                return entry

    commit_tokens = _tokenize(commit_text)
    if not commit_tokens:
        return None

    candidates = []
    for entry in entries:
        if entry.get("type") != "task" or not work_model.is_open(entry):
            continue
        task_text = entry.get("description", "") + " " + entry.get("detail", "")
        task_tokens = _tokenize(task_text)
        if not task_tokens:
            continue
        overlap = len(commit_tokens & task_tokens)
        if overlap > 0:
            score = overlap / min(len(commit_tokens), len(task_tokens))
            candidates.append((score, overlap, entry))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, best_overlap, best_entry = candidates[0]
    # Require at least 2 overlapping tokens or a score above 0.3 to avoid
    # false positives from coincidental single-word matches.
    if best_overlap >= 2 or (best_overlap >= 1 and best_score >= 0.3):
        return best_entry
    return None


# ---------------------------------------------------------------------------
# GitHub Issue import (ms-28)
# ---------------------------------------------------------------------------

def _find_imported_issue_numbers(data: dict) -> set:
    """Return set of GitHub issue numbers already imported as task entries."""
    imported = set()
    for ms in data.get("milestones", []):
        for entry in _iter_all_entries(ms.get("entries", [])):
            n = entry.get("meta", {}).get("issue_number")
            if n is not None:
                imported.add(int(n))
    return imported


def _iter_all_entries(entries: list):
    """Recursively yield all entries."""
    for entry in entries:
        yield entry
        yield from _iter_all_entries(entry.get("entries", []))


def issue_import(data: dict, *, ms_id: str = "", number: int, url: str,
                 title: str = "", body: str = "", date: str = "") -> str:
    """Import a GitHub Issue as a task entry. Returns the new entry id.

    ms-114 e-3743 — record-authoring seam (second inward-machine verb after the
    commit rule, e-3742): this function no longer authors the task entry
    directly. It builds a ``kind="issue_import"`` *report* (the raw external
    fact: an Issue's number / url / title / body) and routes it through
    ``report_primitive.apply_report`` → :func:`issue_import_authoring_rule` (the
    *author*). The client-facing signature and return value (the new entry id)
    are unchanged; the three callers in ``commands.py`` (single import + the sync
    loop) keep working, and the CLI-level "already imported" dedup guard is
    untouched. This is the *intake* dimension of "client reports, server authors"
    (SPEC AC4/AC5): the interpretation — building ``"#N: title"``, stamping the
    untriaged sentinel — now lives server-side, not in the client that observed
    the Issue."""
    report = report_primitive.make_report(
        kind="issue_import",
        actor=_get_actor() or "cli",
        payload={
            "number": number,
            "url": url,
            "title": title,
            "body": body,
            "date": date,
            "ms_id": ms_id,
            # Provenance the rule stamps on the entry; resolved here (the CLI
            # side) so the authoring rule stays a pure transform.
            "created_by": _get_actor(),
        },
        subject=ms_id,
        occurred_at=date or "",
        origin_verb="issue_import",
        evidence=[url] if url else [],
    )
    rules = report_primitive.new_authoring_registry()
    report_primitive.register_authoring_rule(
        rules, "issue_import", issue_import_authoring_rule)
    outcome = report_primitive.apply_report(data, report, rules=rules, seen={})
    if outcome.get("status") == "authored":
        return outcome["result"]["entry_id"]
    # Structurally unreachable (fixed kind, non-empty actor, dict payload, fresh
    # seen). Refuse rather than return a malformed id (data-immutability).
    raise RuntimeError(f"issue_import report was not authored: {outcome}")


def issue_import_authoring_rule(project_data: dict, report: dict) -> dict:
    """Server-side authoring rule for a ``kind="issue_import"`` report
    (ms-114 e-3743). Authors a task entry from a reported GitHub Issue — the
    second concrete authoring rule after :func:`commit_authoring_rule`, following
    the same ``report_primitive.AuthoringRule`` = ``(project_data, report) ->
    result`` shape (pure: mutates ``project_data`` in place, no I/O).

    ``report["payload"]`` carries the raw Issue facts (number / url / title /
    body) the client observed; ``subject`` is the target milestone ref (empty →
    auto-pick). The *interpretation* — the ``"#N: title"`` description and the
    untriaged-priority sentinel — is authored HERE, server-side, so the client
    that fetched the Issue no longer authors the ledger record of it (SPEC AC4/AC5,
    intake dimension). Returns ``{"status": "imported", "entry_id": <eid>, ...}``;
    the ``issue_import`` adapter unwraps ``entry_id`` to preserve its str return."""
    payload = report.get("payload") or {}
    ms_id = report.get("subject", "") or payload.get("ms_id", "")
    number = payload.get("number")
    url = payload.get("url", "")
    title = payload.get("title", "")
    body = payload.get("body", "")
    date = payload.get("date", "")
    created_by = payload.get("created_by") or _get_actor()

    target = find_target_milestone(project_data, ms_id)
    entries = target.setdefault("entries", [])
    eid = next_entry_id(project_data)
    now = _now_iso()
    description = f"#{number}: {title}" if title else f"Issue #{number}"
    # ms-126 (e-4225): importing a GitHub Issue is a machine path — no human has
    # judged its priority yet, so it must land with the ``untriaged`` sentinel
    # (surfaces in the untriaged-backlog debt trigger, prompting a human to
    # triage it). Route that stamp through the single-source helper rather than
    # hardcoding the literal here, so the sentinel-write mechanism has exactly
    # one authority: if its value or machine-path rule ever changes, this import
    # path follows automatically instead of drifting into a stale copy.
    imported_priority = _resolve_priority_for_write("", allow_untriaged=True)
    entry = {
        "id": eid,
        "type": "task",
        "description": description,
        "date": date or now,
        "created_at": now,
        "done_at": None,
        "status": "todo",
        "meta": {
            "issue_number": number,
            "issue_url": url,
            "created_by": created_by,
            "priority": imported_priority,
        },
    }
    if body and body.strip():
        entry["detail"] = body.strip()[:500]
    entries.append(entry)
    return {"status": "imported", "entry_id": eid,
            "issue_number": number, "milestone": target["id"]}


# PR entry (ms-15)
# ---------------------------------------------------------------------------

def pr_add(data: dict, *, ms_id: str = "", url: str, author: str = "",
           intent: str = "", date: str = "", title: str = "",
           commits: list = None, session_id: str = "") -> str:
    """Add a PR entry to a milestone. Returns the new entry id.

    commits: list of {"oid": "<sha>", "messageHeadline": "<msg>"} from gh pr view.
    session_id (ms-57 / e-1062): opaque session identifier persisted as
        meta.session_id so session-log aggregation can pull PRs created in
        a given session. Forward-only — empty string means "no session".
    """
    import re as _re
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])
    eid = next_entry_id(data)
    eid_num = int(eid.split("-")[1])

    pr_number = None
    m = _re.search(r'/pull/(\d+)', url)
    if m:
        pr_number = int(m.group(1))

    # Prefer explicit title; fall back to "PR#{n}" so URL stays only in meta
    description = title or (f"PR#{pr_number}" if pr_number else url)

    child_entries = []
    for i, commit in enumerate(commits or [], start=1):
        sha = commit.get("oid", "")
        msg = commit.get("messageHeadline", "") or (sha[:7] if sha else "commit")
        child_entries.append({
            "id": f"e-{eid_num + i}",
            "type": "commit",
            "description": msg,
            "date": date,
            "created_at": date,
            "status": "done",
            "meta": {"hash": sha[:7] if sha else ""},
        })

    meta = {
        "url": url,
        "author": author,
        "pr_number": pr_number,
        "pr_status": "in_review",
        "review_status": "pending",
        "intent": intent,
        "review_rationale": None,
    }
    if session_id:
        meta["session_id"] = session_id
    entry = {
        "id": eid,
        "type": "pr",
        "description": description,
        "date": date,
        "created_at": date,
        "done_at": None,
        "status": "in_review",
        "meta": meta,
    }
    if child_entries:
        entry["entries"] = child_entries

    entries.append(entry)
    return eid


def _append_review_history(meta: dict, *, status: str,
                            rationale: str = "", actor: str = "") -> None:
    """Append a single transition to meta.review_history[] (e-609).

    Each entry: {"at": <iso>, "status": <new review_status>,
                 "rationale": <text>, "actor": <who>}

    This lives in meta so the timeline view can render the back-and-forth
    sequence: pending → changes_requested → pending → approved, etc.
    Idempotent only by *timestamp* — same status transitioned at the same
    millisecond will collide, but real usage has whole-second gaps.
    """
    history = meta.setdefault("review_history", [])
    if not isinstance(history, list):
        # Heal a corrupted field rather than crashing the caller.
        history = []
        meta["review_history"] = history
    history.append({
        "at": _now_iso(),
        "status": status,
        "rationale": rationale or "",
        "actor": actor or _get_actor(),
    })


def pr_request_review(data: dict, entry_id: str) -> tuple[dict, dict]:
    """Set PR to in_review. Returns (milestone, entry)."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "in_review"
    meta["review_status"] = "pending"
    meta["review_requested_at"] = _now_iso()
    entry["status"] = "in_review"
    _append_review_history(meta, status="pending")
    return ms, entry


def pr_request_changes(data: dict, entry_id: str, *, rationale: str = "") -> tuple[dict, dict]:
    """Request changes on a PR: review_status=changes_requested, entry stays in_review."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "in_review"
    meta["review_status"] = "changes_requested"
    meta["reviewed_at"] = _now_iso()
    entry["status"] = "in_review"
    if rationale:
        meta["review_rationale"] = rationale
    _append_review_history(meta, status="changes_requested", rationale=rationale)
    return ms, entry


def pr_record_review(data: dict, entry_id: str, *, review_text: str,
                     verdict: str, date: str = "") -> tuple[dict, dict, dict]:
    """Record an AI code review as a child note entry under the PR.

    verdict: 'approved' or 'changes_requested'
    Returns (milestone, pr_entry, note_entry).
    """
    import datetime as _dt
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")

    now = _now_iso()
    note_eid = next_entry_id(data)
    note_entry = {
        "id": note_eid,
        "type": "note",
        "description": f"[AI Review] {verdict}",
        "detail": review_text,
        "date": date or now,
        "created_at": now,
        "done_at": now,
        "status": "done",
    }
    entry.setdefault("entries", []).append(note_entry)

    meta = entry.setdefault("meta", {})
    meta["review_status"] = verdict
    if verdict == "approved":
        meta["pr_status"] = "approved"
        entry["status"] = "approved"
    else:
        meta["pr_status"] = "in_review"
        entry["status"] = "in_review"
    _append_review_history(meta, status=verdict, rationale=review_text[:200])

    return ms, entry, note_entry


def pr_merge(data: dict, entry_id: str, *, date: str = "") -> tuple[dict, dict]:
    """Merge a PR: pr_status=merged, entry.status=done, done_at=today.

    Side effect (e-610): for each commit hash recorded under the PR's child
    entries, find any *other* commit entry across all milestones with the
    same short hash and tag it with `meta.pr_id = entry_id`. This is the
    "the same commit shows up both as a beacon-log entry and as a PR child
    entry — link them so the timeline shows the PR origin" rule. Idempotent:
    re-merging the same PR re-applies the same tag.
    """
    import datetime as _dt
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    now = _now_iso()
    meta["pr_status"] = "merged"
    meta["merged_at"] = now
    entry["status"] = "done"
    entry["done_at"] = date or now

    # e-610: back-link beacon-side commit entries to this PR.
    pr_commit_hashes = set()
    for child in entry.get("entries", []) or []:
        if isinstance(child, dict) and child.get("type") == "commit":
            h = (child.get("meta") or {}).get("hash", "")
            if h:
                pr_commit_hashes.add(h[:7])

    if pr_commit_hashes:
        linked = 0
        for ms_iter in data.get("milestones", []):
            if not isinstance(ms_iter, dict):
                continue
            for ent in ms_iter.get("entries", []) or []:
                if not isinstance(ent, dict) or ent.get("type") != "commit":
                    continue
                # Skip the PR's own child entries — they already live under
                # the PR; we don't need a self-loop link.
                if any(ent is c for c in entry.get("entries", []) or []):
                    continue
                ent_meta = ent.get("meta") or {}
                ent_hash = (ent_meta.get("hash") or "")[:7]
                if ent_hash and ent_hash in pr_commit_hashes:
                    ent_meta = ent.setdefault("meta", {})
                    ent_meta["pr_id"] = entry_id
                    linked += 1
        # Record how many backlinks we just stamped (informational).
        meta["linked_commits"] = linked

    return ms, entry


def pr_close(data: dict, entry_id: str) -> tuple[dict, dict]:
    """Close a PR without merging: pr_status=closed, entry.status=cancelled."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "closed"
    meta["closed_at"] = _now_iso()
    entry["status"] = "cancelled"
    return ms, entry


def pr_approve(data: dict, entry_id: str, *, rationale: str = "") -> tuple[dict, dict]:
    """Approve a PR: pr_status=approved, review_status=approved, entry.status=approved."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "approved"
    meta["review_status"] = "approved"
    meta["reviewed_at"] = _now_iso()
    entry["status"] = "approved"
    if rationale:
        meta["review_rationale"] = rationale
    _append_review_history(meta, status="approved", rationale=rationale)
    return ms, entry


def pr_reject(data: dict, entry_id: str, *, rationale: str = "") -> tuple[dict, dict]:
    """Reject a PR: review_status=rejected, entry.status=cancelled."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "closed"
    meta["review_status"] = "rejected"
    meta["reviewed_at"] = _now_iso()
    meta["closed_at"] = _now_iso()
    entry["status"] = "cancelled"
    if rationale:
        meta["review_rationale"] = rationale
    _append_review_history(meta, status="rejected", rationale=rationale)
    return ms, entry


# --- 目的達成レビュー = 職種中立な target 遷移承認 (ms-119 / e-3912) ---
# lib/transition_approval.py の pure primitive を entry-id 採番 + target 探索 +
# 永続化に配線する。今は dev(milestone) + ops(operation) をエンフォース。sales
# opportunity は既存 meeting-wrap -> judge がこのレビューの projection なので、
# ここでは二重ゲートしない (requires_spine_approval が False を返す)。

_APPROVAL_TARGET_KINDS = {
    "ms": "milestone", "op": "operation", "opp": "opportunity", "acc": "account",
}


def _approval_target_kind(target_id: str) -> str:
    return _APPROVAL_TARGET_KINDS.get(target_id.split("-", 1)[0], "")


def _find_approval_target(data: dict, target_id: str) -> dict:
    """Locate the target (milestone / operation) that owns transition-approval
    entries. Profession-neutral dispatch by id prefix."""
    kind = _approval_target_kind(target_id)
    if kind == "milestone":
        return find_target_milestone(data, target_id)
    if kind == "operation":
        ops = find_operations(data, target_id)
        if not ops:
            raise ValueError(f"Operation not found: {target_id}")
        return ops[0]
    raise ValueError(
        f"transition approval not supported for target {target_id!r} "
        f"(kind={kind or 'unknown'}); sales opportunity は既存 judge 経路で確定する"
    )


def unstarted_priority_tasks(target: dict) -> list:
    """Collect a target's UNSTARTED highest/high tasks (ms-119 / e-4579).

    The backlog cross-check for the 目的達成 gate: an unstarted highest/high task is
    a known, encoded piece of important work: leaving it untouched while claiming
    attainment is the "掃除機がある≠掃除した" hole. Walks the target's direct task
    entries (nested sub-tasks included) and returns those that ``transition_approval.
    is_backlog_gated`` flags. ``target`` is a milestone or operation dict.

    NOTE: this is a *cross-check*, not the *definition* of attainment. Attainment is
    defined on outcomes (AC); this only forces an EXPLICIT disposition on the
    important backlog so a skip is never silent (SPEC / task e-4579 §方針1)."""
    out = []

    def _walk(entries):
        for e in entries or []:
            if _ta.is_backlog_gated(e):
                out.append(e)
            # sub-tasks can also be gated work items.
            _walk(e.get("entries", []))

    _walk(target.get("entries", []))
    return out


def target_transition_approval_add(data: dict, target_id: str, *, old_state: str,
                                   new_state: str, intent: str = "",
                                   evidence: list = None, actor: str = "",
                                   session_id: str = "") -> str:
    """Create a pending transition-approval on a target. Returns entry_id.

    目的達成レビュー (= target が目的を達成したかを人間が確定する承認) の永続化
    入口。approve で遷移が実行され、reject で実行されない。呼び出し側は先に
    transition_approval.requires_spine_approval() で「そもそも承認を挟むべき
    遷移か」を判定してから呼ぶ (routine 遷移を過剰にゲートしないため)。
    """
    target = _find_approval_target(data, target_id)
    entry = _ta.build_transition_approval(
        entry_id=next_entry_id(data), target_id=target_id,
        target_kind=_approval_target_kind(target_id),
        old_state=old_state, new_state=new_state, intent=intent,
        evidence=evidence, actor=actor, created_at=_now_iso(),
    )
    if session_id:
        entry["meta"]["session_id"] = session_id
    target.setdefault("entries", []).append(entry)
    return entry["id"]


class EvidenceRequiredError(ValueError):
    """Raised when a target-transition approval is attempted with no review-evidence
    attached and no explicit acknowledgement (ms-119 / e-4205 #504 maint review).

    A ValueError subclass so existing ``except ValueError`` sites still catch it, but
    a distinct type so the CLI can render the friendly "attach evidence or ack"
    recovery path instead of a generic error."""


class BacklogUndisposedError(ValueError):
    """Raised when a target-transition approval is attempted while UNSTARTED
    highest/high tasks still lack an explicit disposition (ms-119 / e-4579).

    Carries ``undisposed`` (the list of blocking task dicts) so the CLI can render
    the disposition-recovery path. A ValueError subclass so existing
    ``except ValueError`` sites still catch it, but a distinct type so the CLI can
    show the concrete backlog rather than a generic error. This is the structural
    close of the "掃除機がある≠掃除した" hole: a completion claim cannot be approved
    while known important work sits untouched WITHOUT that skip being made explicit
    (done / superseded[理由] / blocks-attainment) on the record."""

    def __init__(self, entry_id, undisposed):
        self.entry_id = entry_id
        self.undisposed = undisposed
        super().__init__(entry_id)


class BacklogBlockedError(ValueError):
    """Raised when a target-transition approval is attempted while ≥1 gated backlog
    task's disposition is ``blocks-attainment`` (ms-119 / e-4579, #551 MUST-2).

    ``blocks-attainment`` means the judge/human recorded that the task is still
    required and NOT done — the attainment claim is, by that record, false. Merely
    HAVING a disposition must not satisfy the gate: a task named as blocking must
    actually block. Carries ``blocking`` (the list of {task, disposition-record}
    pairs) so the CLI can name each one. A ValueError subclass (existing
    ``except ValueError`` sites still catch it) but a distinct type so the CLI renders
    the specific "these tasks are marked blocks-attainment" recovery path."""

    def __init__(self, entry_id, blocking):
        self.entry_id = entry_id
        self.blocking = blocking
        super().__init__(entry_id)


def target_transition_approval_approve(data: dict, entry_id: str, *,
                                       rationale: str = "", actor: str = "",
                                       gate: dict = None,
                                       allow_no_evidence: bool = False,
                                       allow_undisposed_backlog: bool = False):
    """Approve a pending transition. Returns (entry, new_state_to_apply).

    approve は verdict を記録するだけで、実際の状態遷移は呼び出し側が
    new_state_to_apply を使って適用する (milestone_update / operation_set_status)。

    ``gate`` (ms-119 e-4006 audit): how the approval passed the human-only guard,
    recorded so an AI-session override approval cannot hide (思想 finding ①b).

    ``allow_no_evidence`` (ms-119 / e-4205, #504 maint review): the "no SILENT
    approval without review-evidence" invariant lives HERE — at the choke point every
    approval path (CLI, and any future API caller) must pass — not in a single CLI-
    layer guard that a new caller could bypass (which would re-open the very hole this
    closes). Empty evidence + not allowed → EvidenceRequiredError. Empty evidence +
    allowed → proceeds AND stamps ``no_evidence_ack`` on the gate so the conscious
    bypass is audited.

    Backlog disposition cross-check (ms-119 / e-4579, #551 AX/maint/思想 review):
    the owning target is *self-derived* from ``find_entry`` (the container that holds
    the approval entry IS the target), NOT passed in by the caller. The earlier
    ``target=None`` opt-in silently skipped the gate when a caller forgot to pass it —
    re-introducing the very SILENT skip this PR closes, inside the enforcement layer.
    Now the cross-check runs on EVERY path, deriving the backlog itself. When any
    UNSTARTED highest/high task on the target lacks a disposition, this raises:

      - ``BacklogBlockedError`` if any such task's LATEST disposition is
        ``blocks-attainment`` (the record explicitly says the goal is NOT met — the
        approval is refused, not merely warned; only an explicit ack proceeds), OR
      - ``BacklogUndisposedError`` if any such task carries NO disposition at all.

    ``allow_undisposed_backlog`` (same shape as ``allow_no_evidence``): a conscious,
    audited bypass. When True the cross-check does not raise; instead it stamps
    ``backlog_check_skipped=True`` on the gate so the skip is grep-able, exactly like
    the ``no_evidence_ack`` path — the default is "gate enabled", the skip is never
    silent.
    """
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    container, _, entry, _ = result
    if entry.get("type") != "target-transition-approval":
        raise ValueError(f"Entry {entry_id} is not a transition-approval entry")
    if not _ta.has_review_evidence(entry):
        if not allow_no_evidence:
            raise EvidenceRequiredError(entry_id)
        gate = dict(gate or {})
        gate["no_evidence_ack"] = True
    # ms-119 / e-4579: the backlog cross-check. An UNSTARTED highest/high task is a
    # known-important piece of work; approving attainment while one sits undisposed
    # (or explicitly marked blocks-attainment) is the silent "掃除機がある≠掃除した"
    # miss. The target is the container that holds this approval entry — self-derived,
    # never caller-supplied (an omitted arg must not open the gate). This is a
    # *cross-check*, not the definition of attainment (outcomes/AC define that) — so it
    # only fires on important, unstarted work, never on drained or lower-tier backlog.
    backlog = unstarted_priority_tasks(container) if container else []
    blocking = _ta.blocks_attainment_backlog(entry, backlog)
    undisposed = _ta.undisposed_backlog(entry, backlog)
    if blocking or undisposed:
        if not allow_undisposed_backlog:
            if blocking:
                raise BacklogBlockedError(entry_id, blocking)
            raise BacklogUndisposedError(entry_id, undisposed)
        gate = dict(gate or {})
        gate["backlog_check_skipped"] = True
    new_state = _ta.append_verdict(entry, status="approved", rationale=rationale,
                                   actor=actor, at=_now_iso(), gate=gate)
    return entry, new_state


def target_transition_approval_attach_evidence(data: dict, entry_id: str, *,
                                               verdict: str, summary: str,
                                               source: str = "",
                                               actor: str = "") -> dict:
    """Attach an INDEPENDENT review's evidence to a pending transition-approval
    (ms-119 / e-4205). Returns the entry.

    Evidence attaches BEFORE the verdict: once approved/rejected the request is
    terminal, so a non-pending entry is refused (attaching after the fact would be
    back-dating the record the approval was supposed to rest on).
    """
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    _, _, entry, _ = result
    if entry.get("type") != "target-transition-approval":
        raise ValueError(f"Entry {entry_id} is not a transition-approval entry")
    if entry.get("status") != "pending":
        raise ValueError(
            f"Entry {entry_id} is not pending (status={entry.get('status')}); "
            f"独立レビュー証拠は verdict の前に添付します")
    _ta.append_review_evidence(entry, verdict=verdict, summary=summary,
                               source=source, actor=actor, at=_now_iso())
    return entry


def target_transition_approval_attach_disposition(data: dict, entry_id: str, *,
                                                  task_id: str, verdict: str,
                                                  reason: str = "", source: str = "",
                                                  actor: str = "") -> dict:
    """Record a disposition for one UNSTARTED highest/high backlog task on a pending
    transition-approval (ms-119 / e-4579). Returns the entry.

    The disposition answers "what happened to this important, unstarted task?" so an
    attainment claim cannot silently skip it. Refused if the entry is not pending
    (same reason as attach-evidence: a terminal approval must not be back-dated), if
    ``task_id`` is not actually a gated backlog task on the target (so a stray id
    can't fake-satisfy the gate), or if the verdict is invalid / superseded lacks a
    reason (enforced in ``_ta.append_disposition``). ``source`` is the self-declared
    provenance (recorded, not structurally proven independent — the value is the
    non-silence + grep-able provenance, exactly like review-evidence)."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    target, _, entry, _ = result  # find_entry returns (container, parent_list, entry, index)
    if entry.get("type") != "target-transition-approval":
        raise ValueError(f"Entry {entry_id} is not a transition-approval entry")
    if entry.get("status") != "pending":
        raise ValueError(
            f"Entry {entry_id} is not pending (status={entry.get('status')}); "
            f"disposition は verdict の前に記録します")
    # Guard: task_id must be a real gated backlog task on THIS target, so a typo or a
    # stray id cannot fake-satisfy the gate for a different task that is still blocking.
    backlog_ids = {t.get("id") for t in unstarted_priority_tasks(target)}
    if task_id not in backlog_ids:
        raise ValueError(
            f"{task_id} is not an unstarted highest/high task of "
            f"{entry['meta'].get('target_id')} — disposition applies only to the "
            f"gated backlog (done/in-flight/lower-priority tasks are not gated)")
    _ta.append_disposition(entry, task_id=task_id, verdict=verdict, reason=reason,
                           source=source, actor=actor, at=_now_iso())
    return entry


def target_transition_approval_reject(data: dict, entry_id: str, *,
                                      rationale: str = "", actor: str = "",
                                      gate: dict = None) -> dict:
    """Reject a pending transition (it does NOT execute). Returns the entry."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    _, _, entry, _ = result
    if entry.get("type") != "target-transition-approval":
        raise ValueError(f"Entry {entry_id} is not a transition-approval entry")
    _ta.append_verdict(entry, status="rejected", rationale=rationale, actor=actor,
                       at=_now_iso(), gate=gate)
    return entry


def _extract_pr_number_from_url(url: str) -> int | None:
    """Extract a PR number from a GitHub PR URL (or return None)."""
    if not url:
        return None
    import re as _re
    m = _re.search(r"/pull/(\d+)(?:[/?#].*)?$", url.strip())
    if m:
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


def plan_pr_sync(data: dict, gh_prs: list) -> list:
    """ms-61 / e-2005 — produce a plan of PR-entry transitions needed to
    align beacon state with GitHub state.

    Pure / read-only — returns the list of actions but does not mutate
    `data`. The CLI wrapper (`cmd_pr_sync`) applies the actions and saves.

    Args:
        data: project dict (= load_project() output).
        gh_prs: list of dicts as returned by ``gh pr list --state all
            --json number,state,url,mergedAt``. Each dict carries at least
            ``number`` (int), ``state`` ("OPEN" / "CLOSED" / "MERGED"),
            and either ``url`` or both number+repo.

    Returns a list of action dicts, each:
        {"entry_id": str, "pr_number": int, "action": "merge"|"close"|"skip",
         "from_status": str, "to_status": str, "reason": str}

    Mapping rules:
      - GitHub state=MERGED and beacon entry not yet in
        {done, merged} → action="merge" (= advance to terminal done).
      - GitHub state=CLOSED (not merged) and beacon entry not yet in
        {cancelled, closed} → action="close" (= cancel, no merge).
      - Everything else (= already aligned, or GitHub still OPEN) →
        action="skip".

    The plan is informative for callers; CLI surfaces it with
    `--dry-run` so humans can audit before applying.
    """
    # Build PR number → GitHub state lookup.
    gh_by_number: dict = {}
    for row in gh_prs or []:
        if not isinstance(row, dict):
            continue
        num = row.get("number")
        if not isinstance(num, int):
            continue
        gh_by_number[num] = (row.get("state") or "").upper()

    actions: list = []

    def _walk(entries: list):
        for e in entries or []:
            if isinstance(e, dict) and e.get("type") == "pr":
                meta = e.get("meta") or {}
                pr_num = meta.get("pr_number")
                if not isinstance(pr_num, int):
                    pr_num = _extract_pr_number_from_url(meta.get("url", ""))
                if not pr_num:
                    continue  # un-numbered (= local-only) PR entry, skip
                gh_state = gh_by_number.get(pr_num)
                if not gh_state:
                    continue  # GitHub doesn't know this PR (= deleted? not visible?), skip
                cur_status = e.get("status", "")
                if gh_state == "MERGED":
                    if cur_status in ("done",) or meta.get("pr_status") == "merged":
                        actions.append({
                            "entry_id": e.get("id", ""),
                            "pr_number": pr_num,
                            "action": "skip",
                            "from_status": cur_status,
                            "to_status": cur_status,
                            "reason": "already merged in beacon",
                        })
                    else:
                        actions.append({
                            "entry_id": e.get("id", ""),
                            "pr_number": pr_num,
                            "action": "merge",
                            "from_status": cur_status,
                            "to_status": "done",
                            "reason": "GitHub MERGED but beacon not yet",
                        })
                elif gh_state == "CLOSED":
                    if cur_status in ("cancelled",) or meta.get("pr_status") == "closed":
                        actions.append({
                            "entry_id": e.get("id", ""),
                            "pr_number": pr_num,
                            "action": "skip",
                            "from_status": cur_status,
                            "to_status": cur_status,
                            "reason": "already closed in beacon",
                        })
                    else:
                        actions.append({
                            "entry_id": e.get("id", ""),
                            "pr_number": pr_num,
                            "action": "close",
                            "from_status": cur_status,
                            "to_status": "cancelled",
                            "reason": "GitHub CLOSED but beacon not yet",
                        })
                # GitHub OPEN → no action; this is the steady state.
            if isinstance(e, dict):
                _walk(e.get("entries", []))

    for ms in data.get("milestones", []):
        if isinstance(ms, dict):
            _walk(ms.get("entries", []))

    return actions


def apply_pr_sync(data: dict, actions: list) -> dict:
    """ms-61 / e-2005 — apply a plan produced by `plan_pr_sync`.

    Mutates `data` in place. Returns a summary dict
    ``{"merged": n, "closed": n, "skipped": n, "errors": [...]}``.
    """
    summary = {"merged": 0, "closed": 0, "skipped": 0, "errors": []}
    for act in actions or []:
        eid = act.get("entry_id", "")
        a = act.get("action", "")
        if a == "skip" or not eid:
            summary["skipped"] += 1
            continue
        try:
            if a == "merge":
                pr_merge(data, eid)
                summary["merged"] += 1
            elif a == "close":
                pr_close(data, eid)
                summary["closed"] += 1
            else:
                summary["skipped"] += 1
        except ValueError as e:
            summary["errors"].append({"entry_id": eid, "error": str(e)})
    return summary


# ---------------------------------------------------------------------------
# Save entry (ms-16)
# ---------------------------------------------------------------------------

def check_duplicate_save(entries: list, source: str, url: str, revision_id: str) -> bool:
    """Check if a save entry with same source+identifier already exists."""
    if source == "manual":
        return False
    for entry in entries:
        if entry.get("type") == "save":
            meta = entry.get("meta", {})
            if meta.get("source") == source:
                if url and meta.get("url") == url:
                    return True
                if revision_id and meta.get("revision_id") == revision_id:
                    return True
        if check_duplicate_save(entry.get("entries", []), source, url, revision_id):
            return True
    return False


def save_entry(data: dict, *, ms_id: str = "", description: str,
               source: str, date: str, url: str = "",
               revision_id: str = "", hash: str = "",
               progress: str = "") -> dict:
    """Record a save entry to the target milestone. Returns result info dict."""
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])

    if check_duplicate_save(entries, source, url, revision_id):
        if progress:
            update_progress(target, progress)
        return {"status": "duplicate", "milestone": target["id"],
                "progress": target.get("progress", 0)}

    now = _now_iso()
    entry = {
        "id": next_entry_id(data),
        "type": "save",
        "description": description,
        "date": date or now,
        "created_at": now,
        "done_at": now,
        "status": "done",
        "meta": {"source": source},
    }
    if url:
        entry["meta"]["url"] = url
    if revision_id:
        entry["meta"]["revision_id"] = revision_id
    if hash:
        entry["meta"]["hash"] = hash
    entries.append(entry)
    update_progress(target, progress)

    return {"status": "saved", "entry_id": entry["id"],
            "milestone": target["id"], "progress": target.get("progress", 0)}


# ---------------------------------------------------------------------------
# Multi-agent coordination (ms-17)
# ---------------------------------------------------------------------------

def milestone_depends(data: dict, ms_id: str, depends_on: list) -> dict:
    """Set depends_on for a milestone. Returns the milestone."""
    all_ids = {ms["id"] for ms in data["milestones"]}
    for dep_id in depends_on:
        if dep_id == ms_id:
            raise ValueError("Milestone cannot depend on itself")
        if dep_id not in all_ids:
            raise ValueError(f"Dependency not found: {dep_id}")
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            if depends_on:
                ms["depends_on"] = depends_on
            elif "depends_on" in ms:
                del ms["depends_on"]
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_workspace(data: dict, ms_id: str, workspace: str) -> dict:
    """Set workspace path for a milestone. Returns the milestone."""
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            if workspace:
                ms["workspace"] = workspace
            elif "workspace" in ms:
                del ms["workspace"]
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


# OSS: worktree lifecycle management core
# The functions below handle git worktree fields on milestone objects.
# workspace_branch, workspace_path, executor, executor_assigned_at are optional fields.
# Human Executor notification (trigger fire, multi-user management) is closed-source.

def milestone_worktree_set(
    data: dict,
    ms_id: str,
    workspace_branch: str,
    workspace_path: str,
    executor: str,
    executor_assigned_at: str,
) -> dict:
    """
    Set worktree fields on a milestone (OSS: git worktree lifecycle core).
    All fields are optional; pass empty string to skip setting.
    Returns the updated milestone dict.
    """
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            if workspace_branch:
                ms["workspace_branch"] = workspace_branch
            if workspace_path:
                ms["workspace_path"] = workspace_path
            if executor:
                ms["executor"] = executor
            if executor_assigned_at:
                ms["executor_assigned_at"] = executor_assigned_at
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_worktree_clear(data: dict, ms_id: str) -> dict:
    """
    Clear worktree fields from a milestone after cleanup (OSS: git worktree lifecycle core).
    Returns the updated milestone dict.
    """
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            for field in ("workspace_path", "workspace_branch", "executor", "executor_assigned_at"):
                ms.pop(field, None)
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_graph(data: dict) -> dict:
    """Build dependency graph with topological waves. Returns {nodes, edges, waves}."""
    nodes = []
    edges = []
    id_set = set()

    for ms in data["milestones"]:
        if ms.get("status") == "cancelled":
            continue
        ms_id = ms["id"]
        id_set.add(ms_id)
        nodes.append({
            "id": ms_id,
            "title": work_model.target_label(ms),
            "status": ms.get("status", ""),
            "progress": ms.get("progress", 0),
            "workspace": ms.get("workspace"),
            "depends_on": ms.get("depends_on", []),
        })
        for dep in ms.get("depends_on", []):
            edges.append({"from": ms_id, "to": dep, "type": "depends_on"})

    # Cross-MS tasks
    for ms in data["milestones"]:
        if ms.get("status") == "cancelled":
            continue
        for entry in ms.get("entries", []):
            req_by = entry.get("meta", {}).get("requested_by", "")
            if req_by and req_by in id_set:
                edges.append({"from": req_by, "to": ms["id"],
                              "type": "cross_task", "task_id": entry.get("id", "")})

    # Kahn's algorithm for waves
    in_degree = {n["id"]: 0 for n in nodes}
    for n in nodes:
        for dep in n["depends_on"]:
            if dep in in_degree:
                in_degree[n["id"]] += 1

    waves = []
    remaining = dict(in_degree)
    while remaining:
        wave = sorted([nid for nid, deg in remaining.items() if deg == 0])
        if not wave:
            # Cycle detected - dump remaining as final wave with warning
            wave = sorted(remaining.keys())
            waves.append({"wave": len(waves) + 1, "milestones": wave, "cycle": True})
            break
        waves.append({"wave": len(waves) + 1, "milestones": wave})
        for nid in wave:
            del remaining[nid]
        for n in nodes:
            if n["id"] in remaining:
                for dep in n["depends_on"]:
                    if dep in {w for w in wave}:
                        remaining[n["id"]] -= 1

    return {"nodes": nodes, "edges": edges, "waves": waves}


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def entries_to_json(entries: list) -> list:
    """Convert entries to a JSON-serializable list (recursive)."""
    result = []
    for e in entries:
        item = {
            "id": e.get("id", ""),
            "type": e.get("type", ""),
            "description": e.get("description", ""),
            "status": e.get("status", "todo"),
            "created_at": e.get("created_at", e.get("date", "")),
            "done_at": e.get("done_at"),
        }
        if e.get("meta"):
            item["meta"] = e.get("meta", {})
        if e.get("detail"):
            item["detail"] = e["detail"]
        # ms-139 e-4949: deadline を JSON に出す (task list --json / session-start /
        # cockpit の締切超過 surface が読む top-level フィールド)。
        for field in ("motivation", "acceptance_criteria", "behavior", "deadline"):
            if e.get(field):
                item[field] = e[field]
        children = e.get("entries", [])
        if children:
            item["entries"] = entries_to_json(children)
        result.append(item)
    return result


def count_task_status(entries: list) -> tuple[int, int]:
    """Count total and done tasks/commits/PRs/saves recursively. Returns (total, done).

    Note: `save` entries are intrinsically completed artifacts (doc adds, etc.)
    so they always count as done. Including them makes MS progress reflect
    save-heavy work (research / design MSs) that would otherwise show 0%.

    ms-61 / e-2005 — PR lifecycle alignment forcing function. A PR entry's
    lifecycle is `in_review → approved → merged` (or `closed`). Until e-2005
    only `done` / `cancelled` counted as completed, so a PR that was
    `beacon pr approve`'d (= status="approved") never advanced the MS
    progress counter even though the human work was finished. This caused
    ms-75 / ms-76 / ms-83 progress to look stuck at < 100 %.

    The fix recognises PR-specific terminal states (`approved` / `merged`)
    as "done" for counting purposes. The structural status of the PR entry
    itself is unchanged; this just teaches the counter to read PR lifecycle
    correctly.

    Tracking-only: `closed` PRs are counted as done as well (= the work is
    no longer in-flight, even if it wasn't merged). PRs in `in_review`
    state still count as in-progress (not done).
    """
    PR_DONE_STATUSES = ("done", "cancelled", "approved", "merged", "closed")
    total = 0
    done = 0
    for e in entries:
        etype = e.get("type")
        if etype in ("task", "commit", "pr", "save"):
            total += 1
            estatus = e.get("status")
            if etype == "pr":
                # PR lifecycle: approved / merged / closed all count as
                # terminal "done" for MS progress purposes (e-2005).
                if estatus in PR_DONE_STATUSES:
                    done += 1
            else:
                if estatus in (work_model.DONE_STATUS, work_model.CANCELLED_STATUS):
                    done += 1
        t, d = count_task_status(e.get("entries", []))
        total += t
        done += d
    return total, done


def project_targets(data: dict) -> list:
    """③ shared-frame projection (ms-108 e-3269): map this development
    project's Targets (Milestones) into the occupation-agnostic shape the
    shared frame (status / session-start) consumes.

    Each item carries the generic core every occupation shares — ``id`` /
    ``label`` (via the tolerant ``work_model.target_label`` reader) /
    ``status`` / ``kind`` and the WorkItem completion counts — plus a
    ``detail`` blob for the occupation-specific fields the frame may render.
    Development semantics (progress %, target date, priority) live only in
    ``detail`` so the generic core stays occupation-neutral (SPEC AC4).

    Cancelled (取消) Targets are excluded, matching the default status view.
    """
    targets = []
    for ms in data.get("milestones", []):
        if ms.get("status") == work_model.CANCELLED_STATUS:
            continue
        total, done = count_task_status(ms.get("entries", []))
        targets.append({
            "id": ms.get("id", ""),
            "label": work_model.target_label(ms),
            "status": ms.get("status", "todo"),
            "kind": "milestone",
            "work_items_total": total,
            "work_items_done": done,
            "detail": {
                "progress": ms.get("progress", 0),
                "target_date": ms.get("target_date", ""),
                "priority": ms.get("priority", ""),
            },
        })
    return targets


def filter_cancelled(entries: list, show_all: bool = False) -> list:
    """Filter out cancelled entries recursively."""
    if show_all:
        return entries
    result = []
    for e in entries:
        if work_model.is_cancelled(e):
            continue
        filtered = dict(e)
        children = e.get("entries", [])
        if children:
            filtered["entries"] = filter_cancelled(children, show_all)
        result.append(filtered)
    return result


def milestone_prepare_info(ms: dict) -> dict:
    """Build prepare info dict for a single milestone (used by log --prepare)."""
    entries = ms.get("entries", [])
    total_tasks, done_tasks = count_task_status(entries)

    all_flat = _collect_entries_flat(entries)
    all_flat.sort(key=lambda x: (x[0], x[1]), reverse=True)
    recent = [{"date": d, "description": desc} for d, _, desc in all_flat[:5]]

    pending_tasks = []
    for e in entries:
        if e.get("type") == "task" and work_model.is_open(e):
            pending_tasks.append({"id": e["id"], "description": e.get("description", "")})

    return {
        "id": ms["id"],
        "title": work_model.target_label(ms),
        "status": ms.get("status", ""),
        "progress": ms.get("progress", 0),
        "total_tasks": total_tasks,
        "done_tasks": done_tasks,
        "pending_tasks": pending_tasks,
        "recent_entries": recent,
    }


def collect_retro_entries(entries: list, since: str, until: str) -> list:
    """Recursively collect entries within date range for retrospective."""
    result = []
    for entry in entries:
        date = entry.get("created_at") or entry.get("date") or ""
        status = entry.get("status", "")
        if status == "cancelled":
            continue
        in_range = True
        if since and date < since:
            in_range = False
        if until and date > until:
            in_range = False

        children = entry.get("entries", [])
        child_results = collect_retro_entries(children, since, until) if children else []

        if in_range or child_results:
            item = {
                "id": entry.get("id", ""),
                "type": entry.get("type", ""),
                "description": entry.get("description", ""),
                "status": status,
                "date": date,
            }
            if entry.get("detail"):
                item["detail"] = entry["detail"]
            if entry.get("meta"):
                item["meta"] = entry["meta"]
            if entry.get("done_at"):
                item["done_at"] = entry["done_at"]
            if child_results:
                item["entries"] = child_results
            if in_range:
                result.append(item)

    return result



# ---------------------------------------------------------------------------
# Deploy operations
# ---------------------------------------------------------------------------

def deploy_void(data: dict, deploy_id: str, *, reason: str) -> dict:
    """Mark a deployment record as voided (immutable — never physically deleted).

    Returns the updated deployment entry.
    """
    for dep in data.get("deployments", []):
        if dep.get("id") == deploy_id:
            dep["voided"] = True
            dep["void_reason"] = reason
            dep["voided_at"] = _now_iso()
            dep["voided_by"] = _get_actor()
            return dep
    raise ValueError(f"Deployment not found: {deploy_id}")


# ---------------------------------------------------------------------------
# Operation operations
# ---------------------------------------------------------------------------

def _find_operation(data: dict, op_id: str) -> dict:
    for op in data.get("operations", []):
        if op.get("id") == op_id:
            return op
    raise ValueError(f"Operation not found: {op_id}")


def operation_open(data: dict, title: str, *,
                   schedule: str = "weekdays", log_source: str = "",
                   status: str = "open", activation_hint: str = "",
                   objective: str = "", acceptance_criteria: str = "",
                   priority: str = "",
                   author: dict | None = None) -> tuple[dict, dict]:
    """Create a new Operation. Returns (data, operation).

    Defaults to status='open' for backward compat. Pass status='todo' to
    create an outline-only Operation that will be activated later via
    operation_set_status (todo → in_progress → open).

    ``author`` (ms-43 / e-2246): optional ``{"user_id", "email",
    "display_name"}`` dict attached to ``meta.author`` — same contract
    as milestone_add / task_add (ms-78 / e-1909). The UI surfaces this
    as the Operation's 起票者 (= creator) in lists / detail.
    """
    if schedule not in SCHEDULE_DAYS:
        raise ValueError(f"Invalid schedule: {schedule}. Valid: {', '.join(sorted(SCHEDULE_DAYS))}")
    if status not in VALID_OP_STATUSES:
        raise ValueError(f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_OP_STATUSES))}")
    if priority and priority not in _ACCEPTED_PRIORITIES:
        raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
    op_id = next_op_id(data)
    op = {
        "id": op_id,
        "title": title,
        "label": title,  # ms-109 e-3625: canonical Target label (dual-write during skew)
        "status": status,
        "opened_at": _now_iso() if status == "open" else None,
        "closed_at": None,
        "schedule": {
            "frequency": schedule,
            "days": SCHEDULE_DAYS[schedule],
        },
        "log_source": log_source or op_id,
        "entries": [],
    }
    if activation_hint:
        op["activation_hint"] = activation_hint
    if objective:
        op["objective"] = objective
    if acceptance_criteria:
        op["acceptance_criteria"] = acceptance_criteria
    if priority:
        op["priority"] = normalize_priority(priority)
    # ms-43 / e-2246 — stamp the human author on the Operation so the UI
    # can surface a creator label (= 起票者) in Operation lists / detail.
    author_clean = _clean_author(author)
    if author_clean:
        op.setdefault("meta", {})["author"] = author_clean
    data.setdefault("operations", []).append(op)
    return data, op


# ms-120 e-3908: one guarded transition mechanism for standard-lifecycle target
# entities. Each entity kind maps a from-status to the set of legal to-statuses;
# an illegal jump raises with the allowed set (原則 3 recoverable / 原則 6 make
# illegal states unrepresentable). Generalized from trek.validate_transition
# (the proven model). The tables are MONOTONIC-FORWARD: forward skips are
# allowed, but backward / reopen-terminal moves are not — recovery from a
# terminal state goes through `purge` or a manual edit, not a standing unguarded
# write path (ms-120 決定). `opportunity` is intentionally absent: SPEC §5 makes
# its phase a human-declared master, not a guarded machine — kinds without a
# table are unguarded by design.
LIFECYCLE_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "operation": {
        "todo": frozenset({"in_progress", "open", "closed"}),
        "in_progress": frozenset({"open", "closed"}),
        "open": frozenset({"closed"}),
        "closed": frozenset(),  # terminal
    },
    # ms-132 e-4507: observing を除去。施策は todo → in_progress → done の素直な
    # ライフサイクルだけを持ち、打ち切りは status でなく削除 (soft-cancel) で表す。
    "acquisition": {
        "todo": frozenset({"in_progress", "done"}),
        "in_progress": frozenset({"done"}),
        "done": frozenset(),  # terminal
    },
}


def validate_lifecycle_transition(kind: str, from_status: str, to_status: str) -> None:
    """Raise ValueError if ``from_status → to_status`` is illegal for ``kind``.

    A kind with no table (e.g. ``opportunity``) is unguarded by design. Same
    from/to is a no-op and always allowed. The error lists the legal moves so a
    caller can recover (原則 3)."""
    table = LIFECYCLE_TRANSITIONS.get(kind)
    if table is None:
        return
    if from_status == to_status:
        return
    allowed = table.get(from_status, frozenset())
    if to_status not in allowed:
        legal = sorted(allowed) if allowed else "none (terminal state)"
        raise ValueError(
            f"illegal {kind} transition {from_status!r} → {to_status!r}. "
            f"Allowed from {from_status!r}: {legal}. "
            f"(To revive a terminal record use `purge` / manual edit.)"
        )


def operation_set_status(data: dict, op_id: str, status: str) -> dict:
    """Transition an Operation's status. Records timestamp for open transitions.

    ms-120 e-3908: the from→to transition is now validated by the shared
    lifecycle guard (illegal jumps like closed→open raise), not just the target
    enum. All named verbs (start/activate/close) and the deprecated status-write
    flow through here, so the guard cannot be bypassed."""
    if status not in VALID_OP_STATUSES:
        raise ValueError(f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_OP_STATUSES))}")
    op = _find_operation(data, op_id)
    prev = op["status"]
    validate_lifecycle_transition("operation", prev, status)
    op["status"] = status
    if status == "open" and not op.get("opened_at"):
        op["opened_at"] = _now_iso()
    if status == "closed":
        op["closed_at"] = _now_iso()
    op.setdefault("meta", {})[f"{status}_at"] = _now_iso()
    op.setdefault("meta", {})[f"{status}_by"] = _get_actor()
    return op


def operation_update(data: dict, op_id: str, *,
                     title: str = "", schedule: str = "",
                     activation_hint: str = "", objective: str = "",
                     acceptance_criteria: str = "", priority: str = "",
                     log_source: str = "") -> dict:
    """Update Operation metadata fields."""
    op = _find_operation(data, op_id)
    if title:
        work_model.set_target_label(op, title, dual_write=True)  # ms-109 e-3625
    if schedule:
        if schedule not in SCHEDULE_DAYS:
            raise ValueError(f"Invalid schedule: {schedule}. Valid: {', '.join(sorted(SCHEDULE_DAYS))}")
        op["schedule"] = {"frequency": schedule, "days": SCHEDULE_DAYS[schedule]}
    if activation_hint:
        op["activation_hint"] = activation_hint
    if objective:
        op["objective"] = objective
    if acceptance_criteria:
        op["acceptance_criteria"] = acceptance_criteria
    if priority:
        if priority not in _ACCEPTED_PRIORITIES:
            raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
        op["priority"] = normalize_priority(priority)
    if log_source:
        op["log_source"] = log_source
    return op


def operation_close(data: dict, op_id: str) -> dict:
    """Close an Operation. Returns the operation."""
    op = _find_operation(data, op_id)
    if op["status"] == "closed":
        raise ValueError(f"Operation {op_id} is already closed")
    # ms-120 e-3908: route the close verb through the SAME single transition
    # guard as operation_set_status instead of writing status directly. Closing
    # is always a legal forward move to the terminal state, so behaviour is
    # unchanged — but this removes the last direct-write path that bypassed the
    # guard, so every operation transition now flows through one mechanism (no
    # backdoor). Done-when: "ガードを迂回する状態直接設定 backdoor が存在しない".
    validate_lifecycle_transition("operation", op.get("status", ""), "closed")
    op["status"] = "closed"
    op["closed_at"] = _now_iso()
    return op


def operation_task_add(data: dict, op_id: str, description: str, *,
                       priority: str = "", motivation: str = "",
                       acceptance_criteria: str = "") -> tuple[dict, dict]:
    """Add an operation_task entry to an Operation. Returns (operation, entry)."""
    op = _find_operation(data, op_id)
    if priority and priority not in _ACCEPTED_PRIORITIES:
        raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
    eid = next_entry_id(data)
    entry = {
        "id": eid,
        "type": "operation_task",
        "description": description,
        "status": "todo",
        "created_at": _now_iso(),
        "done_at": None,
        "meta": {"created_by": _get_actor()},
    }
    if priority:
        entry["meta"]["priority"] = normalize_priority(priority)
    if motivation:
        entry["motivation"] = motivation
    if acceptance_criteria:
        entry["acceptance_criteria"] = acceptance_criteria
    op.setdefault("entries", []).append(entry)
    return op, entry


def operation_task_done(data: dict, entry_id: str, *, reason: str = "") -> dict:
    """Mark an operation_task as done. Returns the entry."""
    for op in data.get("operations", []):
        for e in op.get("entries", []):
            if e.get("id") == entry_id and e.get("type") == "operation_task":
                e["status"] = "done"
                e["done_at"] = _now_iso()
                if reason:
                    e["done_reason"] = reason
                return e
    raise ValueError(f"operation_task not found: {entry_id}")


def run_record_add(data: dict, op_id: str, *,
                   batch: str, status: str, description: str,
                   date: str = "") -> tuple[dict, dict]:
    """Add a run_record entry to an Operation. Returns (operation, entry)."""
    if status not in VALID_RUN_STATUSES:
        raise ValueError(f"Invalid status: {status}. Valid: ok, warning, error")
    op = _find_operation(data, op_id)
    eid = next_entry_id(data)
    entry = {
        "id": eid,
        "type": "run_record",
        "batch": batch,
        "status": status,
        "description": description,
        "date": date or _now_iso(),
        "meta": {"created_by": _get_actor()},
    }
    op.setdefault("entries", []).append(entry)
    return op, entry


def incident_open(data: dict, op_id: str, *,
                  title: str, description: str = "",
                  priority: str = "") -> tuple[dict, dict]:
    """Open an Incident in an Operation. Returns (operation, entry)."""
    op = _find_operation(data, op_id)
    eid = next_entry_id(data)
    if priority and priority not in _ACCEPTED_PRIORITIES:
        raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
    entry = {
        "id": eid,
        "type": "incident",
        "title": title,
        "status": "open",
        "description": description,
        "opened_at": _now_iso(),
        "resolved_at": None,
        "resolution": None,
        "linked_ms_task": None,
        "meta": {"created_by": _get_actor()},
    }
    if priority:
        entry["priority"] = normalize_priority(priority)
    op.setdefault("entries", []).append(entry)
    return op, entry


def incident_close(data: dict, incident_id: str, *, resolution: str) -> tuple[dict, dict]:
    """Resolve an Incident. Returns (operation, entry)."""
    result = find_entry(data, incident_id)
    if not result:
        raise ValueError(f"Incident not found: {incident_id}")
    container, _, entry, _ = result
    if entry.get("type") != "incident":
        raise ValueError(f"{incident_id} is not an incident entry")
    entry["status"] = "resolved"
    entry["resolved_at"] = _now_iso()
    entry["resolution"] = resolution
    meta = entry.setdefault("meta", {})
    meta["resolved_by"] = _get_actor()
    return container, entry


def incident_escalate(data: dict, incident_id: str, ms_id: str) -> tuple[dict, dict, dict]:
    """Escalate an Incident to a Milestone task. Returns (operation, incident_entry, task_entry)."""
    result = find_entry(data, incident_id)
    if not result:
        raise ValueError(f"Incident not found: {incident_id}")
    op, _, incident, _ = result
    if incident.get("type") != "incident":
        raise ValueError(f"{incident_id} is not an incident entry")
    ms = find_target_milestone(data, ms_id)
    eid = next_entry_id(data)
    task = {
        "id": eid,
        "type": "task",
        "description": f"[Incident] {incident.get('title', incident_id)}: {incident.get('description', '')}".strip(": "),
        "status": "todo",
        "created_at": _now_iso(),
        "meta": {
            "created_by": _get_actor(),
            "escalated_from": incident_id,
            "escalated_from_op": op.get("id"),
        },
    }
    ms.setdefault("entries", []).append(task)
    incident["linked_ms_task"] = eid
    return op, incident, task


def backfill_target_labels(data: dict) -> int:
    """Backfill the canonical ``label`` on every development Target — milestone
    and operation — that lacks it, copying from the legacy ``title``. Returns
    the number of records newly stamped. Idempotent: a second run over the same
    data writes nothing and returns 0.

    This is the development half of the ms-109 migrate step (e-3625). The sales
    half (account / opportunity) lives in ``sales_entities.backfill_target_labels``.
    Once both have run over a project and all new writes dual-write ``label``,
    the contract step (e-3626) can drop the legacy fallback safely. Per-record
    stamping is delegated to ``work_model.ensure_target_label`` so the base
    stays the single definition of what a canonical label is.
    """
    n = 0
    for ms in data.get("milestones", []):
        if work_model.ensure_target_label(ms):
            n += 1
    for op in data.get("operations", []):
        if work_model.ensure_target_label(op):
            n += 1
    return n
