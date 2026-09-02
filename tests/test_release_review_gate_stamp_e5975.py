"""Release pushes stamp the review gate on bot bump commits (ms-160 / e-5975).

main's branch protection requires the `beacon-review-gate` check on the pushed
tip. The release bot pushes mechanical bump commits (version, formula, CHANGELOG)
straight to main WITHOUT a PR, so an un-stamped tip is rejected with GH006
"protected branch" and the whole release fails at the version-bump push.

The fix: scripts/release.py stamps `beacon-review-gate=success` on HEAD (via the
same scripts/review-gate-ci.py `set` path `beacon review done` uses) right before
each direct `git push origin main`, and release.yml grants `statuses: write` so
the POST is permitted. These tests pin both halves so the wiring can't silently
regress.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "scripts" / "release.py"
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"


def _load_release():
    spec = importlib.util.spec_from_file_location("release_e5975", RELEASE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


release = _load_release()


# --- the helper exists and delegates to review-gate-ci.py -------------------

def test_stamp_helper_exists():
    assert callable(getattr(release, "_stamp_review_gate", None))


def test_stamp_dry_run_does_not_shell_out(monkeypatch, capsys):
    called = {"run": False, "subprocess": False}

    def fake_run(*a, **k):
        called["run"] = True
        return "deadbeef"

    def fake_sub(*a, **k):
        called["subprocess"] = True
        raise AssertionError("dry-run must not shell out")

    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(release.subprocess, "run", fake_sub)
    release._stamp_review_gate(str(ROOT), dry_run=True)
    assert called == {"run": False, "subprocess": False}
    assert "dry-run" in capsys.readouterr().out


def test_stamp_posts_success_on_resolved_head(monkeypatch):
    """It resolves HEAD, then invokes review-gate-ci.py `set --state success
    --sha <HEAD>` — the mechanical-plumbing classification, not a bypass."""
    monkeypatch.setattr(release, "run", lambda *a, **k: "abc1234def")
    captured = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_sub(argv, **k):
        captured["argv"] = argv
        return _Result()

    monkeypatch.setattr(release.subprocess, "run", fake_sub)
    release._stamp_review_gate(str(ROOT), dry_run=False)
    argv = captured["argv"]
    assert str(RELEASE.parent / "review-gate-ci.py") in argv
    assert "set" in argv
    assert argv[argv.index("--state") + 1] == "success"
    assert argv[argv.index("--sha") + 1] == "abc1234def"


def test_stamp_is_best_effort_on_gate_failure(monkeypatch, capsys):
    """A failed stamp must not raise — the push fails loudly on its own."""
    monkeypatch.setattr(release, "run", lambda *a, **k: "abc1234")

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: _Result())
    release._stamp_review_gate(str(ROOT), dry_run=False)  # must not raise
    assert "review-gate stamp failed" in capsys.readouterr().err


# --- every direct push to main is preceded by a stamp -----------------------

def test_each_push_main_is_preceded_by_a_stamp():
    """Source-level guard: both `git push origin main` calls in the pipeline are
    immediately preceded by a `_stamp_review_gate(` call, so no push route can
    ship an un-stamped bump commit."""
    src = RELEASE.read_text(encoding="utf-8")
    lines = src.splitlines()
    push_idxs = [
        i for i, ln in enumerate(lines)
        if '"git", "push", "origin", "main"' in ln
    ]
    assert len(push_idxs) >= 2, "expected the two bump-commit pushes to main"
    for i in push_idxs:
        window = "\n".join(lines[max(0, i - 6):i])
        assert "_stamp_review_gate(" in window, (
            f"push to main at line {i + 1} is not preceded by a review-gate stamp"
        )


# --- release.yml grants statuses: write -------------------------------------

def test_release_yml_grants_statuses_write():
    text = RELEASE_YML.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        # Fall back to a textual assertion when PyYAML is unavailable.
        assert "statuses: write" in text
        return
    doc = yaml.safe_load(text)
    perms = doc.get("permissions") or {}
    assert perms.get("statuses") == "write", (
        "release.yml must grant statuses: write to POST the gate status (e-5975)"
    )
