"""Contract test: the Python and JS qualitative gates agree (ms-93 e-3340).

The armed qualitative gate exists in two places that MUST stay in lockstep:
  * JS  — channel/bus-qualgate.mjs  (Claude Code's MCP reply tool, bus.mjs)
  * Py  — lib/dm_qualgate.py        (the CLI `beacon bus send` reply path, how a
                                     Codex armed session replies)

If they drift, Codex armed and Claude armed enforce different safety — exactly
the divergence e-2502 warns about. This drives BOTH with the same inputs and
pins identical classification + hold decisions (same two-impl-pin pattern as
tests/test_bus_budget_asymmetry.py).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import dm_qualgate as PY  # noqa: E402

QUALGATE_MJS = REPO / "channel" / "bus-qualgate.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(), reason="node not available — JS gate side is Node-only"
)


def _js(meta: dict, armed: bool) -> dict:
    """Run classifyOutboundReply + evaluateOutboundQualGate on the JS side."""
    script = textwrap.dedent(f"""
        import {{ classifyOutboundReply, evaluateOutboundQualGate }} from '{QUALGATE_MJS.as_posix()}'
        const meta = JSON.parse(process.argv[1])
        const cat = classifyOutboundReply(meta)
        const g = evaluateOutboundQualGate({{ classification: cat, armed: process.argv[2] === 'true' }})
        process.stdout.write(JSON.stringify({{ category: cat, hold: g.hold }}))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script,
         "--", json.dumps(meta), "true" if armed else "false"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr!r}")
    return json.loads(proc.stdout)


def _py(meta: dict, armed: bool) -> dict:
    cat = PY.classify_outbound_reply(
        channel=meta.get("channel", ""),
        sender_project_id=meta.get("senderProjectId", ""),
        recipient_project_id=meta.get("recipientProjectId", ""),
        actions_authorized=meta.get("actionsAuthorized"),
        confidential=meta.get("confidential", False),
    )
    g = PY.evaluate_outbound_qual_gate(cat, armed=armed)
    return {"category": cat, "hold": g["hold"]}


# (meta, armed) cases spanning every category + the not-armed short-circuit.
CASES = [
    ({"channel": "dm", "senderProjectId": "p1", "recipientProjectId": "p1"}, True),
    ({"channel": "dm", "senderProjectId": "p1", "recipientProjectId": "p2"}, True),
    ({"channel": "trek-progress-check", "senderProjectId": "p1", "recipientProjectId": "p2"}, True),
    ({"channel": "operation-trigger", "senderProjectId": "p1", "recipientProjectId": "p2"}, True),
    ({"channel": "dm", "senderProjectId": "p1", "recipientProjectId": "p1", "confidential": True}, True),
    ({"channel": "dm", "senderProjectId": "p1", "recipientProjectId": "p1", "actionsAuthorized": ["task.add"]}, True),
    ({"channel": "dm", "senderProjectId": "p1", "recipientProjectId": "p2"}, False),
    ({"channel": "trek-foo", "senderProjectId": "p1", "recipientProjectId": "p2"}, True),
]


@pytest.mark.parametrize("meta,armed", CASES)
def test_python_and_js_agree(meta, armed):
    py = _py(meta, armed)
    js = _js(meta, armed)
    assert py == js, f"gate drift for meta={meta} armed={armed}: py={py} js={js}"


# A couple of explicit expectations so the intent is readable (not just parity).
def test_external_armed_holds_both():
    meta = {"channel": "dm", "senderProjectId": "p1", "recipientProjectId": "p2"}
    assert _py(meta, True) == {"category": "external", "hold": True}
    assert _js(meta, True) == {"category": "external", "hold": True}


def test_same_project_armed_flows_both():
    meta = {"channel": "dm", "senderProjectId": "p1", "recipientProjectId": "p1"}
    assert _py(meta, True) == {"category": "normal_chat", "hold": False}
    assert _js(meta, True) == {"category": "normal_chat", "hold": False}
