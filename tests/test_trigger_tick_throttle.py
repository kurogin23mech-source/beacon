"""Tests for ms-98 / e-2764 — trigger check/tick split + auto-tick throttle.

Structural fix: the pre-split ``cmd_trigger_check`` unconditionally invoked
``_auto_fire_*`` and ``_cleanup_stale_triggers`` on every call, both of which
hit the cloud. This caused the 2026-07-02 429 storm + 104-hung-process
leak. The split:

  * ``cmd_trigger_check`` — pure local read, gated by TTL stamp.
  * ``cmd_trigger_tick``  — explicit auto-fire + cleanup path.
  * ``_maybe_auto_tick``  — TTL + lock throttle inside ``check``.

These tests pin the observable contract so a future refactor cannot
accidentally re-open the hot path.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import commands  # noqa: E402
import cmd_trigger  # noqa: E402  (monkeypatch target after ms-127 e-4971 split)


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    """Point beacon at a fresh temp project with an empty triggers dir."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".beacon").mkdir()
    (project_dir / ".beacon" / "triggers").mkdir()
    project_file = project_dir / ".beacon" / "project.json"
    project_file.write_text(json.dumps({
        "name": "test",
        "milestones": [],
        "operations": [],
        "release_pipeline": {},
    }), encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
    monkeypatch.chdir(project_dir)
    yield project_dir


def _drop_stamp(project_dir: Path):
    stamp = project_dir / ".beacon" / "triggers" / ".last_tick_at"
    if stamp.exists():
        stamp.unlink()


def _write_stamp(project_dir: Path, age_seconds: float):
    stamp = project_dir / ".beacon" / "triggers" / ".last_tick_at"
    stamp.write_text(str(time.time() - age_seconds), encoding="utf-8")
    # Backdate mtime — that's what _maybe_auto_tick reads.
    os.utime(stamp, (time.time() - age_seconds, time.time() - age_seconds))


def _count_auto_fire_calls(monkeypatch):
    """Wrap the four auto-fire helpers with counters; return the counter."""
    counts = {"retro": 0, "op": 0, "due": 0, "marker": 0, "cleanup": 0}

    def _wrap(key):
        def _fn(*args, **kwargs):
            counts[key] += 1
        return _fn

    # ms-127 e-4971: after the trigger family moved to cmd_trigger.py, the
    # canonical definitions (and monkeypatch targets) are cmd_trigger.*  —
    # patching commands.* only replaces re-export aliases and does NOT
    # intercept calls made from within cmd_trigger's own namespace (the
    # e-4320 monkeypatch-trap rule).
    monkeypatch.setattr(cmd_trigger, "_auto_fire_retro_trigger", _wrap("retro"))
    monkeypatch.setattr(cmd_trigger, "_auto_fire_operation_triggers", _wrap("op"))
    monkeypatch.setattr(cmd_trigger, "_auto_fire_release_due_trigger", _wrap("due"))
    monkeypatch.setattr(cmd_trigger, "_auto_fire_release_marker_trigger", _wrap("marker"))
    monkeypatch.setattr(cmd_trigger, "_cleanup_stale_triggers", _wrap("cleanup"))
    return counts


def test_check_with_fresh_stamp_does_not_auto_fire(
    isolated_project, monkeypatch, capsys,
):
    """Stamp within TTL ⇒ ``check`` reads locally and skips all auto-fire."""
    _write_stamp(isolated_project, age_seconds=60)  # fresh (< 300s TTL)
    counts = _count_auto_fire_calls(monkeypatch)

    commands.cmd_trigger_check()

    assert counts == {"retro": 0, "op": 0, "due": 0, "marker": 0, "cleanup": 0}
    out = capsys.readouterr().out
    assert out.strip() == "[]"


def test_check_with_stale_stamp_auto_ticks(
    isolated_project, monkeypatch, capsys,
):
    """Stamp older than TTL ⇒ ``check`` runs the auto-fire pipeline once."""
    _write_stamp(isolated_project, age_seconds=3600)  # stale (> 300s TTL)
    counts = _count_auto_fire_calls(monkeypatch)

    commands.cmd_trigger_check()

    assert counts["retro"] == 1
    assert counts["op"] == 1
    assert counts["due"] == 1
    assert counts["marker"] == 1
    assert counts["cleanup"] == 1


def test_check_without_stamp_auto_ticks(
    isolated_project, monkeypatch, capsys,
):
    """No stamp on first run ⇒ auto-tick fires and creates the stamp."""
    _drop_stamp(isolated_project)
    counts = _count_auto_fire_calls(monkeypatch)

    commands.cmd_trigger_check()

    assert counts["retro"] == 1
    stamp = isolated_project / ".beacon" / "triggers" / ".last_tick_at"
    assert stamp.exists(), "cmd_trigger_check must create the stamp after auto-tick"


def test_ttl_zero_disables_auto_tick(
    isolated_project, monkeypatch, capsys,
):
    """``BEACON_TRIGGER_TICK_TTL=0`` ⇒ auto-tick never runs, even without stamp."""
    monkeypatch.setenv("BEACON_TRIGGER_TICK_TTL", "0")
    _drop_stamp(isolated_project)
    counts = _count_auto_fire_calls(monkeypatch)

    commands.cmd_trigger_check()

    assert counts == {"retro": 0, "op": 0, "due": 0, "marker": 0, "cleanup": 0}


def test_lock_prevents_concurrent_auto_tick(
    isolated_project, monkeypatch,
):
    """A held lock file ⇒ ``_maybe_auto_tick`` bails without running the pipeline."""
    _drop_stamp(isolated_project)
    counts = _count_auto_fire_calls(monkeypatch)

    lock = isolated_project / ".beacon" / "triggers" / ".tick.lock"
    lock.touch()  # simulate a peer already ticking
    try:
        ran = commands._maybe_auto_tick()
    finally:
        lock.unlink()

    assert ran is False
    assert counts == {"retro": 0, "op": 0, "due": 0, "marker": 0, "cleanup": 0}


def test_tick_command_updates_stamp(isolated_project, monkeypatch):
    """``cmd_trigger_tick`` always runs the pipeline AND touches the stamp."""
    _drop_stamp(isolated_project)
    counts = _count_auto_fire_calls(monkeypatch)

    commands.cmd_trigger_tick()

    assert counts["retro"] == 1
    assert counts["op"] == 1
    assert counts["due"] == 1
    assert counts["marker"] == 1
    assert counts["cleanup"] == 1
    stamp = isolated_project / ".beacon" / "triggers" / ".last_tick_at"
    assert stamp.exists()


def test_check_ignores_dotfiles(isolated_project, monkeypatch, capsys):
    """Stamp / lock files starting with ``.`` are not parsed as trigger JSON."""
    # Fresh stamp — no auto-tick.
    _write_stamp(isolated_project, age_seconds=60)
    _count_auto_fire_calls(monkeypatch)

    triggers_dir = isolated_project / ".beacon" / "triggers"
    (triggers_dir / ".tick.lock").write_text("not-json", encoding="utf-8")
    (triggers_dir / "real.json").write_text(
        json.dumps({"name": "real", "kind": "spec-needed", "message": "hi"}),
        encoding="utf-8",
    )

    commands.cmd_trigger_check()

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "real"
