"""Tests for the per-trek rate brake (channel/trek-rate.mjs).

e-4116 follow-up (PR #491 parent review 2): Trek-internal DMs bypass the budget
+ qual gates, so a per-trek rolling rate cap is the remaining brake on a
leader↔executor autonomous loop. These pin the window math (decideRate) and the
IO round-trip (checkAndRecordTrekReply), driven through a Node probe so any
breakage in the module surfaces here.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TREK_RATE_MJS = REPO_ROOT / "channel" / "trek-rate.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(), reason="node not available — trek rate brake is Node-only")


def _probe(src_body: str, *args: str) -> dict:
    script = textwrap.dedent(f"""
        import {{ decideRate, checkAndRecordTrekReply, DEFAULT_CAP, DEFAULT_WINDOW_SEC }} from '{TREK_RATE_MJS.as_posix()}'
        const argv = process.argv.slice(1)
        {src_body}
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", *args],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"probe failed: {proc.stderr!r}")
    return json.loads(proc.stdout)


# --- decideRate (pure window math) -----------------------------------------

def test_decide_allows_under_cap_and_appends():
    out = _probe(
        "process.stdout.write(JSON.stringify(decideRate([100, 200], 1000, 5, 600)))")
    assert out["allowed"] is True
    assert out["count"] == 3
    assert out["recent"][-1] == 1000  # now appended


def test_decide_holds_at_cap():
    # 3 timestamps within window, cap 3 → next is held.
    out = _probe(
        "process.stdout.write(JSON.stringify(decideRate([100, 200, 300], 1000, 3, 600)))")
    assert out["allowed"] is False
    assert out["count"] == 3
    assert 1000 not in out["recent"]  # not appended when held


def test_decide_prunes_outside_window():
    # windowSec=1 → cutoff = now(10_000ms) - 1000ms = 9000. Only ts>9000 kept.
    out = _probe(
        "process.stdout.write(JSON.stringify(decideRate([1000, 9500], 10000, 5, 1)))")
    # 1000 is pruned (old), 9500 kept, now(10000) appended → 2.
    assert out["allowed"] is True
    assert out["recent"] == [9500, 10000]


# --- checkAndRecordTrekReply (IO round-trip) -------------------------------

def test_check_and_record_holds_after_cap(tmp_path):
    (tmp_path / ".beacon").mkdir()
    root = tmp_path.as_posix()
    # cap=3, window huge. 3 allowed, 4th held. Fixed nowMs so all in-window.
    body = textwrap.dedent("""
        const root = argv[0]
        const results = []
        for (let i = 0; i < 5; i++) {
          results.push(checkAndRecordTrekReply(root, 'tk-1', {cap: 3, windowSec: 3600, nowMs: 1000000 + i}))
        }
        process.stdout.write(JSON.stringify(results))
    """)
    out = _probe(body, root)
    allowed = [r["allowed"] for r in out]
    assert allowed == [True, True, True, False, False]
    assert out[-1]["cap"] == 3


def test_check_and_record_is_per_trek(tmp_path):
    (tmp_path / ".beacon").mkdir()
    root = tmp_path.as_posix()
    # Two treks, cap=1 each: each gets its own allowance (no cross-trek bleed).
    body = textwrap.dedent("""
        const root = argv[0]
        const out = {
          a1: checkAndRecordTrekReply(root, 'tk-a', {cap: 1, windowSec: 3600, nowMs: 1}),
          b1: checkAndRecordTrekReply(root, 'tk-b', {cap: 1, windowSec: 3600, nowMs: 2}),
          a2: checkAndRecordTrekReply(root, 'tk-a', {cap: 1, windowSec: 3600, nowMs: 3}),
        }
        process.stdout.write(JSON.stringify(out))
    """)
    out = _probe(body, root)
    assert out["a1"]["allowed"] is True
    assert out["b1"]["allowed"] is True    # different trek, own budget
    assert out["a2"]["allowed"] is False   # tk-a exhausted


def test_empty_trek_id_always_allows(tmp_path):
    (tmp_path / ".beacon").mkdir()
    out = _probe(
        f"process.stdout.write(JSON.stringify(checkAndRecordTrekReply('{tmp_path.as_posix()}', '', {{cap: 1}})))")
    assert out["allowed"] is True
