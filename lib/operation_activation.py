"""Pending Operation activation classification helpers (ms-61 / e-1843).

The ``/beacon-session-start`` Skill (= markdown) has a Step 3.7 that
proposes to the human whether each pending Operation (= one with
``status="todo"`` or ``status="in_progress"``) is ready to be flipped
to ``status="open"`` and run on its schedule. Before e-1843 the Skill
text said "Step 1a の結果から status==todo / in_progress の Operation を
取得する", but ``beacon status --json`` actually filters operations to
``status == "open"`` only (see ``cmd_status`` in ``lib/commands.py``),
so the Skill silently saw an empty list and Step 3.7 became dead code.

e-1843 closes the gap two ways:

1. ``beacon status --json`` now also emits a ``pending_operations[]``
   field carrying todo / in_progress entries (= the data Step 3.7
   needs). The legacy ``operations[]`` field stays open-only so all
   existing readers / Skills keep working unchanged.

2. This module's helpers turn that pending list into a
   **deterministic classification** (= verdict + reasoning) the Skill
   can render verbatim. The Skill still owns the *natural-language*
   judgement (e.g. "does the activation_hint condition look satisfied
   in this project right now?"); the helpers own the *structural*
   classification (e.g. "are the prerequisite OperationTasks done?",
   "is the hint missing entirely?", "is the verdict the empty-state
   case?"). That split is the same architecture pattern used by
   ``lib/dm_pending.format_pending_dm_summary`` (ms-70 / e-1714).

The Skill is markdown and therefore unrenderable / untestable on its
own — pinning the classifier behaviour in a pure Python helper lets
tests guard the contract across three scenarios required by e-1843:

  (S1) pending list is empty → empty string output (Skill omits the
       section entirely; no "0 件" header noise).
  (S2) at least one pending Operation → multi-line summary block with
       one bullet per Operation carrying the verdict + reasoning.
  (S3) mixed states (some pending + some open) → only pending rows
       appear in the section (open Operations are already covered by
       the "Active Operation" section earlier in Step 3 output).

Edge-case fallback contract (= e-1843 AC #3):

  (E1) ``activation_hint`` is missing / empty → verdict
       ``"unparseable"``; the bullet says "activation_hint 未設定 —
       /beacon-operation-setup でヒントを書き起こしてください".
  (E2) ``activation_hint`` exists but the helper has no way to evaluate
       it (the helper deliberately does NOT do natural-language parsing
       — it only checks the OperationTask gate) → verdict
       ``"needs-ai-judgement"``; the bullet exposes the raw hint and
       defers to the AI to decide.
  (E3) prerequisite OperationTasks are not all done → verdict
       ``"prep-first"``; the bullet lists how many remain.

The verdict vocabulary is intentionally small so the Skill can map
each verdict to one of the three response patterns its markdown
already specifies (activate-now / prep-first / too-early). Adding a
new verdict requires updating both this module and the Skill markdown
(= forcing function: tests will fail if the verdict literal drifts).
"""

from __future__ import annotations

from typing import Iterable


# ---------------------------------------------------------------------------
# Module constants (= Skill markdown reads these strings verbatim by
# calling the helpers below; tests pin the literals so a future tweak
# does not silently change the contract).
# ---------------------------------------------------------------------------

PENDING_OP_HEADER = "Pending Operation 活性化議論:"
"""Section header the Skill renders above the per-Operation bullets."""

PENDING_OP_DETAIL_HINT = (
    "  ※ 詳細は `beacon operation show <op-id>` で展開できます。"
    "活性化したい場合は `beacon operation set-status <op-id> open`、"
    "準備項目を進める場合は `/beacon-operation-setup` を使ってください。"
)
"""Trailing hint line appended unless ``detail_hint=False``."""

# Verdict vocabulary — keep this tuple small (= 4 entries) so each
# verdict maps cleanly to one Skill markdown response pattern. Tests
# pin the membership so a typo at the call site fails loudly.
VERDICT_ACTIVATE_NOW = "activate-now"
VERDICT_NEEDS_AI_JUDGEMENT = "needs-ai-judgement"
VERDICT_PREP_FIRST = "prep-first"
VERDICT_UNPARSEABLE = "unparseable"
VALID_VERDICTS = (
    VERDICT_ACTIVATE_NOW,
    VERDICT_NEEDS_AI_JUDGEMENT,
    VERDICT_PREP_FIRST,
    VERDICT_UNPARSEABLE,
)


# ---------------------------------------------------------------------------
# OperationTask helpers
# ---------------------------------------------------------------------------

def _operation_tasks(op: dict) -> list[dict]:
    """Return the OperationTask entries from an Operation row.

    ``operation_task`` entries live in ``op["entries"]`` mixed with
    other entry types (run_record / incident / log). We filter
    structurally so a future schema addition does not silently leak
    into the activation classifier.
    """
    entries = (op or {}).get("entries") or []
    return [e for e in entries if isinstance(e, dict) and e.get("type") == "operation_task"]


def _count_open_operation_tasks(op: dict) -> tuple[int, int]:
    """Return ``(open_count, total_count)`` for an Operation's tasks.

    ``open`` here means status != "done" (= matches the icon split in
    ``cmd_operation_task_list``: ● done vs ○ open). Returning the
    total alongside lets the bullet say "N/M 件未完了" without the
    caller having to re-scan the entries list.
    """
    tasks = _operation_tasks(op)
    total = len(tasks)
    open_count = sum(1 for t in tasks if t.get("status") != "done")
    return open_count, total


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_pending_operation(op: dict) -> dict:
    """Classify one pending Operation's activation state.

    Returns a dict with:
      - ``op_id``: the Operation id (for the bullet prefix)
      - ``title``: the Operation title
      - ``status``: the current status (todo / in_progress)
      - ``verdict``: one of ``VALID_VERDICTS``
      - ``reason``: a short human-readable explanation
      - ``activation_hint``: the raw hint string (may be empty)
      - ``open_tasks``: number of OperationTasks not yet done
      - ``total_tasks``: number of OperationTasks total

    The verdict order of evaluation is deliberate:

      1. If any OperationTasks remain open → ``prep-first``. Even when
         the hint looks satisfied, the project owner usually wants the
         prerequisite checklist done before flipping the Operation to
         ``open``. This is the safe default.
      2. Else if ``activation_hint`` is missing → ``unparseable``. The
         helper cannot reason about an Operation with no documented
         activation condition, so it hands the case to the human via
         a "fill in the hint" prompt.
      3. Else if ``activation_hint`` is present and all tasks are done
         → ``needs-ai-judgement``. The structural prerequisites are
         met; the natural-language judgement of whether the hint's
         condition is actually true *right now* in the project lives
         in the Skill's AI reasoning, not in this helper.
      4. ``activate-now`` is only reachable through an explicit
         ``force_activate=True`` call (= reserved for tests / future
         CLI flag). The default flow never returns ``activate-now``
         from this helper because that judgement is the Skill's job.

    The split keeps this helper deterministic and testable while
    leaving the qualitative call to the Skill — which is the right
    layer for it (= AI has project context, helper does not).
    """
    op_id = (op or {}).get("id") or "(unknown-op)"
    title = (op or {}).get("title") or ""
    status = (op or {}).get("status") or ""
    hint = ((op or {}).get("activation_hint") or "").strip()
    open_tasks, total_tasks = _count_open_operation_tasks(op)

    if open_tasks > 0:
        verdict = VERDICT_PREP_FIRST
        reason = f"OperationTasks {open_tasks}/{total_tasks} 件未完了 — 先に消化する"
    elif not hint:
        verdict = VERDICT_UNPARSEABLE
        reason = "activation_hint 未設定 — /beacon-operation-setup でヒントを書き起こしてください"
    else:
        verdict = VERDICT_NEEDS_AI_JUDGEMENT
        reason = "structural 前提は満たされている (= hint あり / tasks 完了) — AI が hint 条件を文脈評価する"

    return {
        "op_id": op_id,
        "title": title,
        "status": status,
        "verdict": verdict,
        "reason": reason,
        "activation_hint": hint,
        "open_tasks": open_tasks,
        "total_tasks": total_tasks,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _is_pending_op(op: dict) -> bool:
    """Return True if the row is an Operation in ``todo`` / ``in_progress``.

    Defense-in-depth filter: even if a caller hands the helper a raw
    ``data["operations"]`` list (= mixed open / pending / closed), the
    formatter still renders only the pending subset.
    """
    return (op or {}).get("status") in ("todo", "in_progress")


def filter_pending_only(ops: Iterable[dict]) -> list[dict]:
    """Return only the Operation rows whose status is todo / in_progress.

    Exposed so the CLI / Skill can call it without re-implementing the
    membership check.
    """
    return [op for op in (ops or []) if _is_pending_op(op)]


def _format_one_bullet(classification: dict) -> str:
    """Render one classification result as a single bullet line.

    Format: ``  - [op-id] "<title>" [<status>] verdict=<verdict>: <reason>``

    The bracketed op-id + quoted title pattern matches the rest of
    Step 3 output (= MS bullets, run record bullets) so the section
    visually fits into the same column. The verdict is shown literally
    so a human scanning the output can grep for one of the four
    documented verdict strings.
    """
    op_id = classification.get("op_id") or "(unknown-op)"
    title = classification.get("title") or ""
    status = classification.get("status") or ""
    verdict = classification.get("verdict") or ""
    reason = classification.get("reason") or ""
    return f"  - [{op_id}] \"{title}\" [{status}] verdict={verdict}: {reason}"


def format_pending_activation_section(ops: Iterable[dict] | None,
                                      *, detail_hint: bool = True) -> str:
    """Format pending Operations into a Step 3.7 banner section.

    Returns an empty string when ``ops`` is empty / None / contains no
    pending Operations — so the Skill can ``if section: emit(section)``
    and drop the section entirely without a noisy "0 件" header. This
    matches the contract of ``dm_pending.format_pending_dm_summary``
    (= same architectural pattern, same caller idiom).

    When at least one pending Operation exists, returns a multi-line
    block:

        Pending Operation 活性化議論: N 件
          - [op-X] "..." [todo] verdict=prep-first: OperationTasks 2/3 件未完了 — ...
          - [op-Y] "..." [in_progress] verdict=needs-ai-judgement: ...
          ※ 詳細は `beacon operation show <op-id>` で展開できます。...

    Each bullet's classification comes from
    ``classify_pending_operation``. The Skill renders this block as-is
    and then layers its own AI judgement on top of each bullet (= the
    Step 3.7 markdown's response patterns map 1:1 to each verdict).

    The ``detail_hint`` flag controls whether the trailing hint line
    is appended. Tests pin both modes so a future tweak (= drop the
    hint for non-verbose output) does not silently change the contract.
    """
    pending = filter_pending_only(ops or [])
    if not pending:
        return ""
    n = len(pending)
    lines = [f"{PENDING_OP_HEADER} {n} 件"]
    for op in pending:
        lines.append(_format_one_bullet(classify_pending_operation(op)))
    if detail_hint:
        lines.append(PENDING_OP_DETAIL_HINT)
    return "\n".join(lines)
