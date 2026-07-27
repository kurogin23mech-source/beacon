"""Tests for lib/verb_ledger.py + verb_ledger_data.py — the live Q/R/B/C verb
ledger (ms-114 e-3740). The load-bearing test is ``test_ledger_covers_live_surface``:
it pins the ledger against the real CLI dispatch surface so a newly-added verb
fails CI until it is classified (that is what makes the memo a *live* ledger, not
stale prose). ``test_surface_matches_check_map_drift`` pins the ledger's notion of
"the live surface" to ``check-map-drift.enumerate_cli`` so the two stay 整合."""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import verb_ledger as vl  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")
VALID_CLASSES = set(vl.CLASSES)  # {"Q","R","B","C"}


# --- coverage / drift gate -------------------------------------------------

def test_ledger_covers_live_surface():
    """Every live CLI verb is classified and no ledger entry is stale (e-3740
    AC1: 全 CLI verb が Q/R/B/C に分類され live 台帳)."""
    rec = vl.reconcile()
    assert rec["unclassified"] == [], (
        f"未分類の verb があります (ledger に追記してください): {rec['unclassified']}")
    assert rec["stale"] == [], (
        f"存在しない verb が ledger に残っています (削除/alias してください): {rec['stale']}")


def test_surface_matches_check_map_drift():
    """verb_ledger.enumerate_live_verbs() must equal check-map-drift.enumerate_cli()
    so the ledger tracks exactly the surface the map-drift lint does (e-3740 AC3:
    check-map-drift の surface 追跡と整合)."""
    path = os.path.join(REPO, "scripts", "check-map-drift.py")
    spec = importlib.util.spec_from_file_location("check_map_drift", path)
    cmd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cmd)
    assert vl.enumerate_live_verbs() == cmd.enumerate_cli()


# --- data integrity --------------------------------------------------------

def test_every_entry_wellformed():
    for verb, entry in vl.VERB_LEDGER.items():
        assert entry.get("cls") in VALID_CLASSES, f"{verb}: bad cls {entry.get('cls')}"
        fused = entry.get("fused")
        assert isinstance(fused, list), f"{verb}: fused must be a list"
        for f in fused:
            assert f in VALID_CLASSES, f"{verb}: bad fused class {f}"
        # a verb should not list its own primary class as a fusion seam
        assert entry["cls"] not in fused, f"{verb}: primary class duplicated in fused"
        assert isinstance(entry.get("note", ""), str)


def test_classify_hit_and_miss():
    assert vl.classify("task_done")["cls"] == "R"
    assert vl.classify("no_such_verb") is None


def test_log_is_the_report_prototype():
    """`beacon log` is the SPEC's R系統 雛形: report(R) fused with server author(B)."""
    entry = vl.classify("log")
    assert entry["cls"] == "R" and "B" in entry["fused"]


# --- seams / summary -------------------------------------------------------

def test_fusion_seams_are_only_fused_verbs():
    seams = vl.fusion_seams()
    assert seams, "there should be fused verbs (the decomposition work queue)"
    for s in seams:
        assert s["fused"], f"{s['verb']} listed as a seam but has no fused classes"
    # the canonical fused examples from the memo table are present
    verbs = {s["verb"] for s in seams}
    for expected in ("log", "task_done", "milestone_start", "pr_merge"):
        assert expected in verbs


def test_verbs_in_class_partition():
    """The per-class lists partition the ledger exactly (each verb in one class)."""
    total = sum(len(vl.verbs_in_class(c)) for c in vl.CLASSES)
    assert total == len(vl.VERB_LEDGER)


def test_summary_consistent():
    s = vl.summary()
    assert s["total"] == len(vl.VERB_LEDGER)
    assert sum(s["per_class"].values()) == s["total"]
    assert s["fused"] == len(vl.fusion_seams())
