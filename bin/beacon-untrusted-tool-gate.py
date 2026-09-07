#!/usr/bin/env python3
"""beacon-untrusted-tool-gate — the ms-169 A gate (最後の砦).

A PreToolUse hook that pauses for human approval when the AI is about to call a
**side-effect tool** *while untrusted content is live in the turn*. This is the
structural hard stop behind the ms-169 prompt-injection fix: even if the
untrusted framing (e-6235) is ignored by the model, a Write / Bash / MCP-send /
Skill-deploy triggered by a received DM body stops here for a human.

Two independent modules decide the outcome (both fail-closed):
  * ``untrusted_turn.is_armed`` — is this session in an untrusted turn? (armed by
    the receive hook when it injected untrusted DM content; e-6237)
  * ``tool_effect.is_side_effect`` — does this call change state / send outward?
    (read-only calls pass; unknown tools default-deny; e-6236)

Only when BOTH are true does the gate fire, so read-only work in an untrusted
turn is never blocked (受入条件: 過剰 gate で日常運用を壊さない). It applies to
every session, including an ``armed`` autonomous one — that is exactly when no
human is watching the DM-driven tool call.

Hook protocol (Claude Code PreToolUse)
--------------------------------------
Stdin: JSON {hook_event_name, session_id, cwd, tool_name, tool_input, ...}.
Stdout to FIRE the gate::

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "ask",
                            "permissionDecisionReason": "<why>"}}

``ask`` routes the call to the human's approve/deny prompt — the "human approval
gate" the SPEC calls for. Every other path prints nothing (= no opinion; the
tool proceeds under normal permissions).

The script NEVER raises to the harness and fails SAFE: any error, or an
unreadable state file, means "not armed" → no gate → the tool proceeds. Bricking
every tool call on a hook bug would be worse than the (bounded) miss of a
corrupt state file; the security property is carried by the fail-closed
classification, not by this file surviving.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_LIB_CACHE = None
_LIB_TRIED = False


def _import_lib():
    """Import lib/untrusted_turn + lib/tool_effect lazily. Returns (ut, te) or
    (None, None) if unavailable (→ caller stays silent = fail-safe)."""
    global _LIB_CACHE, _LIB_TRIED
    if _LIB_TRIED:
        return _LIB_CACHE
    _LIB_TRIED = True
    here = Path(__file__).resolve().parent
    for lib_dir in (here.parent / "lib", here.parent.parent / "lib"):
        if (lib_dir / "untrusted_turn.py").exists() and (lib_dir / "tool_effect.py").exists():
            sys.path.insert(0, str(lib_dir))
            try:
                import untrusted_turn as ut  # type: ignore[import-not-found]
                import tool_effect as te  # type: ignore[import-not-found]
            except Exception:
                _LIB_CACHE = (None, None)
                return _LIB_CACHE
            _LIB_CACHE = (ut, te)
            return _LIB_CACHE
    _LIB_CACHE = (None, None)
    return _LIB_CACHE


def _find_beacon_root(start: Path) -> "Path | None":
    """Walk up from `start` looking for a .beacon/project.json marker."""
    cur = start.resolve()
    for cand in (cur, *cur.parents):
        if (cand / ".beacon" / "project.json").exists():
            return cand
    return None


def _emit_ask(reason: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


def _build_reason(tool_name: str, state: dict) -> str:
    ids = state.get("event_ids") or []
    ids_str = ", ".join(str(i) for i in ids[:5]) if ids else "(不明)"
    return (
        "⚠ ms-169 injection ガード: このセッションには信頼できない外部由来の DM 本文 "
        f"(event: {ids_str}) がコンテキストに居ます。その状態で副作用ツール "
        f"『{tool_name}』(状態を変える / 外部に送る) を実行しようとしています。"
        " DM 本文に紛れ込んだ命令に誘導されていないか人間が確認してください。"
        " 正当な操作なら承認、DM 由来の意図しない操作なら拒否してください。"
        " (読み取り専用ツールは gate されません)"
    )


def main() -> None:
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    try:
        hook_input = json.loads(raw) if raw.strip() else {}
    except Exception:
        return
    if not isinstance(hook_input, dict):
        return

    # Only meaningful on PreToolUse; be lenient if the field is absent.
    event = hook_input.get("hook_event_name", "PreToolUse")
    if event and event != "PreToolUse":
        return

    ut, te = _import_lib()
    if ut is None or te is None:
        return  # fail-safe: can't classify → no opinion

    try:
        cwd = Path(hook_input.get("cwd") or ".")
        root = _find_beacon_root(cwd)
        if root is None:
            return  # not a beacon project
        session_key = ut.session_key_from_hook_input(hook_input)
        state = ut.is_armed(root, session_key)
        if not state:
            return  # not in an untrusted turn → allow silently
        tool_name = hook_input.get("tool_name") or ""
        tool_input = hook_input.get("tool_input")
        if not te.is_side_effect(tool_name, tool_input):
            return  # read-only call → passes even in an untrusted turn
        _emit_ask(_build_reason(str(tool_name), state))
    except Exception:
        # Never brick the harness on a gate bug — fail safe (tool proceeds).
        return


if __name__ == "__main__":
    main()
