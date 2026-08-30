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
# Post-e-743: tabs / action emissions live in SHARED (flows into dist via
# build.py). Compare against the final Tauri artifact, not layer.js — that's
# what the user actually sees.
TAURI = ROOT / "desktop" / "dist" / "index.html"
TAURI_LAYER = ROOT / "desktop" / "layer.js"  # kept for messages only

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
    # ms-93 e-3203 — the "copy setup prompt" button (empty-state install help)
    # is wired via an element-local addEventListener (index.html), not the
    # action switch, and is Web-only (Codex/Claude setup prompt copy).
    "copy-setup-prompt",
}
# ms-72 e-1774 — actions wired via element-local `addEventListener` instead
# of the top-level `handleAction` switch (e.g. `<select>` change events,
# `<span>` click handlers attached in `_settingsLoadMemberList`). The drift
# detector previously flagged these as orphans because no `case '...'` exists,
# but the action is genuinely handled — just through a different routing
# mechanism. Listing here keeps the check meaningful for true orphans without
# false-positives on the legitimate event-listener pattern.
EVENT_LISTENER_ROUTED_ACTIONS = {
    "settings-change-role",
    "settings-remove-member",
    "settings-set-filter-trek",
    # ms-93 e-3203 — empty-state "copy setup prompt" button; wired via a
    # local addEventListener in index.html (setup-prompt-copy), not the switch.
    "copy-setup-prompt",
}
# ms-160 e-5808 — buttons rendered with a `-noop` suffix are visible placeholders
# whose handler is intentionally deferred: clicking them does nothing YET. Unlike
# `sales-modal-noop` (which carries an explicit no-op `case`), these have no
# handler on purpose. The Trek Resume/STOP buttons are the current members;
# wiring them to real resume/stop behaviour is Trek-refactor work that ms-160
# SPEC 方針5 explicitly excludes. Listing them keeps the strict CI drift gate
# green on this known-deferred debt while still catching any NEW unhandled
# action. Remove an entry here when its real handler lands.
INTENTIONAL_NOOP_ACTIONS = {
    "trek-resume-noop",
    "trek-stop-noop",
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
    # Post-e-743: tabs are declared in SHARED's renderShell (server/static/index.html).
    # dist absorbs SHARED via build.py, so web vs dist tabs should match by
    # construction. Mismatch here means: (a) dist is stale (run build.py), or
    # (b) someone added a Tauri-only tab in layer.js (allow-list intentionally).
    web_tabs = extract(r'data-tab="([a-z-]+)"', web)
    tauri_tabs = extract(r'data-tab="([a-z-]+)"', tauri)

    only_web = (web_tabs - tauri_tabs) - WEB_ONLY_TABS
    only_tauri = (tauri_tabs - web_tabs) - TAURI_ONLY_TABS

    if only_web:
        issues.append(
            f"  [tabs] Web has tabs that Tauri dist lacks: {sorted(only_web)}\n"
            f"         → Probably stale dist. Run: python3 desktop/build.py\n"
            f"         → If intentionally Web-only, add to WEB_ONLY_TABS allowlist."
        )
    if only_tauri:
        issues.append(
            f"  [tabs] Tauri dist has tabs that Web lacks: {sorted(only_tauri)}\n"
            f"         → Add to renderShell in server/static/index.html, or\n"
            f"         → add to TAURI_ONLY_TABS allowlist (Tauri-only feature)."
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

    web_unhandled = (web_actions - web_handled - EVENT_LISTENER_ROUTED_ACTIONS
                     - INTENTIONAL_NOOP_ACTIONS)
    tauri_unhandled = (tauri_actions - tauri_handled - EVENT_LISTENER_ROUTED_ACTIONS
                       - INTENTIONAL_NOOP_ACTIONS)
    if web_unhandled:
        issues.append(
            f"  [actions/web] HTML emits actions that no handler covers: {sorted(web_unhandled)}\n"
            f"                → Add to handleAction or handleCommonAction in server/static."
        )
    if tauri_unhandled:
        issues.append(
            f"  [actions/tauri] Tauri dist emits actions that no handler covers: {sorted(tauri_unhandled)}\n"
            f"                  → Add to handleAction in desktop/layer.js, or move to handleCommonAction in SHARED."
        )

    # Cross-layer coverage: actions that Web emits but Tauri handler doesn't
    # cover (and not in allowlist) — that means clicking the same UI element
    # on Tauri does nothing.
    cross_drift_web_only = (
        web_actions - tauri_handled - common_actions
        - WEB_ONLY_ACTIONS - TAURI_ONLY_ACTIONS
        - EVENT_LISTENER_ROUTED_ACTIONS - INTENTIONAL_NOOP_ACTIONS
    )
    cross_drift_tauri_only = (
        tauri_actions - web_handled - common_actions
        - WEB_ONLY_ACTIONS - TAURI_ONLY_ACTIONS
        - EVENT_LISTENER_ROUTED_ACTIONS - INTENTIONAL_NOOP_ACTIONS
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

    # --- 4. SHARED region purity (ms-46 e-728) ---
    # SHARED must not reference platform-specific data-fetching primitives:
    # `api(...)` is Web-only (defined outside SHARED) and `invoke(...)` is
    # Tauri-only. All data fetching from SHARED must go through `dataSource`.
    # `invoke('...')` legitimately appears in `loadDocumentContent: async (docId)
    # => { ... await invoke(...) ... }` inside the Tauri dataSource impl — but
    # that lives in desktop/layer.js, not in SHARED. SHARED stays pure.
    shared_match = re.search(
        r'// @BUILD:SHARED-START\n(.*?)// @BUILD:SHARED-END',
        web, re.DOTALL,
    )
    if shared_match:
        shared = shared_match.group(1)
        # Strip SKIP regions (Web-only) so we only inspect what flows to Tauri.
        shared_pure = re.sub(
            r'// @BUILD:SKIP-START\n.*?// @BUILD:SKIP-END\n?',
            '', shared, flags=re.DOTALL,
        )
        # Look for orphan api( / invoke( calls. Allow `dataSource.api` etc.
        # by requiring a word-boundary on the left.
        api_calls = re.findall(r'(?<![\w.])api\(', shared_pure)
        invoke_calls = re.findall(r'(?<![\w.])invoke\(', shared_pure)
        if api_calls:
            issues.append(
                f"  [shared/purity] SHARED region contains {len(api_calls)} `api(...)` call(s).\n"
                f"                  → SHARED must not call platform-specific fetch primitives.\n"
                f"                  → Route through `dataSource.loadX()` instead (ms-46 e-728)."
            )
        if invoke_calls:
            issues.append(
                f"  [shared/purity] SHARED region contains {len(invoke_calls)} `invoke(...)` call(s).\n"
                f"                  → SHARED must not call Tauri invoke directly.\n"
                f"                  → Route through `dataSource.loadX()` instead (ms-46 e-728)."
            )

    # --- report ---
    if not issues:
        print("[drift] OK: server/static and desktop/dist are in sync.")
        return 0

    print("[drift] Frontend drift detected between server/static and desktop/dist:")
    for i in issues:
        print(i)
    print()
    print("See scripts/check-frontend-drift.py for allowlists.")
    print("CORE doc: 'Tauri/Web UI 二層アーキテクチャ' (K8AhPgjpDG3mEa4eVm37).")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
