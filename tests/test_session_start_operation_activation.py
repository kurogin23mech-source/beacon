"""Session-start Step 3.7 Operation activation discussion pin (ms-61 / e-1843).

The ``/beacon-session-start`` Skill (= markdown) has a Step 3.7 that
proposes whether each pending Operation (= ``status="todo"`` or
``status="in_progress"``) is ready to be activated. Before e-1843 the
Skill said "Step 1a の結果から status==todo / in_progress を取得する",
but ``beacon status --json`` actually filtered out everything but
``status="open"`` — so Step 3.7 silently saw an empty list and was dead
code.

e-1843 closes the gap in three places:

  (a) ``beacon status --json`` now emits ``pending_operations[]``
      alongside the legacy ``operations[]`` (= open-only) field.
  (b) ``lib/operation_activation`` classifies each pending row into
      one of four verdicts the Skill can render verbatim.
  (c) ``/beacon-session-start`` Step 3.7 markdown is updated to read
      ``pending_operations[]`` and call the classifier.

These tests pin the structural contract of (a) + (b) across the three
scenarios required by e-1843's acceptance criteria:

  (S1) pending Operation 0 件 / open N 件 → ``pending_operations`` is
       empty; the formatter returns ``""`` so the Skill omits the
       section entirely (no noise).
  (S2) pending Operation N 件 / open 0 件 → every pending row appears
       in ``pending_operations``; the formatter renders one bullet
       per row with a verdict mapped to the row's structural state.
  (S3) mixed (some pending + some open + some closed) → only pending
       rows appear in ``pending_operations``; the legacy
       ``operations[]`` keeps carrying open rows only (= no
       regression for existing readers).

Edge-case fallbacks (= e-1843 AC #3) are pinned alongside the three
scenarios so the verdict mapping stays correct when:

  (E1) ``activation_hint`` is missing / empty → verdict
       ``"unparseable"``.
  (E2) OperationTasks remain open → verdict ``"prep-first"`` even if
       the hint is present (= prep gate takes precedence).
  (E3) ``activation_hint`` is present and all tasks done → verdict
       ``"needs-ai-judgement"`` (= structural prerequisites met, AI
       decides the qualitative call).

The Skill itself is markdown and therefore unrenderable / untestable
on its own — pinning the helper + CLI surface here is the closest we
can get to an end-to-end test of Step 3.7 without spinning up a
Claude Code subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

# Make lib/ importable without packaging gymnastics — same pattern as
# tests/test_session_start_pending_dm.py uses for the DM helper.
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "lib")
)

import operation_activation as oa  # noqa: E402


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BEACON_BIN = os.path.join(REPO_ROOT, "bin", "beacon")


# ---------------------------------------------------------------------------
# Module-level constants pin
# ---------------------------------------------------------------------------

def test_verdict_vocabulary_is_closed():
    """Verdict literals must match the documented vocabulary.

    The Skill markdown branches on these literals. If a new verdict is
    added, the Skill markdown also has to learn about it — pinning the
    membership here means a silent rename / new verdict fails loudly.
    """
    assert set(oa.VALID_VERDICTS) == {
        "activate-now",
        "needs-ai-judgement",
        "prep-first",
        "unparseable",
    }
    # Each constant equals the literal it documents.
    assert oa.VERDICT_ACTIVATE_NOW == "activate-now"
    assert oa.VERDICT_NEEDS_AI_JUDGEMENT == "needs-ai-judgement"
    assert oa.VERDICT_PREP_FIRST == "prep-first"
    assert oa.VERDICT_UNPARSEABLE == "unparseable"


def test_header_and_hint_literals_are_stable():
    """Skill markdown reads these strings verbatim — pin to detect drift."""
    assert oa.PENDING_OP_HEADER == "Pending Operation 活性化議論:"
    assert "beacon operation show" in oa.PENDING_OP_DETAIL_HINT
    assert "beacon operation set-status" in oa.PENDING_OP_DETAIL_HINT
    assert "/beacon-operation-setup" in oa.PENDING_OP_DETAIL_HINT


# ---------------------------------------------------------------------------
# classify_pending_operation — edge cases (E1 / E2 / E3)
# ---------------------------------------------------------------------------

def test_classify_unparseable_when_no_activation_hint():
    """E1: missing / empty activation_hint → ``unparseable`` verdict."""
    op = {
        "id": "op-1",
        "title": "Service health monitoring",
        "status": "todo",
        "entries": [],
    }
    r = oa.classify_pending_operation(op)
    assert r["verdict"] == oa.VERDICT_UNPARSEABLE
    assert r["activation_hint"] == ""
    assert "activation_hint 未設定" in r["reason"]
    assert "/beacon-operation-setup" in r["reason"]


def test_classify_unparseable_when_hint_is_whitespace_only():
    """E1 variant: hint string is non-empty but whitespace → still unparseable."""
    op = {
        "id": "op-1",
        "title": "",
        "status": "todo",
        "activation_hint": "   \n  ",
        "entries": [],
    }
    r = oa.classify_pending_operation(op)
    assert r["verdict"] == oa.VERDICT_UNPARSEABLE


def test_classify_prep_first_when_operation_tasks_remain():
    """E2: OperationTasks not all done → ``prep-first`` (gate over hint)."""
    op = {
        "id": "op-2",
        "title": "Cost watch",
        "status": "in_progress",
        "activation_hint": "Cloud Run 本番稼働後に有効化",
        "entries": [
            {"type": "operation_task", "id": "e-100", "status": "done"},
            {"type": "operation_task", "id": "e-101", "status": "todo"},
            {"type": "operation_task", "id": "e-102", "status": "todo"},
        ],
    }
    r = oa.classify_pending_operation(op)
    assert r["verdict"] == oa.VERDICT_PREP_FIRST
    assert r["open_tasks"] == 2
    assert r["total_tasks"] == 3
    assert "2/3" in r["reason"]


def test_classify_prep_gate_beats_hint_even_when_hint_present():
    """E2 + hint: prep gate takes precedence over the AI judgement path."""
    op = {
        "id": "op-2",
        "title": "Cost watch",
        "status": "todo",
        "activation_hint": "Always activate",
        "entries": [
            {"type": "operation_task", "id": "e-100", "status": "todo"},
        ],
    }
    r = oa.classify_pending_operation(op)
    # Hint is present and looks satisfied to a casual reader, but the
    # prep gate still has to clear first. This is the safe default.
    assert r["verdict"] == oa.VERDICT_PREP_FIRST


def test_classify_needs_ai_judgement_when_hint_present_and_tasks_done():
    """E3: structural prerequisites met → defer to AI for the qualitative call."""
    op = {
        "id": "op-3",
        "title": "Service health monitoring",
        "status": "todo",
        "activation_hint": "Cloud Run 本番稼働後に有効化",
        "entries": [
            {"type": "operation_task", "id": "e-200", "status": "done"},
            {"type": "operation_task", "id": "e-201", "status": "done"},
        ],
    }
    r = oa.classify_pending_operation(op)
    assert r["verdict"] == oa.VERDICT_NEEDS_AI_JUDGEMENT
    assert r["open_tasks"] == 0
    assert r["total_tasks"] == 2
    assert r["activation_hint"] == "Cloud Run 本番稼働後に有効化"


def test_classify_needs_ai_judgement_when_no_tasks_at_all():
    """No OperationTasks + hint present → still needs-ai-judgement.

    Zero tasks means the prep gate is trivially satisfied (= nothing
    to wait on); the hint becomes the only signal, which is the AI's
    domain.
    """
    op = {
        "id": "op-4",
        "title": "Weekly retro",
        "status": "todo",
        "activation_hint": "毎週金曜",
        "entries": [],
    }
    r = oa.classify_pending_operation(op)
    assert r["verdict"] == oa.VERDICT_NEEDS_AI_JUDGEMENT
    assert r["open_tasks"] == 0
    assert r["total_tasks"] == 0


def test_classify_returns_safe_defaults_on_empty_input():
    """``classify_pending_operation({})`` does not raise — verdict is unparseable."""
    r = oa.classify_pending_operation({})
    assert r["verdict"] == oa.VERDICT_UNPARSEABLE
    assert r["op_id"] == "(unknown-op)"
    assert r["title"] == ""


# ---------------------------------------------------------------------------
# Filter / formatter — scenario S1 (empty)
# ---------------------------------------------------------------------------

def test_format_returns_empty_string_when_no_pending():
    """S1: no pending Operations → empty string (Skill omits the section).

    This is the critical contract — under e-1843 ``beacon status
    --json`` always emits ``pending_operations[]``, but the field is
    almost always empty in normal projects. The formatter has to
    collapse silently or every session-start gets noise.
    """
    assert oa.format_pending_activation_section([]) == ""
    assert oa.format_pending_activation_section(None) == ""
    # Only ``open`` / ``closed`` rows present → still empty.
    assert oa.format_pending_activation_section([
        {"id": "op-1", "status": "open"},
        {"id": "op-2", "status": "closed"},
    ]) == ""


def test_filter_pending_only_drops_open_and_closed():
    """Defense-in-depth filter: open / closed rows do not leak through."""
    ops = [
        {"id": "op-1", "status": "open"},
        {"id": "op-2", "status": "todo"},
        {"id": "op-3", "status": "closed"},
        {"id": "op-4", "status": "in_progress"},
        {"id": "op-5", "status": ""},  # malformed row
    ]
    result = oa.filter_pending_only(ops)
    assert [op["id"] for op in result] == ["op-2", "op-4"]


# ---------------------------------------------------------------------------
# Formatter — scenario S2 (N pending)
# ---------------------------------------------------------------------------

def test_format_renders_one_bullet_per_pending_op():
    """S2: N pending Operations → header + N bullets + hint."""
    ops = [
        {
            "id": "op-2",
            "title": "Cost watch",
            "status": "todo",
            "activation_hint": "",  # E1: unparseable
            "entries": [],
        },
        {
            "id": "op-3",
            "title": "Service health",
            "status": "in_progress",
            "activation_hint": "Cloud Run 稼働後",
            "entries": [
                {"type": "operation_task", "status": "done"},
                {"type": "operation_task", "status": "done"},
            ],
        },
    ]
    out = oa.format_pending_activation_section(ops)
    assert out.startswith("Pending Operation 活性化議論: 2 件")
    # One bullet per Operation.
    assert out.count("\n  - [op-") == 2
    # Each verdict literal appears.
    assert "verdict=unparseable" in out
    assert "verdict=needs-ai-judgement" in out
    # Trailing hint line is present by default.
    assert "beacon operation show" in out


def test_format_omits_trailing_hint_when_disabled():
    """``detail_hint=False`` → header + bullets only, no trailing hint."""
    ops = [
        {"id": "op-2", "title": "Cost watch", "status": "todo",
         "activation_hint": "", "entries": []},
    ]
    out = oa.format_pending_activation_section(ops, detail_hint=False)
    assert "Pending Operation 活性化議論: 1 件" in out
    assert "beacon operation show" not in out


def test_format_bullet_includes_op_id_title_and_status():
    """Bullet format: ``  - [op-id] "title" [status] verdict=X: reason``."""
    ops = [
        {
            "id": "op-7",
            "title": "Daily cost watch",
            "status": "in_progress",
            "activation_hint": "本番稼働後",
            "entries": [],
        },
    ]
    out = oa.format_pending_activation_section(ops, detail_hint=False)
    assert "[op-7]" in out
    assert "\"Daily cost watch\"" in out
    assert "[in_progress]" in out


# ---------------------------------------------------------------------------
# Formatter — scenario S3 (mixed)
# ---------------------------------------------------------------------------

def test_format_filters_out_open_and_closed_in_mixed_input():
    """S3: mixed input → only pending rows render; open / closed dropped."""
    ops = [
        {"id": "op-1", "title": "Live monitor", "status": "open",
         "activation_hint": "", "entries": []},
        {"id": "op-2", "title": "Cost watch", "status": "todo",
         "activation_hint": "", "entries": []},
        {"id": "op-3", "title": "Archived", "status": "closed",
         "activation_hint": "", "entries": []},
        {"id": "op-4", "title": "Health check", "status": "in_progress",
         "activation_hint": "Cloud Run 稼働後",
         "entries": [{"type": "operation_task", "status": "done"}]},
    ]
    out = oa.format_pending_activation_section(ops)
    assert "2 件" in out
    assert "[op-2]" in out
    assert "[op-4]" in out
    # open / closed Operations do NOT appear (= Active Operation
    # section covers open; closed Operations should never be in the
    # activation discussion).
    assert "[op-1]" not in out
    assert "[op-3]" not in out


# ---------------------------------------------------------------------------
# CLI-level pin — ``beacon status --json`` exposes pending_operations
# (= the data Step 3.7 reads from Step 1a). Without this field the
# helpers above never get called.
# ---------------------------------------------------------------------------

def _run_beacon(args, *, cwd, env_extra=None):
    env = os.environ.copy()
    # Pin to local mode + non-interactive so the CLI does not try to
    # reach Firebase or prompt for input under pytest.
    env.pop("BEACON_CLOUD", None)
    env["BEACON_OPERATIONS_BACKEND"] = "local"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [BEACON_BIN] + args,
        cwd=cwd, env=env, capture_output=True, text=True,
    )


@pytest.fixture
def beacon_tmp_project(tmp_path):
    """Create a throwaway local-mode Beacon project for CLI-level tests.

    Bypasses the interactive ``beacon init`` flow by writing a minimal
    ``.beacon/project.json`` directly — the same pattern used by
    ``tests/test_log_commit_source.py``. Yields the project root.
    """
    proj = tmp_path / "proj"
    (proj / ".beacon").mkdir(parents=True)
    (proj / ".beacon" / "project.json").write_text(
        json.dumps({
            "name": "test-proj",
            "summary": "",
            "milestones": [],
            "operations": [],
        }),
        encoding="utf-8",
    )
    return proj


def test_cli_status_json_has_pending_operations_field_when_zero(beacon_tmp_project):
    """S1 (CLI): with zero Operations, ``pending_operations`` is ``[]``.

    The field MUST exist even when empty so the Skill can do a
    structural ``len(pending_operations) == 0`` check without a
    defensive ``data.get("pending_operations", [])`` everywhere.
    """
    r = _run_beacon(["status", "--json"], cwd=str(beacon_tmp_project))
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert "pending_operations" in payload, (
        "ms-61 / e-1843: beacon status --json must always emit "
        "pending_operations[] so /beacon-session-start Step 3.7 has "
        "a structural place to read from"
    )
    assert payload["pending_operations"] == []
    # Legacy field still exists and stays open-only.
    assert "operations" in payload
    assert payload["operations"] == []


def test_cli_status_json_surfaces_pending_operation_when_created(beacon_tmp_project):
    """S2 (CLI): ``beacon operation open --status todo`` lands in pending_operations.

    Walks the full path: create a pending Operation via the CLI,
    re-read ``beacon status --json``, verify the row appears with the
    expected shape that the Skill's classifier consumes.
    """
    r = _run_beacon(
        ["operation", "open", "Cost watch",
         "--status", "todo",
         "--activation-hint", "Cloud Run 本番稼働後"],
        cwd=str(beacon_tmp_project),
    )
    assert r.returncode == 0, r.stderr

    r = _run_beacon(["status", "--json"], cwd=str(beacon_tmp_project))
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)

    assert len(payload["pending_operations"]) == 1
    op = payload["pending_operations"][0]
    # Shape the Skill / classifier consumes.
    assert op["status"] == "todo"
    assert op["title"] == "Cost watch"
    assert op["activation_hint"] == "Cloud Run 本番稼働後"
    assert op["operation_tasks_total"] == 0
    assert op["operation_tasks_done"] == 0
    assert "entries" in op  # classifier reads this
    # Legacy open-only field is untouched (= no regression).
    assert payload["operations"] == []


def test_cli_status_json_separates_pending_and_open_in_mixed_state(beacon_tmp_project):
    """S3 (CLI): mixed pending + open Operations split cleanly across fields.

    open Operations only appear in ``operations[]`` (= legacy
    contract). pending Operations only appear in
    ``pending_operations[]`` (= e-1843 contract). Closed Operations
    appear in neither.
    """
    # open Operation
    r = _run_beacon(
        ["operation", "open", "Live monitor"],
        cwd=str(beacon_tmp_project),
    )
    assert r.returncode == 0, r.stderr
    # pending (todo) Operation
    r = _run_beacon(
        ["operation", "open", "Cost watch",
         "--status", "todo",
         "--activation-hint", "本番稼働後"],
        cwd=str(beacon_tmp_project),
    )
    assert r.returncode == 0, r.stderr

    r = _run_beacon(["status", "--json"], cwd=str(beacon_tmp_project))
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)

    # open in operations[] (legacy)
    op_ids_open = [o["id"] for o in payload["operations"]]
    op_ids_pending = [o["id"] for o in payload["pending_operations"]]
    assert len(op_ids_open) == 1
    assert len(op_ids_pending) == 1
    # No overlap — the two fields are disjoint.
    assert set(op_ids_open).isdisjoint(set(op_ids_pending))
    # Pending entry carries the activation_hint round-trip.
    assert payload["pending_operations"][0]["activation_hint"] == "本番稼働後"


def test_cli_status_pending_operations_round_trip_through_helper(beacon_tmp_project):
    """End-to-end pin: CLI payload + helper produces a non-empty section.

    Verifies the full Step 3.7 path:
      beacon status --json  →  payload["pending_operations"]
                            →  format_pending_activation_section(payload)
                            →  banner text the Skill prints
    """
    _run_beacon(
        ["operation", "open", "Cost watch",
         "--status", "todo",
         "--activation-hint", "本番稼働後"],
        cwd=str(beacon_tmp_project),
    )
    r = _run_beacon(["status", "--json"], cwd=str(beacon_tmp_project))
    payload = json.loads(r.stdout)

    section = oa.format_pending_activation_section(payload["pending_operations"])
    assert section != ""
    assert "Pending Operation 活性化議論: 1 件" in section
    assert "Cost watch" in section
    # No OperationTasks at all + hint present → needs-ai-judgement
    # (structural prerequisites trivially met).
    assert "verdict=needs-ai-judgement" in section
