"""Unit tests for the persistent retro trigger (e-575 / UC5-J5').

Locks in the contract:
  - The retro trigger surfaces only after the configured retro_day has
    occurred for the current week.
  - It persists across days until `beacon retro done` runs.
  - Multiple unreviewed weeks accumulate into a single "catchup" trigger.
  - The created_at field is preserved across daily refreshes so users see
    how long the pending retro has been around.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def project_with_retro(monkeypatch):
    """Set up an isolated project dir with .beacon/{project.json, retro/, triggers/}."""
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "retro").mkdir()
        (beacon_dir / "triggers").mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({"name": "test", "milestones": [], "summary": "",
                        "retro_day": "friday"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        # ms-95 e-2438: monkeypatch.delitem auto-restores. Raw pop leaks across
        # the test boundary → adjacent firestore_client mocks vanish.
        if "firestore_client" in sys.modules:
            monkeypatch.delitem(sys.modules, "firestore_client")
        yield Path(tmp)


def _set_today(monkeypatch, year: int, month: int, day: int):
    """Freeze datetime.date.today() globally for this test.

    `_auto_fire_retro_trigger` (and friends) do `import datetime` inside the
    function body, so we can't patch a commands-module-level alias. Instead
    we replace the `date` class on the real `datetime` module — subclasses
    are accepted by every internal usage.
    """
    real_date = datetime.date
    fixed = real_date(year, month, day)

    class _FrozenDate(real_date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr(datetime, "date", _FrozenDate)


def _read_trigger(project_dir: Path) -> dict | None:
    p = project_dir / ".beacon" / "triggers" / "retro.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def test_no_trigger_before_any_retro_day_with_reviewed_marker(project_with_retro, monkeypatch):
    """If `.reviewed` says the most-recent retro slot is done, no trigger fires.

    retro_day is Friday (weekday=4). On Tuesday 2026-05-26, the anchor is
    Friday 2026-05-22 (W21). If `.reviewed` already records W21, we should
    stay silent — the persistence behaviour is gated by an actual unmet
    retro week, not by "is today a retro day".
    """
    import commands  # type: ignore
    # Mark W21 as already reviewed so the anchor's slot is satisfied.
    reviewed_path = project_with_retro / ".beacon" / "retro" / ".reviewed"
    reviewed_path.write_text("2026-W21\n", encoding="utf-8")

    # 2026-05-26 is a Tuesday inside W22; anchor falls back to Friday W21.
    _set_today(monkeypatch, 2026, 5, 26)
    commands._auto_fire_retro_trigger()
    # No trigger — W21 already reviewed and W22's retro day hasn't arrived yet.
    assert _read_trigger(project_with_retro) is None


def test_trigger_fires_mid_week_for_unreviewed_prior_slot(project_with_retro, monkeypatch):
    """Mid-week before this week's retro day, if a prior unreviewed slot exists,
    the trigger persists (this is the core "持続する" requirement of e-575).

    Without any .reviewed marker, Tuesday in W22 sees that the anchor (last
    Friday, in W21) was never retro'd → fire the trigger.
    """
    import commands  # type: ignore
    _set_today(monkeypatch, 2026, 5, 26)  # Tuesday W22
    commands._auto_fire_retro_trigger()
    t = _read_trigger(project_with_retro)
    assert t is not None
    assert t["current_slot"] == "2026-W21"


def test_trigger_fires_on_retro_day(project_with_retro, monkeypatch):
    """On Friday with no prior retro, the trigger appears with this week's slot."""
    import commands  # type: ignore
    # 2026-05-29 is a Friday.
    _set_today(monkeypatch, 2026, 5, 29)
    commands._auto_fire_retro_trigger()
    t = _read_trigger(project_with_retro)
    assert t is not None
    assert t["kind"] == "retro-due"
    assert t["current_slot"] == "2026-W22"
    assert t["overdue_slots"] == ["2026-W22"]


def test_trigger_persists_into_next_day(project_with_retro, monkeypatch):
    """Saturday after a Friday retro_day, with no `beacon retro done`,
    the trigger must still be present (UC5-J5' core requirement).
    """
    import commands  # type: ignore
    # Friday → fires
    _set_today(monkeypatch, 2026, 5, 29)
    commands._auto_fire_retro_trigger()
    assert _read_trigger(project_with_retro) is not None

    # Saturday → re-running cleanup + fire must keep the trigger
    _set_today(monkeypatch, 2026, 5, 30)
    commands._cleanup_stale_triggers()  # the cleanup path that used to delete it
    commands._auto_fire_retro_trigger()
    t = _read_trigger(project_with_retro)
    assert t is not None
    # current_slot is still W22 (the week containing Friday 2026-05-29)
    # because Saturday 2026-05-30 belongs to the same ISO week.
    assert t["current_slot"] == "2026-W22"


def test_trigger_persists_into_next_week_with_overdue_list(project_with_retro, monkeypatch):
    """If the user skips a week, the next Friday accumulates the overdue list.

    Setup: user has retro'd W21. Then they miss W22's retro. On Friday W23
    the trigger must list BOTH W22 and W23 as overdue.
    """
    import commands  # type: ignore
    # Anchor: W21 is reviewed. Earliest unreviewed slot will be W22.
    reviewed_path = project_with_retro / ".beacon" / "retro" / ".reviewed"
    reviewed_path.write_text("2026-W21\n", encoding="utf-8")

    # Friday 2026-05-29 (W22) → fires for W22.
    _set_today(monkeypatch, 2026, 5, 29)
    commands._auto_fire_retro_trigger()
    t1 = _read_trigger(project_with_retro)
    assert t1["current_slot"] == "2026-W22"
    assert t1["overdue_slots"] == ["2026-W22"]

    # Skip the week. Next Friday is 2026-06-05 (W23) — now W22 + W23 overdue.
    _set_today(monkeypatch, 2026, 6, 5)
    commands._cleanup_stale_triggers()
    commands._auto_fire_retro_trigger()
    t = _read_trigger(project_with_retro)
    assert t is not None
    assert t["current_slot"] == "2026-W23"
    # Both weeks are now flagged as overdue.
    assert "2026-W22" in t["overdue_slots"]
    assert "2026-W23" in t["overdue_slots"]


def test_trigger_disappears_after_retro_done(project_with_retro, monkeypatch):
    """`beacon retro done` writes .reviewed and removes the trigger file.
    A subsequent re-fire on the same week MUST NOT recreate it."""
    import commands  # type: ignore
    _set_today(monkeypatch, 2026, 5, 29)
    commands._auto_fire_retro_trigger()
    assert _read_trigger(project_with_retro) is not None

    # Simulate `beacon retro done` for current week (W22).
    reviewed_path = project_with_retro / ".beacon" / "retro" / ".reviewed"
    reviewed_path.write_text("2026-W22\n", encoding="utf-8")
    # Also remove the trigger like cmd_retro_done does.
    trigger_path = project_with_retro / ".beacon" / "triggers" / "retro.json"
    trigger_path.unlink()

    # Same Friday: should NOT re-fire because .reviewed says W22 is done.
    commands._auto_fire_retro_trigger()
    assert _read_trigger(project_with_retro) is None


def test_created_at_preserved_across_daily_refresh(project_with_retro, monkeypatch):
    """The original first-fire date must survive daily refreshes so users
    can see how long the pending retro has been around."""
    import commands  # type: ignore
    _set_today(monkeypatch, 2026, 5, 29)
    commands._auto_fire_retro_trigger()
    t1 = _read_trigger(project_with_retro)
    assert t1["created_at"] == "2026-05-29"

    # Next day, refresh.
    _set_today(monkeypatch, 2026, 5, 30)
    commands._auto_fire_retro_trigger()
    t2 = _read_trigger(project_with_retro)
    assert t2["created_at"] == "2026-05-29"  # unchanged
    assert t2["refreshed_at"] == "2026-05-30"


# ---------------------------------------------------------------------------
# ms-43 e-570: retro_default_since handles delayed retros correctly.
# ---------------------------------------------------------------------------

def test_default_since_uses_next_monday_after_last_reviewed_week(
    project_with_retro, monkeypatch, capsys
):
    """If `.reviewed` says W21 was reviewed, the next retro's default since
    should be W22 Monday (not "this Monday" which would miss most of W22 and
    all of W21's tail)."""
    import commands  # type: ignore
    reviewed_path = project_with_retro / ".beacon" / "retro" / ".reviewed"
    reviewed_path.write_text("2026-W21\n", encoding="utf-8")

    # 2026-05-26 = Tuesday of W22 (delayed retro scenario).
    _set_today(monkeypatch, 2026, 5, 26)
    commands.cmd_retro_default_since()
    out = capsys.readouterr().out.strip()
    # W22 Monday = 2026-05-25
    assert out == "2026-05-25", out


def test_default_since_falls_back_to_retro_day_anchor_without_marker(
    project_with_retro, monkeypatch, capsys
):
    """No `.reviewed` marker: anchor on most recent retro_day (Friday) and
    cover the prior 7 days."""
    import commands  # type: ignore
    # 2026-05-26 Tuesday → most recent Friday on/before = 2026-05-22 (W21).
    # since should be 2026-05-22 - 6 days = 2026-05-16 (Sat of W20).
    _set_today(monkeypatch, 2026, 5, 26)
    commands.cmd_retro_default_since()
    out = capsys.readouterr().out.strip()
    assert out == "2026-05-16", out


def test_default_since_delayed_two_weeks_covers_both(
    project_with_retro, monkeypatch, capsys
):
    """If the last reviewed week is W20 and today is W22 Tuesday, the default
    since should be W21 Monday so both unreviewed weeks (W21+W22 partial) are
    captured. Naive "this Monday" would lose W21 entirely."""
    import commands  # type: ignore
    reviewed_path = project_with_retro / ".beacon" / "retro" / ".reviewed"
    reviewed_path.write_text("2026-W20\n", encoding="utf-8")
    _set_today(monkeypatch, 2026, 5, 26)  # W22 Tuesday
    commands.cmd_retro_default_since()
    out = capsys.readouterr().out.strip()
    # W21 Monday = 2026-05-18
    assert out == "2026-05-18", out


def test_retro_save_writes_local_file_in_local_mode(
    project_with_retro, monkeypatch, capsys
):
    """In local mode, `beacon retro save` writes .beacon/retro/{week}.md.

    Regression guard: the Skill previously bypassed the CLI by calling Write
    directly, which orphaned retros in cloud mode. The CLI must own the
    persistence so local/cloud routing is uniform.
    """
    import commands  # type: ignore
    monkeypatch.setenv("BEACON_RETRO_WEEK", "2026-W23")
    monkeypatch.setenv("BEACON_CONTENT", "# W23 retro\nhello")
    commands.cmd_retro_save()
    fpath = project_with_retro / ".beacon" / "retro" / "2026-W23.md"
    assert fpath.exists()
    assert fpath.read_text(encoding="utf-8") == "# W23 retro\nhello"
    out = capsys.readouterr().out
    assert "2026-W23" in out


def test_retro_save_rejects_bad_week_format(
    project_with_retro, monkeypatch, capsys
):
    """Week must match YYYY-WNN. Anything else is rejected before persistence."""
    import commands  # type: ignore
    monkeypatch.setenv("BEACON_RETRO_WEEK", "not-a-week")
    monkeypatch.setenv("BEACON_CONTENT", "x")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_retro_save()
    assert exc.value.code == 1
    err = capsys.readouterr().out
    assert "YYYY-WNN" in err


def test_retro_save_requires_content(project_with_retro, monkeypatch, capsys):
    """Empty content must fail loudly rather than silently writing an empty file."""
    import commands  # type: ignore
    monkeypatch.setenv("BEACON_RETRO_WEEK", "2026-W23")
    monkeypatch.setenv("BEACON_CONTENT", "")
    # Simulate non-tty stdin returning empty (interactive case is the same)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_retro_save()
    assert exc.value.code == 1
    err = capsys.readouterr().out
    assert "content required" in err
