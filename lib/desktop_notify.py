"""Desktop notification fallback (= ms-93 / e-2519 AC 3, SPEC §8-G option C).

When autonomous reply is unavailable — the bridge is not armed, or neither the
app-server (D) nor exec-worker (B) transport is running — a DM that arrives
while the user is away would otherwise just sit in the inbox until the next
prompt. Option C is the safe, low-risk middle ground: pop a desktop
notification so the human notices and can reply manually (their next Codex
prompt injects the DM via the existing hook path). No autonomous action, no
sandbox concerns — just an alert.

Cross-platform, dependency-free, best-effort:
- macOS   → ``osascript -e 'display notification ...'``
- Linux   → ``notify-send``
- Windows → PowerShell balloon (``System.Windows.Forms.NotifyIcon``)

The command builders are pure (given a ``platform``) so tests pin the argv per
OS; the actual firing is best-effort and never raises.
"""

from __future__ import annotations

import subprocess
import sys


def _osascript_argv(title: str, message: str) -> list[str]:
    def esc(s: str) -> str:
        # AppleScript string literal: escape backslash then double-quote.
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(message)}" with title "{esc(title)}"'
    )
    return ["osascript", "-e", script]


def _notify_send_argv(title: str, message: str) -> list[str]:
    return ["notify-send", title, message]


def _powershell_toast_argv(title: str, message: str) -> list[str]:
    def esc(s: str) -> str:
        # PowerShell single-quoted literal: double any single quote.
        return s.replace("'", "''")

    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(5000, '{esc(title)}', '{esc(message)}', "
        "[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Seconds 5; $n.Dispose()"
    )
    return ["powershell", "-NoProfile", "-Command", ps]


def build_notify_command(
    title: str, message: str, *, platform: str = ""
) -> list[str] | None:
    """Argv for a desktop notification on ``platform`` (default: current OS).

    Returns ``None`` on an unknown platform so the caller silently no-ops."""
    plat = platform or sys.platform
    if plat == "darwin":
        return _osascript_argv(title, message)
    if plat.startswith("linux"):
        return _notify_send_argv(title, message)
    if plat.startswith("win") or plat == "cygwin":
        return _powershell_toast_argv(title, message)
    return None


def _default_run(argv: list[str]):
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=10
    )


def notify(title: str, message: str, *, platform: str = "", run=None) -> bool:
    """Fire a best-effort desktop notification. Returns True on success.

    Never raises: an unknown platform, a missing ``osascript`` / ``notify-send``
    binary, or a non-zero exit all resolve to ``False`` so a headless daemon is
    never taken down by the alert path."""
    argv = build_notify_command(title, message, platform=platform)
    if argv is None:
        return False
    runner = run or _default_run
    try:
        proc = runner(argv)
    except Exception:
        return False
    return getattr(proc, "returncode", 1) == 0


__all__ = ["build_notify_command", "notify"]
