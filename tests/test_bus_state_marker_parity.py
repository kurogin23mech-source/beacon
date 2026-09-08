"""ms-159 review (#735) — lock the JS↔Python parity of the work-unit state
literal the bridge declares on graceful shutdown.

``channel/bus.mjs`` declares ``terminated`` directly when it tears down (the
transport is gone, so the fresh non-terminal marker must be overridden). It is
JavaScript and cannot import ``lib/bus_liveness``, so ``channel/bus-state-marker.mjs``
holds a named copy of the literal (``STATE_TERMINATED``). This test is the
forcing function against drift: if the Python canonical value ever changes and
the JS copy does not (or vice-versa), CI fails loudly. Mirrors
``test_untrusted_frame_all_paths.test_bus_mjs_header_is_byte_identical_to_python``
(ms-169 e-6235), the established JS-copy-of-Python-constant pattern.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import bus_liveness  # noqa: E402


def test_js_state_terminated_matches_python():
    src = (REPO / "channel" / "bus-state-marker.mjs").read_text(encoding="utf-8")
    m = re.search(r"export const STATE_TERMINATED = '([^']*)'", src)
    assert m, "bus-state-marker.mjs lost its STATE_TERMINATED constant"
    js_value = m.group(1)
    assert js_value == bus_liveness.STATE_TERMINATED, (
        "bus-state-marker.mjs STATE_TERMINATED drifted from "
        "bus_liveness.STATE_TERMINATED — edit both together")


def test_bus_mjs_uses_the_shared_constant_not_a_bare_string():
    """The bridge's shutdown path must reference the named constant, not re-inline
    a bare 'terminated' literal (the drift the review flagged)."""
    src = (REPO / "channel" / "bus.mjs").read_text(encoding="utf-8")
    import_line = next(
        (ln for ln in src.splitlines()
         if "from './bus-state-marker.mjs'" in ln), "")
    assert "STATE_TERMINATED" in import_line, (
        "bus.mjs no longer imports STATE_TERMINATED from bus-state-marker.mjs")
    assert "declaredState = STATE_TERMINATED" in src, (
        "bus.mjs shutdown path should assign STATE_TERMINATED, not a bare literal")
