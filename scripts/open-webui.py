#!/usr/bin/env python3
"""Open the Beacon Web UI for the current cloud project (extracted ms-85 e-3178).

Previously inline in the session-start Skill (Step 2.7) as a shell block that
embedded two ``python3 -c`` snippets. e-3178 extracts it verbatim-in-behavior
to a script so the Skill body carries one invocation line instead of ~35 lines
of shell + python.

Behavior (unchanged):
  * local mode (no ``.beacon/cloud.json`` or no ``project_id``) -> print
    nothing, exit 0 (session-start skips the step).
  * cloud mode -> build ``https://beacon-ai.dev/?project=<id>``, resolve the
    default macOS https handler (falling back to Safari when the handler is
    Beacon.app, to avoid launching the Tauri app — ms-46 e-737), launch the
    browser with the same open/xdg-open/cmd.exe/powershell fallback chain,
    and print ``WEBUI_URL=<url>`` so session-start can show it in the header.

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


def main() -> int:
    if not os.path.isfile(".beacon/cloud.json"):
        return 0
    project_id = _project_id()
    if not project_id:
        return 0
    url = f"https://beacon-ai.dev/?project={project_id}"
    _launch(_default_browser(), url)
    print(f"WEBUI_URL={url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
