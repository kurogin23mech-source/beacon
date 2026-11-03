"""Unit coverage for the DM detector consolidation (ms-85 e-3180).

e-3180 merged the session-start DM detectors into one single-pass script:

  * Step 1n + 1n-2 -> scripts/session-start-dm-inbox.py
    (one auth resolution, both DM sections rendered)

The refactor's contract is **display unchanged + local-mode silent skip**.
These tests pin the DM section ordering and the local-mode early return.

ms-185 e-7069: the trek half of this file (Step 1o / 1o-2 ->
scripts/session-start-trek-status.py + lib/trek_status) is gone. That Step was
removed from the session-start Skill along with the script it drove, so the
contract it pinned no longer exists. The DM half is unchanged.
"""
from __future__ import annotations

import importlib.util
import json
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
# scripts/session-start-dm-inbox.py: local-mode skip + section ordering
# ---------------------------------------------------------------------------

DM_SCRIPT = _load_script("session-start-dm-inbox.py")


def test_dm_inbox_local_mode_silent(monkeypatch, capsys):
    monkeypatch.setattr(DM_SCRIPT, "_resolve_context", lambda: ("", "", "", "base"))
    assert DM_SCRIPT.main() == 0
    assert capsys.readouterr().out == ""


def test_dm_inbox_orders_pending_then_catchup(monkeypatch, capsys):
    monkeypatch.setattr(
        DM_SCRIPT, "_resolve_context", lambda: ("proj", "u1", "tok", "base")
    )
    monkeypatch.setattr(DM_SCRIPT, "_pending_action_section",
                        lambda *a: "保留中 DM action: 1 件")
    monkeypatch.setattr(DM_SCRIPT, "_catchup_section",
                        lambda *a: "留守中に届いた DM (user-scoped catch-up):")
    assert DM_SCRIPT.main() == 0
    out = capsys.readouterr().out
    assert out.index("保留中 DM action") < out.index("留守中に届いた DM")


def test_dm_inbox_omits_empty_sections(monkeypatch, capsys):
    monkeypatch.setattr(
        DM_SCRIPT, "_resolve_context", lambda: ("proj", "u1", "tok", "base")
    )
    monkeypatch.setattr(DM_SCRIPT, "_pending_action_section", lambda *a: "")
    monkeypatch.setattr(DM_SCRIPT, "_catchup_section", lambda *a: "only catchup")
    assert DM_SCRIPT.main() == 0
    assert capsys.readouterr().out.strip() == "only catchup"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
