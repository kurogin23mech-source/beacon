"""tool_record_hook — the ONE definition of "record this tool use" (ms-180).

Why this module exists
----------------------
The PostToolUse save hook has TWO entry points, because Beacon ships two ways:

  * ``bin/beacon-save-hook.sh`` → ``bin/beacon-tool-record-hook.py``
    (source / brew install, the bash launcher layout), and
  * the ``beacon-hook-save`` console script → ``beacon_cli.hooks.save_hook``
    (pipx / wheel / Windows), which ``commands._resolve_hook_command`` prefers
    FIRST when it writes ``settings.json``.

ms-180 replaced the hand-written ``case`` statement in the bash leg with a ledger
lookup — and left the Python leg untouched, still carrying its own
``_MONITORED_SOURCES`` table naming two Google-Drive servers. So on the install
path the installer actually prefers, the ledger did nothing at all: a tool with a
declared record sink produced no record, and no debt either (the PreToolUse gate
saw ``action == record`` and stayed quiet). That is precisely the "wiring exists
but nothing runs" failure the milestone was written to remove, reproduced inside
the fix for it. An independent philosophy review of PR #802 found it.

The lesson is not "remember the other file". It is that a decision with two entry
points needs ONE definition, which is what this module is — the same shape the
judgment itself already has (``tool_ledger.record_plan``). Both legs are now thin
wrappers whose only job is to find the project root and the CLI.

Fails safe by contract: every path returns 0 and swallows its own errors. A
recording hook must never break the session it observes. Set
``BEACON_HOOK_DEBUG=1`` to also append the swallowed traceback to
``.beacon/hook-debug.log`` — the production contract is unchanged, but a bug in
here stops being indistinguishable from a correct no-op.

KNOWN INCONSISTENCY (maintainability review of PR #805): ``post_commit`` is still
on the OLD two-leg shape — ``beacon_cli/hooks/post_commit.py`` describes itself as
"a Python translation of bin/beacon-post-commit-hook.sh", i.e. two independently
maintained implementations of one decision, with the same latent "one leg patched,
the other stale" risk that this module was created to remove for the save hook.
It is deliberately NOT migrated here (out of this milestone's scope), but the
inconsistency is named so the next person choosing between the two idioms knows
which is the intended direction.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tool_effect as _te
import tool_ledger as _tl
import tool_turn as _turn
import tool_usage_debt as _debt

#: Fields a tool's input may carry that make a better summary than the bare name.
NAME_KEYS = ("title", "name", "fileName", "file_name", "documentTitle",
             "subject", "query", "description")
URL_KEYS = ("url", "fileUrl", "file_url", "link")


def find_root(start) -> Optional[Path]:
    """Walk up from ``start`` for a ``.beacon/project.json`` marker.

    BOTH legs call this. ``bin/hook_bootstrap.find_beacon_root`` is the same rule
    for the other three hooks, and its own docstring records that the rule had
    been copied into three scripts, drifted, and was consolidated for exactly
    that reason — so the Python leg hand-rolling a FOURTH copy was reproducing
    the hazard that module exists to remove (independent maintainability review
    of PR #805).

    Why the rule lives here and not only in hook_bootstrap: the wheel install has
    no ``bin/`` — ``beacon_cli/hooks/save_hook.py`` cannot import
    ``hook_bootstrap`` at all, but it CAN import this module (``lib/`` is remapped
    into the package). So this is the one place both legs can share. The two
    remaining copies are pinned against each other by
    ``tests/test_tool_record_hook_parity_ms180.test_both_root_finders_agree``,
    because hook_bootstrap by construction cannot depend on ``lib/`` (it is the
    thing that FINDS lib).
    """
    try:
        cur = Path(start).resolve()
    except Exception:
        return None
    for cand in (cur, *cur.parents):
        try:
            if (cand / ".beacon" / "project.json").exists():
                return cand
        except Exception:
            continue
    return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def first_str(tool_input, keys) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def summary_for(tool_name: str, tool_input) -> str:
    """One line describing this use. Falls back to the bare tool name — a record
    with a weak summary is still a record, and the structured ``tool`` field
    carries the identity that matters."""
    label = first_str(tool_input, NAME_KEYS)
    return f"{tool_name}: {label}"[:300] if label else f"{tool_name} を使用"


def resolve_beacon_cli(root: Path) -> Optional[list]:
    """The beacon CLI to invoke, or None.

    Prefers a ``bin/beacon`` next to the beacon source tree so a worktree records
    through its OWN code rather than whatever is first on PATH (a real hazard in
    this repo: a worktree's CLI on PATH resolves to the main checkout's lib).
    """
    for candidate in (Path(__file__).resolve().parent.parent / "bin" / "beacon",
                      root / "bin" / "beacon"):
        try:
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                return [str(candidate)]
        except Exception:
            continue
    found = shutil.which("beacon")
    return [found] if found else None


def _invoke_record(root: Path, tool_name: str, recorder: str, summary: str,
                   url: str) -> str:
    """Run ``beacon tool used``. Returns "" on success, else a short reason.

    The return code is CHECKED, unlike the first version of this hook. A writer
    can legitimately fail to record (no worked Target, a class with no evidence
    arm) and silently dropping that turned "we decided not to write" and "we
    could not write" into the same silence — the very collapse the 3-valued
    record sink exists to prevent, one layer down (philosophy review of PR #802).
    """
    cmd = resolve_beacon_cli(root)
    if cmd is None:
        return "beacon-cli-not-found"
    argv = cmd + ["tool", "used", tool_name, "--summary", summary,
                  "--record", recorder, "--json"]
    if url:
        argv += ["--url", url]
    try:
        proc = subprocess.run(argv, cwd=str(root), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=20,
                              check=False)
    except Exception as exc:
        return f"{type(exc).__name__}"
    if proc.returncode == 0:
        return ""
    try:
        body = json.loads((proc.stdout or b"").decode("utf-8") or "{}")
        return str(body.get("reason") or f"exit-{proc.returncode}")
    except Exception:
        return f"exit-{proc.returncode}"


def handle(payload: dict, root: Path) -> int:
    """Act on one hook payload. Always returns 0 (fail-safe by contract).

    ``UserPromptSubmit`` advances the turn epoch that the ``turn`` record grain
    folds within; ``PostToolUse`` consults the ledger and records, folds, notes a
    debt, or stays silent.
    """
    try:
        beacon_dir = str(root / ".beacon")
        session_key = str(payload.get("session_id") or "")
        event = payload.get("hook_event_name") or "PostToolUse"

        if event == "UserPromptSubmit":
            # A human prompt is the turn boundary. Registered on the SAME hook so
            # the boundary comes from the script that consumes it — see
            # lib/tool_turn on why a dependency on a second hook would fold
            # forever in silence.
            _turn.begin_turn(_turn.turn_path(beacon_dir), session_key, at=_now())
            return 0
        if event != "PostToolUse":
            return 0

        harness_name = str(payload.get("tool_name") or payload.get("tool") or "")
        if not harness_name:
            return 0
        tool_input = payload.get("tool_input")

        # The name the LEDGER keys this call by. For every tool but one it is the
        # harness's own name; for a Skill the harness says ``Skill`` and carries
        # the skill in ``tool_input``, so keying on the harness name meant a
        # Skill registered WITH a record sink left no record and was debited as
        # "`Skill` is not in the ledger" — "listed, and then silently not
        # recorded", which this milestone exists to remove and which an
        # independent attainment judge found still held for Skills. Resolved
        # ONCE here, so the plan, the debt row, the turn fold, the summary and
        # the recorded entry all name the same 道具 (ms-180 follow-up).
        tool_name = _te.ledger_name(harness_name, tool_input)
        if not tool_name:
            return 0

        ledger = _tl.load_ledger(_tl.ledger_path(root))
        plan = _tl.record_plan(ledger, tool_name)
        action = plan.get("action")

        # Two of the three non-recording outcomes are DEBTS and must leave a trace
        # of the USE; the third is an answer and stays silent (方針6 / 受入条件2).
        # The set lives in tool_ledger.DEBT_ACTIONS so this hook and the
        # PreToolUse gate cannot drift apart on what counts as a debt.
        #
        # This hook is the ONLY writer of the debt file. Both interrupts see every
        # call, so having both write it double-counted every use (philosophy
        # review of PR #802): the count stopped being an observed fact. "A use
        # left no record" is a fact about recording, so it belongs to the
        # recording interrupt.
        if _tl.is_debt_action(action):
            _debt.record_no_record_use(_debt.debt_path(beacon_dir), tool_name,
                                       reason=action, at=_now())
            return 0
        if action != "record":
            return 0  # declined — an answer, deliberately silent

        if plan.get("grain") == _tl.GRAIN_TURN:
            turn_file = _turn.turn_path(beacon_dir)
            if _turn.current_epoch(turn_file, session_key) is not None:
                if not _turn.claim_fold(turn_file, session_key, tool_name):
                    return 0  # already recorded once this turn → fold
            else:
                # No epoch observed → the boundary is not wired here, so `turn`
                # degrades to `each`. NOTE it rather than just doing it, so a
                # reader can tell a degraded fold from a working one (受入条件3).
                _turn.note_degraded(turn_file, tool_name, at=_now())

        failure = _invoke_record(root, tool_name, plan["recorder"],
                                 summary_for(tool_name, tool_input),
                                 first_str(tool_input, URL_KEYS))
        if failure:
            # The ledger said "record" and the write did not happen. That is a
            # third kind of debt, and conflating it with silence would hide the
            # one case where the ledger's own promise was broken.
            _debt.record_no_record_use(_debt.debt_path(beacon_dir), tool_name,
                                       reason=_debt.REASON_WRITE_FAILED,
                                       at=_now(), detail=failure)
        return 0
    except Exception:
        # Fail-safe by contract: a recording hook must never break the session.
        # But the double fail-safe (here AND in each thin leg) made a bug inside
        # this module indistinguishable from a correct no-op: renaming one
        # function produced 33 bare "assert 0 == 1" failures with no traceback
        # anywhere (independent maintainability review of PR #805). The escape
        # hatch keeps the production contract and makes the failure findable.
        _debug_dump_exception(root)
        return 0


def _debug_dump_exception(root) -> None:
    """Append the swallowed traceback to ``.beacon/hook-debug.log`` when
    ``BEACON_HOOK_DEBUG`` is set. Silent and best-effort otherwise — the whole
    point is that the production path stays fail-safe."""
    if not os.environ.get("BEACON_HOOK_DEBUG"):
        return
    try:
        import traceback
        path = Path(root) / ".beacon" / "hook-debug.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"--- {_now()} tool_record_hook ---\n")
            fh.write(traceback.format_exc())
    except Exception:
        return


__all__ = ["NAME_KEYS", "URL_KEYS", "first_str", "summary_for",
           "resolve_beacon_cli", "find_root", "handle"]
