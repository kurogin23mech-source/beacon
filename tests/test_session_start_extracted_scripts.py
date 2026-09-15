"""Unit coverage for the session-start inline blocks extracted in ms-85 e-3178.

e-3178 moved three embedded ``python3`` heredocs out of the session-start
Skill markdown into tested scripts/lib:

  * Step 1i (beacon-bus receive capability) -> scripts/check-mcp-receive-capability.py
  * Step 2.7 (board open)                  -> scripts/open-webui.py
    (ms-170 e-6347 folded its cloud/local branch into one `beacon view`)
  * Step 1n-2 (user-scoped DM catch-up)    -> lib/dm_pending formatters
    (fetch orchestration later merged into scripts/session-start-dm-inbox.py
     by ms-85 e-3180; the pure filter/format helpers tested here are shared)

The refactor's contract is **behavior unchanged**: these tests pin the moved
logic (status detection, warning-band text, filter/format, local-mode skip)
so a future edit to the extracted code can't silently drift the observable
session-start output.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))


def _load_script(name: str):
    path = REPO / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Step 1i: MCP receive-capability detection
# ---------------------------------------------------------------------------

MCP = _load_script("check-mcp-receive-capability.py")


def test_mcp_no_file(tmp_path):
    assert MCP.detect_status(str(tmp_path)) == "NO_MCP_JSON"


def test_mcp_beacon_bus_present(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"beacon-bus": {}}}))
    assert MCP.detect_status(str(tmp_path)) == "OK"


def test_mcp_other_server_only(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {}}}))
    assert MCP.detect_status(str(tmp_path)) == "NO_BEACON_BUS_ENTRY"


def test_mcp_malformed(tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json")
    assert MCP.detect_status(str(tmp_path)) == "MCP_JSON_MALFORMED"


def test_mcp_warning_band_present_for_non_ok():
    band = MCP.format_warning("NO_MCP_JSON")
    # The exact literals the Skill used to build inline (Step 1i band).
    assert band.startswith("⚠ この cwd は「送信専用」の恐れ")
    assert "detail: NO_MCP_JSON" in band
    assert "beacon channel install" in band


def test_mcp_warning_band_empty_for_ok_and_unknown():
    assert MCP.format_warning("OK") == ""
    assert MCP.format_warning("UNKNOWN") == ""


# ---------------------------------------------------------------------------
# Step 2.7: board open — one unified viewer (ms-170 e-6347)
#
# The folded contract: main() launches `beacon view` (the unified viewer whose
# conversion layer absorbs local vs cloud) and returns immediately, with NO
# per-project-form branch and NO Tauri desktop / hosted-Web-UI switch. These
# tests pin that fold so a future edit can't silently reintroduce the branch.
# ---------------------------------------------------------------------------

WEBUI = _load_script("open-webui.py")


def test_view_launches_in_local_mode(tmp_path, monkeypatch, capsys):
    """No .beacon/cloud.json -> still launch the one viewer, announce it."""
    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(
        WEBUI, "_launch_viewer", lambda b: seen.update(bin=b) or True
    )
    rc = WEBUI.main()
    assert rc == 0
    assert capsys.readouterr().out.strip() == "VIEWER_LAUNCHED=beacon view"
    assert seen["bin"]  # a beacon binary path was resolved and passed


def test_view_launches_identically_in_cloud_mode(tmp_path, monkeypatch, capsys):
    """cloud.json + project_id -> same launch, same output (no branch).

    The whole point of the fold: project form no longer changes what opens.
    The launcher does not read cloud.json at all, so cloud and local produce a
    byte-identical announcement.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "cloud.json").write_text(
        json.dumps({"project_id": "proj-xyz"})
    )
    monkeypatch.setattr(WEBUI, "_launch_viewer", lambda b: True)
    rc = WEBUI.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "VIEWER_LAUNCHED=beacon view"
    # No cloud URL, no Beacon.app handler, no desktop marker survive the fold.
    assert "beacon-ai.dev" not in out
    assert "WEBUI_URL" not in out and "DESKTOP_LAUNCHED" not in out


def test_view_silent_when_launch_fails(tmp_path, monkeypatch, capsys):
    """beacon view can't be spawned -> print nothing, never block."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(WEBUI, "_launch_viewer", lambda b: False)
    rc = WEBUI.main()
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_beacon_bin_prefers_install_sibling():
    """Resolve the CLI belonging to this install (bin/beacon sibling), not PATH.

    Guards against a shadowing `beacon` earlier on PATH launching a different
    viewer than the one that started the session.
    """
    resolved = WEBUI._beacon_bin()
    assert resolved == str(REPO / "bin" / "beacon")
    assert os.path.isfile(resolved)


def test_launch_viewer_spawns_detached_beacon_view(monkeypatch):
    """Drift guard on the actual spawn: argv is `<bin> view`, detached, silent.

    `beacon view` runs a foreground server; if we ever stop detaching or start
    waiting on it, session-start would hang. Pin the Popen call shape.
    """
    import subprocess as _sp

    calls = {}

    def fake_popen(argv, **kw):
        calls["argv"] = argv
        calls["kw"] = kw

        class _P:
            pass

        return _P()

    monkeypatch.setattr(WEBUI.subprocess, "Popen", fake_popen)
    ok = WEBUI._launch_viewer("/some/where/bin/beacon")
    assert ok is True
    assert calls["argv"] == ["/some/where/bin/beacon", "view"]
    assert calls["kw"].get("start_new_session") is True
    assert calls["kw"].get("stdout") == _sp.DEVNULL
    assert calls["kw"].get("stderr") == _sp.DEVNULL
    assert calls["kw"].get("stdin") == _sp.DEVNULL


# ---------------------------------------------------------------------------
# Step 1n-2: user-scoped DM catch-up filter + format (lib/dm_pending)
# ---------------------------------------------------------------------------

import dm_pending  # noqa: E402


def _ev(eid, ruid, rsid, text="hi", sender="sv-sender", created="2026-07-10T12:00:00Z",
        opened=""):
    return {
        "event_id": eid,
        "sender_session_id": sender,
        "created_at": created,
        "opened_at": opened,
        "payload": {
            "recipient_user_id": ruid,
            "recipient_session_id": rsid,
            "text": text,
        },
    }


def test_catchup_filter_keeps_only_user_scoped_self():
    events = [
        _ev("a", "u1", ""),          # keep: user-scoped, mine
        _ev("b", "u1", "sv-live"),   # drop: session-scoped
        _ev("c", "u2", ""),          # drop: another user
    ]
    rows = dm_pending.filter_user_scoped_catchup(events, "u1")
    assert [r["event_id"] for r in rows] == ["a"]


def test_catchup_filter_ignores_bad_payload():
    events = [{"event_id": "x", "payload": None},
              {"event_id": "y", "payload": "notdict"}]
    assert dm_pending.filter_user_scoped_catchup(events, "u1") == []


def test_catchup_format_empty_collapses():
    assert dm_pending.format_user_scoped_catchup([]) == ""
    assert dm_pending.format_user_scoped_catchup(None) == ""


def test_catchup_format_matches_inline_shape():
    rows = dm_pending.filter_user_scoped_catchup(
        [_ev("aaaaaaaaaaaaaa", "u1", "", text="hello there",
             sender="sv-bbbbbbbbbbbb")],
        "u1",
    )
    out = dm_pending.format_user_scoped_catchup(rows)
    lines = out.splitlines()
    assert lines[0] == "留守中に届いた DM (user-scoped catch-up):"
    assert lines[1] == "  [aaaaaaaaaaaa] from sv-bbbbbbbbb at 2026-07-10T12:00:00"
    assert lines[2] == "    hello there..."


def test_catchup_format_opened_flag_and_overflow():
    events = [_ev(f"e{i:012d}", "u1", "", opened=("t" if i == 0 else ""))
              for i in range(7)]
    rows = dm_pending.filter_user_scoped_catchup(events, "u1")
    out = dm_pending.format_user_scoped_catchup(rows)
    assert "(既読)" in out.splitlines()[1]
    assert "… 他 2 件 (`beacon bus receive --channel dm` で全文)" in out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
