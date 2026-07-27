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

A verb often carries a *secondary* system beyond its primary one (``secondary``);
those seams are the concrete work queue for the decomposition (方針4: 整理 = この
継ぎ目を全 verb に通すこと). The classification was authored in memo
``42YTZkmtzfFhaarzCSv6`` as prose over ~239 verbs; this module makes it a **live**
ledger: machine-readable, and reconcilable against the real CLI surface so a
newly-added verb shows up as ``unclassified`` (the same drift-tracking
``scripts/check-map-drift.py`` does for map wedges).

Pure data + pure transforms. The live-surface enumeration is delegated to
``cli_surface.enumerate_cli_verbs`` — the single source of truth shared with the
map-drift lint, so the two readers can never drift (a test pins the equality).
"""

from __future__ import annotations

from typing import Optional

import cli_surface


CLASSES = {
    "Q": "参照系 (read-only query)",
    "R": "報告系 (ledger append — report primitive target)",
    "B": "バックエンド操作系 (machinery — server-owned)",
    "C": "クライアント操作系 (human / outward / local)",
}


# --- the ledger ------------------------------------------------------------
# key = dispatch key (noun_subcommand, e.g. "task_done"); value = {cls, secondary,
# note}. ``cls`` = the PRIMARY system; ``secondary`` = the OTHER systems the verb
# also carries (the decomposition seam), never repeating the primary. Populated
# from memo 42YTZ; post-memo verbs carry a "[post-memo]" note so a human can
# confirm the newly-applied classification. Data lives in verb_ledger_data.py.

from verb_ledger_data import VERB_LEDGER  # noqa: E402  (data split out for readability)


def _check_class(cls: str) -> str:
    """Validate ``cls`` is one of the closed Q/R/B/C enum, else raise. Stops a
    typo / lowercase from silently returning an empty list (which reads as
    'no verbs in this class' rather than 'invalid class name')."""
    c = (cls or "").strip()
    if c not in CLASSES:
        raise ValueError(
            f"未知の class '{cls}' です (有効: {' / '.join(CLASSES)})")
    return c


def _copy_entry(entry: dict) -> dict:
    """A defensive copy of a ledger entry (with its own ``secondary`` list) so a
    caller inspecting an entry cannot mutate the VERB_LEDGER constant in place."""
    out = dict(entry)
    out["secondary"] = list(entry.get("secondary", []))
    return out


# --- accessors -------------------------------------------------------------

def classify(verb_key: str) -> Optional[dict]:
    """Return a copy of the ledger entry for a dispatch key
    (``{cls, secondary, note}``) or None when the verb is not yet classified. A
    copy is returned so a caller's edits don't pollute the module constant."""
    entry = VERB_LEDGER.get((verb_key or "").strip())
    return _copy_entry(entry) if entry is not None else None


def verbs_in_class(cls: str) -> list:
    """Return the sorted dispatch keys whose PRIMARY class is ``cls``. Note this
    is PRIMARY-only: a verb whose primary is C but that also touches R (e.g.
    ``pr_merge``) is NOT returned for ``"R"`` — use ``verbs_involving`` when you
    want every verb that touches a class. Raises on an invalid ``cls``."""
    c = _check_class(cls)
    return sorted(k for k, v in VERB_LEDGER.items() if v.get("cls") == c)


def verbs_involving(cls: str) -> list:
    """Return the sorted dispatch keys that touch ``cls`` at all — primary OR
    secondary. This is the set to use when folding "all R verbs" into the report
    primitive, so a fused verb like ``pr_merge`` (primary C, secondary R) isn't
    silently missed. Raises on an invalid ``cls``."""
    c = _check_class(cls)
    return sorted(k for k, v in VERB_LEDGER.items()
                  if v.get("cls") == c or c in (v.get("secondary") or []))


def fusion_seams() -> list:
    """Return the fusion-seam verbs — the decomposition work queue — as a sorted
    list of ``{verb, cls, secondary, note}``. A seam verb carries a secondary
    system beyond its primary (方針4 の継ぎ目)."""
    out = []
    for verb in sorted(VERB_LEDGER):
        entry = VERB_LEDGER[verb]
        if entry.get("secondary"):
            out.append({
                "verb": verb,
                "cls": entry.get("cls", ""),
                "secondary": list(entry.get("secondary", [])),
                "note": entry.get("note", ""),
            })
    return out


def summary() -> dict:
    """Return counts: total classified, per-primary-class tally, fusion-seam
    count (verbs with a secondary), and how many entries are post-memo (pending
    human confirmation)."""
    per_class = {c: 0 for c in CLASSES}
    fused = post_memo = 0
    for v in VERB_LEDGER.values():
        per_class[v.get("cls", "")] = per_class.get(v.get("cls", ""), 0) + 1
        if v.get("secondary"):
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
    """Enumerate the live CLI dispatch surface via the shared
    ``cli_surface.enumerate_cli_verbs`` — the single source of truth also used by
    ``scripts/check-map-drift.py``, so the ledger reconciles against exactly the
    surface the map-drift lint tracks (no duplicated enumeration to drift)."""
    return cli_surface.enumerate_cli_verbs(commands_path)


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
