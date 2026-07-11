"""Unit coverage for the session-start inline blocks extracted in ms-85 e-3178.

e-3178 moved three embedded ``python3`` heredocs out of the session-start
Skill markdown into tested scripts/lib:

  * Step 1i (beacon-bus receive capability) -> scripts/check-mcp-receive-capability.py
  * Step 2.7 (Web UI open)                 -> scripts/open-webui.py
  * Step 1n-2 (user-scoped DM catch-up)    -> scripts/dm-user-scoped-catchup.py
                                              + lib/dm_pending formatters

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
# Step 2.7: Web UI open — local mode skip + browser resolver avoids Beacon.app
# ---------------------------------------------------------------------------

WEBUI = _load_script("open-webui.py")


def test_webui_local_mode_skips(tmp_path, monkeypatch, capsys):
    """No .beacon/cloud.json -> print nothing, exit 0 (session-start skips)."""
    monkeypatch.chdir(tmp_path)
    rc = WEBUI.main()
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_webui_no_project_id_skips(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "cloud.json").write_text(json.dumps({}))
    rc = WEBUI.main()
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_webui_cloud_mode_prints_url_without_launching(tmp_path, monkeypatch, capsys):
    """cloud mode -> build URL, print WEBUI_URL=..., never boot Beacon.app.

    The browser launch is stubbed so the test is hermetic; we assert the URL
    shape and that the default-browser resolver never returns a beacon handler.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "cloud.json").write_text(
        json.dumps({"project_id": "proj-xyz"})
    )
    launched = {}
    monkeypatch.setattr(
        WEBUI, "_launch", lambda browser, url: launched.update(browser=browser, url=url)
    )
    rc = WEBUI.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() == "WEBUI_URL=https://beacon-ai.dev/?project=proj-xyz"
    assert launched["url"] == "https://beacon-ai.dev/?project=proj-xyz"
    assert "beacon" not in launched["browser"].lower()


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
