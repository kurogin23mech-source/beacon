#!/usr/bin/env python3
"""beacon-tool-record-hook — record a tool's use because the LEDGER says to (ms-180 e-7001).

This replaces the hand-written ``case`` statement that used to live in
``bin/beacon-save-hook.sh``. That statement named exactly two Google-Drive MCP
servers and ``exit 0``'d on everything else, so "using a tool leaves a record"
was true for two tools and silently false for every other tool in the project.
The wiring was already there — a ``PostToolUse`` hook firing on every MCP call —
and what was missing was a DECLARATION of which tools get recorded and where.

So this hook consults ``lib/tool_ledger`` and does what the row says. The policy
lives in data (``.beacon/tools.json``), not in this file: there is no tool name
anywhere in this script. Adding a tool to the record set is a ``beacon tool add``,
not an edit here (SPEC ms-180 方針1/方針3).

Two events, one script
----------------------
``PostToolUse``      → consult the ledger; record, fold, or stay silent.
``UserPromptSubmit`` → a human prompt is the turn boundary, so advance the turn
                       epoch that the ``turn`` record grain folds within. Wired
                       here rather than in a second hook so the boundary comes
                       from the same script that consumes it — a grain that
                       depended on someone ALSO installing another hook would
                       silently fold forever (see ``lib/tool_turn``).

Why the write is a subprocess
-----------------------------
The record is written by invoking the CLI (``beacon tool used``), not by mutating
the project in-process. Beacon funnels project writes through one writer
(ms-148), and a hook that wrote directly would be a second one. The hot path the
SPEC protects is the ledger READ (local file, no network); the write happens
after the tool already ran, so a subprocess there costs nothing the SPEC cares
about.

Fails safe, always
------------------
Any error — unreadable ledger, missing lib, CLI absent — means "record nothing"
and exit 0. A recording hook must never break the session it observes. The cost
of that posture is bounded and visible: the ledger's promise is checked by
``beacon tool unregistered`` and by tests, not by this script surviving.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hook_bootstrap as hb  # noqa: E402
except Exception:
    hb = None

#: Fields a tool's input may carry that make a better summary than the bare name.
_NAME_KEYS = ("title", "name", "fileName", "file_name", "documentTitle",
              "subject", "query", "description")
_URL_KEYS = ("url", "fileUrl", "file_url", "link")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_str(tool_input, keys) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _summary(tool_name: str, tool_input) -> str:
    """One line describing this use. Falls back to the bare tool name — a record
    with a weak summary is still a record, and the tool field carries the
    identity that matters."""
    label = _first_str(tool_input, _NAME_KEYS)
    return f"{tool_name}: {label}"[:300] if label else f"{tool_name} を使用"


def _beacon_cmd(root: Path):
    """The beacon CLI to invoke, or None. Prefers the repo-local ``bin/beacon``
    next to this hook so a worktree records through its OWN code rather than
    whatever is first on PATH (a real hazard in this repo: a worktree's CLI on
    PATH resolves to the main checkout's lib)."""
    local = Path(__file__).resolve().parent / "beacon"
    if local.exists() and os.access(str(local), os.X_OK):
        return [str(local)]
    found = shutil.which("beacon")
    return [found] if found else None


def _record(root: Path, tool_name: str, recorder: str, summary: str,
            url: str) -> None:
    cmd = _beacon_cmd(root)
    if cmd is None:
        return
    argv = cmd + ["tool", "used", tool_name, "--summary", summary,
                  "--record", recorder, "--json"]
    if url:
        argv += ["--url", url]
    try:
        subprocess.run(argv, cwd=str(root), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20, check=False)
    except Exception:
        return  # fail safe: a failed record must not disturb the session


def main() -> None:
    if hb is None:
        return
    hook_input = hb.read_hook_input()
    if hook_input is None:
        return
    libs = hb.import_lib("tool_ledger", "tool_usage_debt", "tool_turn")
    if libs is None:
        return
    tl, debt, turn = (libs["tool_ledger"], libs["tool_usage_debt"],
                      libs["tool_turn"])
    try:
        root = hb.find_beacon_root(hook_input.get("cwd") or ".")
        if root is None:
            return
        beacon_dir = str(root / ".beacon")
        session_key = str(hook_input.get("session_id") or "")
        event = hook_input.get("hook_event_name") or "PostToolUse"

        # A human prompt opens a new turn: advance the epoch the `turn` grain
        # folds within, and forget what the previous turn folded.
        if event == "UserPromptSubmit":
            turn.begin_turn(turn.turn_path(beacon_dir), session_key, at=_now())
            return
        if event != "PostToolUse":
            return

        tool_name = str(hook_input.get("tool_name") or hook_input.get("tool") or "")
        if not tool_name:
            return
        tool_input = hook_input.get("tool_input")

        ledger = tl.load_ledger(tl.ledger_path(root))
        plan = tl.record_plan(ledger, tool_name)
        action = plan.get("action")

        # Two of the three non-recording outcomes are DEBTS and must leave a
        # trace of the USE; the third is an answer and must stay silent
        # (SPEC 受入条件2 / 方針6):
        #   unregistered → nobody declared this tool,
        #   undecided    → a row exists but its record sink is unjudged. Listing
        #                  the unjudged ROW was never enough — the question only
        #                  becomes a loss when the tool is actually used, and
        #                  that is what was invisible until now.
        #   declined     → a decision was made. Noting it would turn a settled
        #                  answer into permanent noise.
        if action in ("unregistered", "undecided"):
            debt.record_no_record_use(debt.debt_path(beacon_dir), tool_name,
                                      reason=action, at=_now())
            return
        if action != "record":
            return  # declined — an answer, deliberately silent

        if plan.get("grain") == tl.GRAIN_TURN:
            turn_file = turn.turn_path(beacon_dir)
            if turn.current_epoch(turn_file, session_key) is not None:
                if not turn.claim_fold(turn_file, session_key, tool_name):
                    return  # already recorded once this turn → fold
            else:
                # No epoch ever observed → the turn boundary is not wired here,
                # so `turn` degrades to `each`. NOTE it rather than just doing
                # it: a silent fall to one of the two grains would leave nobody
                # able to tell that a tool declared `turn` is recording every
                # call (受入条件3). `beacon tool list` reads this back.
                turn.note_degraded(turn_file, tool_name, at=_now())

        _record(root, tool_name, plan["recorder"],
                _summary(tool_name, tool_input),
                _first_str(tool_input, _URL_KEYS))
    except Exception:
        return


if __name__ == "__main__":
    main()
