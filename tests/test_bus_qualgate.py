"""Tests for the outbound qualitative gate (ms-100 e-3308).

Drives channel/bus-qualgate.mjs directly via node (the gate is Node-only, same
pattern as tests/test_bus_budget_mcp_reply.py). Pins that armed mode holds
外部宛 / 機密 / action付き autonomous replies structurally, while 通常会話
(same-project DM and Trek-scoped channels) flows.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALGATE_MJS = REPO_ROOT / "channel" / "bus-qualgate.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(), reason="node not available — qual gate is Node-only"
)


def _classify(meta: dict) -> str:
    script = textwrap.dedent(f"""
        import {{ classifyOutboundReply }} from '{QUALGATE_MJS.as_posix()}'
        const meta = JSON.parse(process.argv[1])
        process.stdout.write(classifyOutboundReply(meta))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", json.dumps(meta)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr!r}")
    return proc.stdout.strip()


def _gate(classification: str, armed: bool) -> dict:
    script = textwrap.dedent(f"""
        import {{ evaluateOutboundQualGate }} from '{QUALGATE_MJS.as_posix()}'
        const out = evaluateOutboundQualGate({{
            classification: process.argv[1], armed: process.argv[2] === 'true' }})
        process.stdout.write(JSON.stringify(out))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script,
         "--", classification, "true" if armed else "false"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr!r}")
    return json.loads(proc.stdout)


# --- classification --------------------------------------------------------

def test_same_project_dm_is_normal_chat():
    assert _classify({"channel": "dm", "senderProjectId": "p1",
                      "recipientProjectId": "p1"}) == "normal_chat"


def test_cross_project_dm_is_external():
    assert _classify({"channel": "dm", "senderProjectId": "p1",
                      "recipientProjectId": "p2"}) == "external"


def test_trek_named_channel_cross_project_is_external_now():
    """``trek-`` で始まる名前は事前承認済みではない (ms-185 e-7067)。

    以前は前方一致で "normal_chat" に落ち、armed なセッションが別プロジェクトへ
    自律送信できた。任意の名前が前方一致に当たるため、裏付け (= 人間の承認) と
    判定が切り離されていた。今は保留 (= 人間に回す) 側に倒れる。
    """
    assert _classify({"channel": "trek-progress-check", "senderProjectId": "p1",
                      "recipientProjectId": "p2"}) == "external"
    # 任意の名前でも同じ (前方一致の復活をここで捕まえる)
    assert _classify({"channel": "trek-anything", "senderProjectId": "p1",
                      "recipientProjectId": "p2"}) == "external"


def test_operation_trigger_channel_is_normal_chat():
    assert _classify({"channel": "operation-trigger", "senderProjectId": "p1",
                      "recipientProjectId": "p2"}) == "normal_chat"


def test_confidential_signal_wins():
    assert _classify({"channel": "dm", "senderProjectId": "p1",
                      "recipientProjectId": "p1", "confidential": True}) == "confidential"


def test_authorised_actions_is_action():
    assert _classify({"channel": "dm", "senderProjectId": "p1",
                      "recipientProjectId": "p1",
                      "actionsAuthorized": ["task.add"]}) == "action"


# --- gate decision ---------------------------------------------------------

def test_not_armed_never_holds():
    assert _gate("external", armed=False)["hold"] is False


def test_normal_chat_armed_flows():
    assert _gate("normal_chat", armed=True)["hold"] is False


def test_external_armed_holds():
    r = _gate("external", armed=True)
    assert r["hold"] is True and r["category"] == "external"


def test_confidential_armed_holds():
    assert _gate("confidential", armed=True)["hold"] is True


def test_action_armed_holds():
    assert _gate("action", armed=True)["hold"] is True
