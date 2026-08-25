#!/usr/bin/env python3
"""cmd_trigger.py — the `beacon trigger *` command family + auto-fire/tick subsystem (ms-127 e-4971).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules (store, core, subprocess,
shutil, version_rules), never on commands.py — acyclic (SPEC 方針4).
commands.py re-imports these names for dispatch + `commands.X`.

MONKEYPATCH TARGET (= 独立レビュー AX/保守性 medium 由来, e-4971): これらの関数が
内部で解決するヘルパー (get_store / _get_triggers_dir / _extract_token /
_maybe_auto_tick / _push_operation_trigger_to_bus 等) を test で stub する場合、
`monkeypatch.setattr(commands, "_X", ...)` は **届かない** — 関数は _X を自分の
namespace (= この cmd_trigger module) で解決するため。`monkeypatch.setattr(
cmd_trigger, "_X", ...)` を当てること (commands 経由の caller 用に両方 mirror して
よい)。commands 側だけに patch すると stub が効かず test が偽 green になる。
"""

import json
import os
import shutil
import subprocess
import sys

from store import get_store
import core

from commands_shared import (
    get_project_file,
    load_project,
    _get_triggers_dir,
    _get_retro_day,
    _last_reviewed_week,
    _most_recent_retro_day_on_or_before,
    _application_map_applies,
    _get_docs_dir,
    _read_local_doc,
    _spec_exists_for_ms,
    _fire_review_due_for_pr,
    _clear_review_due_for_pr,
    _fire_pr_open_review_triggers,
    _pending_review_types_for_pr,
    _pr_open_reviewed_marker_path,
    _REVIEW_DUE_SUFFIX,
    _get_cloud_config_path,
    _resolve_active_api_url,
    _extract_token,
)


# ---------------------------------------------------------------------------
# Trigger helpers and auto-fire subsystem
# ---------------------------------------------------------------------------


def _iso_week_string(date) -> str:
    """`YYYY-WNN` formatted ISO week for a `datetime.date`."""
    year, week, _ = date.isocalendar()
    return f"{year}-W{week:02d}"


def _auto_fire_retro_trigger():
    """Fire (or refresh) the retro trigger until the user actually retros.

    Per e-575 / SPEC ms-40 §B-1: the retro trigger MUST persist across days
    until the corresponding week is reviewed. Earlier behavior fired only on
    the configured retro day and `_cleanup_stale_triggers` deleted it the
    next morning — meaning a busy user who deferred retro to Monday saw the
    reminder vanish.

    New behavior:
      - Compute the most recent retro-day date on or before today
      - Identify the ISO-week of that retro day as the "current retro slot"
      - If `.reviewed` says we already retro'd that week or later, do nothing
      - Otherwise fire/refresh the trigger with a message that:
          - Names the unreviewed week (or weeks, if multiple slots accumulated)
          - Persists across days until `beacon retro done` writes a fresh
            `.reviewed` value
    """
    import datetime
    today = datetime.date.today()
    retro_day = _get_retro_day()

    # The retro slot for "this period" anchors on the most recent retro_day.
    # Before the first retro_day has occurred for the current week, we don't
    # have a slot yet (the week's data isn't ready to review). Skip silently.
    anchor = _most_recent_retro_day_on_or_before(today, retro_day)
    if anchor > today:
        return
    current_slot = _iso_week_string(anchor)

    last_reviewed = _last_reviewed_week()
    # If we've already reviewed this slot (or a later one), no trigger needed.
    if last_reviewed and last_reviewed >= current_slot:
        return

    # Determine whether multiple unreviewed slots have piled up. We count
    # weeks between (last_reviewed exclusive, current_slot inclusive). For
    # cosmetic purposes only — the trigger fires once either way.
    slots_overdue: list[str] = [current_slot]
    if last_reviewed:
        # Walk back week-by-week from current_slot until last_reviewed.
        cursor = anchor
        while True:
            cursor = cursor - datetime.timedelta(days=7)
            slot = _iso_week_string(cursor)
            if slot <= last_reviewed:
                break
            slots_overdue.insert(0, slot)
            # Safety bound: stop after 12 weeks to avoid runaway messages.
            if len(slots_overdue) >= 12:
                break

    project_dir = os.path.dirname(get_project_file())
    triggers_dir = os.path.join(project_dir, "triggers")
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = os.path.join(triggers_dir, "retro.json")

    # Build the message — singular for one slot, multi for catchup.
    if len(slots_overdue) == 1:
        message = (
            f"今週の振り返りがまだです（{current_slot}）。"
            "/beacon-retro で開始しますか？"
        )
    else:
        weeks_str = ", ".join(slots_overdue)
        message = (
            f"振り返り未完の週が {len(slots_overdue)} 件あります（{weeks_str}）。"
            "/beacon-retro で順に消化できます。"
        )

    # Persist: rewrite the trigger every check so the message reflects the
    # current accumulation. created_at stays as the original first-fire date
    # if the trigger already exists, so users see "this has been pending
    # since YYYY-MM-DD".
    created_at = today.isoformat()
    if os.path.exists(trigger_path):
        try:
            with open(trigger_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing.get("created_at"), str):
                created_at = existing["created_at"]
        except (json.JSONDecodeError, IOError, UnicodeDecodeError):
            # #21 follow-up: legacy retro trigger from pre-fix builds is cp932
            # — skip and let the new write below replace it with UTF-8.
            pass

    trigger_data = {
        "name": "retro",
        "kind": "retro-due",
        "current_slot": current_slot,
        "overdue_slots": slots_overdue,
        "message": message,
        "created_at": created_at,
        "refreshed_at": today.isoformat(),
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


# release-due trigger thresholds (ms-52 e-958, SPEC 設計方針 2).
# feat-primary + fix-supplementary surfaces both "feature accumulation"
# and "fix accumulation" rhythms; see CORE doc rMlHx9n0LYFJ2kWIQELi.
_RELEASE_DUE_FEAT_THRESHOLD = 3
_RELEASE_DUE_FIX_THRESHOLD = 5


def _release_due_trigger_path() -> str:
    return os.path.join(_get_triggers_dir(), "release-due.json")


def _clear_release_due_trigger_if_exists() -> None:
    path = _release_due_trigger_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _auto_fire_release_due_trigger() -> None:
    """Surface a 'release-due' trigger when feat / fix commits since the
    last v* tag cross the threshold defined in CORE doc rMlHx9n0LYFJ2kWIQELi.

    Trigger lifecycle:
      - If repo has no v* tag yet, skip (no baseline to compare against).
      - If commits since tag fall below threshold (e.g. after squash),
        clear any stale trigger and skip.
      - If a trigger already exists for the same since_tag, keep its
        created_at (so users see "this has been pending since YYYY-MM-DD")
        but refresh counts / next_version_hint / refreshed_at.
      - If a trigger exists but since_tag != current tag (a release happened),
        clear it so the rewrite below uses today's created_at.

    Failures (no git / no version_rules / IOError) degrade silently so
    trigger check stays usable even outside a git repo.
    """
    try:
        import version_rules
    except Exception:
        return

    project_dir = os.path.dirname(get_project_file())
    repo_root = os.path.dirname(project_dir) or "."

    try:
        current_tag = version_rules.get_current_tag(prefix="v", repo_path=repo_root)
    except Exception:
        return
    if not current_tag:
        # No baseline tag → no signal to compute. Don't fire (would be noisy
        # for fresh repos that haven't shipped anything yet).
        return

    rev_range = f"{current_tag}..HEAD"
    try:
        result = subprocess.run(
            ["git", "log", rev_range, "--pretty=format:%H%x00%s%x00%b%x1e"],
            capture_output=True, text=True, cwd=repo_root, check=False,
        )
    except (FileNotFoundError, OSError):
        return
    if result.returncode != 0:
        return
    log_output = result.stdout

    commits = []
    for entry in log_output.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x00")
        if len(parts) >= 2:
            subject = parts[1]
            body = parts[2] if len(parts) > 2 else ""
            full_message = subject + ("\n\n" + body if body else "")
            commits.append({"hash": parts[0][:7], "message": full_message})

    if not commits:
        _clear_release_due_trigger_if_exists()
        return

    try:
        info = version_rules.propose_next_version(
            commits, axis="push", repo_path=repo_root
        )
    except Exception:
        return
    counts = info.get("counts", {})
    feat = int(counts.get("feat", 0) or 0)
    fix = int(counts.get("fix", 0) or 0)

    above_threshold = (
        feat >= _RELEASE_DUE_FEAT_THRESHOLD
        or fix >= _RELEASE_DUE_FIX_THRESHOLD
    )
    if not above_threshold:
        _clear_release_due_trigger_if_exists()
        return

    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = _release_due_trigger_path()

    existing = None
    if os.path.exists(trigger_path):
        try:
            with open(trigger_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError, UnicodeDecodeError):
            existing = None

    import datetime
    now_iso = datetime.datetime.now().isoformat()
    # Preserve created_at only if the existing trigger references the same
    # baseline tag (otherwise a release happened and the next "since" date
    # should reset).
    if existing and existing.get("since_tag") == current_tag:
        created_at = existing.get("created_at", now_iso)
    else:
        created_at = now_iso

    next_version = info.get("next") or ""
    summary_parts = []
    if feat:
        summary_parts.append(f"feat {feat} 件")
    if fix:
        summary_parts.append(f"fix {fix} 件")
    summary = " / ".join(summary_parts) if summary_parts else f"{len(commits)} 件"

    hint_path = "`gh workflow run release.yml -f dry_run=true` または `/beacon-push`"
    message = (
        f"前回 release {current_tag} 以降 {summary} が積み上がっています "
        f"(合計 {len(commits)} commits)。次バージョン候補: {next_version}。"
        f"{hint_path} で release.yml 経路の起動を検討してください "
        f"(CORE doc rMlHx9n0LYFJ2kWIQELi: 5 配信チャネル整合の原則)。"
    )

    trigger_data = {
        "name": "release-due",
        "kind": "release-due",
        "since_tag": current_tag,
        "next_version_hint": next_version,
        "feat_count": feat,
        "fix_count": fix,
        "commit_count": len(commits),
        "message": message,
        "created_at": created_at,
        "refreshed_at": now_iso,
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


# map-drift trigger (ms-104 e-3155, re-keyed to release count in e-3342).
#
# Backstop that surfaces when the application-map CORE doc (= 全貌マップ /
# 現在地の surface 索引) has gone stale RELATIVE TO SHIPPING. The PRIMARY forcing
# function to reconcile the map lives at the ship boundary itself — the
# /beacon-deploy and /beacon-push (release 判定) Skills prompt a reconcile in the
# same flow that generates the release note (e-3342). This trigger is only the
# low-priority safety net for when a project ships without reconciling.
#
# Why release count, not commit count (e-3342): the old baseline was "N commits
# since the map was last updated", which is decoupled from shipping. It fired at
# session-start on ordinary dev activity (unrelated to surfaces going to the
# world) and was easily ignored — it once drifted 36→54 commits before anyone
# acted. The application-map is a cumulative *shipped-capability* index; a
# release is the diff. So staleness is correctly measured as "surfaces shipped
# (= releases recorded) since the map was last reconciled", which is exactly the
# unit the map tracks. A project that hasn't shipped since the last reconcile has
# a fresh map no matter how many WIP commits accrued, so this no longer nags
# mid-development.
#
# General mechanism: fires for ANY project that has an application-map doc; the
# actual add/remove reconcile is /beacon-map's job (which uses a per-project
# surface adapter such as scripts/check-map-drift.py when present). Mirrors
# release-due: the count prompts a human/AI action, it does not act on its own.
_MAP_DRIFT_RELEASE_THRESHOLD = 1


def _map_drift_trigger_path() -> str:
    return os.path.join(_get_triggers_dir(), "map-drift.json")


def _clear_map_drift_trigger_if_exists() -> None:
    path = _map_drift_trigger_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _count_releases_since(data: dict, since_iso: str) -> int:
    """Count release records (data["releases"]) recorded strictly after
    ``since_iso`` (an ISO8601 timestamp, typically the application-map doc's
    updated_at). Release records carry a ``date`` field written by
    cmd_deploy_record when a --semver is passed (see _next_release_id path).

    A release is the ship diff; counting releases since the map was last
    reconciled measures exactly the staleness the map cares about (e-3342).
    String comparison is safe here because both sides are ISO8601 (lexical
    order == chronological order); a malformed/absent date sorts before any
    real ``since_iso`` and is simply not counted.
    """
    n = 0
    for rel in data.get("releases", []):
        rel_date = str(rel.get("date") or "")
        if rel_date and rel_date > since_iso:
            n += 1
    return n


def _auto_fire_map_drift_trigger() -> None:
    """Fire a 'map-drift' trigger when >= _MAP_DRIFT_RELEASE_THRESHOLD releases
    have been recorded since the application-map doc was last updated.

    Re-keyed from commit count to RELEASE count in e-3342: staleness that
    matters is "surfaces shipped since the map was last reconciled", not raw dev
    activity. See the module comment above _MAP_DRIFT_RELEASE_THRESHOLD for the
    full rationale. This is the low-priority backstop; the primary reconcile
    forcing function is in the /beacon-deploy and /beacon-push (release 判定)
    Skills, which prompt a reconcile in the same flow that ships.

    Fires only when the map EXISTS; a missing map is the session-start
    proposal's job (ms-104 e-3153), not this backstop's. Baseline = the map
    doc's updated_at (refreshed whenever /beacon-map rewrites it), so no
    separate marker file is needed — reconciling the map moves updated_at past
    the shipped releases and the trigger self-clears. Degrades silently with no
    store / no map.

    Development-only: a non-dev project (e.g. sales) owns no application-map, so
    the backstop never fires and clears any stale trigger (ms-109 e-3404).
    """
    if not _application_map_applies():
        _clear_map_drift_trigger_if_exists()
        return
    try:
        store = get_store()
        doc = store.get_document("application-map")
    except Exception:
        return
    if not doc:
        _clear_map_drift_trigger_if_exists()
        return
    updated_at = str(doc.get("updated_at") or "")
    if not updated_at:
        return

    try:
        data = store.load_project()
    except Exception:
        return
    n = _count_releases_since(data, updated_at)

    if n < _MAP_DRIFT_RELEASE_THRESHOLD:
        _clear_map_drift_trigger_if_exists()
        return

    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = _map_drift_trigger_path()

    existing = None
    if os.path.exists(trigger_path):
        try:
            with open(trigger_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError, UnicodeDecodeError):
            existing = None

    import datetime
    now_iso = datetime.datetime.now().isoformat()
    # Preserve created_at while the baseline (map updated_at) is unchanged;
    # once /beacon-map rewrites the map, updated_at moves and "since" resets.
    if existing and existing.get("map_updated_at") == updated_at:
        created_at = existing.get("created_at", now_iso)
    else:
        created_at = now_iso

    message = (
        f"全貌マップ (application-map) の最終更新以降に {n} 件のリリースが出荷されています。"
        f"出荷時に surface (= 機能の入口) が増減して地図が古い可能性があります。"
        f"本来は出荷フロー (/beacon-deploy・/beacon-push) で reconcile を促しますが、"
        f"取りこぼした場合の安全網です。`/beacon-map` で reconcile (= 足す＆消す) してください。"
    )
    trigger_data = {
        "name": "map-drift",
        "kind": "map-drift",
        "map_updated_at": updated_at,
        "release_count": n,
        "message": message,
        "created_at": created_at,
        "refreshed_at": now_iso,
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


# untriaged-backlog trigger (ms-126). Surfaces the debt created by the new
# untriaged sentinel: entries a machine created without a human-judged priority.
# We count only *active* work (todo / in_progress MS + task) whose priority is
# the explicit ``untriaged`` sentinel — NOT legacy entries that merely lack a
# priority field. That legacy-exclusion is deliberate: retro-fitting a debt
# count onto pre-ms-126 data would flood the trigger with entries no one chose
# to leave untriaged (and would violate the no-backfill rule).
_UNTRIAGED_ACTIVE_STATUSES = {"todo", "in_progress"}


def _untriaged_backlog_trigger_path() -> str:
    return os.path.join(_get_triggers_dir(), "untriaged-backlog.json")


def _clear_untriaged_backlog_trigger_if_exists() -> None:
    path = _untriaged_backlog_trigger_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _count_untriaged_active(data: dict) -> tuple[int, int]:
    """Count active entries carrying the explicit ``untriaged`` sentinel.

    Returns ``(ms_count, task_count)``. Only ``todo`` / ``in_progress``
    milestones and tasks are considered (finished / observing / cancelled work
    is not actionable debt). A milestone's priority lives on ``ms["priority"]``;
    a task's on ``entry["meta"]["priority"]`` (see core.task_add). Legacy
    entries with no priority field are NOT counted (no-backfill).
    """
    # ms-126 (Maint#2): read the sentinel from its single source of truth. No
    # fallback literal — commands.py already depends on ``core`` module-wide, so
    # a duplicated "untriaged" constant would only silently diverge if core's
    # value ever changed.
    _UNTRIAGED = core.UNTRIAGED_PRIORITY
    ms_count = 0
    task_count = 0
    for ms in data.get("milestones", []):
        if ms.get("status") in _UNTRIAGED_ACTIVE_STATUSES:
            if ms.get("priority") == _UNTRIAGED:
                ms_count += 1
        for entry in ms.get("entries", []):
            if entry.get("type") != "task":
                continue
            if entry.get("status") not in _UNTRIAGED_ACTIVE_STATUSES:
                continue
            if (entry.get("meta") or {}).get("priority") == _UNTRIAGED:
                task_count += 1
    return ms_count, task_count


def _auto_fire_untriaged_backlog_trigger() -> None:
    """Fire an 'untriaged-backlog' trigger when active entries carry the
    ``untriaged`` sentinel (ms-126).

    Mirrors the map-drift / release-due backstops: it only surfaces the count,
    it never mutates priorities on its own (that is a human judgement). Cleared
    when the count drops to zero. Degrades silently if the store is unavailable.
    """
    try:
        data = get_store().load_project()
    except Exception:
        return
    ms_count, task_count = _count_untriaged_active(data)
    total = ms_count + task_count
    if total <= 0:
        _clear_untriaged_backlog_trigger_if_exists()
        return

    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = _untriaged_backlog_trigger_path()

    existing = None
    if os.path.exists(trigger_path):
        try:
            with open(trigger_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError, UnicodeDecodeError):
            existing = None

    import datetime
    now_iso = datetime.datetime.now().isoformat()
    # Keep the original created_at so the user sees how long the debt has been
    # pending; only reset it if there was no prior trigger.
    created_at = (existing or {}).get("created_at", now_iso)

    parts = []
    if ms_count:
        parts.append(f"マイルストーン {ms_count} 件")
    if task_count:
        parts.append(f"タスク {task_count} 件")
    breakdown = " / ".join(parts)
    message = (
        f"優先度が未 triage (= 機械が起票し人がまだ優先度を判断していない) の "
        f"active な項目が {total} 件あります ({breakdown})。"
        f"`beacon status` で確認し、`beacon milestone update <id> --priority <P>` / "
        f"`beacon task update <id> --priority <P>` で 5 段階 "
        f"(highest / high / medium / low / lowest) のいずれかを付けてください。"
    )
    trigger_data = {
        "name": "untriaged-backlog",
        "kind": "untriaged-backlog",
        "ms_count": ms_count,
        "task_count": task_count,
        "total": total,
        "message": message,
        "created_at": created_at,
        "refreshed_at": now_iso,
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def _count_commits_between_tags(repo_root: str, prev_tag: str, tag: str) -> int:
    """Count commits between `prev_tag..tag`. If prev_tag is empty, count
    commits up to and including `tag`. Returns 0 on any git failure."""
    try:
        if prev_tag:
            rev_range = f"{prev_tag}..{tag}"
        else:
            rev_range = tag
        result = subprocess.run(
            ["git", "log", rev_range, "--oneline"],
            capture_output=True, text=True, cwd=repo_root, check=False,
        )
        if result.returncode != 0:
            return 0
        return len([line for line in result.stdout.splitlines() if line.strip()])
    except (FileNotFoundError, OSError):
        return 0


def _previous_v_tag(repo_root: str, tag: str) -> str:
    """Return the v* tag immediately preceding `tag`, or '' if none."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*", "--sort=v:refname"],
            capture_output=True, text=True, cwd=repo_root, check=False,
        )
        if result.returncode != 0:
            return ""
        prev = ""
        for line in result.stdout.splitlines():
            t = line.strip()
            if not t:
                continue
            if t == tag:
                return prev
            prev = t
        return prev  # tag not found — return last seen as best effort
    except (FileNotFoundError, OSError):
        return ""


def _auto_fire_release_marker_trigger() -> None:
    """Surface a 'release-<version>' trigger for the latest v* tag if not
    already present locally (ms-52 e-952).

    Rationale: release.yml runs in GitHub Actions and fires
    `beacon trigger fire release-X.Y.Z` against the *ephemeral CI runner*
    filesystem, so the maintainer's local cloud-mode session never sees
    the trigger. Instead we derive it from git state (which IS synced via
    `git pull`): if the latest v* tag has no matching local trigger file,
    create one with the same payload release.py would have written
    (commit count + /discord-post hint).

    Lifecycle: this fires only for the *latest* tag. Older missed releases
    require explicit `beacon trigger fire release-vX.Y.Z "<message>"` (AC 3
    listed this catchup as optional). The trigger file persists until the
    user explicitly clears it (the standard release-marker contract).

    Silent failures: any git / version_rules error -> no-op.
    """
    try:
        import version_rules
    except Exception:
        return

    project_dir = os.path.dirname(get_project_file())
    repo_root = os.path.dirname(project_dir) or "."

    try:
        latest_tag = version_rules.get_current_tag(prefix="v", repo_path=repo_root)
    except Exception:
        return
    if not latest_tag:
        return

    # Use the bare version (without leading 'v') in the trigger name to match
    # the convention scripts/release.py established (release-0.8.0, not
    # release-v0.8.0). Trigger payload keeps the full tag for clarity.
    version_str = latest_tag[1:] if latest_tag.startswith("v") else latest_tag
    trigger_name = f"release-{version_str}"

    triggers_dir = _get_triggers_dir()
    trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
    if os.path.exists(trigger_path):
        return  # Already fired (locally or via this auto-derivation earlier).

    prev_tag = _previous_v_tag(repo_root, latest_tag)
    commit_count = _count_commits_between_tags(repo_root, prev_tag, latest_tag)

    os.makedirs(triggers_dir, exist_ok=True)
    import datetime
    message = (
        f"beacon {latest_tag} released ({commit_count} commits). "
        f"Use /discord-post to share."
    )
    trigger_data = {
        "name": trigger_name,
        "kind": "release-marker",
        "tag": latest_tag,
        "previous_tag": prev_tag,
        "commit_count": commit_count,
        "message": message,
        "created_at": datetime.datetime.now().isoformat(),
        "source": "auto-from-tag",
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def _auto_fire_pr_open_review_triggers_for_open_prs() -> None:
    """Anchor firing to the real PR-open event (ms-119 e-4060): scan GitHub's
    OPEN PRs and fire the pr-open review-due triggers for any that lack them.

    Fixes the path-dependence hole — `_fire_pr_open_review_triggers` only ran
    from `beacon pr add/create`, so a PR opened via `gh pr create` / the GitHub
    UI never fired its review-due and slipped past the gate. Running here (in
    `trigger tick`, which session-start / log call) means every open PR ends up
    gated regardless of how it was opened. Best-effort: any gh / parse failure
    is swallowed so a tick never fails over this."""
    # Fast skip: under test (no live GitHub) or when gh is not installed, do
    # nothing — the gate still works for beacon-pr-add-fired triggers; the
    # anchor only adds coverage for gh-direct / UI PRs on real machines.
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("BEACON_TEST_MODE"):
        return
    if not shutil.which("gh"):
        return
    try:
        import subprocess
        r = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--json",
             "number,title,url"],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            return
        open_prs = json.loads(r.stdout)
    except Exception:
        return
    for pr in open_prs:
        num = str(pr.get("number") or "").strip()
        if not num:
            continue
        # Only fire types not already present (don't clobber a running review's
        # cleared state — a cleared trigger means "reviewed", must NOT re-fire).
        # We detect "already handled" by the presence OR a done-marker; here we
        # simply skip if the trigger file exists (present = still owed) and rely
        # on _fire being idempotent. To avoid re-firing a review already done,
        # only fire when NO review-due file for this PR exists at all yet (fresh
        # PR). A PR mid-review keeps its remaining triggers from the first fire.
        try:
            existing = os.listdir(_get_triggers_dir())
        except OSError:
            existing = []
        if any(fn.endswith(f"{_REVIEW_DUE_SUFFIX}{num}.json") for fn in existing):
            continue
        # Fresh open PR with no review-due yet → fire the pr-open set.
        if _pr_open_reviewed_marker_exists(num):
            continue
        _fire_pr_open_review_triggers(num, pr.get("title") or "",
                                      pr.get("url") or "")


def _pr_open_reviewed_marker_exists(pr_number: str) -> bool:
    """True when this PR's reviews were already run once (a done-marker exists),
    so the anchor must NOT re-fire review-due for it. `beacon review done`
    stamps this marker the first time all of a PR's reviews clear."""
    return os.path.exists(_pr_open_reviewed_marker_path(pr_number))


def _cleanup_spec_needed_triggers() -> None:
    """Remove spec-needed triggers for milestones that now have SPEC docs,
    or for milestones that no longer exist (cancelled/deleted)."""
    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        return
    try:
        data = load_project()
    except Exception:
        return
    valid_ms_ids = {ms.get("id") for ms in data.get("milestones", [])
                    if ms.get("status") not in ("cancelled",)}
    for fname in os.listdir(triggers_dir):
        if not fname.startswith("spec-needed-"):
            continue
        if not fname.endswith(".json"):
            continue
        ms_id = fname[len("spec-needed-"):-len(".json")]
        # Cleared if MS gone, or SPEC now exists for it
        if ms_id not in valid_ms_ids or _spec_exists_for_ms(ms_id):
            try:
                os.remove(os.path.join(triggers_dir, fname))
            except OSError:
                pass


def _cleanup_review_due_triggers() -> None:
    """Remove review-due triggers once they no longer represent a live 節目
    (ms-119 / e-3911).

    A review nudge is time-sensitive — "you just completed X, review it now".
    Cleared when:
      - the target no longer exists,
      - (gated = went through the approval gate) the pending approval was
        resolved (approved / rejected), so the review is no longer in-flight,
      - (non-gated) the completion was undone (target re-opened), OR the nudge
        has aged past the day it fired (bounds indefinite accumulation, matches
        the moment-in-time nature of a 節目 reminder).
    """
    import datetime
    today = datetime.date.today()
    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        return
    try:
        data = load_project()
    except Exception:
        return
    status_by_id = {}
    pending_targets = set()
    for c in list(data.get("milestones", [])) + list(data.get("operations", [])):
        status_by_id[c.get("id")] = c.get("status")
        for e in c.get("entries", []):
            if (e.get("type") == "target-transition-approval"
                    and e.get("meta", {}).get("approval_status") == "pending"):
                pending_targets.add(e.get("meta", {}).get("target_id"))
    for fname in os.listdir(triggers_dir):
        if not fname.startswith("review-due-") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(triggers_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                trig = json.load(f)
        except (json.JSONDecodeError, IOError, UnicodeDecodeError):
            continue
        tid = trig.get("target_id")
        remove = False
        if tid not in status_by_id:
            remove = True  # target gone
        elif trig.get("gated"):
            if tid not in pending_targets:
                remove = True  # approval resolved
        else:
            try:
                aged = datetime.date.fromisoformat(
                    trig.get("created_at", "")[:10]) < today
            except ValueError:
                aged = False
            if status_by_id.get(tid) not in ("done", "closed") or aged:
                remove = True  # completion undone, or nudge aged out
        if remove:
            try:
                os.remove(fpath)
            except OSError:
                pass


def _cleanup_stale_triggers():
    import datetime
    today = datetime.date.today()
    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        return
    # Clean up stale spec-needed triggers (MS gone or SPEC now exists)
    _cleanup_spec_needed_triggers()
    # ms-119 e-3911: clear review-due nudges that no longer represent a live 節目
    _cleanup_review_due_triggers()
    # NOTE (e-575): Do NOT auto-delete retro.json by age. The retro trigger now
    # persists across days until `beacon retro done` (cmd_retro_done) explicitly
    # removes it. _auto_fire_retro_trigger refreshes the message daily so the
    # "overdue weeks" list stays accurate; deletion solely on age would defeat
    # the persistence requirement of UC5-J5'.
    # Clean up stale operation_check triggers
    for fname in os.listdir(triggers_dir):
        if not fname.startswith("operation_check_") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(triggers_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                trigger = json.load(f)
            created = datetime.date.fromisoformat(trigger["created_at"][:10])
            if today > created:
                os.remove(fpath)
        except (json.JSONDecodeError, KeyError, ValueError, IOError):
            pass

    # ms-80 e-1829: release-marker triggers accumulate forever otherwise (e.g.
    # 28 markers from v0.8.0 〜 v0.38.1 observed in session-start). Keep only
    # the *latest* tag's marker (= matches current `version_rules.get_current_tag`),
    # delete older ones. The current marker is preserved for the "did I post
    # to Discord?" reminder (= release-due / release-marker pair).
    _cleanup_old_release_marker_triggers()


def _cleanup_old_release_marker_triggers():
    """Sweep release-marker triggers, keeping only the one matching the
    current git tag. Older markers accumulate on every release fire and
    have no signal value once a newer marker exists.

    Silent failures: any git / version_rules / IO error -> no-op.
    """
    try:
        import version_rules
    except Exception:
        return
    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        return

    project_dir = os.path.dirname(get_project_file())
    repo_root = os.path.dirname(project_dir) or "."
    try:
        latest_tag = version_rules.get_current_tag(prefix="v", repo_path=repo_root)
    except Exception:
        return
    if not latest_tag:
        return
    version_str = latest_tag[1:] if latest_tag.startswith("v") else latest_tag
    keep_name = f"release-{version_str}"

    for fname in os.listdir(triggers_dir):
        if not fname.startswith("release-") or not fname.endswith(".json"):
            continue
        # Skip release-due (= not a release-marker, different trigger kind)
        if fname == "release-due.json":
            continue
        # Keep the trigger for the current tag
        if fname == f"{keep_name}.json":
            continue
        fpath = os.path.join(triggers_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                trigger = json.load(f)
            if trigger.get("kind") != "release-marker":
                continue
            os.remove(fpath)
        except (json.JSONDecodeError, IOError, OSError, ValueError):
            pass


def _auto_fire_operation_triggers():
    import datetime
    today = datetime.date.today()
    day_abbr = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][today.weekday()]
    today_str = today.isoformat()
    try:
        data = load_project()
    except Exception:
        return
    triggers_dir = _get_triggers_dir()
    for op in data.get("operations", []):
        if op.get("status") != "open":
            continue
        # ms-152 e-5484: a PAUSED Operation's scheduled fire is suppressed — an operator
        # deliberately stopped this monitor (distinct from the tick's failure-backoff).
        # Read-only skip; `operation_resume` returns it to idle and re-enables firing.
        if core.operation_execution_phase(op) == core.EXECUTION_PHASE_PAUSED:
            continue
        op_id = op["id"]
        days = op.get("schedule", {}).get("days", [])
        if day_abbr not in days:
            continue
        has_today_run = any(
            e.get("type") == "run_record" and e.get("date", "").startswith(today_str)
            for e in op.get("entries", [])
        )
        if has_today_run:
            continue
        trigger_name = f"operation_check_{op_id}"
        trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
        if os.path.exists(trigger_path):
            continue
        log_source = op.get("log_source", op_id)
        # Find linked SPEC doc for this operation
        spec_ref = ""
        spec_doc_id = ""
        try:
            docs_dir = _get_docs_dir()
            if os.path.isdir(docs_dir):
                for fname in sorted(os.listdir(docs_dir)):
                    if not fname.endswith(".md"):
                        continue
                    doc = _read_local_doc(os.path.join(docs_dir, fname))
                    if doc.get("operation") == op_id and doc.get("scope") == "spec":
                        spec_doc_id = doc["doc_id"]
                        spec_ref = f" (doc: {spec_doc_id} 参照)"
                        break
        except Exception:
            pass
        os.makedirs(triggers_dir, exist_ok=True)
        trigger_data = {
            "name": trigger_name,
            "message": f"{op_id} ({log_source}) のバッチ確認が必要です{spec_ref}。/beacon-operation-review で記録してください。",
            "created_at": today_str,
        }
        with open(trigger_path, "w", encoding="utf-8") as f:
            json.dump(trigger_data, f, ensure_ascii=False)
            f.write("\n")
        # ms-95 / e-1668 + e-2350: cloud-side atomic claim before the bus
        # push. The local trigger file above is per-cwd UI state and
        # harmless when duplicated across cwds (= each cwd has its own
        # `.beacon/triggers/`); the bus push fanout is what would
        # N-multiply across parallel bclaude sessions without this gate.
        # The claim runs in a Firestore transaction server-side so only
        # one of N concurrent bclaude sessions per (project, op, date)
        # wins — losers skip the bus push.
        if not _claim_operation_fire_for_bus_push(op_id):
            continue
        # ms-60 / e-1340: also mirror onto the bus so AI sessions subscribed
        # via the inbox hook can autonomously run `/beacon-operation-execute`.
        # Channel "operation-trigger" + delivery="auto-execute" is the autonomous
        # path; the inbox hook downgrades to propose-to-ai unless the channel
        # is in bus_auto_execute_channels (opt-in).
        _push_operation_trigger_to_bus(op_id, log_source, trigger_data, spec_doc_id)


def _push_trigger_to_bus(trigger_data: dict) -> None:
    """Mirror a fired trigger onto the cloud bus as a propose-to-ai event.

    No-ops for local-mode projects (no cloud.json), missing credentials, or
    any cloud failure. The user-facing behavior of `beacon trigger fire`
    is unchanged on the failure path — bus is purely additive.
    """
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_id = config.get("project_id")
        if not project_id:
            return
        from auth import load_credentials  # local import — heavy module
        creds = load_credentials()
        if creds is None:
            return
        from api_client import ApiClient
        api_url = _resolve_active_api_url()
        client = ApiClient(api_url, _extract_token(creds))
        # Channel "trigger" is the convention for system-fired bus events,
        # distinct from "session-dm" used for agent-to-agent chat. Receivers
        # can filter on it for triage UI, or treat it like any inbox event.
        # sender_session_id is empty to mark a system-originated event so the
        # inbox hook can render "from=system" cleanly.
        client.post_bus_event(
            project_id, "trigger",
            sender_session_id="",
            payload={
                "trigger_name": trigger_data.get("name", ""),
                "message": trigger_data.get("message", ""),
                "created_at": trigger_data.get("created_at", ""),
            },
            delivery="propose-to-ai",
        )
    except Exception as exc:
        # stderr keeps the failure visible in BEACON_HOOK_DEBUG=1 traces
        # without polluting normal CLI output.
        sys.stderr.write(
            f"[beacon] trigger bus mirror failed silently: "
            f"{type(exc).__name__}: {exc}\n"
        )


def _resolve_operation_trigger_recipient(op_id: str) -> str:
    """Resolve the unicast recipient_session_id for an operation-trigger event.

    ms-76 / e-1860 / e-1604: operation-trigger default = unicast to a single
    claimer / owner session. Broadcast (= empty recipient → fan out to every
    live session in the project) is the LEGACY behaviour and is retained
    only as fallback when no claimer is registered. The CORE doc
    QvyVwRU8otQEn5iMfP36 (= AI 自律 action の envelope tier framework)
    section "構造的禁止" makes default broadcast a禁止帯; this resolver
    is the structural enforcement point.

    Resolution order (first hit wins):
      1. ``meta.claimer_session_id`` on the Operation (= explicit
         claim-based registration, e-1604). A session calls
         ``beacon operation claim <op-id>`` to register itself as the
         sole receiver; subsequent triggers route only here.
      2. ``meta.open_by`` on the Operation (= the session that opened
         the Operation, treated as default owner when no explicit claim).
      3. Empty string (= legacy broadcast). Best-effort fallback for
         pre-ms-76 projects that have no owner/claimer recorded.

    The ``BEACON_OPERATION_TRIGGER_BROADCAST=1`` env flag opts back into
    legacy broadcast explicitly (= for SPECs that legitimately want all
    sessions notified, e.g. a release announcement trigger). This is the
    "explicit opt-in pattern" from ms-76 SPEC EuLwGrAawmMzeKYsxkrd
    設計方針 7.
    """
    # Explicit broadcast opt-in (= rare, only when SPEC declares it).
    if os.environ.get("BEACON_OPERATION_TRIGGER_BROADCAST", "") == "1":
        return ""
    try:
        data = load_project()
    except Exception:
        return ""
    for op in data.get("operations", []):
        if op.get("id") != op_id:
            continue
        meta = op.get("meta", {}) or {}
        claimer = (meta.get("claimer_session_id") or "").strip()
        if claimer:
            return claimer
        owner = (meta.get("open_by") or "").strip()
        if owner:
            return owner
        break
    return ""


def _claim_operation_fire_for_bus_push(op_id: str) -> bool:
    """Return True iff this CLI should post the operation-trigger bus event.

    Cloud mode: call ``POST /api/projects/<pid>/operation-fires/<op>/claim``
    which uses a Firestore transaction to dedup across parallel bclaude
    sessions in the same project. Server returns ``{claimed: True}`` for
    the first caller per ``(project, op, today)`` and ``{claimed: False}``
    for the rest. The losers skip ``_push_operation_trigger_to_bus`` so
    only one operation-trigger event lands on the bus per day.

    Local mode (no ``cloud.json``): always returns True. Without the
    cloud, there is no cross-cwd race surface — each project root has its
    own ``.beacon/triggers/`` and the per-cwd file gate above prevents
    intra-cwd duplicates.

    Failure-open policy (= ms-95 SPEC §設計方針 1 fail-open):
    on network / auth / missing config errors, return True. The cost of
    fail-open is a duplicate operation-trigger event (= cheap, the inbox
    hook is idempotent on op_id + date). The cost of fail-close is a
    silently missed daily Operation fire (= user-facing ritual broken
    until they notice). Daily Operations are part of the SPEC contract
    that "approved ops fire reliably"; a transient cloud hiccup must
    not turn into silently lost fires.

    ms-95 / e-1668 (= N-multiplied fires across parallel bclaude) and
    e-2350 (= 4-6 min retrigger storms when local CLI run_record landed
    but cloud sync lag hides it from the next scheduler tick) collapse
    into a single root cause: scheduler decision based on a
    locally-fetched view of cloud state. Pushing the decision to the
    server transaction layer makes the dedup atomic.
    """
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return True
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_id = config.get("project_id")
        if not project_id:
            return True
        from auth import load_credentials
        creds = load_credentials()
        if creds is None:
            return True
        from api_client import ApiClient
        api_url = _resolve_active_api_url()
        client = ApiClient(api_url, _extract_token(creds))
        session_id = ""
        try:
            import session as _session
            session_id = _session.get_session_id() or ""
        except Exception:
            pass
        result = client.claim_operation_fire(project_id, op_id, session_id)
        return bool(result.get("claimed", True))
    except Exception as exc:
        # ms-98 / e-2781: narrow the fail-open policy. The original
        # rationale for "return True on any error" was to avoid silently
        # losing a scheduled operation fire during a transient network
        # blip. But on 2026-07-02 that same policy amplified the 429
        # storm: every parallel bclaude session that failed the claim
        # STILL pushed a bus event, adding to the server's load. Two
        # classes of failure are now split:
        #
        #   * Rate-limit family (429 in the exception message, or the
        #     e-2777 circuit breaker returning "circuit open"). Treat as
        #     "not this tick" — return False so the caller skips the bus
        #     push. The scheduler's next tick will retry the claim and
        #     the fire lands as long as the storm has cleared. This
        #     stops the client from adding to the storm.
        #
        #   * Everything else (auth misconfig, config missing, generic
        #     network failure, malformed response, etc.) — keep the
        #     original fail-open so a genuine transient issue still
        #     doesn't drop a fire.
        msg = str(exc)
        rate_limited = "429" in msg or "circuit open" in msg.lower()
        if rate_limited:
            sys.stderr.write(
                f"[beacon] operation fire claim skipped (rate-limited; "
                f"will retry next tick): {type(exc).__name__}: {exc}\n"
            )
            return False
        sys.stderr.write(
            f"[beacon] operation fire claim failed (fail-open, "
            f"may duplicate-fire): {type(exc).__name__}: {exc}\n"
        )
        return True


def _push_operation_trigger_to_bus(op_id: str, log_source: str,
                                   trigger_data: dict,
                                   spec_doc_id: str = "") -> None:
    """Mirror an operation-check trigger onto the bus (ms-60 / e-1340).

    Distinct from ``_push_trigger_to_bus`` because operation triggers carry
    structured fields the autonomous Skill needs (op_id, spec_doc_id), and
    they ride a dedicated channel so the auto-execute allowlist can opt in
    to operation autonomy without arming every other trigger source.

    Channel ``"operation-trigger"`` + delivery ``"auto-execute"`` means: a
    project that has opted in (``beacon bus auto-execute add --channel
    operation-trigger``) lets the inbox hook run ``/beacon-operation-execute``
    without human review. Without opt-in, the event is downgraded to
    ``propose-to-ai`` and surfaces in the AI inbox for human review.

    The post carries a T2 Operation-scope envelope so the server-side verify
    pipeline (e-1155) preserves the auto-execute delivery. Without it the
    server treats the post as legacy (effective_tier=T5) and degrades
    delivery to notify-user-only, which never injects into AI context —
    silently breaking the autonomous loop (e-1393).

    ms-76 / e-1860 / e-1604: payload now carries ``recipient_session_id``
    by default (= unicast to the registered claimer/owner). Legacy broadcast
    is retained only as fallback when no claimer is registered, or when
    ``BEACON_OPERATION_TRIGGER_BROADCAST=1`` is set for SPECs that
    explicitly opt into broadcast.

    Best-effort — failures don't break the local trigger file write.
    """
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_id = config.get("project_id")
        if not project_id:
            return
        from auth import load_credentials
        creds = load_credentials()
        if creds is None:
            return
        from api_client import ApiClient
        api_url = _resolve_active_api_url()
        client = ApiClient(api_url, _extract_token(creds))

        # e-1393: mint a T2 Operation-scope envelope so the server's verify
        # pipeline keeps `delivery="auto-execute"` instead of degrading to
        # `notify-user-only`. Without an envelope the post is treated as
        # legacy (effective_tier=T5) and `decide_delivery` caps auto-execute
        # at notify-user-only, which the inbox hook never injects — breaking
        # the autonomous loop silently. T2 is the right tier because the
        # trigger fire is scoped to a single op_id and is initiated by the
        # autofire system (not a human typing → not T1).
        envelope_obj = None
        try:
            envelope_obj = client.issue_bus_envelope(
                project_id,
                tier="T2",
                actions_authorized=["operation.trigger.fire"],
                scope=f"op:{op_id}",
                data_class="free",
            )
        except Exception as env_exc:
            sys.stderr.write(
                f"[beacon] operation-trigger envelope mint failed "
                f"({type(env_exc).__name__}: {env_exc}); posting without "
                "envelope — delivery will be degraded to notify-user-only.\n"
            )

        # ms-76 / e-1860 / e-1604: stamp the unicast recipient. Empty string
        # falls through to legacy broadcast (= fan out to every session)
        # only when no claimer/owner is registered and broadcast was not
        # explicitly opted into. The server's /bus/unread filter
        # (server/app.py _bus_event_addressed_to) honours the field for
        # all channels except dm; for operation-trigger an empty recipient
        # behaves as legacy fan-out so back-compat is preserved.
        recipient_session_id = _resolve_operation_trigger_recipient(op_id)
        payload = {
            "op_id": op_id,
            "log_source": log_source,
            "spec_doc_id": spec_doc_id,
            "trigger_name": trigger_data.get("name", ""),
            "message": trigger_data.get("message", ""),
            "created_at": trigger_data.get("created_at", ""),
        }
        if recipient_session_id:
            payload["recipient_session_id"] = recipient_session_id
        client.post_bus_event(
            project_id, "operation-trigger",
            sender_session_id="",
            payload=payload,
            delivery="auto-execute",
            envelope=envelope_obj,
        )
    except Exception as exc:
        sys.stderr.write(
            f"[beacon] operation-trigger bus mirror failed silently: "
            f"{type(exc).__name__}: {exc}\n"
        )


def _push_trek_trigger_to_bus(trek_id: str, trigger_data: dict) -> None:
    """Mirror a trek-scoped trigger onto the bus (ms-75 / e-1870).

    Twin of ``_push_operation_trigger_to_bus`` for Trek scope. Posts on the
    ``trek-trigger`` channel with ``delivery=auto-execute`` and a T2 Trek-
    scope envelope. Sessions opted in via
    ``beacon bus auto-execute add --channel trek-trigger`` see the event
    routed by the inbox hook into a structured "TREK ACTION" block that
    launches ``/beacon-trek-execute <trek-id>`` without a confirmation
    prompt. Without opt-in the event is downgraded to ``propose-to-ai``
    (= safe fallback, user reviews before launching).

    Why a dedicated channel:
    - operation-trigger is scoped to one ``op_id`` (= a single periodic
      check). Trek-trigger is scoped to a ``trek_id`` (= an entire
      workspace of MS / task / Operation). Sharing one channel would force
      every receiver to inspect the payload before they can decide whether
      to act.
    - Opt-in is per-channel. A project that auto-executes Operations
      may still want to keep Trek autonomy gated behind manual review
      until it has dogfooded the trek-execute Skill once.

    Best-effort: failures don't break the local trigger file write.
    """
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_id = config.get("project_id")
        if not project_id:
            return
        from auth import load_credentials
        creds = load_credentials()
        if creds is None:
            return
        from api_client import ApiClient
        api_url = _resolve_active_api_url()
        client = ApiClient(api_url, _extract_token(creds))

        # Mint a T2 Trek-scope envelope so the server's verify pipeline
        # keeps ``delivery=auto-execute`` instead of degrading it to
        # ``notify-user-only`` (same gate as e-1393 for operations).
        # ``scope=trek:<trek-id>`` mirrors the ``op:<op-id>`` convention
        # used by operation-trigger so the audit log carries the trek id
        # in a uniform shape.
        envelope_obj = None
        try:
            envelope_obj = client.issue_bus_envelope(
                project_id,
                tier="T2",
                actions_authorized=["trek.trigger.fire"],
                scope=f"trek:{trek_id}",
                data_class="free",
            )
        except Exception as env_exc:
            sys.stderr.write(
                f"[beacon] trek-trigger envelope mint failed "
                f"({type(env_exc).__name__}: {env_exc}); posting without "
                "envelope — delivery will be degraded to notify-user-only.\n"
            )

        client.post_bus_event(
            project_id, "trek-trigger",
            sender_session_id="",
            payload={
                "trek_id": trek_id,
                "trigger_name": trigger_data.get("name", ""),
                "message": trigger_data.get("message", ""),
                "created_at": trigger_data.get("created_at", ""),
            },
            delivery="auto-execute",
            envelope=envelope_obj,
        )
    except Exception as exc:
        sys.stderr.write(
            f"[beacon] trek-trigger bus mirror failed silently: "
            f"{type(exc).__name__}: {exc}\n"
        )


_TRIGGER_TICK_TTL_SECONDS = 300  # 5 minutes; overridable via BEACON_TRIGGER_TICK_TTL


def _trigger_tick_stamp_path() -> str:
    return os.path.join(_get_triggers_dir(), ".last_tick_at")


def _trigger_tick_lock_path() -> str:
    return os.path.join(_get_triggers_dir(), ".tick.lock")


def _trigger_tick_ttl_seconds() -> int:
    raw = os.environ.get("BEACON_TRIGGER_TICK_TTL", "")
    if raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _TRIGGER_TICK_TTL_SECONDS


def _maybe_auto_tick(*, verbose: bool = False) -> bool:
    """Run auto-fire pipeline iff the last tick is older than the TTL and no
    other process holds the lock.

    Returns True if this call actually ran the tick. Non-blocking — if a
    peer is already ticking (lock held) or the stamp is fresh, this
    returns False without doing any server-touching work.

    Silent failure by design: the throttle gate must never block
    ``beacon trigger check``; the user's local read is the guaranteed
    contract, freshness is best-effort.
    """
    try:
        triggers_dir = _get_triggers_dir()
        os.makedirs(triggers_dir, exist_ok=True)
    except OSError:
        return False

    ttl = _trigger_tick_ttl_seconds()
    if ttl <= 0:
        return False

    stamp = _trigger_tick_stamp_path()
    now = __import__("time").time()
    try:
        if os.path.exists(stamp) and (now - os.path.getmtime(stamp)) < ttl:
            return False  # fresh enough
    except OSError:
        pass  # stamp unreadable — treat as stale and try to tick

    lock = _trigger_tick_lock_path()
    # Atomic acquire: O_EXCL fails if another process is mid-tick. Never
    # block on the lock — the peer's tick will refresh the stamp for us.
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False  # peer is ticking; skip

    try:
        _auto_fire_retro_trigger()
        _auto_fire_operation_triggers()
        _auto_fire_release_due_trigger()
        _auto_fire_release_marker_trigger()
        _auto_fire_map_drift_trigger()
        _auto_fire_untriaged_backlog_trigger()  # ms-126
        _cleanup_stale_triggers()
        # Touch stamp so subsequent checks within TTL skip the tick.
        try:
            with open(stamp, "w", encoding="utf-8") as f:
                f.write(str(now))
        except OSError:
            pass
        if verbose:
            sys.stderr.write("[beacon] trigger tick ran (auto)\n")
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(lock)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public command handlers
# ---------------------------------------------------------------------------


def cmd_trigger_fire():
    trigger_name = os.environ.get("BEACON_TRIGGER_NAME", "")
    trigger_message = os.environ.get("BEACON_TRIGGER_MESSAGE", "")
    # ms-75 / e-1870: Trek-aware trigger. When a trek-id is supplied
    # (= via `beacon trigger fire --trek <id> <name> [msg]`), the trigger
    # rides a dedicated ``trek-trigger`` channel on the bus with
    # ``delivery=auto-execute`` so opted-in sessions can run
    # ``/beacon-trek-execute`` autonomously — twin of the
    # ``operation-trigger`` path. Without --trek the legacy
    # ``trigger`` channel + ``propose-to-ai`` delivery is unchanged.
    trek_id = os.environ.get("BEACON_TRIGGER_TREK_ID", "").strip()
    if not trigger_name:
        print("Error: trigger name required")
        sys.exit(1)
    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
    if os.path.exists(trigger_path):
        return
    import datetime
    trigger_data = {
        "name": trigger_name,
        "message": trigger_message,
        "created_at": datetime.datetime.now().isoformat(),
    }
    # Persist trek_id on the on-disk trigger so `beacon trigger check`
    # readers (= session-start, dispatch) can detect a trek-scoped
    # trigger without re-querying the bus event.
    if trek_id:
        trigger_data["trek_id"] = trek_id
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")
    # ms-54 / e-1136: dogfood the bus by also posting the trigger as a bus
    # event. Every cloud-connected session subscribing through the inbox hook
    # will see it as a propose-to-ai event and the AI can decide what to do.
    #
    # The post is best-effort: a network failure, missing creds, or local-mode
    # project must NOT prevent the trigger file from being written. Triggers
    # are the existing single source of truth; the bus event is a propagation
    # layer on top. _push_trigger_to_bus / _push_trek_trigger_to_bus swallow
    # every error path to stderr.
    if trek_id:
        _push_trek_trigger_to_bus(trek_id, trigger_data)
    else:
        _push_trigger_to_bus(trigger_data)


def cmd_trigger_check():
    """Read ``.beacon/triggers/*.json`` and print as JSON.

    ms-98 / e-2764: previously this command **unconditionally** invoked
    ``_auto_fire_*`` and ``_cleanup_stale_triggers`` on every call. Both
    paths reach the cloud (``load_project`` / ``claim_operation_fire`` /
    bus push). Multiplied by every Skill Step that surfaced trigger
    state, this created a hidden hot path from ``beacon trigger check``
    to ``/api/me/heartbeat`` and
    ``/api/projects/*/operation-fires/*/claim``. Concurrent Skill fires
    piled up into the 2026-07-02 429 storm + 104-hung-process leak.

    Two structural changes tame the hot path while keeping the observable
    contract intact:

      1. Auto-fire and cleanup moved to :func:`cmd_trigger_tick`. Callers
         who explicitly want fresh state can run ``beacon trigger tick``
         and then ``beacon trigger check``.

      2. This function opportunistically ticks **only when** the local
         stamp file (``.beacon/triggers/.last_tick_at``) is older than
         ``BEACON_TRIGGER_TICK_TTL`` seconds (default 300). Concurrent
         checks are gated by an ``O_EXCL`` lock so at most one process
         per (project, TTL window) reaches the cloud. Callers who want
         to opt out entirely set ``BEACON_TRIGGER_TICK_TTL=0``.

    The net effect: default calls stay under a millisecond of local IO,
    freshness is refreshed at most once every 5 minutes per project, and
    a caller that wants freshness NOW can force it with an explicit
    ``beacon trigger tick``.
    """
    _maybe_auto_tick()

    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        print("[]")
        return
    triggers = []
    for fname in sorted(os.listdir(triggers_dir)):
        if not fname.endswith(".json"):
            continue
        if fname.startswith("."):
            # ms-98 / e-2764: internal stamp / lock files (.last_tick_at,
            # .tick.lock) live alongside the trigger payloads. Skip them
            # here so JSON parsing never touches them.
            continue
        fpath = os.path.join(triggers_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                triggers.append(json.load(f))
        except (json.JSONDecodeError, IOError, UnicodeDecodeError) as exc:
            # #21 follow-up: legacy trigger files written by pre-encoding-fix
            # builds on Windows are persisted in cp932 and fail UTF-8 decode.
            # We silently skip so `beacon trigger check` keeps working; the
            # bad file stays on disk until the user clears it explicitly
            # (or the firing code overwrites it with valid UTF-8).
            sys.stderr.write(
                f"[beacon] skipping malformed trigger file "
                f"{os.path.basename(fpath)}: {type(exc).__name__}\n"
            )
    print(json.dumps(triggers, ensure_ascii=False))


def cmd_trigger_tick():
    """Refresh trigger state — run the auto-fire + cleanup pipeline.

    ms-98 / e-2764: split out from ``cmd_trigger_check`` so the expensive
    server-touching path can be scheduled separately from the cheap local
    read. This function runs the four auto-fire evaluators (retro,
    operation, release-due, release-marker) and the stale-trigger cleanup,
    all of which may touch the cloud (via ``load_project`` /
    ``claim_operation_fire`` / bus push).

    Intended callers:
      * Skills that need to surface freshly-computed triggers to the user
        (session-start, log, push): call ``tick`` first, then ``check``.
      * Interactive users: ``beacon trigger tick`` on demand, or wire it
        into their editor / shell hook at a debounced cadence.

    Not intended for high-frequency hook fires. Each tick issues at least
    one authenticated API call (operation claim) plus a full project load.
    """
    _auto_fire_retro_trigger()
    _auto_fire_operation_triggers()
    _auto_fire_release_due_trigger()
    _auto_fire_release_marker_trigger()
    _auto_fire_map_drift_trigger()
    _auto_fire_untriaged_backlog_trigger()  # ms-126
    _auto_fire_pr_open_review_triggers_for_open_prs()  # ms-119 e-4060 anchor
    _cleanup_stale_triggers()
    # Touch the stamp so subsequent ``check`` calls within the TTL window
    # skip the auto-tick gate. Mirrors what ``_maybe_auto_tick`` writes on
    # its own successful path.
    try:
        stamp = _trigger_tick_stamp_path()
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as f:
            f.write(str(__import__("time").time()))
    except OSError:
        pass


def cmd_trigger_clear():
    trigger_name = os.environ.get("BEACON_TRIGGER_NAME", "")
    if not trigger_name:
        print("Error: trigger name required")
        sys.exit(1)
    triggers_dir = _get_triggers_dir()
    trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
    if os.path.exists(trigger_path):
        os.remove(trigger_path)
        print(f"Cleared trigger: {trigger_name}")
    else:
        print(f"No trigger: {trigger_name}")
