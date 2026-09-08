"""ms-169 #738 review follow-ups — the three maintainability/AX refactors.

  * e-6296: the three Python hook scripts share ONE bootstrap (bin/hook_bootstrap)
    for beacon-root discovery, stdin parsing, and the lib-import convention —
    instead of verbatim copies that had already drifted (while-loop vs cur.parents).
  * e-6297: the untrusted-turn source record {event_id, sender, preview} has ONE
    definition (untrusted_turn.make_source / SOURCE_KEYS); every producer builds
    through it.
  * e-6298: arm() returns an ARM_* status (armed / noop_blank_key / write_failed)
    so a caller can tell WHY arming did or didn't take effect from the return
    value alone — a blank key and a failed write no longer both collapse into
    False (and a swallowed write error no longer fakes a green "armed").
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
BIN = REPO / "bin"
for p in (str(LIB), str(BIN)):
    if p not in sys.path:
        sys.path.insert(0, p)

import hook_bootstrap as hb  # noqa: E402
import untrusted_turn as ut  # noqa: E402
import dm_untrusted as du  # noqa: E402


def _beacon_root(tmp_path: Path) -> Path:
    (tmp_path / ".beacon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({"name": "t"}))
    return tmp_path


# ---------------------------------------------------------------------------
# e-6296 — shared hook bootstrap
# ---------------------------------------------------------------------------

def test_find_beacon_root_walks_up_and_returns_none_outside(tmp_path):
    root = _beacon_root(tmp_path)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert hb.find_beacon_root(nested) == root.resolve()
    assert hb.find_beacon_root(root) == root.resolve()
    # A directory with no .beacon marker anywhere above → None.
    lonely = tmp_path.parent / "somewhere-else-xyz"
    lonely.mkdir(exist_ok=True)
    assert hb.find_beacon_root(lonely) is None


def test_read_hook_input_parses_empty_and_malformed(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name": "Bash"}'))
    assert hb.read_hook_input() == {"tool_name": "Bash"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert hb.read_hook_input() == {}          # empty stdin → usable empty dict
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json{"))
    assert hb.read_hook_input() is None        # malformed → None
    monkeypatch.setattr(sys, "stdin", io.StringIO("[1, 2, 3]"))
    assert hb.read_hook_input() is None        # non-object top-level → None


def test_import_lib_returns_modules_or_none():
    libs = hb.import_lib("untrusted_turn", "tool_effect")
    assert libs is not None
    assert libs["untrusted_turn"] is ut
    assert hasattr(libs["tool_effect"], "is_side_effect")
    # A module that doesn't exist → the whole batch fails safe to None.
    assert hb.import_lib("untrusted_turn", "definitely_not_a_real_lib_xyz") is None
    assert hb.import_lib() is None


@pytest.mark.parametrize("script", [
    "beacon-untrusted-tool-gate.py",
    "beacon-untrusted-turn-vet-hook.py",
    "beacon-bus-inbox-hook.py",
])
def test_hooks_delegate_to_bootstrap_no_local_copies(script):
    """Drift lock: no hook re-defines its own root-walk / lib-import; all three
    reach the shared bootstrap. This is the guard the finding asked for — a future
    edit can't quietly re-introduce a fourth copy of the convention."""
    src = (BIN / script).read_text(encoding="utf-8")
    assert "import hook_bootstrap" in src
    assert not re.search(r"\ndef _find_beacon_root\b", src)
    assert not re.search(r"\ndef _import_lib\b", src)


# ---------------------------------------------------------------------------
# e-6297 — one definition of the source shape
# ---------------------------------------------------------------------------

def test_make_source_shape_and_coercion():
    s = ut.make_source(event_id="e-1", sender="user-a", preview="hi")
    assert tuple(s.keys()) == ut.SOURCE_KEYS
    assert s == {"event_id": "e-1", "sender": "user-a", "preview": "hi"}
    # None / missing fields coerce to "" so every record has all keys present.
    blank = ut.make_source(event_id="e-2")
    assert blank == {"event_id": "e-2", "sender": "", "preview": ""}
    assert ut.make_source(event_id=None, sender=None, preview=None)["event_id"] == ""


def test_build_source_goes_through_make_source():
    event = {"event_id": "e-9", "sender_user_id": "u-x",
             "payload": {"text": "こんにちは 世界"}}
    s = du.build_source(event)
    assert set(s.keys()) == set(ut.SOURCE_KEYS)   # producer can't drift from shape
    assert s["event_id"] == "e-9"
    assert s["sender"] == "u-x"
    assert "こんにちは" in s["preview"]


def test_normalize_sources_dedups_through_shape():
    out = ut._normalize_sources(
        [{"event_id": "e-1", "sender": "a", "preview": "p"},
         {"event_id": "e-1", "sender": "dup", "preview": "x"}],  # dup event_id
        ["e-2"])                                                  # bare back-compat
    assert [r["event_id"] for r in out] == ["e-1", "e-2"]
    assert all(set(r.keys()) == set(ut.SOURCE_KEYS) for r in out)
    assert out[1] == {"event_id": "e-2", "sender": "", "preview": ""}


# ---------------------------------------------------------------------------
# e-6298 — arm() reports WHY, not just yes/no
# ---------------------------------------------------------------------------

def test_arm_ok_when_written(tmp_path):
    root = _beacon_root(tmp_path)
    status = ut.arm(root, "sv-1", event_ids=["e-1"])
    assert status == ut.ARM_OK
    assert ut.arm_succeeded(status) is True
    assert ut.is_armed(root, "sv-1") is not None


def test_arm_noop_on_blank_key(tmp_path):
    root = _beacon_root(tmp_path)
    status = ut.arm(root, "", event_ids=["e-1"])
    assert status == ut.ARM_NOOP_BLANK_KEY
    assert ut.arm_succeeded(status) is False    # a benign no-op, NOT armed


def test_arm_reports_write_failure_instead_of_faking_armed(tmp_path, monkeypatch):
    """The old bare-bool arm() returned True even when the best-effort _write_all
    silently swallowed the failure. Now a failed write is its OWN status, so
    cmd_dm_show can't show a green 'armed' light the state never actually got."""
    root = _beacon_root(tmp_path)
    monkeypatch.setattr(ut, "_write_all", lambda *a, **k: False)
    status = ut.arm(root, "sv-1", event_ids=["e-1"])
    assert status == ut.ARM_WRITE_FAILED
    assert ut.arm_succeeded(status) is False


def test_arm_status_constants_are_distinct():
    assert len({ut.ARM_OK, ut.ARM_NOOP_BLANK_KEY, ut.ARM_WRITE_FAILED}) == 3
