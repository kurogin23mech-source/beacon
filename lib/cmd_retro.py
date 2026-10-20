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
import date_window as _dw
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


def _resolve_window(since_spec: str, until_spec: str):
    """Turn the two period strings the CLI front passes into ISO bounds.

    Each flag names ONE end of the window, and the spec it carries is read by
    :func:`date_window.parse_since` — the same parser ``beacon recap --since``
    uses — taking that spec's lower bound for ``--since`` and its upper bound
    for ``--until``. So ``--since yesterday --until today`` resolves to
    yesterday..today, ``--since this-week`` to Monday, and an ISO date to
    itself (the shape the /beacon-retro Skill passes, unchanged).

    Why this exists (ms-181 e-7101)
    -------------------------------
    The two strings used to go straight into text comparisons. Measured on
    2026-10-20: ``beacon retro --since yesterday --until today`` exited 0,
    echoed ``"since": "yesterday"`` and returned an empty week — the spec was
    neither honoured nor refused, and nothing in the output said so. The same
    vocabulary works in ``beacon recap``, so the two verbs disagreed about
    what "yesterday" means while both reported success. An unreadable period
    now fails loudly instead of answering a period nobody asked for; a
    confident empty answer is worse than an error, because an error gets
    fixed.

    Exits 1 (never falls back to a different window) with the accepted forms
    listed, so the caller learns the vocabulary from the refusal itself.
    """
    def _bound(spec: str, flag: str, end: bool) -> str:
        spec = (spec or "").strip()
        if not spec:
            # Both fronts fill their own default before calling, so an empty
            # spec here means "no bound on this side" — reachable only from a
            # direct/programmatic call, where unbounded is the honest reading.
            return ""
        try:
            lo, hi = _dw.parse_since(spec)
        except ValueError as exc:
            print(f"Error: {flag}: {exc}", file=sys.stderr)
            sys.exit(1)
        return hi if end else lo

    since = _bound(since_spec, "--since", end=False)
    until = _bound(until_spec, "--until", end=True)
    problem = _dw.validate_range(since, until,
                                 lower_flag="--since", upper_flag="--until")
    if problem:
        print(f"Error: {problem}", file=sys.stderr)
        sys.exit(1)
    return since, until


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
    since_spec = os.environ.get("BEACON_SINCE", "")
    until_spec = os.environ.get("BEACON_UNTIL", "")
    since, until = _resolve_window(since_spec, until_spec)
    catch_up_mode = os.environ.get("BEACON_RETRO_CATCH_UP", "") == "1"
    data = load_project()
    #: Stamps retro could not judge against the period, tallied by entry type
    #: and reported in the payload (ms-181 e-7101) — owned here, filled by the
    #: walker and the deploy loop below, same idiom as ``search``.
    undated: dict = {}

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
                ms.get("entries", []), since, until, undated)
            if ms_entries:
                weekly_milestones.append({
                    "id": ms["id"],
                    # ms-155 e-5600 AX review: tag the producing class so a
                    # consumer of the (milestone-named, for back-compat) list can
                    # apply class-appropriate field extraction — a non-milestone
                    # deliverable-bearing class uses ``phase`` not ``status`` /
                    # ``progress``, and the bare key name no longer signals which.
                    "target_class": kind,
                    "title": ms.get("title", ""),
                    "status": ms.get("status", ""),
                    "progress": ms.get("progress", 0),
                    "entries": ms_entries,
                })

    # Include deploy records that fall within the period.
    #
    # ms-164 e-5948: read the deploy's worked-Target attribution generically.
    # e-5946 (cmd_deploy.py) stamps ``target_ids`` on every deploy record — the
    # AUTHORITATIVE set of deliverable-bearing Targets it shipped, spanning ALL
    # classes (a non-milestone class like a sales opportunity is included there,
    # never in ``milestones``). Retro is the weekly review of produced value, so
    # it must surface that generic attribution, otherwise a non-dev project's
    # deploys look Target-less in the retro. The legacy ``milestones`` field
    # (milestone-only) is kept for back-compat readers; ``target_ids`` is the
    # generic read-back, symmetric with cmd_deploy.py's own re-read idiom
    # (``d.get("target_ids", [])``, line 329/452).
    #
    # ms-181 e-7101: the window test goes through ``date_window`` like the
    # entry walker and ``search`` do. The truncation to ``[:10]`` that used to
    # stand in for "compare by day" only worked here by accident — it hid the
    # date-only-upper-bound hole on this one facet while the entry walker, fed
    # full timestamps, dropped the final day. Two spellings of one rule in one
    # function is how the facets of a single payload start disagreeing.
    weekly_deploys = []
    for dep in data.get("deployments", []):
        dep_verdict = _dw.classify(dep.get("date") or "", since, until)
        if dep_verdict == _dw.UNDATED:
            undated["deploy"] = undated.get("deploy", 0) + 1
        if dep_verdict == _dw.IN_WINDOW:
            weekly_deploys.append({
                "id": dep["id"],
                "type": dep.get("type", ""),
                "date": dep.get("date", "")[:10],
                "milestones": dep.get("milestones", []),
                "target_ids": dep.get("target_ids", []),
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
        # ms-181 e-7101: ``since`` / ``until`` are the RESOLVED bounds, not the
        # raw strings. They used to be the echo of whatever arrived, so
        # ``--since yesterday`` printed ``"since": "yesterday"`` beside an
        # empty answer and read as if the period had been honoured. For an
        # ISO-date argument (every call the Skill makes) the value is
        # unchanged; ``requested`` keeps what was actually typed so a resolved
        # window is never invisible and never mistaken for the input.
        "period": {
            "since": since,
            "until": until,
            "requested": {"since": since_spec, "until": until_spec},
        },
        "summary": data.get("summary", ""),
        "milestones": weekly_milestones,
        "deploys": weekly_deploys,
        "source_breakdown": source_breakdown,
        # Records carrying no stamp at all: excluded from the period (as
        # before) but counted, so "we could not judge these" cannot read as
        # "these did not happen". Same shape as ``search``'s own block.
        "undated": {
            "excluded": sum(undated.values()),
            "by_type": dict(sorted(undated.items())),
        },
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
