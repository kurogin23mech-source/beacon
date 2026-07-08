"""Desktop notification fallback (ms-93 / e-2519 AC 3, SPEC §8-G option C).

Pins the per-OS command shape and the best-effort contract (never raises,
unknown platform / failing binary → False).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    path = REPO_ROOT / "lib" / "desktop_notify.py"
    spec = importlib.util.spec_from_file_location("desktop_notify_test", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_macos_uses_osascript_display_notification():
    dn = _load()
    argv = dn.build_notify_command("Beacon: new DM", "from sv-x: hi", platform="darwin")
    assert argv[0] == "osascript"
    assert argv[1] == "-e"
    assert "display notification" in argv[2]
    assert 'with title "Beacon: new DM"' in argv[2]
    assert "from sv-x: hi" in argv[2]


def test_macos_escapes_quotes_in_message():
    dn = _load()
    argv = dn.build_notify_command('t"itle', 'a "quote" and \\ back', platform="darwin")
    # No raw unescaped double-quote breaks the AppleScript string literal.
    assert '\\"quote\\"' in argv[2]
    assert "\\\\ back" in argv[2]


def test_linux_uses_notify_send():
    dn = _load()
    argv = dn.build_notify_command("T", "M", platform="linux")
    assert argv == ["notify-send", "T", "M"]


def test_windows_uses_powershell_balloon():
    dn = _load()
    argv = dn.build_notify_command("T", "M", platform="win32")
    assert argv[0] == "powershell"
    assert "ShowBalloonTip" in argv[-1]


def test_unknown_platform_returns_none():
    dn = _load()
    assert dn.build_notify_command("T", "M", platform="plan9") is None


def test_notify_returns_true_on_success():
    dn = _load()
    calls = []

    def fake_run(argv):
        calls.append(argv)

        class _P:
            returncode = 0

        return _P()

    assert dn.notify("T", "M", platform="darwin", run=fake_run) is True
    assert calls and calls[0][0] == "osascript"


def test_notify_returns_false_on_nonzero_exit():
    dn = _load()

    def fake_run(argv):
        class _P:
            returncode = 1

        return _P()

    assert dn.notify("T", "M", platform="linux", run=fake_run) is False


def test_notify_never_raises_when_binary_missing():
    dn = _load()

    def boom(argv):
        raise FileNotFoundError("notify-send not installed")

    assert dn.notify("T", "M", platform="linux", run=boom) is False


def test_notify_unknown_platform_is_false_without_running():
    dn = _load()
    ran = []
    dn.notify("T", "M", platform="plan9", run=lambda a: ran.append(a))
    assert ran == []
