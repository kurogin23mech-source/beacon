"""Cross-language drift guard for T5 short-ping allowlist (ms-54 / e-1306).

The T5 short-ping schema constants ``T5_PING_KEYS`` and
``T5_PING_VALUE_MAX_LEN`` are duplicated between two production modules by
necessity:

  * ``server/envelope.py``       — the server-side validator (source of truth)
  * ``channel/bus-envelope.mjs`` — the MCP bridge's pre-flight check so it can
    pick T5 vs T1 without a server round-trip.

Both copies need to agree, because the server re-validates anyway and a drift
would mean the bridge happily mints "T5" envelopes that the server then
rejects (or vice-versa). PR #107 flagged this as a low-risk drift surface;
this test pins the contract by running the JS module via ``node -e`` and
comparing its exported constants against the Python ones.

If you change one side, change the other or this test fails. That's the
whole point.

The probe re-uses the exact pattern from
``test_channel_bus_mjs_envelope.py`` so we exercise the real ``.mjs``
module the MCP bridge imports — no test-private duplicate that could
silently drift.

Skipped gracefully when ``node`` is not on PATH (CI environments without
Node still pass; the contract is checked anywhere Node is installed,
which covers all developer machines + the canonical CI).
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
BUS_ENVELOPE_MJS = REPO_ROOT / "channel" / "bus-envelope.mjs"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import envelope as env_mod  # noqa: E402


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(),
    reason="node not available — cross-language drift check needs Node",
)


def _read_js_constants() -> dict:
    """Import bus-envelope.mjs in a Node subprocess and emit its constants.

    Returns a dict with keys ``ping_keys`` (sorted list) and
    ``ping_value_max_len`` (int). Going through the real module path means
    any rename or accidental removal of the export surfaces here as a probe
    failure rather than a silent skip.
    """
    script = textwrap.dedent(f"""
        import {{ T5_PING_KEYS, T5_PING_VALUE_MAX_LEN }} from '{BUS_ENVELOPE_MJS.as_posix()}'
        const out = {{
          ping_keys: [...T5_PING_KEYS].sort(),
          ping_value_max_len: T5_PING_VALUE_MAX_LEN,
        }}
        process.stdout.write(JSON.stringify(out))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"node probe failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout)


def test_t5_ping_keys_match_between_python_and_js():
    """The allowlist of permitted T5 short-ping field names must be identical.

    A new key on one side without the other means the bridge could pre-clear
    a payload the server will reject (false-positive T5), or the bridge
    falls back to T1 on a payload the server would have accepted as T5
    (false-negative — extra round-trip, lost T5 throttling).
    """
    js = _read_js_constants()
    py_keys = sorted(env_mod.T5_PING_KEYS)
    assert js["ping_keys"] == py_keys, (
        f"T5_PING_KEYS drift detected — "
        f"python={py_keys}, js={js['ping_keys']}. "
        f"Update both server/envelope.py and channel/bus-envelope.mjs."
    )


def test_t5_ping_value_max_len_matches_between_python_and_js():
    """The max-length cap on T5 string values must agree.

    If the JS cap is higher than the Python cap, the bridge mints T5
    envelopes that fail server validation. If lower, the bridge over-rejects
    and forces T1 unnecessarily.
    """
    js = _read_js_constants()
    assert js["ping_value_max_len"] == env_mod.T5_PING_VALUE_MAX_LEN, (
        f"T5_PING_VALUE_MAX_LEN drift detected — "
        f"python={env_mod.T5_PING_VALUE_MAX_LEN}, "
        f"js={js['ping_value_max_len']}. "
        f"Update both server/envelope.py and channel/bus-envelope.mjs."
    )
