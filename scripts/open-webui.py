#!/usr/bin/env python3
"""Open the Beacon front-end for the current project (extracted ms-85 e-3178).

Previously inline in the session-start Skill (Step 2.7) as a shell block that
embedded two ``python3 -c`` snippets. e-3178 extracts it verbatim-in-behavior
to a script so the Skill body carries one invocation line instead of ~35 lines
of shell + python.

Behavior — one front-end per mode, mirroring how the project is backed:
  * cloud mode (``.beacon/cloud.json`` with a ``project_id``) -> build
    ``https://beacon-ai.dev/?project=<id>``, resolve the default macOS https
    handler (falling back to Safari when the handler is Beacon.app, to avoid
    launching the Tauri app — ms-46 e-737), launch the browser with the same
    open/xdg-open/cmd.exe/powershell fallback chain, and print
    ``WEBUI_URL=<url>`` so session-start can show it in the header.
  * local mode (no ``.beacon/cloud.json`` or no ``project_id``) -> launch the
    Beacon Tauri desktop app (which reads the local ``.beacon/project.json``),
    and print ``DESKTOP_LAUNCHED=<name>`` so session-start can show it. If the
    desktop app isn't installed, print nothing (best-effort, never blocks).

A cloud project's live front-end is the Web UI (the server is the source of
truth); a local project's is the desktop app (there is no server URL). So the
mode that backs the data also picks the front-end that renders it. This
restores the local-mode launch that was dropped when Step 2.7 was condensed to
the cloud-only Web UI open.

Always exits 0 — never blocks session-start.

Call sites:
  * /beacon-session-start Skill (Step 2.7)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def _project_id() -> str:
    try:
        with open(".beacon/cloud.json") as f:
            return str(json.load(f).get("project_id", "") or "")
    except Exception:
        return ""


def _default_browser() -> str:
    """Return the default macOS https handler app id, avoiding Beacon.app.

    ms-46 e-737: ``open <url>`` can let macOS route the URL to Beacon.app as a
    URL handler and boot Tauri (confusing in cloud mode). We read the default
    https handler directly and skip any handler whose id mentions 'beacon',
    falling back to Safari (always present).
    """
    import plistlib

    p = os.path.expanduser(
        "~/Library/Preferences/com.apple.LaunchServices/"
        "com.apple.launchservices.secure.plist"
    )
    try:
        with open(p, "rb") as f:
            d = plistlib.load(f)
        for h in d.get("LSHandlers", []):
            if h.get("LSHandlerURLScheme") == "https":
                r = h.get("LSHandlerRoleAll", "")
                if r and "beacon" not in r.lower():
                    return r
    except Exception:
        pass
    return "com.apple.Safari"


def _launch(browser: str, url: str) -> None:
    """Best-effort open, mirroring the Skill's fallback chain (all silent)."""
    chains = [
        ["open", "-b", browser, url],
        ["open", "-a", "Safari", url],
        ["xdg-open", url],
        ["cmd.exe", "/c", "start", url],
        ["powershell.exe", "-Command", f"Start-Process '{url}'"],
    ]
    for cmd in chains:
        try:
            rc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            if rc == 0:
                return
        except Exception:
            continue


# Beacon Tauri desktop app identity (desktop/src-tauri/tauri.conf.json).
DESKTOP_BUNDLE_ID = "dev.beacon-ai.desktop"
DESKTOP_APP_NAME = "Beacon"


def _launch_desktop() -> bool:
    """Best-effort launch of the Beacon Tauri desktop app (local mode).

    Cloud mode opens the Web UI in a browser; local mode has no server URL, so
    the desktop app is the equivalent always-on front-end. On macOS resolve by
    bundle id first (stable across app renames), then by app name; other
    platforms try a PATH binary. Returns True on the first launcher that
    exits 0, False if none are available (app not installed).
    """
    chains = [
        ["open", "-b", DESKTOP_BUNDLE_ID],
        ["open", "-a", DESKTOP_APP_NAME],
        ["beacon-desktop"],
        ["cmd.exe", "/c", "start", "", "Beacon.exe"],
    ]
    for cmd in chains:
        try:
            rc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            if rc == 0:
                return True
        except Exception:
            continue
    return False


def _open_desktop() -> int:
    """Local mode front-end: launch the desktop app, announce if it started."""
    if _launch_desktop():
        print(f"DESKTOP_LAUNCHED={DESKTOP_APP_NAME}")
    return 0


def main() -> int:
    if not os.path.isfile(".beacon/cloud.json"):
        return _open_desktop()
    project_id = _project_id()
    if not project_id:
        # cloud.json present but no project_id — not a live cloud project, so
        # treat it as local and fall back to the desktop front-end.
        return _open_desktop()
    url = f"https://beacon-ai.dev/?project={project_id}"
    _launch(_default_browser(), url)
    print(f"WEBUI_URL={url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
