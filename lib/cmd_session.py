#!/usr/bin/env python3
"""cmd_session.py — the `beacon session *` command family (ms-127 e-4317b).

Extracted verbatim from commands.py (the god-module split). Holds the session
lifecycle / session-log / fork CLI handlers and their private helpers. Depends
only on commands_shared (upward) + leaf domain modules (core / work_model /
store / session / session_log), never on commands.py — so the dependency graph
stays acyclic (SPEC 方針4). commands.py re-imports these names so `import
commands; commands.cmd_session_end()` and the dispatch dict keep resolving.

`_release_all_occupations_for_session` reads data["milestones"]; it lives here
(with its only caller cmd_session_end) rather than commands_shared so the ms-134
capability-scope checker — now module-aware (e-4317a) — still attributes the
reviewed-correct `session_end→milestones` read to the session_end verb.
"""

import json
import os
import re
import sys
from typing import Optional

from store import get_store
import core
import work_model  # ms-109 e-3559: 職種非依存の Target 正準ラベルアクセサ

from commands_shared import (
    get_project_file,
    load_project,
    save_project,
    _get_api_client,
    _get_notes_path,
    _refuse_if_bus_origin,
    _resolve_bus_project_id,
)


# --- session log / lifecycle / fork helpers ---

def _session_logs_dir() -> str:
    """Local cache dir for session log entries (mirrors the cloud subcollection).

    In cloud mode the authoritative store is Firestore; we still write a
    local copy so list/show work offline. In local-only mode this is the
    primary store.
    """
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "session_logs")


def _session_log_path(session_id: str) -> str:
    """Slug-safe path for a session log file. Doc-id-safe characters only."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"
    return os.path.join(_session_logs_dir(), f"{safe}.json")


def _read_local_session_log(session_id: str) -> Optional[dict]:
    path = _session_log_path(session_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_local_session_log(payload: dict) -> None:
    """Merge-write a session log entry to the local cache.

    We merge (read-modify-write) so a partial rescue payload doesn't wipe
    fields a richer session-end already produced — mirrors the server's
    merge=True semantics on the cache layer too.
    """
    sid = payload.get("session_id", "")
    if not sid:
        return
    path = _session_log_path(sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = _read_local_session_log(sid) or {}
    existing.update(payload)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _push_session_log_to_cloud(payload: dict) -> bool:
    """Best-effort upsert via Store.upsert_session_log (ms-84 Phase 2 e-2036).

    LocalStore returns False unconditionally (= cloud session log subcollection
    has no local analogue); StoreApi calls the API and swallows transport
    failures the same way the legacy inline cloud branch did. The session
    log primary truth is the local cache when cloud is unreachable — a later
    run will re-aggregate and resync.
    """
    sid = payload.get("session_id", "")
    if not sid:
        return False
    body = {k: v for k, v in payload.items()
            if k != "session_id" and v is not None}
    try:
        return get_store().upsert_session_log(sid, body)
    except BaseException:
        if os.environ.get("BEACON_DEBUG") == "1":
            import traceback as _tb
            _tb.print_exc()
        return False


def _stamp_cloud_session_shutdown(session_id: str) -> bool:
    """Mark a cloud session document as shut down (ms-95 / e-2305).

    Writes ``shutdown=true`` + ``last_poll_at=now`` to the cloud
    sessions/{session_id} doc so the server-side directory query
    (``/api/projects/{pid}/sessions?healthy_only=true``) classifies this
    session as not-healthy *immediately* — without waiting for the
    bridge's natural ``last_poll_at`` aging window (= max(30s, 2×
    poll_interval_ms)).

    Why this exists (e-2305 background): the shutdown stamp was
    previously written only by ``channel/bus.mjs`` from its SIGINT /
    SIGTERM graceful-exit path. That single path has three silent-failure
    modes that leave the session advertised as ``healthy=true`` long
    after the user-facing terminal has closed:

      1. The bridge process outlives its Claude Code parent (= MCP
         stdio shutdown unreliable on some hosts). bridge keeps polling,
         so ``last_poll_at`` never goes stale → directory keeps the
         row healthy indefinitely.
      2. The shutdown PUT fails (cloud transient blip). The bridge's
         try/catch swallows the error; the bridge then exits. The
         server-side row carries no ``shutdown`` flag, but
         ``last_poll_at`` will age out within 30s — so this case
         self-heals at the heartbeat-staleness threshold.
      3. Hard kill (SIGKILL / OS shutdown). No shutdown stamp; the
         session ages out at the same 30s window as case 2.

    Case 1 is the e-2305 reproduction: 3 sessions stacking up,
    ``healthy=true`` persistently displayed. Adding a CLI-driven shutdown
    stamp closes this gap structurally: when the user explicitly says
    "this session is ending" via ``beacon session end``, we converge on
    the same server-side merge field bus.mjs uses (= idempotent), but
    via a path that does NOT depend on the bridge process's liveness or
    its signal-delivery ordering. The two paths are integrity-safe by
    Firestore merge=True semantics.

    Best-effort: cloud unreachable, local-mode project, or not-logged-in
    all return False without raising. Caller (cmd_session_end /
    cmd_session_rescue) MUST NOT abort on a False return — the local
    session log persistence is the primary truth source; the cloud
    stamp is an immediacy optimization on the directory side.
    """
    if not session_id:
        return False
    store = get_store()
    if not store.is_cloud():
        return False  # Local mode: no cloud session doc to stamp.
    import datetime
    # ms precision matches what bus-heartbeat.mjs writes (= JavaScript
    # Date.toISOString output). Server parses both ms and µs precision
    # via fromisoformat with the Z→+00:00 replace dance, so the choice
    # is stylistic — we go ms-precision to match the bridge so the two
    # paths produce indistinguishable rows on the server.
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    body = {
        "last_active": now_iso,
        "last_poll_at": now_iso,
        "shutdown": True,
    }
    try:
        from auth import load_credentials
        if load_credentials() is None:
            return False
        client, cfg = _get_api_client()
        pid = cfg.get("project_id", "")
        if not pid:
            return False
        client.upsert_session(pid, session_id, body)
        return True
    except BaseException:
        if os.environ.get("BEACON_DEBUG") == "1":
            import traceback as _tb
            _tb.print_exc()
        return False


def _list_other_session_ids() -> list:
    """Return session_ids (other than the current one) that have entries
    associated with them in the project data or local notes.

    Used by rescue (e-1039). Walks project entries' meta.session_id and
    the local notes jsonl. In cloud mode we additionally fetch the cloud
    session registry so cross-machine orphans are discovered.
    """
    seen = set()
    try:
        import session as _session
        current = _session.get_session_id()
    except Exception:
        current = ""

    try:
        data = load_project()
    except Exception:
        data = {"milestones": []}

    def _walk(entries):
        for e in entries or []:
            sid = (e.get("meta") or {}).get("session_id")
            if sid and sid != current:
                seen.add(sid)
            _walk(e.get("entries", []))

    for ms in data.get("milestones", []) or []:
        _walk(ms.get("entries", []))

    notes_path = _get_notes_path()
    if os.path.exists(notes_path):
        try:
            with open(notes_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        note = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = note.get("session_id")
                    if sid and sid != current:
                        seen.add(sid)
        except OSError:
            pass

    # ms-84 Phase 2 (e-2036): Store.list_session_ids unifies cloud + local.
    # LocalStore returns []; StoreApi returns the API list (swallowing
    # transport failures), so the union below does not branch on backend.
    try:
        for sid in get_store().list_session_ids():
            if sid and sid != current:
                seen.add(sid)
    except BaseException:
        if os.environ.get("BEACON_DEBUG") == "1":
            import traceback as _tb
            _tb.print_exc()

    return sorted(seen)


def _aggregate_and_persist(session_id: str, *, recovered: bool,
                            summary_override: Optional[str] = None) -> dict:
    """Run the aggregation core, write the local cache, push to cloud.

    Returns the persisted payload. Single helper used by both cmd_session_end
    (recovered=False) and cmd_session_rescue (recovered=True) so the
    idempotency / merge semantics live in one place.
    """
    import session_log as _slog
    from pathlib import Path as _Path
    beacon_dir = _Path(os.path.dirname(get_project_file()) or ".beacon")

    data = load_project()
    existing_local = _read_local_session_log(session_id)

    # ms-84 Phase 2 (e-2036): fetch any persisted remote session log via
    # Store.get_session_log so the merge step is backend-uniform. LocalStore
    # returns None; StoreApi returns the persisted dict (or None on 404 /
    # transport failure). The cloud_client / cloud_pid pass-through to
    # aggregate_session is left as a separate slice because session_log.py
    # still talks to ApiClient directly via collect_cloud_notes; folding
    # that into the Store interface is a follow-up commit.
    store = get_store()
    remote = store.get_session_log(session_id)
    if remote and (not existing_local
                   or remote.get("last_aggregated_at", "")
                      >= existing_local.get("last_aggregated_at", "")):
        existing_local = remote

    cloud_client = None
    cloud_pid = ""
    if store.is_cloud():
        try:
            from auth import load_credentials
            if load_credentials() is not None:
                cloud_client, cfg = _get_api_client()
                cloud_pid = cfg.get("project_id", "")
        except BaseException:
            if os.environ.get("BEACON_DEBUG") == "1":
                import traceback as _tb
                _tb.print_exc()

    payload = _slog.aggregate_session(
        project_data=data,
        beacon_dir=beacon_dir,
        session_id=session_id,
        recovered=recovered,
        summary_override=summary_override,
        cloud_client=cloud_client,
        cloud_project_id=cloud_pid,
        existing_log=existing_local,
    )
    _write_local_session_log(payload)
    _push_session_log_to_cloud(payload)
    return payload


def cmd_session_end():
    """Finalize the current session: aggregate its entries into the session log.

    Idempotent per SPEC §3: running twice on the same session produces the
    same logical result. Callers (typically /beacon-session-end Skill) may
    supply an AI-generated summary via BEACON_SUMMARY; otherwise the
    mechanical fallback in session_log.aggregate_session is used.
    """
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    summary_override = os.environ.get("BEACON_SUMMARY", "").strip() or None
    explicit_sid = os.environ.get("BEACON_SESSION_ID", "").strip()

    # ms-54 / e-1293: persistence poisoning defense. A bus-origin caller
    # MUST NOT be able to upsert a session log — a poisoned summary that
    # lands in the session_log subcollection survives into the next
    # /beacon-session-start context restore, which is the cross-session
    # infection vector this defense exists to block.
    if _refuse_if_bus_origin(
        "session_end",
        {
            "session_id": explicit_sid,
            "summary_preview": (summary_override or "")[:80],
        },
    ):
        sys.exit(1)
    try:
        import session as _session
        sid = explicit_sid or _session.get_session_id()
    except Exception:
        sid = explicit_sid
    if not sid:
        print("Error: no session id (no .beacon/session.json and BEACON_SESSION_ID unset)",
              file=sys.stderr)
        sys.exit(1)

    # ms-81 e-1918 (SPEC AC #15): release any MS occupations held by this
    # session before aggregating. Done here so the session_log includes the
    # release events; running it after persistence would race the cloud sync.
    _released_count = _release_all_occupations_for_session(sid)
    if _released_count:
        print(
            f"  released {_released_count} occupation claim(s) held by this "
            f"session (status unchanged)",
            file=sys.stderr,
        )

    # ms-95 / e-2305: stamp shutdown=true on the cloud session doc so the
    # directory's healthy_only filter drops this session immediately —
    # without waiting for the bridge's natural last_poll_at aging window.
    # See _stamp_cloud_session_shutdown docstring for the failure modes
    # this closes (bridge outliving Claude Code parent, network blips on
    # the bridge's own graceful-exit stamp). Best-effort: local-mode or
    # cloud-unreachable returns False silently so the session log
    # persistence remains the primary truth source.
    _stamped = _stamp_cloud_session_shutdown(sid)
    if _stamped and os.environ.get("BEACON_DEBUG") == "1":
        print(f"  cloud session {sid} marked shutdown=true", file=sys.stderr)

    payload = _aggregate_and_persist(sid, recovered=False,
                                      summary_override=summary_override)
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Session log upserted: {sid}")
        print(f"  notes: {len(payload.get('note_ids', []))}, "
              f"commits: {len(payload.get('commit_ids', []))}, "
              f"prs: {len(payload.get('pr_ids', []))}")


def cmd_session_rescue():
    """Aggregate all sessions other than the current one.

    Per SPEC §7 the rescue path does NOT gate on heartbeat freshness — the
    aggregation is idempotent, so even rescuing an alive session is safe:
    the alive side's session-end will overwrite us later with a richer
    summary. ``recovered=True`` is set on first creation so forensics can
    distinguish rescue-born entries.
    """
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    sids = _list_other_session_ids()
    results = []
    for sid in sids:
        try:
            payload = _aggregate_and_persist(sid, recovered=True)
            # ms-95 / e-2305: rescued sessions are by definition orphan
            # (= the original bclaude tab is no longer driving them).
            # Stamp shutdown=true so the directory's healthy_only filter
            # doesn't keep advertising them as receive-capable. Best-effort;
            # see _stamp_cloud_session_shutdown for failure semantics.
            _stamp_cloud_session_shutdown(sid)
            results.append({"session_id": sid, "status": "ok",
                            "notes": len(payload.get("note_ids", [])),
                            "commits": len(payload.get("commit_ids", [])),
                            "prs": len(payload.get("pr_ids", [])),
                            "recovered": payload.get("recovered", False)})
        except BaseException as e:
            results.append({"session_id": sid, "status": "error", "error": str(e)})
    if json_mode:
        print(json.dumps(results, ensure_ascii=False))
    else:
        if not results:
            print("No other sessions to rescue.")
        else:
            for r in results:
                if r["status"] == "ok":
                    print(f"  {r['session_id']}: rescued "
                          f"(notes={r['notes']}, commits={r['commits']}, prs={r['prs']}, "
                          f"recovered={r['recovered']})")
                else:
                    print(f"  {r['session_id']}: error — {r.get('error','')}")


def cmd_session_log_list():
    """List recent session log entries. Used by /beacon-session-start."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    try:
        limit = int(os.environ.get("BEACON_LIMIT", "0") or 0)
    except ValueError:
        limit = 0

    # ms-84 Phase 2 (e-2036): Store.list_session_logs returns the cloud
    # rows in cloud mode or [] in local mode, so the fallback to walking
    # ``.beacon/session_logs/`` directly only fires when the Store could
    # not supply anything.
    entries: list[dict] = get_store().list_session_logs(limit=limit)
    if not entries:
        # Local cache fallback
        d = _session_logs_dir()
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith(".json"):
                    try:
                        with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                            entries.append(json.load(f))
                    except (OSError, json.JSONDecodeError):
                        continue
            entries.sort(key=lambda e: e.get("last_aggregated_at", ""), reverse=True)
            if limit:
                entries = entries[:limit]

    if json_mode:
        print(json.dumps(entries, ensure_ascii=False))
        return
    if not entries:
        print("(no session logs)")
        return
    for e in entries:
        sid = e.get("session_id", "?")
        when = e.get("last_aggregated_at", "")
        summary = (e.get("summary") or "")[:120]
        print(f"  {sid} [{when}]: {summary}")


def cmd_session_log_show():
    """Show a single session log entry by session_id."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    sid = os.environ.get("BEACON_SESSION_ID", "").strip()
    if not sid:
        print("Error: session id required", file=sys.stderr)
        sys.exit(1)
    entry = _read_local_session_log(sid)
    # ms-84 Phase 2 (e-2036): when the local cache is empty, ask the Store
    # for any persisted remote copy. LocalStore returns None (= no cloud
    # registry to consult); StoreApi returns the fetched dict or None on
    # 404 / transport failure, so the caller only checks truthiness.
    if entry is None:
        entry = get_store().get_session_log(sid)
    if entry is None:
        print(f"Session log not found: {sid}", file=sys.stderr)
        sys.exit(1)
    if json_mode:
        print(json.dumps(entry, ensure_ascii=False))
    else:
        print(f"Session: {entry.get('session_id','')}")
        print(f"Aggregated: {entry.get('last_aggregated_at','')}")
        print(f"Summary: {entry.get('summary','')}")
        print(f"Notes: {len(entry.get('note_ids', []))}")
        print(f"Commits: {len(entry.get('commit_ids', []))}")
        print(f"PRs: {len(entry.get('pr_ids', []))}")
        if entry.get("recovered"):
            print("(recovered)")


def cmd_session_id():
    """Print the current session_id — pure getter, no heartbeat (ms-54 e-1319).

    Post Option C (PR #111 / commit 78048b6) the bridge poll loop owns
    mint+heartbeat (``last_active`` + ``last_poll_at``); CLI's role is
    *resolve only*. This command still mints once on first call so the
    bridge has an id to read from .beacon/session.json, but never bumps
    ``last_active`` or pushes to cloud — keeping the bridge as the single
    truth source for liveness.

    Used by channel/bus.mjs at startup (cold-start session-id discovery)
    and historically by /beacon-session-start Step 0b (now no-op post
    e-1319). ms-54 e-1150 / e-1152 / e-1319.
    """
    try:
        import session as _session
        # ms-93: prefer the bridge-aware resolver (same principle as ms-95
        # e-2419 for bus sender). In a git worktree with no local bridge, this
        # reconnects to the running bridge's sid via the cross-worktree pid-tree
        # scan instead of reporting the orphan cwd session — so `beacon session
        # id` matches the sid the bridge actually receives on, and bus.mjs
        # cold-start discovers the right identity. Falls through to
        # get_session_id() (which mints) when no claim exists.
        sid = _session.resolve_active_session_id() or _session.get_session_id()
        if not sid:
            print("Error: failed to materialise session_id", file=sys.stderr)
            sys.exit(1)
        print(sid)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _resolve_current_session_id() -> str:
    """Return the local session_id for `beacon session focus / attention`.

    Prefers the bridge's claim (.beacon/bridge.json) when a bus.mjs is
    actively running here (ms-54 / e-1331 quick fix). Without that step,
    this CLI call would mint or adopt the env's session_id and silently
    target a different session document than the bridge writes its
    heartbeat into — leaving intent stamps invisible to the directory
    picker.

    Falls back to :func:`session.get_session_id` when no live bridge
    claim exists. Errors degrade to an empty string so the caller can
    surface a clean "could not resolve" message rather than a traceback.
    """
    try:
        import session as _session
        return _session.resolve_active_session_id() or ""
    except Exception:
        return ""


def cmd_session_focus():
    """Stamp the AI's free-form intent on the current session (ms-54 / e-1369).

    Three modes:
      * `beacon session focus "<text>"`  — set the intent text
      * `beacon session focus --clear`   — clear the intent text (sets "")
      * `beacon session focus --show`    — read the current intent

    The intent is the only narrative field on the session document; Layer
    0-3 (Identity / Where / What / Reach) are stamped by the bridge from
    pure machine observation. Intent lets the AI explain *why* in one line,
    which is what the directory picker shows to a sender deciding "who to
    DM".
    """
    show = os.environ.get("BEACON_SESSION_FOCUS_SHOW", "") == "1"
    clear = os.environ.get("BEACON_SESSION_FOCUS_CLEAR", "") == "1"
    text = os.environ.get("BEACON_SESSION_FOCUS_TEXT", "")
    json_out = os.environ.get("BEACON_JSON", "") == "1"

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    session_id = _resolve_current_session_id()
    if not session_id:
        print("Error: could not resolve current session_id "
              "(run `beacon session id` first)", file=sys.stderr)
        sys.exit(1)

    if show:
        s = client.get_session(project_id, session_id)
        intent = s.get("intent") or {}
        if json_out:
            print(json.dumps(intent, ensure_ascii=False))
        else:
            txt = intent.get("text") or "(no intent set)"
            attn = intent.get("attention_required")
            print(f"focus: {txt}")
            if attn is not None:
                print(f"  attention_required: {attn}")
        return

    if clear:
        result = client.upsert_session_intent(
            project_id, session_id, text="", attention_required=None,
        )
    else:
        if not text:
            print("Usage: beacon session focus \"<text>\" "
                  "| --clear | --show [--json]", file=sys.stderr)
            sys.exit(2)
        result = client.upsert_session_intent(
            project_id, session_id, text=text, attention_required=None,
        )

    if json_out:
        print(json.dumps(result, ensure_ascii=False))
    else:
        intent = (result.get("intent") or {}).get("text") or "(cleared)"
        print(f"focus: {intent}")


def cmd_session_attention():
    """Raise / lower the attention_required flag on the current session.

    `beacon session attention --set true` signals "this session is waiting
    on a human decision" — the directory picker surfaces it prominently
    so a teammate sees "who needs me" without reading every intent text.
    Lowered with `--set false` once the human input lands.
    """
    set_val = os.environ.get("BEACON_SESSION_ATTENTION_SET", "")
    json_out = os.environ.get("BEACON_JSON", "") == "1"
    if set_val not in ("true", "false"):
        print("Usage: beacon session attention --set true|false [--json]",
              file=sys.stderr)
        sys.exit(2)

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    session_id = _resolve_current_session_id()
    if not session_id:
        print("Error: could not resolve current session_id", file=sys.stderr)
        sys.exit(1)

    flag = (set_val == "true")
    result = client.upsert_session_intent(
        project_id, session_id, text=None, attention_required=flag,
    )
    if json_out:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"attention_required: {flag}")


def cmd_session_fork():
    """Fork a sibling worktree for parallel work on a target milestone (ms-67 / e-1549).

    Creates ``.worktrees/<ms-id>-fork-<short-uuid>``, binds it to the same
    Beacon project by copying ``.beacon/cloud.json``, runs ``beacon channel
    install`` so the child worktree can host its own bclaude with a clean
    MCP server declaration, and records the parent ↔ child link in
    ``.beacon/fork.json``.

    The actual subprocess work lives in :func:`session.fork_workspace` so
    the CLI layer here only does arg parsing + lookups + reporting. Tests
    target the helper directly with a fake runner.
    """
    import subprocess as _sp

    ms_id = os.environ.get("BEACON_SESSION_FORK_MS_ID", "").strip()
    json_out = os.environ.get("BEACON_JSON", "") == "1"
    if not ms_id:
        print("Usage: beacon session fork <ms-id> [--json]", file=sys.stderr)
        sys.exit(2)

    # Resolve ms_title from the local project.json cache. fork is a git-worktree
    # operation (creates .worktrees/<ms-id>-fork-…, a branch, fork.json with
    # target_ms_id) — it is milestone-scoped BY DESIGN: only a dev milestone is
    # forkable (a sales Opportunity has no git worktree). So reading milestones
    # here is a legitimate exact read, NOT profession coupling — recorded as such
    # in capability_ledger.REVIEWED_LEGITIMATE_COLLECTION_READS (ms-134 e-4737).
    data = load_project()
    ms_title = ""
    for ms in data.get("milestones", []):
        if ms.get("id") == ms_id:
            ms_title = work_model.target_label(ms)
            break
    if not ms_title:
        print(f"Error: milestone not found: {ms_id}", file=sys.stderr)
        sys.exit(1)

    # Parent session info — used by the child's session-start to surface
    # "you were forked from sv-xxxx". Tolerate missing values: forking
    # outside a live bclaude session shouldn't be blocked just because
    # the parent id can't be resolved.
    import session as _session
    parent_session_id = _session.resolve_active_session_id() or ""

    parent_repo_path = os.getcwd()  # bin/beacon has already cd'd to project root
    # git rev-parse runs in the project root; if it fails fall back to empty
    proc = _sp.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False, cwd=parent_repo_path,
    )
    parent_branch = proc.stdout.strip() if proc.returncode == 0 else ""

    try:
        result = _session.fork_workspace(
            ms_id, ms_title,
            parent_session_id=parent_session_id,
            parent_branch=parent_branch,
            parent_repo_path=parent_repo_path,
        )
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_out:
        print(json.dumps(result, ensure_ascii=False))
    else:
        wt = result["worktree_path"]
        branch = result["branch"]
        channel_ok = result["fork_record"]["channel_install"]["ok"]
        print(f"Forked worktree: {wt}")
        print(f"  branch:        {branch}")
        print(f"  target_ms:     {ms_id} {ms_title}")
        print(f"  parent_branch: {parent_branch or '(unresolved)'}")
        print(f"  parent_sid:    {parent_session_id or '(unresolved)'}")
        if not channel_ok:
            print("  channel install failed — re-run `beacon channel install` "
                  "from the worktree once you cd into it")
        print("")
        print(f"  next: cd {wt} && bclaude")


def cmd_session_fork_list():
    """List active forked worktrees in this repo (ms-67 / e-1553).

    Walks ``git worktree list`` and, for each worktree, checks for
    ``.beacon/fork.json``. Only fork-created worktrees show up — this is
    explicitly *not* a generic worktree listing (use ``git worktree list``
    for that). Used by ``/beacon-session-merge-back`` (e-1552) as its
    picker source.
    """
    import session as _session

    json_out = os.environ.get("BEACON_JSON", "") == "1"
    repo_root = os.getcwd()  # bin/beacon already cd'd to project root
    forks = _session.list_forks(repo_root)

    if json_out:
        print(json.dumps(forks, ensure_ascii=False))
        return

    if not forks:
        print("No active forks")
        return

    for fk in forks:
        print(f"{fk['worktree_path']}")
        print(f"  target:        {fk['target_ms_id']} {fk['target_ms_title']}")
        print(f"  child_branch:  {fk['child_branch']}")
        print(f"  parent_sid:    {fk['parent_session_id'] or '(unknown)'}")
        print(f"  parent_branch: {fk['parent_branch'] or '(unknown)'}")
        print(f"  created:       {fk['created_at']}")
        print("")


# --- occupation release on session end (ms-81 e-1918) ---

def _release_all_occupations_for_session(session_id: str) -> int:
    """ms-81 e-1918 (SPEC AC #15): release every TARGET this session is occupying.

    Called from session-end so a clean exit leaves the next session free to claim.
    Returns the number of releases performed (0 for sessions that weren't holding
    anything).

    ms-142 T7 (e-5162): walks EVERY claimable target collection via the occupation
    abstraction (``claim_target_collections``), not just ``data["milestones"]`` — so
    a session that occupied an operation / opportunity / release target releases it
    too. This de-couples session_end from the milestones concrete (the old ms-127
    ``session_end→milestones`` ledger coupling is retired: the read now goes through
    the abstraction, not a literal ``data['milestones']``). The append-only audit
    event log (``worktree_sessions``, keyed by ms_id) stays milestone-scoped — it is
    a milestone-specific history, not the live-claim layer the warning reads.
    """
    if not session_id:
        return 0
    import occupation as _occ
    import work_model as _wm
    data = load_project()
    try:
        import agent as _agent_for_se
        actor = _agent_for_se.get_actor()
    except Exception:
        actor = {}
    released = 0
    for coll in _occ.claim_target_collections(data):
        for rec in data.get(coll, []) or []:
            occ = rec.get("occupation")
            if not (occ and occ.get("session_id") == session_id):
                continue
            tid = rec.get("id", "")
            core.release_occupation(data, tid, reason="session-end")
            # Milestone-only audit history (worktree_sessions); non-milestone
            # targets carry the live-claim stamp but not this milestone event log.
            if _wm.target_kind(tid) == "milestone":
                core.milestone_record_occupation_event(
                    data, ms_id=tid, event_type="release",
                    session_id=session_id,
                    machine=actor.get("machine", ""),
                    agent=actor.get("agent", ""),
                    reason="session-end",
                )
            released += 1
    if released:
        save_project(data, op={"op": "session_end_release", "count": released})
    return released
