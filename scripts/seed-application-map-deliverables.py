#!/usr/bin/env python3
"""Backfill the hand-maintained application-map into surface-grained
deliverable-changelog entries (ms-161 e-5851).

WHY. e-5825 re-homed the milestone deliverable projector onto the root
deliverable-changelog, but the changelog only holds coarse milestone-completion
entries — so the DERIVED map is far thinner than the hand-maintained
application-map (99 surface bullets across 10 大節 / 25 小節, each carrying its
machine-checkable wedge). e-5851 closes that gap by SEEDING the current map's
every bullet as a surface-grained changelog entry, after which the CORE doc can
become a pure derived view (``deliverable_map.render_map``) with the wedge
machine-check preserved.

WHAT this does. Parses the application-map markdown into one entry per bullet:

    ## A. 見失わない …            → area   (大節, an ``area:`` tag)
    ### A1. 状態を一望する          → category (小節)
    - <散文> `cli:…` `api:…`       → entry: summary=<散文>, tags=[area:…, cli:…, api:…]

Every entry is attributed to the ROOT target (``source={"target_id":"root",
"kind":"root"}``) — the backfill is a migrated BASELINE, not the product of a
single milestone's completion, and saying so is more honest than inventing a
per-bullet origin milestone. Each entry also carries a ``seed:application-map-v1``
marker tag so the write path is IDEMPOTENT: a second run refuses rather than
double-appending.

SAFETY. ``--dry-run`` (the default) writes NOTHING — it parses, builds the
entries, renders the DERIVED map from them, and reconciles that render through
``scripts/check-map-drift.py`` to PROVE the wedge machine-check still passes after
derivation. Only ``--commit`` performs the live ``load_project`` → append-all →
``save_project`` (one atomic write). Run the dry-run, read the drift result, THEN
commit.
"""
from __future__ import annotations

import argparse
import importlib
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import deliverable_changelog as _dc  # noqa: E402
import deliverable_map as _dm  # noqa: E402

# The marker tag that identifies a backfilled entry — the idempotency key and a
# provenance breadcrumb ("this row came from the v1 application-map migration").
SEED_MARKER = "seed:application-map-v1"

# A wedge token inside a bullet: `type:ident` in backticks. The surface-type set is
# the SHARED ``deliverable_changelog.WEDGE_SURFACE_TYPES`` (one home with the render
# + reconciler, PR#699 review) so a new surface type is not silently missed by this
# parser. Captured WITH the backticks stripped.
_WEDGE_RE = re.compile(
    r"`((?:" + "|".join(_dc.WEDGE_SURFACE_TYPES) + r"):[^`]+)`")


def _strip_frontmatter(text: str) -> list:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = lines.index("---", 1)
            return lines[end + 1:]
        except ValueError:
            return lines
    return lines


def parse_map(text: str) -> list:
    """Parse an application-map markdown into deliverable-changelog entry dicts
    (pure — no I/O, so a test drives it on a fixture). One entry per ``- `` bullet,
    tagged with its 大節 (``area:``) + the bullet's wedges + the seed marker.

    A bullet with NO wedge still becomes an entry (its capability exists even if
    no machine-checkable surface was cited) — dropping it would silently lose a
    line from the map. A ``- `` line before any ``### `` (should not happen in a
    well-formed map) is attributed to an empty category and still captured."""
    entries: list = []
    area = ""
    category = ""
    for raw in _strip_frontmatter(text):
        line = raw.rstrip()
        if line.startswith("## ") and not line.startswith("### "):
            area = line[3:].strip()
            continue
        if line.startswith("### "):
            category = line[4:].strip()
            continue
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        body = stripped[2:].strip()
        wedges = _WEDGE_RE.findall(body)
        # summary = the 散文, i.e. the bullet with its wedge tokens removed.
        summary = _WEDGE_RE.sub("", body)
        # also drop a trailing drill-down ` `→ ref` ` if present, and tidy space.
        summary = re.sub(r"`→[^`]*`", "", summary).strip()
        summary = re.sub(r"\s+", " ", summary).strip()
        if not summary:
            continue
        tags = ([f"{_dc.AREA_TAG_PREFIX}{area}"] if area else []) + list(wedges)
        tags.append(SEED_MARKER)
        entries.append({
            "source": {"target_id": "root", "kind": "root"},
            "category": category,
            "title": summary,
            "summary": summary,
            "ref": "",
            "tags": tags,
        })
    return entries


def already_seeded(data: dict) -> bool:
    """True if the changelog already holds a backfill entry — the idempotency
    guard so ``--commit`` never double-appends the map."""
    for e in data.get(_dc.CHANGELOG_KEY, []) or []:
        if isinstance(e, dict) and SEED_MARKER in (e.get("tags") or []):
            return True
    return False


def _drift_key(reconcile: dict) -> tuple:
    """Flatten a reconcile result to comparable (missing, phantom) sets so two
    renders' drift can be checked for EQUALITY (parity), not just counted."""
    m = reconcile["missing"]
    p = reconcile["phantom"]
    missing = frozenset(m["cli"]) | frozenset(m["api"]) | frozenset(m["skill"])
    phantom = frozenset().union(*(frozenset(v) for v in p.values()))
    return missing, phantom


def _reconcile_render(entries: list, source_text: str = "") -> dict:
    """Seed a THROWAWAY data dict with ``entries``, render the derived dev map, and
    reconcile it through check-map-drift. When ``source_text`` (the ORIGINAL map)
    is given, also reconcile THAT and compute PARITY — the honest "楔維持" test is
    that derivation introduces NO NEW drift vs the hand-maintained source, not that
    the source was already at 0 (it may carry pre-existing reconcile debt)."""
    tmp: dict = {"name": "x", "profession": "dev"}
    for e in entries:
        _dc.append_deliverable(tmp, e)
    rendered = _dm.render_map(tmp)
    drift = importlib.import_module("check-map-drift")
    derived = drift.reconcile(rendered)
    out = {"rendered": rendered, "reconcile": derived}
    if source_text:
        source = drift.reconcile(source_text)
        d_missing, d_phantom = _drift_key(derived)
        s_missing, s_phantom = _drift_key(source)
        out["source_reconcile"] = source
        out["parity"] = (d_missing == s_missing and d_phantom == s_phantom)
        out["new_missing"] = sorted(d_missing - s_missing)
        out["new_phantom"] = sorted(d_phantom - s_phantom)
    return out


def _load_map_text(args) -> str:
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            return fh.read()
    # default: pull the live CORE doc through the CLI (read-only).
    out = subprocess.run(["beacon", "doc", "show", args.doc_id],
                         capture_output=True, text=True, timeout=30, check=True)
    return out.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--doc-id", default="application-map",
                    help="source map CORE doc id (既定 application-map)")
    ap.add_argument("--file", default="",
                    help="doc id の代わりに markdown ファイルから読む (test/preview 用)")
    ap.add_argument("--commit", action="store_true",
                    help="実際に live project へ書き込む (既定は dry-run)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    text = _load_map_text(args)
    entries = parse_map(text)
    # Guard the empty parse LOUDLY (PR#699 AX review): a map with no bullets (empty
    # / malformed --file) would otherwise IndexError on entries[0] below with a bare
    # traceback — the caller cannot tell "0 parsed" from a code bug.
    if not entries:
        print("Error: 入力 doc から bullet が 1 件も parse できませんでした。"
              "--file / --doc-id の内容を確認してください。", file=sys.stderr)
        return 1
    result = _reconcile_render(entries, source_text=text)
    counts = result["reconcile"]["counts"]
    parity = result.get("parity", True)

    if not args.commit:
        # DRY-RUN: report the plan + the PARITY proof, write nothing.
        print(f"[dry-run] {len(entries)} entries を application-map から parse")
        src = result.get("source_reconcile", {}).get("counts", {})
        print(f"  導出 map 照合: 書き漏れ(missing)={counts['missing']} / "
              f"幽霊(phantom)={counts['phantom']}")
        print(f"  元 doc 照合:  書き漏れ(missing)={src.get('missing','?')} / "
              f"幽霊(phantom)={src.get('phantom','?')}")
        print(f"  楔維持 (parity, 導出が新規 drift を持ち込まないか): "
              f"{'✓ OK' if parity else '✗ NG'}")
        if not parity:
            print(f"    新規 missing: {result.get('new_missing')}")
            print(f"    新規 phantom: {result.get('new_phantom')}")
        if counts["missing"]:
            print(f"  注記: missing={counts['missing']} は元 doc の既存 reconcile 債務 "
                  f"(移行前から地図が古い分)。移行後 `beacon deliverable add` で解消可能。")
        cats: dict = {}
        for e in entries:
            cats[e["category"]] = cats.get(e["category"], 0) + 1
        print(f"  小節(category) {len(cats)} 種 / 大節(area) は tags に付与")
        if args.json:
            import json as _json
            print(_json.dumps({"count": len(entries), "counts": counts,
                               "parity": parity, "sample": entries[:3]},
                              ensure_ascii=False))
        else:
            print("  sample entry[0]:")
            print(f"    category={entries[0]['category']!r}")
            print(f"    summary={entries[0]['summary'][:60]!r}")
            print(f"    tags={entries[0]['tags']}")
        print("\n  → parity OK なら --commit で live project に seed します。")
        return 0 if parity else 1

    # COMMIT: one atomic load → append-all → save, THEN swap the doc to derived.
    from commands_shared import load_project, save_project
    import deliverable_doc_sync as _dsync
    data = load_project()
    if already_seeded(data):
        print("Error: 既に seed 済 (marker tag が存在)。二重 append を回避して中止。",
              file=sys.stderr)
        return 1
    for e in entries:
        _dc.append_deliverable(data, e)
    save_project(data, op={"type": "deliverable_seed_application_map",
                           "count": len(entries)})
    print(f"✓ {len(entries)} entries を root deliverable-changelog に seed 済")
    # Swap the CORE doc to the derived render (the e-5851 re-home). sync overwrites
    # the existing hand-maintained application-map with the generated body — from
    # here the doc auto-follows every deliverable mutation (受入条件4).
    swapped = _dsync.sync_application_map(data)
    if swapped:
        print("✓ application-map を導出物へ差替 (手メンテ廃止、以降 add/retire に自動追随)")
    else:
        # PR#699 AX review: the changelog seed SUCCEEDED but the doc-swap did not —
        # that is a PARTIAL migration (map still hand-maintained), so exit NON-ZERO
        # rather than reporting exit 0 for an incomplete result. The seed is durable;
        # re-running is blocked by the idempotency guard, so fix the doc (dev + doc
        # must exist) and run `beacon deliverable map` / `beacon doc update`.
        print("⚠ application-map doc が見つからず差替をスキップ = 移行は未完了 "
              "(dev+doc存在が前提)。changelog の seed は完了済。", file=sys.stderr)
    if args.json:
        import json as _json
        print(_json.dumps({"seeded": len(entries), "doc_swapped": swapped},
                          ensure_ascii=False))
    return 0 if swapped else 1


if __name__ == "__main__":
    sys.exit(main())
