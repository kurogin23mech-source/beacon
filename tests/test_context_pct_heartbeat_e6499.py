"""ms-159 / e-6499 — pin the context-usage % carry: monitor persist → state file
→ JS reader → heartbeat body (both twins) → server field.

The value travels monitor(Python) → .claude/context-usage-state.json →
bus-context-usage.mjs(JS reader) → buildHeartbeatBody/heartbeat_body(twins) →
SessionUpsert(server). This file pins the parts that are unit-verifiable without
a live server: the monitor persists the %, the JS reader parses it fail-safe, and
the two heartbeat-body twins both carry it (drift guard). The server store + row
projection are exercised by the model field + existing upsert pass-through, and
are runtime-verified after the production state-stamp deploy (see e-6488 note).
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

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "beacon_cli" / "hooks"))
import context_monitor as cm  # noqa: E402

CONTEXT_MJS = REPO / "channel" / "bus-context-usage.mjs"


# --------------------------------------------------------------------------- #
# Monitor persist (Layer 1) — the state file now carries the current %
# --------------------------------------------------------------------------- #

class TestMonitorPersist:
    def test_save_state_writes_context_fields(self, tmp_path):
        state = tmp_path / "context-usage-state.json"
        cm._save_state(state, "sess-1", [20, 40],
                       context_pct=57, context_used=114000, context_limit=200000)
        data = json.loads(state.read_text())
        assert data["session_id"] == "sess-1"
        assert data["notified_thresholds"] == [20, 40]
        assert data["context_pct"] == 57
        assert data["context_used"] == 114000
        assert data["context_limit"] == 200000

    def test_save_state_omits_context_when_absent(self, tmp_path):
        # Back-compat: an old caller that doesn't pass context_pct writes no such
        # key (the reader then reports null and the heartbeat omits the field).
        state = tmp_path / "context-usage-state.json"
        cm._save_state(state, "sess-1", [])
        data = json.loads(state.read_text())
        assert "context_pct" not in data

    def test_save_state_zero_pct_is_written(self, tmp_path):
        state = tmp_path / "context-usage-state.json"
        cm._save_state(state, "s", [], context_pct=0)
        assert json.loads(state.read_text())["context_pct"] == 0


# --------------------------------------------------------------------------- #
# JS reader (Layer 3) — bus-context-usage.mjs, fail-safe
# --------------------------------------------------------------------------- #

def _have_node() -> bool:
    return shutil.which("node") is not None


jsmark = pytest.mark.skipif(not _have_node(), reason="node not available")


def _read_via_node(state_path: Path):
    script = textwrap.dedent(f"""
        import {{ readContextUsage }} from '{CONTEXT_MJS.as_posix()}'
        const r = readContextUsage({json.dumps(str(state_path))})
        process.stdout.write(JSON.stringify(r))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@jsmark
class TestJsReader:
    def test_reads_context_pct(self, tmp_path):
        p = tmp_path / "context-usage-state.json"
        p.write_text(json.dumps({"session_id": "s", "context_pct": 73}))
        assert _read_via_node(p) == {"contextPct": 73}

    def test_zero_is_returned_not_treated_as_absent(self, tmp_path):
        p = tmp_path / "context-usage-state.json"
        p.write_text(json.dumps({"context_pct": 0}))
        assert _read_via_node(p) == {"contextPct": 0}

    def test_absent_file_is_null(self, tmp_path):
        assert _read_via_node(tmp_path / "nope.json") is None

    def test_malformed_file_is_null(self, tmp_path):
        p = tmp_path / "context-usage-state.json"
        p.write_text("{not json")
        assert _read_via_node(p) is None

    def test_no_context_pct_field_is_null(self, tmp_path):
        # Back-compat: an old state file (session_id + notified only) → null.
        p = tmp_path / "context-usage-state.json"
        p.write_text(json.dumps({"session_id": "s", "notified_thresholds": [20]}))
        assert _read_via_node(p) is None

    def test_out_of_range_is_null(self, tmp_path):
        for bad in (-5, 150, "73", None):
            p = tmp_path / "context-usage-state.json"
            p.write_text(json.dumps({"context_pct": bad}))
            assert _read_via_node(p) is None, bad


# --------------------------------------------------------------------------- #
# Drift guard (Layer 2) — both heartbeat-body twins carry context_pct
# --------------------------------------------------------------------------- #

class TestTwinParity:
    def test_python_body_builder_has_context_pct_param(self):
        import inspect
        sys.path.insert(0, str(REPO / "lib"))
        import bus_protocol  # noqa: E402
        sig = inspect.signature(bus_protocol.heartbeat_body)
        assert "context_pct" in sig.parameters, (
            "lib/bus_protocol.heartbeat_body lost context_pct — edit both twins")

    def test_js_body_builder_references_context_pct(self):
        # The JS twin must both accept contextPct and emit body.context_pct, or it
        # silently drops the value the bridge reads (the drift the task warns of).
        src = (REPO / "channel" / "bus-heartbeat.mjs").read_text(encoding="utf-8")
        assert "contextPct" in src, "buildHeartbeatBody lost the contextPct param"
        assert "body.context_pct" in src, "buildHeartbeatBody stopped emitting context_pct"

    def test_server_model_declares_context_pct(self):
        # SessionUpsert drops undeclared fields; context_pct must be explicit or the
        # value the bridge sends is silently discarded before persistence.
        src = (REPO / "server" / "routers_projects.py").read_text(encoding="utf-8")
        assert "context_pct: Optional[int]" in src, (
            "SessionUpsert lost the context_pct field — the value would be dropped")
