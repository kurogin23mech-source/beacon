#!/usr/bin/env python3
"""cmd_log.py — the `beacon log *` command family (ms-127 e-4320).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules, never on commands.py — acyclic
(SPEC 方針4). commands.py re-imports these names for dispatch + `commands.X`.
"""

import json
import os
import sys

import core
import occupation
from commands_shared import (
    get_project_file,
    load_project,
    save_project,
    _resolve_session_id,
    _resolve_commit_source,
    _check_ms_status_for_write,
)


def cmd_log():
    summary = os.environ.get("BEACON_SUMMARY", "")
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    behavior = os.environ.get("BEACON_BEHAVIOR", "")
    resolves = os.environ.get("BEACON_RESOLVES", "")
    resolves_explicit = os.environ.get("BEACON_RESOLVES_SET", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    # ms-51 / e-934: attach actor (see cmd_log_finalize for the matching
    # detail). Done in both entry points because beacon log is exposed
    # directly to users (`beacon log "msg"`) and via the post-commit hook.
    try:
        import agent as _agent
        actor = _agent.get_actor()
    except Exception:
        actor = None

    session_id = _resolve_session_id()  # ms-57 / e-1062
    # ms-79 / e-1817 (UC3-F3): detect envelope context to tag auto-op
    # commits. The source label is stored on meta.source so retrospect /
    # retro can filter "AI 自律でやった commit だけ".
    source = _resolve_commit_source()

    # ms-79 / e-1816 (UC3-F2): fork.json の target_ms_id を最優先する。
    # 親が --ms を明示しているならそれが勝つ (= fork.json は hint であって
    # 命令ではない、cmd_log_prepare と同じセマンティクスを揃える)。
    if not ms_id:
        fork_target = _read_fork_target_ms_id()
        if fork_target:
            # fork target_ms_id が project.json にあるか軽くチェック。無ければ
            # 普通の auto-pick (find_target_milestone) に倒れる。
            data_check = load_project()
            if any(m.get("id") == fork_target for m in occupation.target_records(data_check, "milestone")):
                ms_id = fork_target

    data = load_project()

    # ms-133 e-4650: same non-dev guard as the prepare/finalize entry points —
    # a milestone-less sales/backoffice project must not crash the direct
    # `beacon log "msg"` path (or the bare hook path) with "No active milestone".
    _profession = occupation.resolve_profession(data)
    _active_ms = [m for m in occupation.target_records(data, "milestone")
                  if m.get("status") in ("todo", "in_progress", "observing")]
    if _profession != "dev" and not ms_id and not _active_ms:
        msg = (f"{_profession} project has no milestones — commit "
               f"{commit_hash[:7] or '(unknown)'} not milestone-bound.")
        if json_mode:
            print(json.dumps({"status": "skipped", "milestone_binding": "none",
                              "profession": _profession, "note": msg},
                             ensure_ascii=False))
        else:
            print(msg)
        return

    result = core.log_commit(
        data, ms_id=ms_id, commit_hash=commit_hash,
        message=message, date=date, summary=summary, progress=progress,
        behavior=behavior, resolves=resolves, resolves_explicit=resolves_explicit,
        actor=actor, session_id=session_id, source=source,
    )
    save_project(data)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    elif result["status"] == "duplicate":
        print(f"Already logged: {commit_hash}")
    else:
        loc = f"[{result['matched_task']}]" if "matched_task" in result else result["milestone_title"]
        print(f"Logged {commit_hash} to {loc}")
        if progress:
            print(f"  Progress: {result['progress']}%")


def cmd_log_prepare():
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    summary_text = os.environ.get("BEACON_SUMMARY", "")

    data = load_project()

    # ms-133 e-4650: a non-dev project (sales / backoffice) drives work through
    # its own targets (opportunities / …), not milestones — milestones[] is
    # empty by construction. The post-commit hook fires /beacon-log on EVERY
    # commit, so erroring "No active milestone" here would fail the hook on any
    # commit in a sales repo. Emit a benign prepare payload the Skill reads to
    # record/skip without milestone binding, instead of exiting non-zero.
    _profession = occupation.resolve_profession(data)
    _active_ms = [m for m in occupation.target_records(data, "milestone")
                  if m.get("status") in ("todo", "in_progress", "observing")]
    if _profession != "dev" and not _active_ms:
        print(json.dumps({
            "commit": {"hash": commit_hash, "message": message,
                       "date": date, "summary": summary_text},
            "current_summary": data.get("summary", ""),
            "profession": _profession,
            "milestone_binding": "none",
            "note": (f"{_profession} project has no milestones; this commit is "
                     "not milestone-bound. Report it without selecting a "
                     "milestone and skip progress evaluation."),
        }, ensure_ascii=False))
        return

    # ms-79 / e-1816 (UC3-F2): fork.json の target_ms_id を最優先する。
    # ms-67 で fork した子 worktree は明示意図 (= 親が「この MS をやれ」と
    # 指示した target_ms_id) を持つ。fork.json があれば、--ms 明示が無い
    # ときの候補選定の前にそれを優先採用する。これにより「fork 子で
    # commit したら親 MS に誤記録」 のドリフトを構造的に防ぐ。
    fork_target_ms_id = ""
    if not ms_id:
        fork_target_ms_id = _read_fork_target_ms_id()

    effective_ms_id = ms_id or fork_target_ms_id

    if effective_ms_id:
        for ms in occupation.target_records(data, "milestone"):
            if ms["id"] == effective_ms_id:
                targets = [ms]
                break
        else:
            # fork.json が指す ms_id がローカル project.json に無い場合
            # (= stale fork、活性化前 / 削除済 MS) は普通の active 候補に
            # fallback。explicit --ms ms_id が無効なときと違って
            # silent fallback でよい (= fork.json は hint であって命令ではない)。
            if fork_target_ms_id and not ms_id:
                targets = [ms for ms in occupation.target_records(data, "milestone")
                           if ms["status"] in ("todo", "in_progress", "observing")]
                if not targets:
                    print("No active milestone. Run: beacon milestone start <ms-id>")
                    sys.exit(1)
            else:
                print(f"Milestone not found: {effective_ms_id}")
                sys.exit(1)
    else:
        targets = [ms for ms in occupation.target_records(data, "milestone") if ms["status"] in ("todo", "in_progress", "observing")]
        if not targets:
            print("No active milestone. Run: beacon milestone start <ms-id>")
            sys.exit(1)

    output = {
        "commit": {"hash": commit_hash, "message": message, "date": date, "summary": summary_text},
        "current_summary": data.get("summary", ""),
    }
    # e-1816: 透明性のため fork.json 由来であることを payload に明示する。
    # /beacon-log Skill はこれを見て「fork 由来の active MS を採用しています」
    # と user に notice を出せる (= 暗黙の選定で誤解されるのを防ぐ)。
    if fork_target_ms_id and not ms_id:
        output["fork_target_ms_id"] = fork_target_ms_id

    if len(targets) == 1:
        output["milestone"] = core.milestone_prepare_info(targets[0])
    else:
        output["candidates"] = [core.milestone_prepare_info(ms) for ms in targets]

    print(json.dumps(output, ensure_ascii=False))


def _read_fork_target_ms_id() -> str:
    """Return the ``target_ms_id`` from ``.beacon/fork.json``, or "".

    Fork worktrees are created by /beacon-session-fork (ms-67) and carry
    a fork.json next to project.json that records the intent (= "this
    worktree exists to work on ms-X"). cmd_log_prepare consults this
    before falling back to the active-MS heuristic so commits made from
    a fork worktree route to the milestone the fork was created for —
    even if the parent repo has multiple active milestones (which would
    otherwise force a candidates / picker dialog the human did not
    intend in this child session).

    Returns "" when:
      - no fork.json exists (= regular worktree / main checkout)
      - fork.json is malformed (= treat as no fork hint, don't crash)
      - target_ms_id field is missing or empty

    Reads from ``$(dirname project.json)/fork.json`` so it stays
    consistent with how every other beacon helper resolves its
    ``.beacon/`` directory.
    """
    try:
        project_file = get_project_file()
        fork_path = os.path.join(os.path.dirname(project_file), "fork.json")
        if not os.path.exists(fork_path):
            return ""
        with open(fork_path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        if not isinstance(rec, dict):
            return ""
        target = rec.get("target_ms_id")
        if isinstance(target, str) and target.strip():
            return target.strip()
        return ""
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


def cmd_log_finalize():
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    summary_text = os.environ.get("BEACON_SUMMARY", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    # e-1040: project-level summary writes are deprecated. We still read
    # BEACON_NEW_SUMMARY so we can warn the caller (typically the legacy
    # /beacon-log Skill or a stale script), but we no longer mutate
    # data["summary"]. Human narrative lives in the project-vision CORE
    # doc; session-scoped context lives in the session_logs subcollection.
    legacy_new_summary = os.environ.get("BEACON_NEW_SUMMARY", "")
    behavior = os.environ.get("BEACON_BEHAVIOR", "")
    resolves = os.environ.get("BEACON_RESOLVES", "")
    resolves_explicit = os.environ.get("BEACON_RESOLVES_SET", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    # ms-51 / e-934: attach actor metadata so multi-machine commits are
    # distinguishable downstream. lib.agent.get_actor is the single source
    # of truth for this (configured via .beacon/agent.json + env vars).
    try:
        import agent as _agent
        actor = _agent.get_actor()
    except Exception:
        actor = None

    session_id = _resolve_session_id()  # ms-57 / e-1062
    # ms-79 / e-1817 (UC3-F3): tag the commit's source axis (= human dialog
    # vs auto-op envelope execution). Retrospect / retro can then filter
    # "AI 自律 commit だけ見たい" type queries.
    source = _resolve_commit_source()

    data = load_project()

    # ms-133 e-4650: mirror cmd_log_prepare — a non-dev project with no
    # milestones must not fail the commit hook. Nothing to bind the commit to,
    # so acknowledge and return 0 rather than raising "No active milestone".
    _profession = occupation.resolve_profession(data)
    _active_ms = [m for m in occupation.target_records(data, "milestone")
                  if m.get("status") in ("todo", "in_progress", "observing")]
    if _profession != "dev" and not ms_id and not _active_ms:
        msg = (f"{_profession} project has no milestones — commit "
               f"{commit_hash[:7] or '(unknown)'} recorded no milestone binding.")
        if json_mode:
            print(json.dumps({"ok": True, "milestone_binding": "none",
                              "profession": _profession, "note": msg},
                             ensure_ascii=False))
        else:
            print(msg)
        return

    # ms-81 e-1916: status gate. Resolve the target MS the same way log_commit
    # would internally, then surface the warning before mutating state.
    try:
        # ms-143: profession-generic target resolution (not the dev-concrete symbol).
        target_ms = occupation.resolve_target(data, ms_id)
    except ValueError:
        target_ms = None
    if target_ms is not None:
        if not _check_ms_status_for_write(
            target_ms, f"log commit {commit_hash[:7]}"
        ):
            sys.exit(1)
    result = core.log_commit(
        data, ms_id=ms_id, commit_hash=commit_hash,
        message=message, date=date, summary=summary_text, progress=progress,
        behavior=behavior, resolves=resolves, resolves_explicit=resolves_explicit,
        actor=actor, session_id=session_id, source=source,
    )

    # e-1040 deprecation: don't write data["summary"] anymore. The legacy
    # path used to set it from BEACON_NEW_SUMMARY; that field is now
    # ignored at write time. Callers should switch to project-vision
    # CORE doc updates (human narrative) or rely on session_logs
    # subcollection (session context).
    summary_was_ignored = bool(legacy_new_summary)

    save_project(data)

    if json_mode:
        result["summary_updated"] = False  # always False post-e-1040
        if summary_was_ignored:
            result["summary_deprecated"] = True
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["status"] == "duplicate":
            print(f"Already logged: {commit_hash} (updated progress)")
        else:
            loc = f"[{result['matched_task']}]" if "matched_task" in result else result["milestone_title"]
            print(f"Logged {commit_hash} → {loc}")
        if progress:
            print(f"  Progress: {result['progress']}%")
        if summary_was_ignored:
            # stderr so JSON consumers / pipes don't break; interactive
            # users still see it. Mirrors the pattern in cmd_summary.
            sys.stderr.write(
                "[deprecated] --summary was provided but is no longer written to "
                "the project. The project summary path was retired in e-1040 — "
                "use the `project-vision` CORE doc for human narrative and the "
                "session_logs subcollection for per-session context. This "
                "argument will be removed in a future release.\n"
            )
