#!/usr/bin/env python3
"""beacon-code-graph-edit-hook.py — Edit/Write で reconcile を促す PostToolUse hook (ms-156 e-5542)

Claude Code の PostToolUse JSON を stdin で受け、編集された file が code-graph の対象
module なら reconcile 促し文を ``hookSpecificOutput.additionalContext`` で返す
(commit→beacon-log の post-commit hook と同じ出力契約)。判定は純粋な
``lib/code_graph_reconcile.reminder_for_edit`` が持つ。

fail-safe: どんな入力でも例外を投げず exit 0。対象 module でなければ何も出力しない
(= module 編集時だけ喋る)。最小スライス = 促すのみ (強制ブロックはしない)。
"""

from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))


def _emit(message: str) -> None:
    payload = {"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": message}}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def _file_path_from_payload(payload: dict) -> str:
    """Edit / Write / MultiEdit の tool_input から編集対象 path を取り出す。"""
    ti = payload.get("tool_input") or {}
    return ti.get("file_path") or ti.get("path") or ""


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # 壊れた入力でも hook は静かに通す
    try:
        import code_graph_reconcile
        file_path = _file_path_from_payload(payload)
        message = code_graph_reconcile.reminder_for_edit(file_path, REPO)
        if message:
            _emit(message)
    except Exception:
        return 0  # 判定失敗は握りつぶす (編集を妨げない)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
