"""Tests for scripts/check-verb-ledger.py — the pre-commit mirror of the CI verb
ledger coverage gate (ms-114 e-6275).

The script re-runs ``verb_ledger.reconcile()`` at commit time so a forgotten
ledger entry (which bit PR #734 = dm_show and PR #735 = attention) is caught in
seconds locally instead of ~10min into CI. These tests pin its three-way
contract: clean → 0, drift → 1 (with both unclassified and stale surfaced),
tooling failure → 0 (fail-safe, never brick a commit on a gate bug)."""

import importlib.util
import os

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(REPO, "scripts", "check-verb-ledger.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("check_verb_ledger", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _patch_reconcile(monkeypatch, fn):
    """Import verb_ledger the same way the script does and swap reconcile()."""
    import sys
    sys.path.insert(0, os.path.join(REPO, "lib"))
    import verb_ledger as vl
    monkeypatch.setattr(vl, "reconcile", fn)


def test_clean_surface_exits_zero(monkeypatch):
    _patch_reconcile(monkeypatch, lambda: {"unclassified": [], "stale": []})
    assert _load_script().main() == 0


def test_unclassified_verb_blocks(monkeypatch, capsys):
    _patch_reconcile(monkeypatch, lambda: {"unclassified": ["attention"], "stale": []})
    rc = _load_script().main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "attention" in out
    assert "verb_ledger_data" in out


def test_stale_entry_blocks(monkeypatch, capsys):
    _patch_reconcile(monkeypatch, lambda: {"unclassified": [], "stale": ["gone_verb"]})
    rc = _load_script().main()
    assert rc == 1
    assert "gone_verb" in capsys.readouterr().out


def test_tooling_failure_is_fail_safe(monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated reconcile failure")
    _patch_reconcile(monkeypatch, boom)
    rc = _load_script().main()
    out = capsys.readouterr().out
    assert rc == 0  # never brick a commit on a gate bug
    assert "[skip]" in out


def test_live_ledger_is_actually_clean():
    """Dogfood: against the real repo surface the gate must currently pass (0),
    i.e. this PR did not itself introduce an unclassified verb."""
    assert _load_script().main() == 0
