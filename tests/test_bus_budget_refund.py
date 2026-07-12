"""Budget refund on held / failed send (ms-100 e-2999).

The budget gate is a *pessimistic* decrement: it commits the slot before the
send so the gate can't be raced. If the send is then held (qual gate) or fails
(network / server), that slot was never used — it must be refunded so a failed
send doesn't silently burn an autonomous-reply turn. This pins the refund
primitive on both the CLI (Python) and MCP (Node) sides, and that a
consume+refund round-trip nets to zero.
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

BUS_BUDGET_MJS = REPO_ROOT / "channel" / "bus-budget.mjs"


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir()
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_budget(root: Path, *, total: int, used: int) -> None:
    (root / ".beacon" / "bus-budget.json").write_text(
        json.dumps({"total": total, "used": used,
                    "granted_at": "2026-07-12T00:00:00Z", "channels": []}))


def _read_used(root: Path) -> int:
    return json.loads(
        (root / ".beacon" / "bus-budget.json").read_text())["used"]


# --- CLI (Python) side -----------------------------------------------------

def test_cli_refund_gives_back_one_slot(project_dir):
    _write_budget(project_dir, total=3, used=2)
    assert commands._bus_budget_refund_one() is True
    assert _read_used(project_dir) == 1


def test_cli_consume_then_refund_nets_zero(project_dir):
    _write_budget(project_dir, total=3, used=0)
    allowed, _ = commands._bus_budget_consume_one()
    assert allowed is True and _read_used(project_dir) == 1
    assert commands._bus_budget_refund_one() is True
    assert _read_used(project_dir) == 0, "a held/failed send must not cost a turn"


def test_cli_refund_clamps_at_zero(project_dir):
    _write_budget(project_dir, total=3, used=0)
    assert commands._bus_budget_refund_one() is False
    assert _read_used(project_dir) == 0, "refund without a prior consume is a no-op"


def test_cli_refund_noop_when_not_armed(project_dir):
    _write_budget(project_dir, total=0, used=0)
    assert commands._bus_budget_refund_one() is False


def test_cli_refund_noop_when_no_budget_file(project_dir):
    # no budget file at all
    assert commands._bus_budget_refund_one() is False


# --- MCP (Node) side -------------------------------------------------------

def _have_node() -> bool:
    return shutil.which("node") is not None


def _mcp(project_root: Path, fn: str) -> dict:
    script = textwrap.dedent(f"""
        import {{ consumeBusBudgetOne, refundBusBudgetOne }} from '{BUS_BUDGET_MJS.as_posix()}'
        const fn = process.argv[2] === 'consume' ? consumeBusBudgetOne : refundBusBudgetOne
        process.stdout.write(JSON.stringify(fn(process.argv[1])))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", str(project_root), fn],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr!r}")
    return json.loads(proc.stdout)


@pytest.mark.skipif(not _have_node(), reason="node not available")
def test_mcp_consume_then_refund_nets_zero(project_dir):
    _write_budget(project_dir, total=3, used=0)
    consumed = _mcp(project_dir, "consume")
    assert consumed["allowed"] is True and _read_used(project_dir) == 1
    refunded = _mcp(project_dir, "refund")
    assert refunded["refunded"] is True and _read_used(project_dir) == 0


@pytest.mark.skipif(not _have_node(), reason="node not available")
def test_mcp_refund_clamps_at_zero(project_dir):
    _write_budget(project_dir, total=3, used=0)
    refunded = _mcp(project_dir, "refund")
    assert refunded["refunded"] is False
    assert _read_used(project_dir) == 0
