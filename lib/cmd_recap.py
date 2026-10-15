#!/usr/bin/env python3
"""cmd_recap.py — the `beacon recap *` command family (ms-184).

Two verbs, one derivation
=========================
Both read-only verbs answer "what moved in this period" from the single
definition in :mod:`progress_window`, but they answer two DIFFERENT human
questions and so stay two verbs (ms-184 SPEC 方針 6):

``beacon recap projects``
    「昨日って何やったっけ」— **breadth**. Which of the projects I own moved
    at all. Projects that did not move are not returned: a list where every
    project looks the same is the problem this replaces (方針 2). (e-7017)

``beacon recap targets``
    「あのプロジェクト直近何やってたっけ」— **depth**. Which Targets of ONE
    project progressed, and the evidence that moved them. ``--project <id>``
    reads a project OTHER than the current one without moving the working
    directory or swapping a config file (e-7015 + e-7020), which is what CORE
    doc ``ux-principle-no-terminal`` requires.

Project selection follows the write side's existing spelling — ``--project
<id>``, as ``beacon dm send`` / ``beacon dm audit`` already use (方針 7).
``--as-project`` is deliberately NOT reused: that flag means "what would
project P be allowed to see" (a disclosure view), a different question.

Period selection never asks a person to compute a date (受入条件 7):
``--since yesterday`` / ``3d`` / ``last-week`` / ``2026-10-14`` all parse, in
:func:`date_window.parse_since`. ``--from`` / ``--to`` remain for an explicit
range.
"""

import json
import os
import sys
from typing import Optional

import date_window as _dw
import progress_window as _pw
from commands_shared import (
    load_project,
    _is_cloud_mode,
    _get_api_client,
)

#: Used when neither --since nor --from/--to is given. A week is the span over
#: which "what has been moving" is still a useful answer; the resolved window is
#: always printed so the default is never invisible.
DEFAULT_SINCE = "7d"


# ---------------------------------------------------------------------------
# Period resolution
# ---------------------------------------------------------------------------

def _resolve_window() -> tuple:
    """Return ``(from_date, to_date, label)`` from the env the CLI front sets.

    ``--since`` and ``--from``/``--to`` are mutually exclusive: honouring one
    while ignoring the other would answer a period the caller did not ask for,
    and they would have no way to tell from the output. Exits 1 on a bad spec
    rather than falling back to a different window.
    """
    since = (os.environ.get("BEACON_SINCE", "") or "").strip()
    from_date = (os.environ.get("BEACON_FROM", "") or "").strip()
    to_date = (os.environ.get("BEACON_TO", "") or "").strip()

    if since and (from_date or to_date):
        print("Error: --since and --from/--to are mutually exclusive. "
              "Pass one period, not two.", file=sys.stderr)
        sys.exit(1)

    if from_date or to_date:
        return from_date, to_date, f"{from_date or '…'}..{to_date or '…'}"

    spec = since or DEFAULT_SINCE
    try:
        lo, hi = _dw.parse_since(spec)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    suffix = "" if since else "  (default)"
    label = f"{spec}{suffix}"
    return lo, hi, label


def _int_env(key: str, default: int) -> int:
    raw = (os.environ.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Error: {key.lower().replace('beacon_', '--')} must be an "
              f"integer (got {raw!r})", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

#: Failures that mean "the server could not answer" — the decision stream is
#: cloud-only and may be absent on a deployment that predates it, so these
#: degrade to "source not consulted". Deliberately NOT ``Exception``: an
#: ``AttributeError`` / ``TypeError`` is a bug in THIS file, and swallowing it
#: would report "no decisions available" forever while looking like a server
#: limitation (that exact mistake was made and caught here during ms-184).
_REMOTE_ERRORS = (RuntimeError, ConnectionError, TimeoutError, OSError,
                  ValueError)


def _decisions_for(client, project_id: str) -> Optional[list]:
    """Best-effort read of the decision stream (cloud-only, may be undeployed).

    Returns ``None`` — not ``[]`` — when the stream could not be read, so
    :func:`progress_window.derive` reports ``decision`` as a source it did NOT
    consult instead of implying no decisions were made.
    """
    if client is None or not project_id:
        return None
    try:
        rows = client.list_decisions(project_id, limit=500)
    except _REMOTE_ERRORS:
        return None
    if isinstance(rows, dict):
        rows = rows.get("decisions") or []
    return rows if isinstance(rows, list) else None


def _read_current_project() -> tuple:
    """``(project_id, name, project, documents, decisions)`` for the cwd project."""
    data = load_project()
    pid = data.get("project_id", "") or ""
    documents = []
    try:
        import store as _store      # noqa: PLC0415 — optional in bare unit runs
        documents = _store.get_store().list_documents() or []
    except _REMOTE_ERRORS + (ImportError,):
        documents = []
    decisions = None
    if _is_cloud_mode():
        try:
            client, config = _get_api_client()
        except SystemExit:
            raise
        except _REMOTE_ERRORS:
            client, config = None, {}
        decisions = _decisions_for(client, pid or config.get("project_id", ""))
    return pid, data.get("name", "") or pid, data, documents, decisions


def _read_named_project(project_ref: str) -> tuple:
    """``(project_id, name, project, documents, decisions)`` for another project.

    Cloud-only by nature: reading a project you are not standing in means
    asking the one shared truth source for it (方針 3). In local mode there is
    no other project to read, so this says so instead of silently recapping
    the current one — answering a different project than the one asked for
    would be indistinguishable from the right answer in the output.
    """
    if not _is_cloud_mode():
        print(f"Error: --project {project_ref} needs cloud mode "
              "(this project is local-only, so there is no other project to "
              "read). Run `beacon cloud status` to check.", file=sys.stderr)
        sys.exit(1)

    client, _config = _get_api_client()

    pid = project_ref
    try:
        import project_ref as _pr   # noqa: PLC0415
        pid = _pr.resolve_project_ref(project_ref,
                                      db_or_lister=lambda: client.list_projects())
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except _REMOTE_ERRORS:
        # Slug expansion needs the project listing; without it the ref is used
        # verbatim and the read below returns the server's own 404.
        pid = project_ref

    try:
        data = client.get_project(pid)
    except _REMOTE_ERRORS as exc:
        print(f"Error: cannot read project {pid}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict) or not data:
        print(f"Error: project {pid} returned no data (not a member?)",
              file=sys.stderr)
        sys.exit(1)

    try:
        documents = client.list_documents(pid) or []
    except _REMOTE_ERRORS:
        documents = []
    return (pid, data.get("name", "") or pid, data, documents,
            _decisions_for(client, pid))


# ---------------------------------------------------------------------------
# beacon recap targets  (e-7015 / e-7020)
# ---------------------------------------------------------------------------

def cmd_recap_targets():
    """Print the Targets of one project that progressed in the period."""
    lo, hi, label = _resolve_window()
    evidence_limit = _int_env("BEACON_EVIDENCE", 5)
    limit = _int_env("BEACON_LIMIT", 0)
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    project_ref = (os.environ.get("BEACON_RECAP_PROJECT", "") or "").strip()

    if project_ref:
        pid, name, data, documents, decisions = _read_named_project(project_ref)
    else:
        pid, name, data, documents, decisions = _read_current_project()

    result = _pw.derive(data, from_date=lo, to_date=hi,
                        documents=documents, decisions=decisions,
                        evidence_limit=evidence_limit)
    result["project_id"] = pid
    result["project_name"] = name
    result["window"]["spec"] = label
    if limit > 0:
        result["targets"] = result["targets"][:limit]

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
        return

    _print_targets(result)


def _print_targets(result: dict) -> None:
    w = result["window"]
    header = f"{w['from']}..{w['to']} [{w.get('spec', '')}] — {result['project_name']}"
    print(header)
    if not result["targets"]:
        print("  この期間に進んだ対象はありません。")
    else:
        print(f"  進んだ対象 {result['target_count']} 件 / "
              f"証跡 {result['evidence_count']} 件")
    for t in result["targets"]:
        kinds = " / ".join(f"{k} {n}" for k, n in
                           sorted(t["evidence_by_kind"].items(),
                                  key=lambda kv: -kv[1]))
        print(f"  [{t['target_id']}] {t['label'][:70]}  ({t['status']})")
        print(f"      証跡 {t['evidence_count']} 件 ({kinds})  最新 {t['last_evidence_at']}")
        for ev in t["evidence"]:
            print(f"        · {ev['at'][:19]}  {ev['kind']:<8} "
                  f"{ev['id']:<8} {ev['summary'][:64]}")
    _print_caveats(result)


def _print_caveats(result: dict) -> None:
    """Say what could NOT be judged or was not consulted (方針 8 / 受入条件 6).

    Printed for a zero result too — that is exactly when a reader is most
    likely to conclude "nothing happened" from "we could not tell".
    """
    und = result.get("undated") or {}
    if und.get("count"):
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(und["by_kind"].items()))
        print(f"  ※ 日付が無く期間で判定できなかった記録: {und['count']} 件 ({kinds})")
    missing = result.get("sources_unavailable") or []
    if missing:
        print(f"  ※ 参照できなかった証跡の種類: {', '.join(missing)} "
              "(この種類の動きは上の件数に入っていません)")


# ---------------------------------------------------------------------------
# beacon recap projects  (e-7017)
# ---------------------------------------------------------------------------

def cmd_recap_projects():
    """Print which of the caller's projects moved in the period."""
    lo, hi, label = _resolve_window()
    limit = _int_env("BEACON_LIMIT", 0)
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not _is_cloud_mode():
        rows, scanned, source = _scan_local_only(lo, hi)
    else:
        rows, scanned, source = _scan_owned_projects(lo, hi)

    result = _pw.aggregate(rows, from_date=lo, to_date=hi)
    result["window"]["spec"] = label
    result["projects_scanned"] = scanned
    result["source"] = source
    if limit > 0:
        result["projects"] = result["projects"][:limit]

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
        return

    _print_projects(result)


def _scan_local_only(lo: str, hi: str) -> tuple:
    """Local mode: the only readable project is the one we are standing in.

    Reported as ``source="local"`` and ``projects_scanned=1`` so the caller
    sees that this was NOT a cross-project answer — a one-project result
    rendered like a 12-project sweep would read as "only one project moved".
    """
    pid, name, data, documents, decisions = _read_current_project()
    result = _pw.derive(data, from_date=lo, to_date=hi,
                        documents=documents, decisions=decisions,
                        evidence_limit=0)
    return [{"project_id": pid, "name": name, "result": result}], 1, "local"


def _scan_owned_projects(lo: str, hi: str) -> tuple:
    """Cloud mode: run the SAME derivation over every project the user owns.

    The truth for all of them already lives in one place (方針 3), so this
    reads each project's record and derives — it never asks projects to
    publish a summary, which would put a second copy of the answer in play.

    A project that cannot be read becomes an ``error`` row rather than being
    skipped, so ``aggregate`` can keep "did not move" and "could not tell"
    apart (受入条件 8).
    """
    client, _config = _get_api_client()
    try:
        listing = client.me_list_projects() or []
    except _REMOTE_ERRORS as exc:
        print(f"Error: cannot list your projects: {exc}", file=sys.stderr)
        sys.exit(1)

    rows = []
    for item in listing:
        pid = (item.get("id") or item.get("project_id") or "").strip()
        if not pid:
            continue
        name = item.get("name") or pid
        try:
            data = client.get_project(pid)
        except _REMOTE_ERRORS as exc:
            rows.append({"project_id": pid, "name": name, "error": exc})
            continue
        if not isinstance(data, dict) or not data:
            rows.append({"project_id": pid, "name": name,
                         "error": "empty project payload"})
            continue
        try:
            documents = client.list_documents(pid) or []
        except _REMOTE_ERRORS:
            documents = []
        rows.append({
            "project_id": pid, "name": name,
            "result": _pw.derive(data, from_date=lo, to_date=hi,
                                 documents=documents,
                                 decisions=_decisions_for(client, pid),
                                 evidence_limit=0),
        })
    return rows, len(rows), "cloud"


def _print_projects(result: dict) -> None:
    w = result["window"]
    scanned = result.get("projects_scanned", 0)
    print(f"{w['from']}..{w['to']} [{w.get('spec', '')}] — "
          f"動いたプロジェクト {result['project_count']} 件 / 調べた {scanned} 件")
    if result.get("source") == "local":
        print("  ※ local mode: このプロジェクト 1 件しか読めません "
              "(横断には cloud mode が必要)")
    if not result["projects"]:
        print("  この期間に動いたプロジェクトはありません。")
    for p in result["projects"]:
        print(f"  [{p['project_id']}] {p['name']}")
        print(f"      対象 {p['target_count']} 件 / 証跡 {p['evidence_count']} 件"
              f"  最新 {p['last_evidence_at']}")
        for t in p["targets"][:3]:
            print(f"        · {t['target_id']} {t['label'][:56]} "
                  f"({t['evidence_count']})")
        if len(p["targets"]) > 3:
            print(f"        … 他 {len(p['targets']) - 3} 対象")
    bad = result.get("unavailable") or []
    if bad:
        print(f"  ⚠ 読めなかったプロジェクト {len(bad)} 件 "
              "(動いていないのではなく、判定できていません):")
        for b in bad:
            print(f"      - {b['project_id']} {b['name']}: {b['error']}")
    if result["projects"]:
        print("  → 詳細: beacon recap targets --project <id> "
              f"--from {w['from']} --to {w['to']}")


__all__ = ["cmd_recap_targets", "cmd_recap_projects", "DEFAULT_SINCE"]
