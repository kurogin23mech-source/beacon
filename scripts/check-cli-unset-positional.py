#!/usr/bin/env python3
"""Census: no `beacon` invocation may die from an unset positional parameter.

`bin/beacon` runs under ``set -euo pipefail``, so a single unguarded ``$1`` /
``$2`` reference turns a missing argument into an abrupt
``$1: unbound variable`` exit instead of the usage message the dispatcher
means to print. That failure mode is environment-sensitive — it was first seen
only on CI (Linux / bash 5, parallel load), never locally (macOS / bash 3.2) —
so a guard that only reads the source statically is not enough: the probe has
to RUN, in whatever environment is asking.

The census walks every command path the help registry advertises (the same
single registry `beacon help --json` renders from, so a verb added later is
probed without touching this file) and invokes it in three shapes:

  1. the full verb path with no further arguments
  2. the verb path minus its last token (a partial path)
  3. the verb path plus one of its own flags, with the flag's value missing

Every probe runs under the read-only probe gate (`BEACON_SURFACE_PROBE`,
lib/readonly_gate.py), so a probe that reaches a verb which writes is refused
at the python chokepoint rather than mutating anything. HOME and the working
directory are redirected into a scratch tree as well, so a probe cannot touch
the operator's real settings.

Exit 0 = no invocation died this way. Exit 1 = at least one did; each is
reported with the exact argv and the shell's own error line.

Deliberately NOT a pytest test: the CI drift guards also run from jobs that
have no pytest installed (see scripts/ci-strict-drift-guards.sh).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

MARKER = "unbound variable"

# Verbs that take over the terminal (a dashboard / viewer) or wait on a
# human. Probing them would hang, not inform.
SKIP_FIRST_TOKEN = {"start", "view", "setup", "launch"}

# The gate's env vars, from lib/readonly_gate.py. Stripped from the probe env
# so an ambient value in the parent process cannot make every probe refuse
# under the WRONG reason (which would fabricate a clean census).
GATE_ENV_VARS = ("BEACON_HELP_ONLY", "BEACON_SURFACE_PROBE")
GATE_REASON_ENV = "BEACON_SURFACE_PROBE"


def _verb_paths(beacon_bin: pathlib.Path) -> list[dict]:
    proc = subprocess.run([str(beacon_bin), "help", "--json"],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise SystemExit(
            f"cannot read the help registry: `beacon help --json` exited "
            f"{proc.returncode}\n{proc.stderr}")
    return json.loads(proc.stdout)["commands"]


def _shapes(entries: list[dict], flags_per_command: int) -> list[list[str]]:
    """The argv shapes to probe, in registry order, de-duplicated."""
    shapes: list[list[str]] = []
    for entry in entries:
        tokens = entry["command"].split()
        if not tokens or tokens[0] != "beacon":
            continue
        # Drop the registry's placeholders (`<id>`, `[--json]`, …) — we want
        # the verb path itself, with its arguments deliberately absent.
        verb = [t for t in tokens[1:] if not t.startswith(("-", "<", "[", '"'))]
        if not verb or verb[0] in SKIP_FIRST_TOKEN:
            continue
        shapes.append(verb)
        if len(verb) > 1:
            shapes.append(verb[:-1])
        for flag in entry.get("flags", [])[:flags_per_command]:
            name = flag.split("=")[0].split()[0]
            if name.startswith("-"):
                shapes.append(verb + [name])

    seen: set[tuple[str, ...]] = set()
    unique = []
    for shape in shapes:
        key = tuple(shape)
        if key not in seen:
            seen.add(key)
            unique.append(shape)
    return unique


def run_census(beacon_bin: pathlib.Path, scratch: pathlib.Path,
               flags_per_command: int = 4,
               timeout: int = 25) -> tuple[int, list[tuple[str, int, str]]]:
    home = scratch / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    cwd = scratch / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)

    env = {k: v for k, v in os.environ.items() if k not in GATE_ENV_VARS}
    env["HOME"] = str(home)
    env[GATE_REASON_ENV] = "1"

    findings: list[tuple[str, int, str]] = []
    probed = 0
    for shape in _shapes(_verb_paths(beacon_bin), flags_per_command):
        try:
            proc = subprocess.run([str(beacon_bin)] + shape,
                                  capture_output=True, text=True, env=env,
                                  cwd=str(cwd), timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            # A probe we could not finish is not evidence either way. Say so
            # by not counting it, rather than recording a clean result.
            continue
        probed += 1
        stderr = proc.stderr or ""
        if MARKER in stderr:
            line = next(l for l in stderr.splitlines() if MARKER in l)
            findings.append(("beacon " + " ".join(shape), proc.returncode, line))
    return probed, findings


def main() -> int:
    repo = pathlib.Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--beacon-bin", default=str(repo / "bin" / "beacon"))
    parser.add_argument("--flags-per-command", type=int, default=4,
                        help="how many of each command's own flags to probe "
                             "with a missing value (0 disables that shape)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="beacon-unset-positional-") as tmp:
        probed, findings = run_census(
            pathlib.Path(args.beacon_bin), pathlib.Path(tmp),
            flags_per_command=args.flags_per_command)

    if findings:
        print(f"FAIL: {len(findings)} invocation(s) of {probed} died from an "
              f"unset positional parameter.", file=sys.stderr)
        print("`bin/beacon` runs under `set -u`, so a missing argument must "
              "reach the command's usage message, never an unbound-variable "
              "exit. Guard the reference (`\"${1:-}\"`) or check `$#` first.",
              file=sys.stderr)
        for argv, rc, line in findings:
            print(f"  {argv}  (exit {rc})\n      {line}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"OK: {probed} invocation(s) probed; none died from an unset "
              f"positional parameter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
