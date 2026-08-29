#!/usr/bin/env python3
"""Codex PostToolUse halt-check hook (= ms-160 / e-5856).

The Codex parity of ``beacon_cli/hooks/halt_check.py``. Wired as a Codex
``PostToolUse`` hook with matcher ``.*`` so it fires after EVERY tool call,
this is the "止まる側" surface that works *mid autonomous loop* — when no
``UserPromptSubmit`` fires between tool calls and the pull-on-prompt inbox
hook (``codex-inbox-hook.py``) never runs.

Why a separate Codex hook (not the shared ``halt_check.py``)
------------------------------------------------------------
``halt_check.py`` resolves the session id from ``.beacon/session.json``
(Claude Code's marker). A Codex session's id lives instead in
``.beacon/codex/receive-loop.session.json`` — published by the receive-loop
daemon. This hook resolves the sid there, then does the same read → render →
acknowledge as ``halt_check.py``.

Self-contained write side
--------------------------
Mid-loop, the daemon has persisted any ``stop-signal`` event into the Codex
file inbox, but nothing turned it into a ``halt-request.json`` yet (the
inbox hook that would has not fired). So this hook ALSO runs the shared
``stop_signal.process_inbox_events`` reducer on the pending stop-signal
events and archives them, exactly once, before rendering — closing the loop
without depending on a user prompt.

Failure modes
-------------
Like every Beacon hook: swallow all errors, return 0, print ``{}`` when there
is nothing to surface. A broken halt-check must never block Codex's tool call.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional


def _resolve_lib_dir(install_root: Path) -> Path:
    """Return the lib dir across source/editable vs pipx/brew wheel layouts.

    Mirrors ``codex-inbox-hook.py``: when this hook runs from the bundled
    ``_bundled_scripts/`` dir, its self-resolved install_root is the
    ``beacon_cli`` package and lib lives at the ``_bundled_lib`` sibling.
    """
    lib_dir = install_root / "lib"
    if lib_dir.is_dir():
        return lib_dir
    bundled = install_root / "_bundled_lib"
    if bundled.is_dir():
        return bundled
    return lib_dir


def _import_modules(install_root: Path):
    """Make ``lib`` importable; return (stop_signal, codex_receive_loop).

    Either import may return ``None`` — the hook degrades to a silent no-op
    rather than raising into Codex.
    """
    lib_dir = str(_resolve_lib_dir(install_root))
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    try:
        import stop_signal as ss  # noqa: E402
    except Exception:
        ss = None
    try:
        import codex_receive_loop as crl  # noqa: E402
    except Exception:
        crl = None
    return ss, crl


def _find_beacon_root(start: Path) -> Optional[Path]:
    """Walk up from ``start`` looking for a ``.beacon/project.json`` marker."""
    try:
        cur = start.resolve()
    except Exception:
        return None
    while True:
        if (cur / ".beacon" / "project.json").exists():
            return cur
        if cur == cur.parent:
            return None
        cur = cur.parent


def _resolve_codex_sid(root: Path) -> str:
    """Read the active receive-loop session id from the daemon pointer.

    Same source of truth the inbox hook uses (= ms-93 / e-2502):
    ``<root>/.beacon/codex/receive-loop.session.json``. The daemon must be
    running for a mid-loop STOP to have been received at all, so the pointer
    is present in every case this hook needs to act on. Returns "" when the
    pointer is absent / malformed — the hook then no-ops.
    """
    pointer = root / ".beacon" / "codex" / "receive-loop.session.json"
    try:
        if pointer.is_file():
            with pointer.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return (data.get("session_id") or "").strip()
    except Exception:
        return ""
    return ""


def _resolve_cwd(args_cwd: str, hook_input: dict) -> str:
    """Codex passes cwd either as ``--cwd`` or inside the hook input JSON."""
    if args_cwd:
        return args_cwd
    cwd = hook_input.get("cwd")
    if not cwd:
        tool_input = hook_input.get("tool_input") or {}
        if isinstance(tool_input, dict):
            cwd = tool_input.get("cwd")
    return cwd or os.getcwd()


def _emit_inject(message: str) -> None:
    """Same JSON shape as the other Beacon PostToolUse hooks."""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def main(argv: Optional[list] = None) -> int:
    """Entry point. Returns 0 always; prints ``{}`` on the silent path."""
    try:
        parser = argparse.ArgumentParser(
            description="Codex PostToolUse halt-check hook (= ms-160 / e-5856).",
        )
        parser.add_argument("--cwd", default="", help="Override working directory.")
        parser.add_argument(
            "--install-root", default="",
            help="Beacon source checkout (defaults to script-relative).",
        )
        args = parser.parse_args(argv)

        raw = sys.stdin.read()
        try:
            hook_input = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            hook_input = {}
        if not isinstance(hook_input, dict):
            hook_input = {}

        cwd = Path(_resolve_cwd(args.cwd, hook_input))
        root = _find_beacon_root(cwd)
        if root is None:
            sys.stdout.write("{}")
            return 0

        session_id = _resolve_codex_sid(root)
        if not session_id:
            sys.stdout.write("{}")
            return 0

        install_root = Path(
            args.install_root or Path(__file__).resolve().parent.parent
        )
        ss, crl = _import_modules(install_root)
        if ss is None:
            # e-5803 review (AX-3): this is the remote-STOP kill-switch surface.
            # A silent no-op here means a STOP would go unnoticed every tool call
            # while the autonomous loop keeps running. Fail LOUD instead: tell the
            # session the halt-check is degraded so it pauses for human confirmation
            # rather than assuming "no signal = safe to continue".
            _emit_inject(
                "⚠ BEACON HALT-CHECK DEGRADED: stop_signal module を import できず、"
                "remote STOP (緊急停止) を検知できません。自律ループを続行せず、"
                "停止要求が来ていないか人間に確認してから進めてください "
                "(module 修復までこの警告が毎回出ます)。"
            )
            return 0

        beacon_dir = str(root / ".beacon")

        # Write side (mid-loop): turn any pending stop-signal inbox events into
        # a halt-request, then archive them so a later fire does not re-process
        # (re-processing would clobber the acknowledged_at stamp and re-surface
        # every tool call). Best-effort — a broken write must not stop the
        # render of an already-persisted halt-request below.
        if crl is not None:
            try:
                entries = crl.list_inbox_events(cwd=str(root))
                stop_entries = [
                    e for e in entries
                    if str((e.get("event") or {}).get("channel") or "")
                    == ss.STOP_CHANNEL
                ]
                if stop_entries:
                    ss.process_inbox_events(
                        [e["event"] for e in stop_entries],
                        session_id=session_id,
                        beacon_dir=beacon_dir,
                    )
                    for e in stop_entries:
                        try:
                            crl.archive_inbox_event(e["path"], cwd=str(root))
                        except Exception:
                            pass
            except Exception:
                pass

        # Render side (parity with halt_check.py): surface the active
        # halt-request once, then acknowledge so repeated PostToolUse fires
        # don't spam the same halt.
        request = ss.read_halt_request(session_id, beacon_dir=beacon_dir)
        if not ss.halt_inject_needed(request):
            sys.stdout.write("{}")
            return 0

        message = ss.render_halt_inject(request)
        if not message:
            sys.stdout.write("{}")
            return 0
        _emit_inject(message)

        try:
            ss.acknowledge_halt_request(
                session_id, beacon_dir=beacon_dir,
                note="surfaced via Codex PostToolUse",
            )
        except Exception:
            pass
        return 0
    except Exception:
        # Hook must never propagate exceptions to Codex.
        try:
            sys.stdout.write("{}")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
