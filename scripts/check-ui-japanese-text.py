#!/usr/bin/env python3
"""
ms-82 / e-1978 — UI text Japanese grep gate.

Scans `server/static/index.html` and `desktop/layer.js` for Japanese
characters in user-visible text (= outside developer comments) and prints a
warn-only summary. Blocks nothing; the goal is to surface English-ization
regressions on PRs so they can be fixed before merge.

Heuristic: line-based scan. Skip lines that are pure comments
(`//`, `/* ... */` on one line, `*` continuation lines, or fully inside an
HTML `<!-- ... -->` block). Multiline `/* ... */` blocks are tracked via a
simple in_block flag.

Existing comment JP (developer-facing context inside source) is intentionally
ignored — UI text lives in HTML markup / template literals / string content
returned to the browser.

Exit code is always 0 (warn-only).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

JP_RE = re.compile(r"[぀-ゟ゠-ヿ一-龯]")
TARGETS = (
    "server/static/index.html",
    "desktop/layer.js",
)


def _strip_inline_comments(line: str) -> str:
    """Best-effort: drop `// ...` and `/* ... */` portions from a line so a
    comment that happens to contain JP doesn't trigger a warning. Conservative —
    if quoting is unclear, we keep the line as-is (false positives are cheap;
    silent misses are not)."""
    # // line comment — only if it's actually at the start of the visible text.
    # Avoid stripping inside string literals; check for a leading `//` after
    # only whitespace.
    stripped = line.lstrip()
    if stripped.startswith("//"):
        return ""
    # /* ... */ inline block
    line = re.sub(r"/\*.*?\*/", "", line)
    # <!-- ... --> single-line HTML comment
    line = re.sub(r"<!--.*?-->", "", line)
    return line


def scan_file(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    in_block_comment = False
    in_html_comment = False
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return hits
    for idx, raw in enumerate(text.splitlines(), 1):
        line = raw
        # Track /* ... */ across lines.
        if in_block_comment:
            end = line.find("*/")
            if end == -1:
                continue
            line = line[end + 2 :]
            in_block_comment = False
        # Track <!-- ... --> across lines.
        if in_html_comment:
            end = line.find("-->")
            if end == -1:
                continue
            line = line[end + 3 :]
            in_html_comment = False
        # Detect block comment start (no closing on this line).
        start = line.find("/*")
        if start != -1 and "*/" not in line[start:]:
            in_block_comment = True
            line = line[:start]
        html_start = line.find("<!--")
        if html_start != -1 and "-->" not in line[html_start:]:
            in_html_comment = True
            line = line[:html_start]
        # Continuation of a JSDoc-style block (` * description...`).
        if re.match(r"\s*\*(\s|$)", line):
            continue
        line = _strip_inline_comments(line)
        if JP_RE.search(line):
            preview = line.strip()[:120]
            hits.append((idx, preview))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=os.environ.get("REPO_ROOT") or os.getcwd(),
        help="Repository root (defaults to cwd).",
    )
    args = parser.parse_args()
    root = Path(args.root)
    total = 0
    any_hits = False
    for rel in TARGETS:
        path = root / rel
        if not path.exists():
            continue
        hits = scan_file(path)
        if not hits:
            continue
        any_hits = True
        total += len(hits)
        print(f"[ui-japanese-grep] {rel}: {len(hits)} non-comment line(s) with Japanese")
        for line_no, preview in hits[:10]:
            print(f"  L{line_no}: {preview}")
        if len(hits) > 10:
            print(f"  ... and {len(hits) - 10} more")
    if any_hits:
        print(
            "[ui-japanese-grep] WARN: Japanese characters detected in UI text "
            "(warn-only — fix in a follow-up commit). Set "
            "BEACON_SKIP_UI_JP_CHECK=1 to silence."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
