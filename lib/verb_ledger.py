"""Live ledger of the CLI verb → Q/R/B/C classification (ms-114 e-3740).

The ms-114 SPEC decomposes every CLI verb into 4 systems (設計方針 4):

  - **Q** 参照系: read-only (list / show / status / search) — changes nothing.
  - **R** 報告系: appends a fact/decision to the ledger — the *report* primitive
    (``report_primitive.py``) target.
  - **B** バックエンド操作系: drives machinery (schedule / projection / migration /
    authorization / store / autonomous-exec / maintenance / control) — the
    server owns this in the final shape.
  - **C** クライアント操作系: human interface / outward effect / local tool /
    consequential request — stays on the CLI agent + human.

A verb often *fuses* several systems (``fused``); those seams are the concrete
work queue for the decomposition (方針4: 整理 = この継ぎ目を全 verb に通すこと). The
classification was authored in memo ``42YTZkmtzfFhaarzCSv6`` as prose over ~239
verbs; this module makes it a **live** ledger: machine-readable, and reconcilable
against the real CLI surface so a newly-added verb shows up as ``unclassified``
(the same drift-tracking ``scripts/check-map-drift.py`` does for map wedges).

Pure data + pure transforms; no I/O beyond reading ``lib/commands.py`` to
enumerate the live surface (which mirrors ``check-map-drift.enumerate_cli`` so
the two surfaces stay 整合 — a test pins the equality).
"""

from __future__ import annotations

import os
import re
from typing import Optional


CLASSES = {
    "Q": "参照系 (read-only query)",
    "R": "報告系 (ledger append — report primitive target)",
    "B": "バックエンド操作系 (machinery — server-owned)",
    "C": "クライアント操作系 (human / outward / local)",
}

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))

# Kept in step with scripts/check-map-drift.py so the ledger's notion of "the
# live CLI surface" is identical to the map-drift lint's (整合, e-3740 AC3).
_CLI_INTERNAL = {
    "auth_check", "common_setup", "cloud_check_project",
    "retro_default_since", "help_json", "version",
}
_CLI_SHELL_TOPLEVEL = {"status", "reset"}


# --- the ledger ------------------------------------------------------------
# key = dispatch key (noun_subcommand, e.g. "task_done"); value = {cls, fused,
# note}. Populated from memo 42YTZ; post-memo verbs carry a "[post-memo]" note so
# a human can confirm the newly-applied classification. Filled by e-3740.

from verb_ledger_data import VERB_LEDGER  # noqa: E402  (data split out for readability)


# --- accessors -------------------------------------------------------------

def classify(verb_key: str) -> Optional[dict]:
    """Return the ledger entry for a dispatch key (``{cls, fused, note}``) or
    None when the verb is not yet classified."""
    return VERB_LEDGER.get((verb_key or "").strip())


def verbs_in_class(cls: str) -> list:
    """Return the sorted dispatch keys whose PRIMARY class is ``cls``."""
    return sorted(k for k, v in VERB_LEDGER.items() if v.get("cls") == cls)


def fusion_seams() -> list:
    """Return the fused verbs — the decomposition work queue — as a sorted list
    of ``{verb, cls, fused, note}``. A fused verb carries duties of more than its
    primary system; ``fused`` names the secondary systems (方針4 の継ぎ目)."""
    out = []
    for verb in sorted(VERB_LEDGER):
        entry = VERB_LEDGER[verb]
        if entry.get("fused"):
            out.append({
                "verb": verb,
                "cls": entry.get("cls", ""),
                "fused": list(entry.get("fused", [])),
                "note": entry.get("note", ""),
            })
    return out


def summary() -> dict:
    """Return counts: total classified, per-primary-class tally, fused count,
    and how many entries are post-memo (pending human confirmation)."""
    per_class = {c: 0 for c in CLASSES}
    fused = post_memo = 0
    for v in VERB_LEDGER.values():
        per_class[v.get("cls", "")] = per_class.get(v.get("cls", ""), 0) + 1
        if v.get("fused"):
            fused += 1
        if (v.get("note") or "").startswith("[post-memo]"):
            post_memo += 1
    return {
        "total": len(VERB_LEDGER),
        "per_class": per_class,
        "fused": fused,
        "post_memo": post_memo,
    }


# --- live surface reconciliation (整合 with check-map-drift) ----------------

def enumerate_live_verbs(commands_path: str = "") -> set:
    """Enumerate the live CLI dispatch surface: the keys of the ``commands = {}``
    dict in ``lib/commands.py`` (minus internal-only keys) plus the shell
    top-level verbs. This mirrors ``check-map-drift.enumerate_cli`` exactly so the
    ledger reconciles against the same surface the map-drift lint tracks."""
    path = commands_path or os.path.join(_LIB_DIR, "commands.py")
    src = open(path, encoding="utf-8").read()
    m = re.search(r"\n    commands = \{(.*?)\n    \}", src, re.S)
    if not m:
        raise RuntimeError(
            "dispatch dict `commands = {...}` を lib/commands.py に見つけられません")
    keys = set(re.findall(r'"([a-z0-9_]+)":', m.group(1)))
    return {k for k in keys if k not in _CLI_INTERNAL} | _CLI_SHELL_TOPLEVEL


def reconcile(live: Optional[set] = None) -> dict:
    """Reconcile the ledger against the live CLI surface. Returns:

    - ``unclassified``: live verbs with no ledger entry (a verb was added but not
      classified — the drift the live ledger exists to catch);
    - ``stale``: ledger entries whose verb no longer exists in the live surface
      (a verb was removed/renamed — the ledger should drop or alias it).

    Empty ``unclassified`` + ``stale`` = the ledger fully covers the live surface
    (e-3740 AC1: 全 CLI verb が Q/R/B/C に分類され live 台帳)."""
    live = enumerate_live_verbs() if live is None else live
    ledger = set(VERB_LEDGER)
    return {
        "unclassified": sorted(live - ledger),
        "stale": sorted(ledger - live),
    }
