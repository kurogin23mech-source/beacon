"""Version / environment skew detection (= ms-93 / e-3121).

Codex flagged this as "the biggest hole outside the A/B/C/D launch blockers":
when an old ``beacon`` CLI, hook, Skill, or receive daemon is mixed in with a
newer install, the user ends up "動いているようで別物を見ている" — commands run
against one version while a daemon started by another keeps serving. Today's
session hit exactly this (two ``beacon`` binaries on PATH, 0.56.1 vs 0.2.1, plus
session_id churn), which is why it belongs in the launch checklist
(launch-criteria doc ``jHSyK1gJJxroJZP8JI0c``).

This module is the **structural core**: pure functions that take the versions
already collectable elsewhere (the BEACON_BIN resolver probes PATH binaries; the
bus directory carries a running daemon's ``agent.version``) and turn them into
human-facing skew warnings. It never raises — skew detection must be best-effort
and fail-open so it can never block a launch (e-3121 AC 3). Callers
(session-start, ``bin/bcodex``) surface the returned lines; the detection logic
lives here so it is unit-testable without a live environment.
"""

from __future__ import annotations

from typing import Iterable


def distinct_versions(binaries: Iterable[dict]) -> list[str]:
    """Return the sorted distinct non-empty versions among probed binaries.

    ``binaries`` is a list of dicts shaped like the BEACON_BIN resolver's
    ``candidates_probed`` entries: each has a ``version_raw`` (e.g.
    ``"beacon 0.56.1"``) or ``version`` field and a ``path`` / ``bin_path``.
    Malformed rows are skipped rather than raising (fail-open).
    """
    seen: set[str] = set()
    for row in binaries or []:
        try:
            ver = _extract_version(row)
        except Exception:
            continue
        if ver:
            seen.add(ver)
    return sorted(seen)


def _extract_version(row: dict) -> str:
    """Pull a bare version string (``0.56.1``) out of a probed-binary row."""
    if not isinstance(row, dict):
        return ""
    raw = (row.get("version") or row.get("version_raw") or "").strip()
    if not raw:
        return ""
    # ``beacon 0.56.1`` -> ``0.56.1``; a bare ``0.56.1`` passes through.
    return raw.split()[-1].strip()


def _binary_path(row: dict) -> str:
    return str((row or {}).get("bin_path") or (row or {}).get("path") or "?")


def format_skew_report(
    current_version: str,
    binaries: Iterable[dict] | None = None,
    daemon_version: str = "",
    hook_version: str = "",
) -> list[str]:
    """Build human-facing skew warning lines. Empty list = no skew detected.

    Three skews are surfaced (all the "混じっている" cases Codex flagged):

    1. **Multiple CLI versions on PATH** — more than one distinct ``beacon``
       version among the probed binaries. This is the "別 binary を見ている"
       case: the shell may resolve a different ``beacon`` than the user expects.
    2. **Running daemon older/newer than this CLI** — ``daemon_version`` (e.g.
       the receive daemon's registered ``agent.version``) differs from
       ``current_version``. This is the "別 daemon を見ている" case: a daemon
       started by another install keeps serving stale behavior until restarted.
    3. **Installed hook older/newer than this CLI** — ``hook_version`` (the
       beacon version of the install serving the Codex ``UserPromptSubmit``
       hook) differs from ``current_version``. This is the "別 hook を見ている"
       case: DM inbox injection runs old code from another install. This is
       version skew, distinct from ``beacon doctor``'s content-hash
       skills-drift (e-3135 AC 3: complement, not duplicate).

    Never raises; any bad input degrades to "no warning for that check".
    """
    lines: list[str] = []
    binaries = list(binaries or [])

    try:
        versions = distinct_versions(binaries)
    except Exception:
        versions = []
    if len(versions) > 1:
        lines.append(
            "⚠ version skew: PATH 上に複数バージョンの beacon があります "
            f"({', '.join(versions)})。想定と別の beacon を叩いている恐れ:"
        )
        for row in binaries:
            v = _extract_version(row)
            if v:
                lines.append(f"    {v}  {_binary_path(row)}")
        lines.append(
            "  対処: 意図した install を PATH の先頭に置く / 不要な beacon を外す。"
        )

    cur = (current_version or "").strip()
    dv = (daemon_version or "").strip()
    if cur and dv and dv != cur:
        lines.append(
            "⚠ version skew: 起動中の受信 daemon は別バージョンです "
            f"(daemon={dv} / CLI={cur})。『動いているようで別 daemon を見ている』"
            "状態の恐れ。"
        )
        lines.append(
            "  対処: daemon を再起動して CLI と揃える "
            "(bcodex 再起動 / beacon channel install の再実行)。"
        )

    hv = (hook_version or "").strip()
    if cur and hv and hv != cur:
        lines.append(
            "⚠ version skew: install 済の Codex hook は別バージョンです "
            f"(hook={hv} / CLI={cur})。DM 受信の hook が古い install の "
            "コードで動いている恐れ。"
        )
        lines.append(
            "  対処: `beacon-codex-bridge install-hook` を再実行して hook を "
            "現行 install に貼り直す。"
        )

    return lines


def has_skew(
    current_version: str,
    binaries: Iterable[dict] | None = None,
    daemon_version: str = "",
    hook_version: str = "",
) -> bool:
    """True iff any skew warning would be produced (convenience predicate)."""
    return bool(
        format_skew_report(current_version, binaries, daemon_version, hook_version)
    )


def _probe_path_binaries(binary_name: str = "beacon") -> list[dict]:
    """Best-effort: every ``beacon`` on PATH with its ``--version`` output.

    Impure (spawns subprocesses); kept out of the pure helpers above so those
    stay unit-testable. Fail-open: any error yields an empty list.
    """
    import os
    import shutil
    import subprocess

    seen_paths: list[str] = []
    path_env = os.environ.get("PATH", "")
    for d in path_env.split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, binary_name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK) and cand not in seen_paths:
            seen_paths.append(cand)
    if not seen_paths:
        one = shutil.which(binary_name)
        if one:
            seen_paths = [one]

    rows: list[dict] = []
    for p in seen_paths:
        ver = ""
        try:
            out = subprocess.run(
                [p, "--version"], capture_output=True, text=True, timeout=5
            )
            ver = (out.stdout or out.stderr or "").strip().splitlines()[0] if (out.stdout or out.stderr) else ""
        except Exception:
            ver = ""
        rows.append({"path": p, "version_raw": ver})
    return rows


def _probe_daemon_version(cwd: str) -> str:
    """Best-effort read of a running Codex receive daemon's stamped version.

    e-3135: the daemon writes ``beacon_version`` into
    ``<cwd>/.beacon/codex/receive-loop.session.json``. We only report it when
    the daemon pid (``receive-loop.pid``) is still alive — a stale pointer from
    a dead daemon must not raise a false skew warning. Impure (reads files /
    signals a pid); fail-open to "".
    """
    import json
    import os
    from pathlib import Path

    try:
        base = Path(cwd) / ".beacon" / "codex"
        pid_file = base / "receive-loop.pid"
        pointer = base / "receive-loop.session.json"
        if not pid_file.is_file() or not pointer.is_file():
            return ""
        pid = int(pid_file.read_text().strip())
        if pid <= 0:
            return ""
        # Liveness probe: signal 0 raises if the pid is gone.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ""
        except PermissionError:
            pass  # alive but owned by another user — still counts as running
        data = json.loads(pointer.read_text(encoding="utf-8"))
        return str(data.get("beacon_version") or "").strip()
    except Exception:
        return ""


def _read_beacon_version_from_root(root) -> str:
    """Regex ``__version__`` out of ``commands.py`` under a beacon root.

    Handles both the source layout (``<root>/lib/commands.py``) and the wheel
    layout (``<root>/_bundled_lib/commands.py``). Fail-open to "".
    """
    import re
    from pathlib import Path

    root = Path(root)
    for sub in ("lib", "_bundled_lib"):
        try:
            src = (root / sub / "commands.py").read_text(encoding="utf-8")
        except Exception:
            continue
        m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
        if m:
            return m.group(1)
    return ""


def _probe_hook_version() -> str:
    """Best-effort read of the beacon version serving the Codex inbox hook.

    e-3135 AC 3: the ``UserPromptSubmit`` entry in ``$CODEX_HOME/hooks.json``
    (default ``~/.codex``) runs ``.../scripts/codex-inbox-hook.py`` at an
    absolute path. We derive that install's root (script's grandparent — same
    convention as the daemon / bridge) and read its beacon version, so a hook
    left behind by an older install surfaces as skew. Fail-open to "".

    Respects ``$CODEX_HOME`` (e-3202) so isolated / dogfood environments are
    read correctly rather than always hitting the real ``~/.codex``.
    """
    import json
    import os
    from pathlib import Path

    try:
        codex_home = os.environ.get("CODEX_HOME", "").strip()
        base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
        hooks_path = base / "hooks.json"
        if not hooks_path.is_file():
            return ""
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        buckets = (data.get("hooks") or {}).get("UserPromptSubmit") or []
        for bucket in buckets:
            for hook in (bucket.get("hooks") or []):
                cmd = str(hook.get("command") or "")
                if "codex-inbox-hook.py" not in cmd:
                    continue
                # command == "/usr/bin/python3 <script> --cwd <cwd>"; pull the
                # script token that ends with the hook basename.
                for tok in cmd.split():
                    if tok.endswith("codex-inbox-hook.py"):
                        root = Path(tok).resolve().parent.parent
                        return _read_beacon_version_from_root(root)
        return ""
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    """CLI entry: print skew warnings for the current environment (fail-open).

    ``bin/bcodex``, session-start, and other launchers call this and route
    stdout to the user's stderr. Prints nothing when there is no skew. Always
    exits 0 — skew is a warning, never a launch blocker (e-3121 AC 3).

    ``--cwd`` (e-3135): when given and ``--daemon-version`` is not supplied,
    auto-discover a running receive daemon's stamped version so callers don't
    each re-implement the pointer read.
    """
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--current-version", default="")
    parser.add_argument("--daemon-version", default="")
    parser.add_argument("--hook-version", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--no-hook-probe", action="store_true")
    try:
        args, _ = parser.parse_known_args(argv)
    except SystemExit:
        return 0

    try:
        binaries = _probe_path_binaries()
    except Exception:
        binaries = []

    current = (args.current_version or "").strip()
    if not current and binaries:
        current = _extract_version(binaries[0])

    daemon_version = (args.daemon_version or "").strip()
    if not daemon_version and args.cwd:
        daemon_version = _probe_daemon_version(args.cwd)

    # Hook version is global ($CODEX_HOME/hooks.json), not cwd-scoped, so it is
    # probed unconditionally unless suppressed. Harmless on non-Codex machines
    # (no hooks.json → "").
    hook_version = (args.hook_version or "").strip()
    if not hook_version and not args.no_hook_probe:
        hook_version = _probe_hook_version()

    try:
        for line in format_skew_report(current, binaries, daemon_version, hook_version):
            print(line)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
