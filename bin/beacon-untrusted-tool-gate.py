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

# Shared hook bootstrap lives next to this script (bin/hook_bootstrap.py); reach
# it via THIS file's own directory so there is no lib-search convention to
# duplicate just to import it (ms-169 e-6296). It owns beacon-root discovery,
# stdin parsing, and the lib-import convention for all three hook scripts.
#
# Guard the import: if the sibling bootstrap is somehow absent (e.g. a stale
# install where only the hook scripts were copied), fail SAFE (hb=None → the
# gate no-ops) rather than let an ImportError escape into the harness. This
# hook's contract is "never brick the harness"; that must hold at import time
# too, not only inside main()'s try (ms-169 e-6296 review, maintainability finding).
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hook_bootstrap as hb  # noqa: E402
except Exception:
    hb = None


def _emit_ask(reason: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


def _strip_fence(s: str) -> str:
    """Remove the 「」 quote-fence chars so attacker-influenced text (sender /
    preview) can't escape its quoting in the approval prompt (e-6280 review)."""
    return str(s).replace("「", " ").replace("」", " ")


def _source_lines(state: dict) -> str:
    """Render the untrusted DMs (event_id / sender / body preview) inline so the
    human judges the *context*, not the tool (e-6280 option A). Falls back to a
    bare event_id list when no preview was recorded (legacy / bare arm)."""
    sources = [s for s in (state.get("sources") or []) if isinstance(s, dict)]
    if sources:
        lines = []
        for s in sources[:5]:
            eid = s.get("event_id") or "?"
            # Sanitize sender too, not just preview (e-6280 review, AX finding
            # #2): sender is attacker-influenced data flowing into the approval
            # prompt; strip the 「」 quote-fence so it can't break out of quoting.
            sender = _strip_fence(s.get("sender") or "?")
            preview = s.get("preview") or ""
            if preview:
                lines.append(f"  - [{eid}] from {sender}: 「{preview}」")
                # A truncated preview (trailing …) may hide an injection past the
                # cap; point the human at the full body so "先頭だけ見て安全" fails
                # closed (ms-169 e-6280 review fix, AX finding).
                if preview.endswith("…"):
                    lines.append(f"    (本文は途中まで。全文: `beacon dm show {eid}`)")
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
        "  承認 → この操作を実行します。**承認範囲は上の untrusted 文脈のみ**で、以降その"
        "文脈内の副作用は確認を省きます。後から別の untrusted DM が届いたら、その文脈ごとに"
        "改めて一度確認します (セッション全体を信頼済みにはしません)。\n"
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
        "ことを確認してください)。**この承認はこの1操作だけに効きます** — 状態ファイルが"
        "復元されるまで、次の副作用でも再び確認します (文脈単位の信頼付与にはなりません)。\n"
        "  拒否 → この操作を止めます。\n"
        "状態ファイルを復旧するには一度人間プロンプトを送ると再生成されます。"
        " (読み取り専用ツールは gate されません)"
    )


def main() -> None:
    if hb is None:
        return  # bootstrap missing → fail-safe (gate no-ops)
    hook_input = hb.read_hook_input()
    if hook_input is None:
        return

    # Only meaningful on PreToolUse; be lenient if the field is absent.
    event = hook_input.get("hook_event_name", "PreToolUse")
    if event and event != "PreToolUse":
        return

    libs = hb.import_lib("untrusted_turn", "tool_effect")
    if libs is None:
        return  # fail-safe: can't classify → no opinion
    ut, te = libs["untrusted_turn"], libs["tool_effect"]

    try:
        cwd = Path(hook_input.get("cwd") or ".")
        root = hb.find_beacon_root(cwd)
        if root is None:
            return  # not a beacon project
        session_key = ut.resolve_session_key(root, hook_input)
        tool_name = hook_input.get("tool_name") or ""
        tool_input = hook_input.get("tool_input")
        state = ut.is_armed(root, session_key)
        if state:
            if not te.is_side_effect(tool_name, tool_input):
                return  # read-only call → passes even in an untrusted turn
            # Stamp that the gate actually asked, BEFORE emitting — the vet hook
            # requires this marker, so a side-effect that ran without a real ask
            # (fail-safe / auto-accept) can't later silently vet the session.
            try:
                ut.record_asked(root, session_key, str(tool_name))
            except Exception:
                pass  # best-effort; a failed stamp only makes vet MORE conservative
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
