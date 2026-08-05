"""Tests for the MCP reply tool's bus budget gate (ms-54 / e-1193).

The CLI's `beacon bus send --in-reply-to` has been gated by
.beacon/bus-budget.json since e-1000. The MCP reply tool (channel/bus.mjs)
shared the file but had no gate — every MCP reply went through, even when
the budget was 0 or never granted. This was an asymmetry that made the
budget gate's "structurally prevent runaway autonomous loops" promise hollow
in armed mode.

These tests pin down the symmetry:

  * No budget file        → MCP reply refused with `not_granted`
  * Budget present, room  → MCP reply allowed, used incremented; the (N+1)-th
                            attempt refused with `exhausted`.
  * Corrupted budget file → fail-closed, refused with `corrupt`.
  * total <= 0 sentinel   → allowed without decrement (matches CLI's
                            _bus_budget_consume_one — "file present, not armed").

The tests drive consumeBusBudgetOne() from bus-budget.mjs directly via a
small Node probe script. Going through the full MCP server would require an
MCP client harness; the gate logic is the load-bearing piece, not the JSON-RPC
plumbing on top of it. The handler in bus.mjs just calls this function — its
behavior is fully determined by these tests + refuseMessage's output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUS_BUDGET_MJS = REPO_ROOT / "channel" / "bus-budget.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(),
    reason="node not available — MCP reply gate is Node-only",
)


def _run_probe(project_root: Path, action: str) -> dict:
    """Drive consumeBusBudgetOne (or readBusBudget) against project_root.

    The probe imports the helper module from its real path so any breakage in
    the module surfaces as a probe failure (no test-private duplicate).
    """
    script = textwrap.dedent(f"""
        import {{ consumeBusBudgetOne, readBusBudget, refuseMessage, isArmed }} from '{BUS_BUDGET_MJS.as_posix()}'
        // With `node --input-type=module -e <src> -- ARG1 ARG2`:
        //   argv[0] = node, argv[1] = ARG1, argv[2] = ARG2
        // (the -e content is not a positional script file).
        const root = process.argv[1]
        const action = process.argv[2]
        let out
        if (action === 'consume') {{
            out = consumeBusBudgetOne(root)
        }} else if (action === 'read') {{
            out = readBusBudget(root)
        }} else if (action === 'armed') {{
            out = {{ armed: isArmed(root) }}
        }} else if (action === 'refuse_not_granted') {{
            out = {{ message: refuseMessage('not_granted', null) }}
        }} else if (action === 'refuse_exhausted') {{
            out = {{ message: refuseMessage('exhausted', {{ total: 3, used: 3 }}) }}
        }} else if (action === 'refuse_corrupt') {{
            out = {{ message: refuseMessage('corrupt', null) }}
        }} else {{
            out = {{ error: 'unknown action' }}
        }}
        process.stdout.write(JSON.stringify(out))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script,
         "--", str(project_root), action],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"node probe failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout)


@pytest.fixture
def project_root(tmp_path):
    """Tiny .beacon dir so the probe has a real budget file location."""
    (tmp_path / ".beacon").mkdir()
    return tmp_path


def _write_budget(root: Path, *, total: int, used: int = 0) -> None:
    (root / ".beacon" / "bus-budget.json").write_text(
        json.dumps({
            "total": total, "used": used,
            "granted_at": "2026-06-07T00:00:00.000000Z",
            "channels": [],
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Refusal paths (the default-OFF posture)
# ---------------------------------------------------------------------------

def test_mcp_reply_refused_when_no_budget_file(project_root):
    """The CLI's default is "manual one-off sends are fine, autonomous
    replies need an explicit grant". The MCP tool must enforce the same
    default — a missing file means "no autonomous replies allowed"."""
    result = _run_probe(project_root, "consume")
    assert result == {"allowed": False, "reason": "not_granted"}


def test_mcp_reply_refused_when_budget_exhausted(project_root):
    """The (N+1)-th MCP reply attempt after N grants must refuse. Same
    cap shape as the CLI."""
    _write_budget(project_root, total=2, used=0)
    # Burn N
    for _ in range(2):
        r = _run_probe(project_root, "consume")
        assert r["allowed"] is True
    # The (N+1)-th must refuse with exhausted.
    r = _run_probe(project_root, "consume")
    assert r["allowed"] is False
    assert r["reason"] == "exhausted"
    assert r["budget"]["used"] == 2
    assert r["budget"]["total"] == 2


def test_mcp_reply_refused_when_budget_file_corrupt(project_root):
    """Fail-closed on a half-written or hand-mangled file. Otherwise a
    crashed write could lock the gate in a half-state where decrement
    fails silently and replies all go through. Refuse, don't guess."""
    (project_root / ".beacon" / "bus-budget.json").write_text(
        "{this is not json", encoding="utf-8")
    result = _run_probe(project_root, "consume")
    assert result["allowed"] is False
    assert result["reason"] == "corrupt"


def test_mcp_reply_refused_when_budget_file_is_array(project_root):
    """Defensive shape check — the schema is "object with total/used", a
    JSON array would crash a naive index access. Treat as corrupt."""
    (project_root / ".beacon" / "bus-budget.json").write_text(
        "[1,2,3]", encoding="utf-8")
    result = _run_probe(project_root, "consume")
    assert result["allowed"] is False
    assert result["reason"] == "corrupt"


# ---------------------------------------------------------------------------
# Happy path + persistence
# ---------------------------------------------------------------------------

def test_mcp_reply_allowed_when_budget_has_room(project_root):
    _write_budget(project_root, total=3, used=0)
    result = _run_probe(project_root, "consume")
    assert result["allowed"] is True
    assert result["budget"] == {"total": 3, "used": 1, "armed": True}


def test_mcp_reply_decrement_persists_to_disk(project_root):
    """The CLI's pessimistic semantics (decrement-before-send) must hold
    on the MCP side too. After a single allowed consume the used count
    in the file is incremented, independent of any subsequent action."""
    _write_budget(project_root, total=5, used=2)
    _run_probe(project_root, "consume")
    data = json.loads(
        (project_root / ".beacon" / "bus-budget.json").read_text())
    assert data["used"] == 3
    assert data["total"] == 5


def test_mcp_reply_n_grants_n_sends(project_root):
    """End-to-end shape: grant N, send N times, all allowed; the next is
    refused with exhausted. This is the test the AC mentions."""
    _write_budget(project_root, total=4, used=0)
    for expected_used in (1, 2, 3, 4):
        r = _run_probe(project_root, "consume")
        assert r["allowed"] is True, f"reply #{expected_used} should be allowed"
        assert r["budget"]["used"] == expected_used
    r = _run_probe(project_root, "consume")
    assert r["allowed"] is False
    assert r["reason"] == "exhausted"


# ---------------------------------------------------------------------------
# Sentinel: total<=0 means "file present but not armed" — mirrors CLI.
# ---------------------------------------------------------------------------

def test_mcp_reply_allows_when_total_is_zero_sentinel(project_root):
    """Matches lib/cmd_bus._bus_budget_consume_one's branch for "file
    present, total<=0" → allow without decrement, armed=False. Without
    this the two code paths diverge on this corner."""
    _write_budget(project_root, total=0, used=0)
    result = _run_probe(project_root, "consume")
    assert result["allowed"] is True
    assert result["budget"]["armed"] is False
    # Used must NOT have been touched.
    data = json.loads(
        (project_root / ".beacon" / "bus-budget.json").read_text())
    assert data["used"] == 0


# ---------------------------------------------------------------------------
# refuseMessage — consistency with the CLI's error text
# ---------------------------------------------------------------------------

def test_refuse_message_not_granted_points_at_grant_command(project_root):
    r = _run_probe(project_root, "refuse_not_granted")
    assert "bus budget grant" in r["message"]
    assert "not granted" in r["message"]


def test_refuse_message_exhausted_mentions_used_over_total(project_root):
    r = _run_probe(project_root, "refuse_exhausted")
    assert "3/3" in r["message"]
    assert "exhausted" in r["message"]
    assert "bus budget grant" in r["message"]


def test_refuse_message_corrupt_points_at_inspection(project_root):
    r = _run_probe(project_root, "refuse_corrupt")
    assert "bus-budget.json" in r["message"]
    assert "bus budget grant" in r["message"]


# ---------------------------------------------------------------------------
# readBusBudget classification
# ---------------------------------------------------------------------------

def test_read_classifies_missing(project_root):
    r = _run_probe(project_root, "read")
    assert r == {"state": "missing"}


def test_read_classifies_present(project_root):
    _write_budget(project_root, total=1, used=0)
    r = _run_probe(project_root, "read")
    assert r["state"] == "present"
    assert r["data"]["total"] == 1


def test_read_classifies_corrupt(project_root):
    (project_root / ".beacon" / "bus-budget.json").write_text(
        "not json at all", encoding="utf-8")
    r = _run_probe(project_root, "read")
    assert r == {"state": "corrupt"}


# ---------------------------------------------------------------------------
# isArmed — e-3901 MCP-path armed detection (PR #475 review regression)
# ---------------------------------------------------------------------------
# The reply tool (channel/bus.mjs) gates on `isArmed(CWD) || in_reply_to`. An
# earlier inline read used readBusBudget().total (undefined — the dict is under
# .data), so armed was ALWAYS false and an armed loop that omitted --in-reply-to
# slid past both the budget cap and the cross-user HOLD on the MCP path. These
# pin that isArmed unwraps { state, data } correctly so the gate actually fires.

def test_is_armed_true_when_budget_present(project_root):
    _write_budget(project_root, total=3)
    assert _run_probe(project_root, "armed") == {"armed": True}


def test_is_armed_false_when_no_budget_file(project_root):
    # missing file → not armed (the omit-the-flag path stays ungated only when
    # the human never armed the session, which is the intended manual default).
    assert _run_probe(project_root, "armed") == {"armed": False}


def test_is_armed_false_when_total_zero(project_root):
    _write_budget(project_root, total=0)
    assert _run_probe(project_root, "armed") == {"armed": False}


def test_is_armed_false_when_corrupt(project_root):
    (project_root / ".beacon" / "bus-budget.json").write_text(
        "not json", encoding="utf-8")
    assert _run_probe(project_root, "armed") == {"armed": False}
