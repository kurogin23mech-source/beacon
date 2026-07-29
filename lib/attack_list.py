"""Attack-list schema for inside-sales acquisition (ms-132 e-4501).

An *attack list* is the concrete shape ms-132 puts on top of the generic ms-131
table-doc primitive: a table linked to an Acquisition (``acq-``) whose every row
is one prospect Account to reach out to. This module owns the canonical column
schema — Account reference / 打診フェーズ (prospect phase) / 最終接触日
(last-contact date) / メモ — so the downstream ms-132 tasks (bulk register
e-4503, bulk outreach e-4504, reply-watch wiring e-4505, lead conversion e-4506)
all read and drive the *same* typed columns instead of re-deriving an ad-hoc
shape at each call site.

Design (SPEC ms-132 設計方針 1 / 2 / 6):

- 方針1 (2層モデル): the施策 (Acquisition) carries the generic
  todo/in_progress/done lifecycle; the *per-prospect* state lives here as a
  row's ``phase`` column (未接触 → 連絡済 → 返信あり → アポ). The two axes stay
  separate — a phase value is never folded into the Acquisition's own status, so
  the same Account can sit at different phases across two施策.
- 方針2 (table-doc): the list *is* a ms-131 table-doc, so it is typed, auditable
  (per-row append-only history) and rides the document-store transparency. This
  module defines only the schema; creation / linkage go through the existing
  table-doc write path (``doc table create --target acq-N``).
- 方針6 (Account ref, not embedded prospect): the ``account`` column is a typed
  ``ref`` pinned to the ``acc-`` id-space, so a row *points at* an existing
  Account (ms-115 の未接触 Account = 生リスト) rather than duplicating its contact
  data —证跡 (Communication) then attaches at Account granularity for free.

The prospect funnel is configurable: e-4502 will let ``beacon phase`` drive the
allowed set. Until then ``DEFAULT_PROSPECT_PHASES`` is the seed, and
``attack_list_columns(phases=...)`` takes an optional override so e-4502 can inject
the project's configured funnel without changing any call site here.
"""

from __future__ import annotations

# Column keys — the stable identifiers downstream tasks address cells by. Kept as
# constants (not string literals scattered across call sites) so e-4503..e-4506
# reference one source of truth and a rename is a single edit.
COL_ACCOUNT = "account"
COL_PHASE = "phase"
COL_LAST_CONTACT = "last_contact"
COL_MEMO = "memo"

# The id-space the ``account`` ref column is pinned to (Account = ``acc-``). A row
# whose account cell is not an ``acc-*`` id is rejected at write time by the
# table-doc ref checker (table_type._check_ref), so an attack list can never point
# a prospect row at a non-Account entity.
ACCOUNT_REF_PREFIX = "acc"

# Default prospect funnel (SPEC 方針1). The ordering encodes the intended forward
# progression 未接触 → 連絡済 → 返信あり → アポ; e-4502 will let a project override
# it via ``beacon phase``.
DEFAULT_PROSPECT_PHASES = ["未接触", "連絡済", "返信あり", "アポ"]

# The phase a freshly-registered prospect row starts at (= 生リストの未接触). Used
# by the bulk-register path (e-4503) so a new row's phase is well-defined.
INITIAL_PROSPECT_PHASE = DEFAULT_PROSPECT_PHASES[0]


def attack_list_columns(phases=None) -> list:
    """Return the canonical attack-list column definitions.

    ``phases`` overrides the ``phase`` enum's allowed set (e-4502 injects a
    project's configured prospect funnel); it defaults to
    ``DEFAULT_PROSPECT_PHASES``. The returned shape is the ms-131 table-doc column
    form (``{key, label, type, ...}``), ready to hand to ``table_doc.new_table``
    or ``doc table create --columns``.

    Raises ``ValueError`` when ``phases`` is given but empty — an attack list with
    no prospect phases is meaningless, and surfacing it here (at schema build)
    beats a confusing enum-validation error on the first add-row.
    """
    allowed = list(phases) if phases is not None else list(DEFAULT_PROSPECT_PHASES)
    if not allowed:
        raise ValueError("attack-list には最低 1 つの打診フェーズが必要です (phases)")
    return [
        {"key": COL_ACCOUNT, "label": "対象顧客", "type": "ref",
         "ref": ACCOUNT_REF_PREFIX},
        {"key": COL_PHASE, "label": "打診フェーズ", "type": "enum",
         "values": allowed},
        {"key": COL_LAST_CONTACT, "label": "最終接触日", "type": "date"},
        {"key": COL_MEMO, "label": "メモ", "type": "text"},
    ]


# Funnel-position indices (ms-132 e-4505 review): the send/reply verbs advance a
# row by *position* in the (configurable) prospect funnel, so name the positions
# once instead of scattering literal vals[0]/[1]/[2] across call sites. Position,
# not name, because a company may rename the phases via ``beacon phase prospect``.
PHASE_IDX_UNTOUCHED = 0   # 未接触 (entry)
PHASE_IDX_CONTACTED = 1   # 連絡済 (sent, awaiting reply)
PHASE_IDX_REPLIED = 2     # 返信あり (replied)


def phase_values(model_or_columns) -> list:
    """The prospect funnel's allowed values in order, or [] if absent.

    Accepts a table model (``{"columns": [...]}``) or a raw columns list. One
    reader for the phase enum so the send / reply / worklist verbs don't each
    re-derive it (ms-132 e-4505 保守性レビュー)."""
    cols = (model_or_columns.get("columns", [])
            if isinstance(model_or_columns, dict) else (model_or_columns or []))
    for c in cols:
        if isinstance(c, dict) and c.get("key") == COL_PHASE:
            return list(c.get("values") or [])
    return []


def is_reacted(phase, phase_values_list) -> bool:
    """True when ``phase`` is 返信あり or later (funnel position ≥ REPLIED) — a
    prospect that reacted and is therefore convertible to a lead (ms-132 e-4506).
    Pure predicate so the promote guard is unit-testable in isolation."""
    vals = phase_values_list or []
    if len(vals) <= PHASE_IDX_REPLIED:
        return False
    return phase in vals[PHASE_IDX_REPLIED:]


def find_row_by_account(rows, acc_id):
    """The first row whose account cell == ``acc_id`` (from an already-filtered
    row list, e.g. ``table_doc.active_rows(model)``), or None. Centralizes the
    row-lookup the send / reply verbs both need (ms-132 e-4505 保守性レビュー)."""
    for row in rows or []:
        if row.get("cells", {}).get(COL_ACCOUNT) == acc_id:
            return row
    return None


def is_attack_list(columns) -> bool:
    """True when a table-doc's columns match the attack-list schema shape.

    Used to pick attack lists out of an Acquisition's linked table-docs (a施策
    could carry other tables). The test is the presence of the two columns that
    *make* a table an outreach list — an ``account`` ref and a ``phase`` enum —
    rather than an exact column-set match, so a project that appended an extra
    note/owner column is still recognized.
    """
    by_key = {c.get("key"): c for c in (columns or []) if isinstance(c, dict)}
    acc = by_key.get(COL_ACCOUNT)
    phase = by_key.get(COL_PHASE)
    return bool(
        acc and acc.get("type") == "ref"
        and phase and phase.get("type") == "enum"
    )
