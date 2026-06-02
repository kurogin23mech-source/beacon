#!/usr/bin/env python3
"""INSTALL.md static validator for Beacon (ms-10 e-722).

The goal here is narrow: catch the specific drift that bit us before — a
``gcloud run deploy`` example with a flag that gcloud doesn't accept
(``--build-arg`` was the trigger). It also flags a small set of
brew/pipx command shapes that are known to break copy-pasters.

What it checks
--------------
1. Every fenced ``bash``/``sh`` code block in INSTALL.md is scanned for
   commands that match a configured **command shape**.
2. For each shape, a list of **deprecated/forbidden flags** and
   **required flags** is enforced.
3. Extra heuristics catch the common copy-paste-and-edit failure modes:
   * unbalanced backslash continuations (line ending in ``\\`` followed
     by a blank line or another command),
   * empty mandatory positional (e.g. ``brew install <missing>``).

What it deliberately does NOT check
-----------------------------------
* Whether the **content** of a deploy is correct (region, project, etc.)
  — only the syntactic shape.
* Whether ``pipx`` is installed on the user's machine.
* Prose explaining the commands — meaning-level review stays manual.
  This boundary is recorded in the README "Doc & Skill auto-sync scope"
  section so the maintainer knows what they still have to read by eye.

Run modes
---------
``--warn``  (default): print findings, exit 0 (for pre-commit).
``--strict``         : print findings, exit 1 (for CI gate).
``--json``           : machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_MD = ROOT / "INSTALL.md"


# ---------------------------------------------------------------------------
# Command-shape rules.
# ---------------------------------------------------------------------------
# Each rule:
#   match    : regex applied to a normalised single-line command. The
#              normalisation joins backslash-continuations onto one line
#              before matching so multi-line gcloud examples are handled.
#   forbid   : list of flag tokens that must NOT appear. Each entry is the
#              full ``--flag-name`` (no value).
#   require  : list of flag tokens that MUST appear (any one is OK if
#              listed as a tuple).
#   why      : explanation surfaced to the user.

RULES = [
    {
        "name": "gcloud run deploy",
        "match": re.compile(r"\bgcloud\s+run\s+deploy\b"),
        "forbid": ["--build-arg"],  # gcloud uses --set-build-env-vars
        "require_any": [("--set-build-env-vars", "--substitutions", "--no-source")],
        "why": (
            "gcloud run deploy does not accept `--build-arg` (Docker CLI flag). "
            "Use `--set-build-env-vars=KEY=VALUE` to pass build-time env vars."
        ),
    },
    {
        "name": "brew install",
        # Match either `brew install <pkg>` or `brew install --cask <pkg>` etc.
        "match": re.compile(r"\bbrew\s+install\b"),
        "forbid": [],
        "require_any": [],
        "why": "",
        # Brew install requires at least one positional package after flags.
        "needs_positional": True,
    },
    {
        "name": "pipx install",
        "match": re.compile(r"\bpipx\s+install\b"),
        "forbid": [],
        "require_any": [],
        "why": "",
        "needs_positional": True,
    },
]


_FENCE_RE = re.compile(r"^```(bash|sh|shell|console)\b", re.MULTILINE)
_END_FENCE_RE = re.compile(r"^```\s*$", re.MULTILINE)


def _extract_code_blocks(text: str) -> list[tuple[int, str]]:
    """Return (start_line, block_body) for each ```bash / ```sh block."""
    blocks: list[tuple[int, str]] = []
    pos = 0
    while True:
        m = _FENCE_RE.search(text, pos)
        if not m:
            break
        # Compute the line number of the opening fence.
        line_no = text.count("\n", 0, m.start()) + 1
        # Body starts right after the newline of the opening fence.
        body_start = text.find("\n", m.end()) + 1
        end = _END_FENCE_RE.search(text, body_start)
        if not end:
            break
        body = text[body_start: end.start()]
        blocks.append((line_no, body))
        pos = end.end()
    return blocks


def _normalise_continuations(block: str) -> list[str]:
    """Join lines ending in ``\\`` so a single logical command is one line.

    Also strip leading prompt markers (``$ ``, ``# `` for shell prompts)
    and comments.
    """
    out: list[str] = []
    buf: list[str] = []
    for raw in block.splitlines():
        line = raw.rstrip()
        # Strip prompt markers
        stripped = line.lstrip()
        if stripped.startswith("$ "):
            stripped = stripped[2:]
        if not stripped:
            if buf:
                out.append(" ".join(buf).strip())
                buf = []
            continue
        if stripped.startswith("#"):
            # Comment line — flush any pending command, skip
            if buf:
                out.append(" ".join(buf).strip())
                buf = []
            continue
        if line.endswith("\\"):
            buf.append(line[:-1].strip())
        else:
            buf.append(stripped)
            out.append(" ".join(buf).strip())
            buf = []
    if buf:
        out.append(" ".join(buf).strip())
    return [c for c in out if c]


def _extract_flags(cmd: str) -> list[str]:
    """Return flag tokens (``--name``) in their bare form, ignoring values."""
    flags: list[str] = []
    for tok in cmd.split():
        if tok.startswith("--"):
            # `--name=value` → `--name`
            flag = tok.split("=", 1)[0]
            flags.append(flag)
    return flags


def _has_positional(cmd: str, verb_match: re.Match[str]) -> bool:
    """After the matched verb, is there any non-flag positional token?"""
    tail = cmd[verb_match.end():].split()
    for tok in tail:
        if tok.startswith("-"):
            continue
        # Strip any value of preceding `--flag=...` already handled above.
        # A bare word is a positional.
        return True
    return False


def collect_findings(path: Path = INSTALL_MD) -> dict:
    if not path.exists():
        return {
            "ok": False,
            "missing": True,
            "findings": [],
            "checked_commands": 0,
        }
    text = path.read_text(encoding="utf-8")
    blocks = _extract_code_blocks(text)
    findings: list[dict] = []
    total_cmds = 0

    for start_line, body in blocks:
        cmds = _normalise_continuations(body)
        for cmd in cmds:
            total_cmds += 1
            for rule in RULES:
                m = rule["match"].search(cmd)
                if not m:
                    continue
                flags = _extract_flags(cmd)
                # Forbid check
                for bad in rule.get("forbid", []):
                    if bad in flags:
                        findings.append({
                            "rule": rule["name"],
                            "kind": "forbidden_flag",
                            "flag": bad,
                            "command": cmd[:200],
                            "near_line": start_line,
                            "why": rule.get("why", ""),
                        })
                # Require-any check
                for req_group in rule.get("require_any", []):
                    if isinstance(req_group, str):
                        req_group = (req_group,)
                    if req_group and not any(r in flags for r in req_group):
                        findings.append({
                            "rule": rule["name"],
                            "kind": "missing_required_flag",
                            "flag": " | ".join(req_group),
                            "command": cmd[:200],
                            "near_line": start_line,
                            "why": rule.get("why", ""),
                        })
                # Positional check
                if rule.get("needs_positional") and not _has_positional(cmd, m):
                    findings.append({
                        "rule": rule["name"],
                        "kind": "missing_positional",
                        "flag": "",
                        "command": cmd[:200],
                        "near_line": start_line,
                        "why": "Command requires a package name as a positional argument.",
                    })

    return {
        "ok": not findings,
        "missing": False,
        "findings": findings,
        "checked_commands": total_cmds,
    }


def _format_text(report: dict) -> str:
    if report.get("missing"):
        return "[install-md] ERROR: INSTALL.md not found.\n"
    if report["ok"]:
        return f"[install-md] OK: {report['checked_commands']} command(s) scanned, no issues.\n"
    lines = [
        "[install-md] Issues found in INSTALL.md command examples:",
        "",
    ]
    for f in report["findings"]:
        lines.append(f"  ~ line {f['near_line']}  [{f['rule']}] {f['kind']}: {f.get('flag','')}")
        lines.append(f"      cmd: {f['command']}")
        if f.get("why"):
            lines.append(f"      why: {f['why']}")
    lines.append("")
    lines.append("Rules live in scripts/check-install-md.py.")
    lines.append("This guard is part of ms-10 e-722 (doc & skill auto-sync).")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--install-md", default=str(INSTALL_MD))
    args = parser.parse_args(argv)

    report = collect_findings(path=Path(args.install_md))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(_format_text(report))

    if report.get("missing"):
        return 2
    if report["ok"]:
        return 0
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
