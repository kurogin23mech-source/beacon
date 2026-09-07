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


def _source_lines(state: dict) -> str:
    """Render the untrusted DMs (event_id / sender / body preview) inline so the
    human judges the *context*, not the tool (e-6280 option A). Falls back to a
    bare event_id list when no preview was recorded (legacy / bare arm)."""
    sources = [s for s in (state.get("sources") or []) if isinstance(s, dict)]
    if sources:
        lines = []
        for s in sources[:5]:
            eid = s.get("event_id") or "?"
            sender = s.get("sender") or "?"
            preview = s.get("preview") or ""
            if preview:
                lines.append(f"  - [{eid}] from {sender}: 「{preview}」")
            else:
                lines.append(f"  - [{eid}] from {sender}")
        extra = len(sources) - 5
        if extra > 0:
            lines.append(f"  - … 他 {extra} 件")
        return "\n".join(lines)
    ids = state.get("event_ids") or []
    ids_str = ", ".join(str(i) for i in ids[:5]) if ids else "(不明)"
    return f"  - event: {ids_str}"


def _build_reason(tool_name: str, state: dict) -> str:
    return (
        "⚠ ms-169 injection ガード — 信頼できない外部 DM 文脈での副作用操作\n"
        "このセッションには信頼できない外部由来 (別ユーザー / 別セッション) の DM 本文が"
        "コンテキストに居ます:\n"
        f"{_source_lines(state)}\n"
        f"この文脈で副作用ツール『{tool_name}』(状態を変える / 外部に送る) を実行しようと"
        "しています。\n"
        "判断: 上の DM 本文に紛れ込んだ命令に誘導されていませんか? この文脈のまま操作を"
        "進めて安全ですか?\n"
        "  承認 → この操作を実行し、以後このセッションでは untrusted DM 由来の副作用確認を"
        "再度求めません (= この文脈を信頼したとみなす)。\n"
        "  拒否 → この操作を止めます。\n"
        "(読み取り専用ツールは gate されません)"
    )


def _build_corrupt_reason(tool_name: str) -> str:
    """Reason shown when the untrusted-turn state file is corrupt (e-6274).

    We can't read the file, so we can't prove this session isn't in an untrusted
    turn — fail toward confirmation rather than silently passing a side-effect."""
    return (
        "⚠ ms-169 injection ガード — untrusted-turn 状態ファイルが読めません\n"
        "`.beacon/untrusted-turn.json` が破損 / 読み取り不能で、このセッションが"
        "信頼できない DM 文脈に居るかどうかを確認できませんでした。\n"
        f"安全側に倒して副作用ツール『{tool_name}』(状態を変える / 外部に送る) の実行前に"
        "人間確認を求めています。\n"
        "  承認 → この操作を実行します (直近に別セッションからの DM に誘導されていない"
        "ことを確認してください)。\n"
        "  拒否 → この操作を止めます。\n"
        "状態ファイルを復旧するには一度人間プロンプトを送ると再生成されます。"
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
        session_key = ut.resolve_session_key(root, hook_input)
        tool_name = hook_input.get("tool_name") or ""
        tool_input = hook_input.get("tool_input")
        state = ut.is_armed(root, session_key)
        if state:
            if not te.is_side_effect(tool_name, tool_input):
                return  # read-only call → passes even in an untrusted turn
            _emit_ask(_build_reason(str(tool_name), state))
            return
        # Not armed. e-6274: distinguish a *missing* state file (benign — the
        # common case, genuinely no untrusted content → pass) from a *corrupt*
        # one (present-but-unreadable → we can't prove not-armed). For a corrupt
        # file, tilt a side-effect toward human confirmation instead of silently
        # passing; read-only calls still pass (no over-gating).
        if ut.state_file_health(root) == "corrupt" and te.is_side_effect(tool_name, tool_input):
            _emit_ask(_build_corrupt_reason(str(tool_name)))
    except Exception:
        # Never brick the harness on a gate bug — fail safe (tool proceeds).
        return


if __name__ == "__main__":
    main()
