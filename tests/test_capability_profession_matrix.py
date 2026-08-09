"""e-5012: the Capability × profession coverage matrix, CI-enforced.

Runs every GREEN abstraction-consuming capability against dev, sales, AND the
synthetic profession, and asserts each surfaces that profession's Target and work
item. A NEW shared capability added to ``GREEN_PROBES`` that does not light up the
synthetic profession fails here — the positive forcing function that keeps
"declare a manifest ⇒ every shared capability lights up" true (ms-142 の芯).
"""
from __future__ import annotations

import sys
from pathlib import Path

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402
from capability_profession_matrix import (  # noqa: E402
    PROFESSIONS, GREEN_PROBES, EXPECTED_RED)


def _cases():
    for prof, spec in PROFESSIONS.items():
        for cap, probe in GREEN_PROBES.items():
            yield prof, cap, probe, spec


@pytest.mark.parametrize(
    "prof,cap,probe,spec",
    [pytest.param(p, c, pr, s, id=f"{c}:{p}") for p, c, pr, s in _cases()],
)
def test_green_cell_lights_up(prof, cap, probe, spec):
    project = spec["project"]()
    ok = probe(project, spec["target_id"], spec["work_item_id"])
    assert ok, (
        f"GREEN cell [{cap} × {prof}] did not light up: the capability failed to "
        f"surface target={spec['target_id']} / work_item={spec['work_item_id']}. "
        f"Either the capability reaches a concrete collection / hardcodes an arm "
        f"name (make it consume the occupation abstraction), or it belongs in "
        f"EXPECTED_RED with an owning MS.")


def test_matrix_covers_the_synthetic_profession():
    # The whole point: the synthetic profession (arms unlike dev/sales) is one of
    # the columns, so every GREEN capability is proven declaration-driven, not
    # dev-shaped.
    assert "compliance" in PROFESSIONS
    assert GREEN_PROBES, "the matrix must probe at least one shared capability"


def test_expected_red_entries_carry_owning_ms():
    # Every RED cell must name an owning MS (the SPEC's 'debt visible, owner
    # assigned' contract). Empty until debts are catalogued (e-5013).
    for cap, meta in EXPECTED_RED.items():
        assert meta.get("owning_ms"), f"RED cell {cap} has no owning MS"
