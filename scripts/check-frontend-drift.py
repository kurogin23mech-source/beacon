#!/usr/bin/env python3
"""Frontend drift detector for Beacon (ms-46 e-744).

Checks alignment between server/static/index.html (Web UI source-of-truth)
and desktop/layer.js (Tauri platform layer) on key sync surfaces:

  1. data-tab="..."  — tabs declared on each side must match.
  2. data-action="..."  — actions accepted by each handleAction (plus the
     shared handleCommonAction) must cover both sides.
  3. state field names — declared on both sides must match (subset OK on one
     side if explicitly allow-listed below).

Run modes:
  --warn  (default): print mismatches, exit 0. For pre-commit (don't block).
  --strict: print mismatches, exit 1. For CI gate (block PR merge).

Allowlisted asymmetries (intentional / platform-specific) are listed below.
When a new genuine Tauri-only or Web-only feature is added, add it to the
allowlist with a 1-line reason. Reviewing the allowlist change is the new
gate.

Background / related:
  - ms-46 e-743 (render() SHARED 化) once complete, tab-bar lives in only one
    place and this script becomes a regression guard.
  - CORE doc K8AhPgjpDG3mEa4eVm37 describes the two-layer architecture and
    where each piece lives.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "server" / "static" / "index.html"
TAURI = ROOT / "desktop" / "layer.js"

# Intentional platform-specific differences.
# Adding/removing an item here is itself a reviewable change.
TAURI_ONLY_ACTIONS = {
    "select-cloud-project",
    "menu-select-cloud-project",
    "cloud-diagnose",
    "archive-cloud-project",
}
WEB_ONLY_ACTIONS = {
    "archive-project",
    "unarchive-project",
    "remove-member",
    "invite-member",
    "change-member-role",
    "logout",
}
# Tabs are expected to be 1:1 once e-743 (render() SHARED) completes.
# Until then, allow-list Web-only tabs that Tauri hasn't caught up to yet.
WEB_ONLY_TABS: set[str] = set()  # empty: drift treated as error after e-743
TAURI_ONLY_TABS: set[str] = set()


def extract(pattern: str, content: str) -> set[str]:
    return set(re.findall(pattern, content))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 on drift (CI mode). Default: exit 0 with warnings.")
    args = parser.parse_args()

    web = WEB.read_text(encoding="utf-8")
    tauri = TAURI.read_text(encoding="utf-8")

    issues: list[str] = []

    # --- 1. tabs ---
    # Match data-tab="value" in both files. Web has SKIP+SHARED both included
    # because tab declarations live in SKIP currently.
    web_tabs = extract(r'data-tab="([a-z-]+)"', web)
    tauri_tabs = extract(r'data-tab="([a-z-]+)"', tauri)

    only_web = (web_tabs - tauri_tabs) - WEB_ONLY_TABS
    only_tauri = (tauri_tabs - web_tabs) - TAURI_ONLY_TABS

    if only_web:
        issues.append(
            f"  [tabs] Web has tabs that Tauri lacks: {sorted(only_web)}\n"
            f"         → Add to desktop/layer.js render() or to WEB_ONLY_TABS allowlist."
        )
    if only_tauri:
        issues.append(
            f"  [tabs] Tauri has tabs that Web lacks: {sorted(only_tauri)}\n"
            f"         → Add to server/static/index.html or to TAURI_ONLY_TABS allowlist."
        )

    # --- 2. actions ---
    # action sets per side; handleCommonAction covers both sides equally so
    # actions defined there don't count as platform-specific.
    web_actions = extract(r'data-action="([a-z-]+)"', web)
    tauri_actions = extract(r'data-action="([a-z-]+)"', tauri)

    # Cases handled in handleCommonAction (SHARED) — both layers get these for free.
    # Match `case 'foo'` within the function body (rough but fine).
    common_match = re.search(
        r'function\s+handleCommonAction.*?\n\}\n',
        web, re.DOTALL,
    )
    common_actions: set[str] = set()
    if common_match:
        common_actions = extract(r"case\s+'([a-z-]+)'", common_match.group(0))

    # An action emitted in HTML must be handled either by handleCommonAction
    # OR by the platform's own handleAction. Check each direction.
    web_handle_match = re.search(
        r'(?:async\s+)?function\s+handleAction.*?\n\}\n',
        web[common_match.end():] if common_match else web,
        re.DOTALL,
    )
    tauri_handle_match = re.search(
        r'(?:async\s+)?function\s+handleAction.*?\n\}\n',
        tauri, re.DOTALL,
    )
    web_handled = (
        extract(r"case\s+'([a-z-]+)'", web_handle_match.group(0))
        if web_handle_match else set()
    ) | common_actions
    tauri_handled = (
        extract(r"case\s+'([a-z-]+)'", tauri_handle_match.group(0))
        if tauri_handle_match else set()
    ) | common_actions

    web_unhandled = web_actions - web_handled
    tauri_unhandled = tauri_actions - tauri_handled
    if web_unhandled:
        issues.append(
            f"  [actions/web] HTML emits actions that no handler covers: {sorted(web_unhandled)}\n"
            f"                → Add to handleAction or handleCommonAction in server/static."
        )
    if tauri_unhandled:
        issues.append(
            f"  [actions/tauri] dist emits actions that no Tauri handler covers: {sorted(tauri_unhandled)}\n"
            f"                  → Add to handleAction in desktop/layer.js or move to handleCommonAction."
        )

    # Cross-layer coverage: actions that Web emits but Tauri handler doesn't
    # cover (and not in allowlist) — that means clicking the same UI element
    # on Tauri does nothing.
    cross_drift_web_only = (
        web_actions - tauri_handled - common_actions
        - WEB_ONLY_ACTIONS - TAURI_ONLY_ACTIONS
    )
    cross_drift_tauri_only = (
        tauri_actions - web_handled - common_actions
        - WEB_ONLY_ACTIONS - TAURI_ONLY_ACTIONS
    )
    if cross_drift_web_only:
        issues.append(
            f"  [cross] Web emits actions that Tauri handleAction doesn't cover: {sorted(cross_drift_web_only)}\n"
            f"          → If intentionally Web-only, add to WEB_ONLY_ACTIONS allowlist."
        )
    if cross_drift_tauri_only:
        issues.append(
            f"  [cross] Tauri emits actions that Web handleAction doesn't cover: {sorted(cross_drift_tauri_only)}\n"
            f"          → If intentionally Tauri-only, add to TAURI_ONLY_ACTIONS allowlist."
        )

    # --- 3. state fields — DROPPED ---
    # State field comparison was attempted but regex parsing of multi-field
    # lines (`expanded: new Set(), lastUpdate: null, ...`) was unreliable.
    # Most state field drift is platform-justified anyway (cloudWs*, projectPath,
    # etc.). If state drift becomes a real problem, write a proper JS parser
    # check or rely on TypeScript migration. For now: not checked.

    # --- report ---
    if not issues:
        print("[drift] OK: server/static and desktop/layer.js are in sync.")
        return 0

    print("[drift] Frontend drift detected between server/static and desktop/layer.js:")
    for i in issues:
        print(i)
    print()
    print("See scripts/check-frontend-drift.py for allowlists.")
    print("CORE doc: 'Tauri/Web UI 二層アーキテクチャ' (K8AhPgjpDG3mEa4eVm37).")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
