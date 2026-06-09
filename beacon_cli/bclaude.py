"""bclaude — Claude Code launcher with the Beacon DM channel pre-wired.

Python entry-point for `bclaude` (ms-54 e-1328). Mirrors `bin/bclaude` (bash)
so `pip install beacon` produces a `bclaude` / `bclaude.exe` in the user's
Scripts directory — critical for Windows where the bash wrapper is unusable
and the .cmd wrapper isn't installed via pip.

Behaviour:

    bclaude [...claude args]

is equivalent to:

    claude --dangerously-load-development-channels server:beacon-bus [...claude args]

…unless the user has opted out (env BEACON_NO_BUS, project flag in
``.beacon/project.json``, or global flag in ``~/.beacon/config.json``), in
which case it launches plain ``claude`` with a one-line audit message on
stderr.

Resolution order (per-OS):

- **Unix (macOS / Linux)**: prefer ``bin/bclaude`` (bash wrapper) when the
  source/editable layout exposes it — keeps zero startup overhead for
  existing Homebrew users and ensures the bash version stays the reference
  implementation. Falls back to the Python forwarding path when bash or the
  script isn't available (pipx-only installs).
- **Windows**: always use the Python forwarding path. Git-for-Windows ships
  a ``bash`` on PATH but ``bin/bclaude`` lives at a POSIX path the cmd-bash
  hybrid can't always resolve, and ``bin/bclaude.cmd`` is not bundled by
  pip. Python forwarding is the safest cross-environment option.

Opt-out precedence (must match ``bin/bclaude`` and ``lib/commands.py``
``_is_bus_opted_out()`` byte-for-byte):

  1. env ``BEACON_NO_BUS`` (any truthy value: not in {"", "0", "false",
     "no", "off"}) — transient, current shell only.
  2. project ``.beacon/project.json`` ``bus.disabled`` — scoped to cwd.
  3. global ``~/.beacon/config.json`` ``bus.disabled`` — applies everywhere.

Any of the three being set → fall back to plain ``claude`` and print a
one-line audit message to stderr.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Opt-out probe
# ---------------------------------------------------------------------------
#
# Keep this logic in lockstep with bin/bclaude (bash) and
# lib/commands.py:_is_bus_opted_out(). The three sources are checked in the
# documented precedence; the first hit wins and is returned as a
# human-readable reason string that the caller prints to stderr.

# Same falsy-string set the bash wrapper uses. Anything else (including
# "1", "true", "yes", and weird values like "foo") counts as enabled.
_FALSY_BUS_VALUES = {"", "0", "false", "no", "off"}


def _env_opt_out() -> Optional[str]:
    """Return a reason string if BEACON_NO_BUS is set to a truthy value."""
    val = os.environ.get("BEACON_NO_BUS")
    if val is None:
        return None
    if val in _FALSY_BUS_VALUES:
        return None
    return "env BEACON_NO_BUS"


def _read_bus_disabled(path: Path) -> bool:
    """Best-effort read of ``bus.disabled`` from a JSON config file.

    Returns ``False`` on any error (missing file, malformed JSON, permission
    error) — the bash wrapper swallows the same failures via ``try/except``
    so we keep parity.
    """
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    bus = data.get("bus")
    if not isinstance(bus, dict):
        return False
    return bool(bus.get("disabled"))


def _project_opt_out() -> Optional[str]:
    """Return reason string if cwd's .beacon/project.json disables the bus."""
    p = Path(".beacon") / "project.json"
    if p.exists() and _read_bus_disabled(p):
        return "project .beacon/project.json"
    return None


def _global_opt_out() -> Optional[str]:
    """Return reason string if global ~/.beacon/config.json disables the bus."""
    # Match bin/bclaude: prefer $HOME, fall back to $USERPROFILE (Windows).
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if not home:
        return None
    g = Path(home) / ".beacon" / "config.json"
    if g.exists() and _read_bus_disabled(g):
        return "global ~/.beacon/config.json"
    return None


def _opt_out_reason() -> Optional[str]:
    """Walk the three opt-out sources in precedence order."""
    for probe in (_env_opt_out, _project_opt_out, _global_opt_out):
        reason = probe()
        if reason:
            return reason
    return None


# ---------------------------------------------------------------------------
# Bash wrapper discovery (Unix only)
# ---------------------------------------------------------------------------


def _find_bash_wrapper() -> Optional[Path]:
    """Locate bin/bclaude (bash) in the source/editable layout, if present.

    Wheel installs don't bundle bin/, so this returns None there and the
    caller falls through to the Python forwarding path. Homebrew installs
    have bin/bclaude on PATH directly — they typically don't reach this
    Python entry-point at all because the bash wrapper wins PATH order.
    """
    here = Path(__file__).resolve().parent.parent  # beacon_cli/ -> repo root
    candidate = here / "bin" / "bclaude"
    if candidate.exists():
        return candidate
    return None


# ---------------------------------------------------------------------------
# Forwarding paths
# ---------------------------------------------------------------------------


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _exec_bash_wrapper(wrapper: Path, argv: list[str]) -> int:
    """Delegate to bin/bclaude (bash) — Unix preferred path."""
    bash = shutil.which("bash")
    if bash is None:
        # Source layout exists but bash isn't on PATH. Extremely rare on
        # Unix; fall back to direct Python forwarding rather than failing.
        return _python_forward(argv)
    try:
        return subprocess.call([bash, str(wrapper), *argv])
    except OSError as exc:
        _eprint(f"bclaude: failed to launch bash wrapper: {exc}")
        return 2


def _python_forward(argv: list[str]) -> int:
    """Forward directly to ``claude`` via subprocess.

    Used on Windows always, and on Unix when bin/bclaude isn't available
    (pipx / pip wheel installs). Probes opt-out sources and assembles the
    same argv the bash wrapper would.
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        _eprint("bclaude: 'claude' is not on PATH. Install Claude Code first.")
        _eprint("  https://docs.claude.com/claude-code/install")
        return 127

    reason = _opt_out_reason()
    if reason:
        # Honour the opt-out: launch plain `claude` and announce why so the
        # audit trail (tmux logs, shell history) shows it.
        _eprint(f"[bclaude] DM opt-out detected ({reason}), launching plain claude")
        cmd = [claude_bin, *argv]
    else:
        cmd = [
            claude_bin,
            "--dangerously-load-development-channels",
            "server:beacon-bus",
            *argv,
        ]

    try:
        return subprocess.call(cmd)
    except OSError as exc:
        _eprint(f"bclaude: failed to launch claude: {exc}")
        return 2


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """Entry-point declared in pyproject.toml ``[project.scripts]``.

    Returns the exit code of the launched ``claude`` process so shell
    pipelines (``bclaude ... && echo ok``) behave identically to running
    ``claude`` directly.
    """
    if argv is None:
        argv = sys.argv[1:]

    # Windows: always use the Python forwarding path. bin/bclaude.cmd isn't
    # bundled by pip, and Git-for-Windows bash can't reliably resolve POSIX
    # paths to bin/bclaude.
    if os.name == "nt":
        return _python_forward(argv)

    # Unix: prefer bin/bclaude (bash) for parity with Homebrew users and to
    # keep the bash wrapper as the reference implementation. Falls through
    # to Python forwarding when the wrapper isn't present (pipx install).
    wrapper = _find_bash_wrapper()
    if wrapper is not None:
        return _exec_bash_wrapper(wrapper, argv)

    return _python_forward(argv)


if __name__ == "__main__":
    raise SystemExit(main())
