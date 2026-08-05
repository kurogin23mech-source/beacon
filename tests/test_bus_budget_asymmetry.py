"""Contract test: the CLI vs MCP budget gate asymmetry is INTENTIONAL (ms-100 e-3310).

The autonomous-reply budget gate has two implementations that read/write the
same ``.beacon/bus-budget.json``:

  * CLI  — ``lib/cmd_bus._bus_budget_consume_one`` (a human typing ``beacon``)
  * MCP  — ``channel/bus-budget.mjs consumeBusBudgetOne`` (the AI's reply tool)

They deliberately DIVERGE on the *missing-file* case and AGREE on every armed
case. The divergence is the whole point of armed mode (ms-100 SPEC
``PVaNf6HYFjucgBS3lkQF``, "armed の本質"):

  * missing file → **CLI allows** (a human's manual one-off DM should not
    require ``grant`` first) but **MCP refuses** (the AI's autonomous replies
    are default-OFF; sending requires an explicit budget grant).

Because the two live in different languages, nothing structurally stops a
future edit from silently aligning them — e.g. "simplifying" the CLI to also
refuse on missing (which would break every manual ``beacon bus send``), or the
MCP to allow on missing (which would let an un-granted AI send freely, the
exact safety the gate exists to prevent). This test pins BOTH behaviors in one
place so such a drift fails loudly, and documents that the difference is by
design, not an oversight.

The armed cases (total<=0 → allow, exhausted → refuse) MUST stay in lockstep;
this test asserts that too.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))
os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402
import cmd_bus  # noqa: E402  (ms-127 e-4803: bus handlers live here)

BUS_BUDGET_MJS = REPO_ROOT / "channel" / "bus-budget.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(), reason="node not available — MCP gate is Node-only"
)


def _mcp_consume(project_root: Path) -> dict:
    """Drive the Node MCP gate (consumeBusBudgetOne) against a project root."""
    script = textwrap.dedent(f"""
        import {{ consumeBusBudgetOne }} from '{BUS_BUDGET_MJS.as_posix()}'
        const out = consumeBusBudgetOne(process.argv[1])
        process.stdout.write(JSON.stringify(out))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", str(project_root)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node probe failed: {proc.stderr!r}")
    return json.loads(proc.stdout)


def _cli_consume(project_root: Path, monkeypatch) -> tuple[bool, dict]:
    """Drive the Python CLI gate (_bus_budget_consume_one) against a project."""
    beacon_dir = project_root / ".beacon"
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    monkeypatch.chdir(project_root)
    return cmd_bus._bus_budget_consume_one()


@pytest.fixture
def project_root(tmp_path):
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir()
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []})
    )
    return tmp_path


def _write_budget(root: Path, *, total: int, used: int = 0) -> None:
    (root / ".beacon" / "bus-budget.json").write_text(
        json.dumps({"total": total, "used": used,
                    "granted_at": "2026-07-12T00:00:00Z", "channels": []}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The intentional divergence: missing budget file
# ---------------------------------------------------------------------------

def test_missing_file_cli_allows_but_mcp_refuses(project_root, monkeypatch):
    # No budget file exists.
    cli_allowed, _ = _cli_consume(project_root, monkeypatch)
    mcp = _mcp_consume(project_root)

    assert cli_allowed is True, (
        "CLI (human) must ALLOW a manual send when no budget is granted — "
        "requiring `grant` for a hand-typed DM would break normal use."
    )
    assert mcp["allowed"] is False and mcp["reason"] == "not_granted", (
        "MCP (AI) must REFUSE when no budget is granted — autonomous replies "
        "are default-OFF. Aligning it to the CLI would remove armed mode's "
        "core safety."
    )
    # State the contract explicitly so the failure message names the intent.
    assert cli_allowed != mcp["allowed"], (
        "the missing-file asymmetry (CLI allow / MCP refuse) is INTENTIONAL "
        "(ms-100 e-3310 / SPEC PVaNf6HYFjucgBS3lkQF) — do not unify"
    )


# ---------------------------------------------------------------------------
# The armed cases: MUST stay in lockstep
# ---------------------------------------------------------------------------

def test_exhausted_both_refuse(project_root, monkeypatch):
    _write_budget(project_root, total=1, used=1)
    cli_allowed, _ = _cli_consume(project_root, monkeypatch)
    mcp = _mcp_consume(project_root)
    assert cli_allowed is False
    assert mcp["allowed"] is False and mcp["reason"] == "exhausted"


def test_zero_total_both_allow_without_decrement(project_root, monkeypatch):
    _write_budget(project_root, total=0, used=0)
    cli_allowed, cli_state = _cli_consume(project_root, monkeypatch)
    mcp = _mcp_consume(project_root)
    assert cli_allowed is True and cli_state["armed"] is False
    assert mcp["allowed"] is True and mcp["budget"]["armed"] is False


def test_armed_with_room_both_allow_and_decrement(project_root, monkeypatch):
    # CLI consumes one slot (0->1); then the MCP consumes the next (1->2).
    _write_budget(project_root, total=3, used=0)
    cli_allowed, _ = _cli_consume(project_root, monkeypatch)
    assert cli_allowed is True
    after_cli = json.loads(
        (project_root / ".beacon" / "bus-budget.json").read_text())
    assert after_cli["used"] == 1

    mcp = _mcp_consume(project_root)
    assert mcp["allowed"] is True
    after_mcp = json.loads(
        (project_root / ".beacon" / "bus-budget.json").read_text())
    assert after_mcp["used"] == 2, "both gates persist the same file/counter"
