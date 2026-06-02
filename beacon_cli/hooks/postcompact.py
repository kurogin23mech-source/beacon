"""Cross-platform PostCompact hook: post-compaction Tier-2 orientation.

Python translation of ``bin/beacon-postcompact.sh`` (e-565, ms-44 e-777).
Emits a JSON ``systemMessage`` after Claude Code transcript compaction
so the model re-fetches concrete IDs / numbers from the source of truth
instead of trusting the (now-blurry) summary.

Contract:
  * Stdin: Claude Code PostCompact JSON ``{"session_id": "...",
    "transcript_path": "...", ...}`` (we don't actually need any field).
  * Stdout: ``{"systemMessage": "<orientation text>"}`` JSON, or empty
    string if we couldn't find a beacon project / beacon CLI.
  * Return code: 0 (always — never block compaction).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_beacon_root(start: Path) -> Optional[Path]:
    """Walk up looking for ``.beacon/project.json`` (identical contract to
    the bash ``find_beacon_root``)."""
    try:
        cur = start.resolve()
    except OSError:
        cur = start
    while True:
        if (cur / ".beacon" / "project.json").is_file():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def _which_beacon() -> Optional[str]:
    """Return path to the beacon CLI or None."""
    return shutil.which("beacon")


def _run_beacon(beacon: str, args: List[str], cwd: Path) -> str:
    """Run a beacon subcommand and return its stdout (empty on failure)."""
    try:
        result = subprocess.run(
            [beacon, *args],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=15,
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------------------
# Orientation text assembly (mirrors the inline PYEOF in the bash)
# ---------------------------------------------------------------------------


def _format_context(status_raw: str, notes_raw: str) -> str:
    """Build the multi-line context summary the bash inlines.

    Order and labels are preserved exactly so the AI-facing text reads the
    same regardless of which platform the hook ran on.
    """
    lines: List[str] = []

    try:
        status = json.loads(status_raw) if status_raw else {}
    except (ValueError, json.JSONDecodeError):
        status = {}

    name = status.get("name", "")
    if name:
        lines.append(f"プロジェクト: {name}")

    active_ms = []
    for ms in (status.get("milestones") or []):
        if ms.get("status") == "in_progress":
            active_ms.append(ms)

    if active_ms:
        lines.append("Active MS:")
        for ms in active_ms:
            lines.append(
                f"  - {ms.get('id', '?')} \"{ms.get('title', '')}\""
                f" ({ms.get('progress', 0)}% / "
                f"{ms.get('done_tasks', 0)}/{ms.get('total_tasks', 0)}タスク完了)"
            )
    else:
        lines.append("Active MS: なし")

    active_ops = []
    for op in (status.get("operations") or []):
        if op.get("status") == "open":
            active_ops.append(op)

    if active_ops:
        lines.append("Active Operation:")
        for op in active_ops:
            lines.append(f"  - {op.get('id', '?')} \"{op.get('title', '')}\"")

    try:
        notes = json.loads(notes_raw) if notes_raw else []
    except (ValueError, json.JSONDecodeError):
        notes = []

    recent_headings = []
    for note in (notes or [])[-3:]:
        text = (note.get("text") or "").strip()
        first_line = text.split("\n", 1)[0].strip().lstrip("#").strip()
        if first_line:
            recent_headings.append(first_line)

    if recent_headings:
        lines.append("直近 note 見出し:")
        for heading in recent_headings:
            lines.append(f"  - {heading}")

    return "\n".join(lines)


_FALLBACK_CONTEXT = (
    "（beacon status の取得に失敗しました。/beacon-session-start で復元してください）"
)


def _build_message(context_text: str) -> str:
    """Build the user-visible orientation. Mirrors the bash here-doc body."""
    if not context_text.strip():
        context_text = _FALLBACK_CONTEXT
    return (
        "BEACON [post-compaction orientation]\n"
        "コンパクションが発生しました。summary は内容を保持していますが、"
        "具体ID・数値・直近の決定は抽象化されている可能性があります。"
        "下記は正源 (.beacon/project.json) から再取得した現在地です:\n"
        "\n"
        f"{context_text}\n"
        "\n"
        "判断指針:\n"
        "- summary 内の具体ID (ms-XX / e-XX / op-XX)・数値・直近 1 セッションの決定は"
        "鵜呑みにせず、必要なら beacon コマンドで再 fetch してください。\n"
        "- 細部 (タスク一覧・SPEC・直近コミット) が必要なら /beacon-session-start"
        "を実行してください。\n"
        "- メモやサマリーで足りる範囲なら、そのまま作業を続けて構いません。"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    try:
        # Consume stdin so Claude Code's pipe doesn't block, but we don't need
        # any field from it (matches the bash, which just sets -euo pipefail
        # and reads nothing).
        try:
            sys.stdin.read()
        except OSError:
            pass

        beacon_root = _find_beacon_root(Path(os.getcwd()))
        if beacon_root is None:
            return 0  # silent exit, no output

        beacon = _which_beacon()
        if beacon is None:
            # No beacon on PATH — mirrors the bash `command -v beacon` guard.
            print("[postcompact] beacon CLI not found — skipping", file=sys.stderr)
            return 0

        status_raw = _run_beacon(beacon, ["status", "--json"], beacon_root)
        notes_raw = _run_beacon(beacon, ["note", "list", "--json"], beacon_root)

        context_text = _format_context(status_raw, notes_raw or "[]")
        message = _build_message(context_text)

        sys.stdout.write(json.dumps({"systemMessage": message}, ensure_ascii=False))
        return 0
    except Exception:
        # Defensive: never block compaction on hook bugs.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
