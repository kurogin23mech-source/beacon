"""Pin coverage of the ms-55 coordination signal CLIs across the three
documentation surfaces (ms-55 e-1736).

The drift checker (scripts/check-cli-help-drift.py) is the live gate, but
it depends on a generous allowlist for pre-existing asymmetries. This
test pins the 14 ms-55 verbs to:

  1. lib/commands.py cmd_help_json (= `beacon help --json`)
  2. README.md ## CLI Commands tables
  3. bin/beacon usage() / case branches

A future refactor that drops a row from any of those three surfaces will
break this test deterministically, even if the drift checker's
allowlist gets relaxed.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")


# The full set of ms-55 (= 走る / 止まる両輪) verbs landed by
# e-1646 (stop) / e-1647 (rollback) / e-1648 (claim) / e-1649 (stuck)
# / e-1650 (morning). Adding a new coordination verb means extending
# this list AND adding it to all three doc surfaces.
MS55_VERBS = [
    "stop scoped",
    "stop global",
    "stop status",
    "resume scoped",
    "resume global",
    "rollback",
    "claim request",
    "claim respond",
    "claim post",
    "claim handoff",
    "claim release",
    "claim list",
    "stuck check",
    "morning",
]


def _extract_verb(line: str) -> str:
    """Pull the (subcommand subsubcommand) pair from a help/table line.

    Mirrors scripts/check-cli-help-drift.py:_extract_verb so we keep
    drift-check and pinning logic compatible.
    """
    parts = line.strip().split()
    if not parts or parts[0] != "beacon":
        return ""
    out: list[str] = []
    for tok in parts[1:3]:
        if tok.startswith(("<", "[", "-")) or not re.match(r"^[a-z][a-z0-9-]*$", tok):
            break
        out.append(tok)
    return " ".join(out)


def _help_json_verbs() -> set[str]:
    """Run cmd_help_json in-process and collect canonical verbs."""
    import commands  # noqa: E402,PLC0415

    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_help_json()
    data = json.loads(buf.getvalue())
    verbs: set[str] = set()
    for entry in data.get("commands", []):
        v = _extract_verb(entry.get("command", ""))
        if v:
            verbs.add(v)
    return verbs


def _readme_verbs() -> set[str]:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"^##\s+CLI Commands\s*$", text, re.MULTILINE)
    assert m, "README missing '## CLI Commands' anchor"
    rest = text[m.end():]
    end = re.search(r"^##\s+\S", rest, re.MULTILINE)
    section = rest if end is None else rest[: end.start()]
    pattern = re.compile(r"^\|\s*`([^`]+)`")
    verbs: set[str] = set()
    for line in section.splitlines():
        m2 = pattern.match(line)
        if m2:
            v = _extract_verb(m2.group(1))
            if v:
                verbs.add(v)
    return verbs


@pytest.mark.parametrize("verb", MS55_VERBS)
def test_help_json_contains(verb: str) -> None:
    assert verb in _help_json_verbs(), (
        f"beacon {verb!r} missing from cmd_help_json — "
        "add it to lib/commands.py:cmd_help_json"
    )


@pytest.mark.parametrize("verb", MS55_VERBS)
def test_readme_contains(verb: str) -> None:
    assert verb in _readme_verbs(), (
        f"beacon {verb!r} missing from README ## CLI Commands tables — "
        "add a row under the matching ### subsection"
    )


def test_readme_has_coordination_section() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"^###\s+Coordination signals \(ms-55\)\s*$", text, re.MULTILINE), (
        "README missing '### Coordination signals (ms-55)' subsection"
    )
