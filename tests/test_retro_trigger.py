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
        sys.modules.pop("firestore_client", None)
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
