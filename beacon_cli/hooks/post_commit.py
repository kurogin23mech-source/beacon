"""Cross-platform PostToolUse hook: commit / push / deploy detection.

Python translation of ``bin/beacon-post-commit-hook.sh`` (ms-43 e-613,
ms-44 e-777). Reads the Claude Code PostToolUse JSON payload on stdin
and emits a ``hookSpecificOutput`` JSON line on stdout when the matched
Bash command was a real commit / push / deploy — NOT a heredoc body or
quoted argument that merely *mentions* those patterns.

Filtering pipeline (matches bash):
  1. Drop heredoc bodies: ``<<[-]?'?MARKER'?`` ... ``MARKER`` (single or
     tab-indented marker). Multiple heredocs per command are supported.
  2. Strip single- and double-quoted string contents to neutralise
     ``--summary "...git push..."`` style false positives.
  3. Match the surviving "bare" command text against the same regex set
     as the bash script.

If no ``.beacon/project.json`` is reachable from the resolved cwd we
exit silently (return code 0, no output) — same fail-safe contract the
bash version provides.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Heredoc + quote stripping
# ---------------------------------------------------------------------------


# Anchored at the start so we identify the FIRST heredoc opener on a line.
# We accept ``<<`` or ``<<-`` then optional space, then optional single/double
# quote, then an identifier marker, then optional matching close quote.
_HEREDOC_OPENER_RE = re.compile(
    r"<<-?[ \t]*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?"
)


def _strip_heredoc_bodies(text: str) -> str:
    """Remove heredoc body lines (including the closing marker line stays in
    the output to keep line counts roughly aligned, matching bash awk).

    Supports multiple heredocs on different lines. The marker may be quoted
    in the opener (``<<'EOF'``, ``<<"EOF"``) — quotes are stripped before
    matching the closing line.
    """
    out_lines = []
    in_heredoc = False
    marker = ""
    for line in text.splitlines():
        if in_heredoc:
            # Trim leading tabs/spaces for <<- markers, then compare full line.
            trimmed = line.lstrip(" \t")
            if trimmed == marker:
                # Keep the marker line (its text is not user content).
                out_lines.append(line)
                in_heredoc = False
                marker = ""
            # else: drop the heredoc body line
            continue

        m = _HEREDOC_OPENER_RE.search(line)
        if m:
            marker = m.group(1)
            in_heredoc = True
            out_lines.append(line)
            continue

        out_lines.append(line)
    return "\n".join(out_lines)


# Greedy-by-default but the character classes [^"]* / [^']* terminate at the
# next matching quote, which is what we want (mirrors the bash sed).
_DQUOTE_RE = re.compile(r'"[^"]*"')
_SQUOTE_RE = re.compile(r"'[^']*'")


def _strip_quoted_strings(text: str) -> str:
    """Remove the contents of single- and double-quoted strings."""
    text = _DQUOTE_RE.sub("", text)
    text = _SQUOTE_RE.sub("", text)
    return text


def _bare_command(cmd: str) -> str:
    """Apply the heredoc + quote strip pipeline used by the bash hook."""
    return _strip_quoted_strings(_strip_heredoc_bodies(cmd))


# ---------------------------------------------------------------------------
# Project root resolution
# ---------------------------------------------------------------------------


def _find_beacon_root(start: Path) -> Optional[Path]:
    """Walk up from ``start`` looking for ``.beacon/project.json``."""
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


def _resolve_command_cwd(tool_cwd: str) -> Path:
    """Expand ``~`` and return the directory the bash command actually ran in.

    Falls back to ``os.getcwd()`` when the payload didn't include
    ``tool_input.cwd`` (legacy contract).
    """
    if tool_cwd:
        # bash expands ~ only at the start of the path; mimic that. The cwd
        # originates from the Bash tool (git-bash), where ``~`` means ``$HOME``.
        # os.path.expanduser ignores HOME on Windows (it keys off USERPROFILE),
        # so honor HOME explicitly when it resolves to a real directory; else
        # fall back to expanduser. (ms-44 e-844)
        if tool_cwd.startswith("~"):
            home = os.environ.get("HOME")
            if not (home and os.path.isabs(home) and os.path.isdir(home)):
                home = os.path.expanduser("~")
            tool_cwd = home + tool_cwd[1:]
        return Path(tool_cwd)
    return Path(os.getcwd())


# ---------------------------------------------------------------------------
# Detection rules — keep parallel with bash regexes
# ---------------------------------------------------------------------------


_COMMIT_RE = re.compile(r"git commit ")
_PUSH_RE = re.compile(r"git push")
_DEPLOY_RES = [
    re.compile(r"gcloud run deploy|gcloud app deploy|scripts/deploy\.sh"),
    re.compile(
        r"aws s3 sync.*s3://|terraform apply|aws cloudfront create-invalidation"
    ),
    re.compile(
        r"vercel( --prod| deploy)|firebase deploy|fly deploy|flyctl deploy|netlify deploy"
    ),
    re.compile(
        r"kubectl apply|cdk deploy|serverless deploy|sls deploy|pulumi up|"
        r"eb deploy|az webapp deploy|az functionapp"
    ),
]


def _classify(bare_cmd: str) -> Optional[Tuple[str, str]]:
    """Return ``(skill_name, message)`` for the matched action, or None.

    Precedence: commit > push > deploy (mirrors the bash if/elif chain).
    """
    if _COMMIT_RE.search(bare_cmd):
        return (
            "/beacon-log",
            "BEACON: Commit detected. You MUST now run /beacon-log Skill to record this commit.",
        )
    if _PUSH_RE.search(bare_cmd):
        return (
            "/beacon-push",
            "BEACON: Push detected. You MUST now run /beacon-push Skill to record this push.",
        )
    for rx in _DEPLOY_RES:
        if rx.search(bare_cmd):
            return (
                "/beacon-deploy",
                "BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment.",
            )
    return None


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def _emit(message: str, beacon_root: Path) -> None:
    """Write the Claude Code PostToolUse additionalContext JSON to stdout.

    Format intentionally matches the bash ``printf`` exactly so downstream
    Claude Code parsing stays identical.
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"{message} (project: {beacon_root})",
        }
    }
    # No trailing newline, matching the bash printf.
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    """Console-script entry point.

    Returns 0 on success (whether or not we matched). Any unexpected error
    is also swallowed and reported as 0 — a misbehaving hook MUST NOT block
    the user's Bash tool call.
    """
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return 0  # Malformed payload — fail-safe silent exit.

        tool_input = payload.get("tool_input") or {}
        cmd = tool_input.get("command", "") or ""
        tool_cwd = tool_input.get("cwd", "") or ""

        cwd = _resolve_command_cwd(tool_cwd)
        beacon_root = _find_beacon_root(cwd)
        if beacon_root is None:
            return 0

        bare = _bare_command(cmd)
        classified = _classify(bare)
        if classified is None:
            return 0
        _, message = classified
        _emit(message, beacon_root)
        return 0
    except Exception:
        # Last-ditch: never let a hook bug propagate to Claude Code.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
