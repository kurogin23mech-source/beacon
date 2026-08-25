#!/usr/bin/env python3
"""cmd_retro.py — the `beacon retro *` command family (ms-127 e-4809).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules (core), never on commands.py —
acyclic (SPEC 方針4). commands.py re-imports the PUBLIC handlers for dispatch +
`commands.X`; the family-private helper `_retro_catch_up_block` is NOT
re-exported (patch it at cmd_retro._retro_catch_up_block — see the e-4320 rule).

The retro-day / week / document / content-input leaf helpers this family shares
with commands.py callers (_get_retro_day / _last_reviewed_week /
_most_recent_retro_day_on_or_before + DAY_NAMES / _load_local_documents /
_resolve_content_input) were promoted to commands_shared in this same change
(e-4809-foundation) so _auto_fire_retro_trigger / cmd_search / cmd_doc_add /
cmd_doc_update can keep using them without importing cmd_retro (which would cycle).

Test patch target: a test driving a cmd_retro_* handler patches helpers the
handler resolves in cmd_retro's own namespace (cmd_retro._X), including the
re-exported ones — `commands._X` is an independent binding and a silent no-op on
the cmd_retro call path (the e-4320 monkeypatch-trap rule).
"""

import json
import os
import sys
from typing import Optional

import core
import occupation
from commands_shared import (
    load_project,
    get_project_file,
    _is_cloud_mode,
    _get_api_client,
    _get_retro_day,
    _last_reviewed_week,
    _most_recent_retro_day_on_or_before,
    _load_local_documents,
    _resolve_content_input,
)


def cmd_retro_prepare():
    """Prepare the JSON payload that /beacon-retro Skill renders into a
    weekly markdown.

    The historical core path (= ``core.collect_retro_entries``) is kept
    as the "per-MS narrative grouping" layer — it walks each milestone's
    entry tree recursively in date order, which is exactly what the Skill
    wants for the "今週の取り組み" section.

    ms-79 / e-1836 additions:

      - ``source_breakdown`` (source 別件数): a top-level facet showing
        how many of the week's entries came from human dialog vs auto-op
        (= envelope auto-execute) vs DM. Generated via the unified
        ``retro_query`` base so the same human/auto-op/dm tagging used
        by /beacon-retrospect is reused here without duplication.
      - ``catch_up`` block: when multiple retro slots are unreviewed
        (= the retro trigger reports ``overdue_slots`` length > 1), this
        block surfaces the overdue weeks list so the Skill can offer a
        catch-up batch path (e-1837 / UC5-F2). The flag
        ``BEACON_RETRO_CATCH_UP=1`` enables the listing; without it the
        block is omitted so existing Skill output is unchanged.
    """
    since = os.environ.get("BEACON_SINCE", "")
    until = os.environ.get("BEACON_UNTIL", "")
    catch_up_mode = os.environ.get("BEACON_RETRO_CATCH_UP", "") == "1"
    data = load_project()

    # ms-155 e-5600: the per-class narrative grouping walks the project's
    # DELIVERABLE-BEARING classes (derived from the deliverable declaration), not a
    # hardcoded ``"milestone"`` literal — retro is the weekly review of produced
    # value, so the class it groups by is single-sourced from the same declaration
    # e-5599 unions. For a dev project this resolves to exactly ``["milestone"]``,
    # so the output is byte-identical; the coupling to milestone now comes from the
    # declaration rather than a bare string, and a project that adopts another
    # deliverable-bearing class is no longer silently excluded. A record that is
    # not milestone-shaped (no ``entries`` arm) contributes nothing (``.get`` → []),
    # so the loop stays safe across classes.
    weekly_milestones = []
    for kind in occupation.deliverable_bearing_classes(data):
        for ms in occupation.target_records(data, kind):
            ms_entries = core.collect_retro_entries(
                ms.get("entries", []), since, until)
            if ms_entries:
                weekly_milestones.append({
                    "id": ms["id"],
                    "title": ms.get("title", ""),
                    "status": ms.get("status", ""),
                    "progress": ms.get("progress", 0),
                    "entries": ms_entries,
                })

    # Include deploy records that fall within the period
    weekly_deploys = []
    for dep in data.get("deployments", []):
        dep_date = (dep.get("date") or "")[:10]
        if (not since or dep_date >= since) and (not until or dep_date <= until):
            weekly_deploys.append({
                "id": dep["id"],
                "type": dep.get("type", ""),
                "date": dep.get("date", "")[:10],
                "milestones": dep.get("milestones", []),
                "newly_completed_ms": dep.get("newly_completed_ms", []),
                "description": dep.get("description", ""),
            })

    # ms-79 / e-1836: source breakdown via the unified retro_query base.
    # Counts how many of the week's history events were human vs auto-op
    # vs DM. Silent-fail-tolerant so the retro prepare path never breaks
    # on a malformed entry — we just omit the breakdown in that case.
    source_breakdown: dict[str, int] = {}
    try:
        import retro_query as _rq  # noqa: PLC0415
        documents = _load_local_documents()
        rq_result = _rq.retro_query(
            data,
            documents,
            from_date=since,
            to_date=until,
            limit=10_000,
        )
        source_breakdown = (rq_result.get("facets") or {}).get("source") or {}
    except Exception:
        pass

    output: dict = {
        "project": data.get("name", ""),
        "period": {"since": since, "until": until},
        "summary": data.get("summary", ""),
        "milestones": weekly_milestones,
        "deploys": weekly_deploys,
        "source_breakdown": source_breakdown,
    }

    # ms-79 / e-1837 (UC5-F2): catch-up batch info.
    # Read the retro trigger payload (= written by _auto_fire_retro_trigger)
    # to discover whether more than one slot is overdue. The trigger file
    # is the canonical record so we don't recompute the slot list here.
    if catch_up_mode:
        catch_up_block = _retro_catch_up_block()
        if catch_up_block:
            output["catch_up"] = catch_up_block

    print(json.dumps(output, ensure_ascii=False))


def _retro_catch_up_block() -> Optional[dict]:
    """Return the catch-up payload built from the persistent retro trigger.

    Shape::

        {
          "overdue_slots": ["2026-W23", "2026-W24", "2026-W25"],
          "count": 3,
          "since_first_overdue": "2026-06-01",
        }

    Returns ``None`` if there is no retro trigger (= nothing to catch up
    on) or only a single overdue slot (= the regular retro flow already
    covers it).
    """
    project_dir = os.path.dirname(get_project_file())
    trigger_path = os.path.join(project_dir, "triggers", "retro.json")
    if not os.path.exists(trigger_path):
        return None
    try:
        with open(trigger_path, "r", encoding="utf-8") as f:
            trig = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    overdue = trig.get("overdue_slots") or []
    if not isinstance(overdue, list) or len(overdue) < 2:
        return None
    # Compute the Monday of the earliest overdue slot for since-display.
    import datetime as _dt
    since_first = ""
    try:
        first_slot = overdue[0]  # YYYY-WNN
        year_str, wk_str = first_slot.split("-W")
        year = int(year_str); wk = int(wk_str)
        jan4 = _dt.date(year, 1, 4)
        week1_mon = jan4 - _dt.timedelta(days=jan4.weekday())
        first_mon = week1_mon + _dt.timedelta(weeks=wk - 1)
        since_first = first_mon.strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        pass
    return {
        "overdue_slots": overdue,
        "count": len(overdue),
        "since_first_overdue": since_first,
    }


def cmd_retro_default_since():
    """Print the recommended default `--since` date for `beacon retro`.

    ms-43 e-570: when the user delays a retro past the configured retro_day,
    the previous default ("this Monday") covers too short a window and misses
    the actual unreviewed period. Logic:

      1. Read `.reviewed` for the most recent reviewed ISO week (if any).
      2. If found, return the Monday of the **next** ISO week after that.
      3. Otherwise fall back to the most recent retro_day on or before today,
         minus 6 days (= the start of the slot being reviewed).
      4. Final fallback: this Monday.

    Output is a YYYY-MM-DD line; empty on error (the shell wrapper has its
    own date-arithmetic fallback so older installs still function).
    """
    import datetime
    try:
        today = datetime.date.today()
        # 1. Try the .reviewed marker first.
        reviewed = _last_reviewed_week()
        if reviewed:
            # ISO week format: YYYY-WNN. The Monday of the NEXT week begins
            # the period we still owe a retro for.
            try:
                year_str, wk_str = reviewed.split("-W")
                year = int(year_str); wk = int(wk_str)
                # ISO week's Monday: use isocalendar inverse.
                # week N's Monday = first ISO date with isocalendar() == (year, wk, 1)
                jan4 = datetime.date(year, 1, 4)  # Always in ISO week 1
                week1_mon = jan4 - datetime.timedelta(days=jan4.weekday())
                last_reviewed_mon = week1_mon + datetime.timedelta(weeks=wk - 1)
                next_mon = last_reviewed_mon + datetime.timedelta(days=7)
                # Don't let it go past today.
                if next_mon <= today:
                    print(next_mon.strftime("%Y-%m-%d"))
                    return
            except (ValueError, IndexError):
                pass  # malformed marker, fall through

        # 2. No marker: anchor on the most recent retro_day.
        retro_day = _get_retro_day()
        anchor = _most_recent_retro_day_on_or_before(today, retro_day)
        # Cover the week ending on the anchor day.
        since = anchor - datetime.timedelta(days=6)
        # But not past today (defensive).
        if since > today:
            since = today
        print(since.strftime("%Y-%m-%d"))
    except Exception:
        # Empty output → shell wrapper falls back to its own date math.
        pass


def cmd_retro_save():
    """Persist a retro markdown document for a given ISO week.

    Cloud mode: pushes to the cloud retros subcollection (the source of truth
    for the Web UI Reviews tab). Local mode: writes `.beacon/retro/{week}.md`.

    /beacon-retro Skill MUST call this instead of writing the file directly.
    The legacy Write-tool path orphaned retros in cloud mode because the only
    push path was the initial `beacon cloud push` migration.
    """
    week = os.environ.get("BEACON_RETRO_WEEK", "")
    content = os.environ.get("BEACON_CONTENT", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not week:
        print("Error: --week required (e.g. 2026-W23)")
        sys.exit(1)

    import re
    if not re.match(r"^\d{4}-W\d{2}$", week):
        print(f"Error: week must be in YYYY-WNN format (got {week!r})")
        sys.exit(1)

    content = _resolve_content_input(content)

    if not content:
        print("Error: content required (pass via BEACON_CONTENT or stdin)")
        sys.exit(1)

    if _is_cloud_mode():
        client, config = _get_api_client()
        try:
            client.save_retro(config["project_id"], week, content)
        except RuntimeError as e:
            print(f"Error: {e}")
            sys.exit(1)
        location = f"cloud:projects/{config['project_id']}/retros/{week}"
    else:
        project_dir = os.path.dirname(get_project_file())
        retro_dir = os.path.join(project_dir, "retro")
        os.makedirs(retro_dir, exist_ok=True)
        fpath = os.path.join(retro_dir, f"{week}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        location = fpath

    if json_mode:
        print(json.dumps({"week": week, "location": location}, ensure_ascii=False))
    else:
        print(f"Saved retro: {week} -> {location}")


def cmd_retro_done():
    import datetime
    today = datetime.date.today()
    year, week, _ = today.isocalendar()
    current_week = f"{year}-W{week:02d}"

    project_dir = os.path.dirname(get_project_file())
    retro_dir = os.path.join(project_dir, "retro")
    os.makedirs(retro_dir, exist_ok=True)
    reviewed_path = os.path.join(retro_dir, ".reviewed")
    with open(reviewed_path, "w", encoding="utf-8") as f:
        f.write(current_week + "\n")

    triggers_dir = os.path.join(project_dir, "triggers")
    retro_trigger = os.path.join(triggers_dir, "retro.json")
    if os.path.exists(retro_trigger):
        os.remove(retro_trigger)

    print(f"Retro reviewed: {current_week}")
