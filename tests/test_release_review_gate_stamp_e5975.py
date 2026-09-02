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
    """A failed stamp must not raise — the push fails loudly on its own (the
    subsequent `git push` has check=True and aborts on GH006)."""
    monkeypatch.setattr(release, "run", lambda *a, **k: "abc1234")

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: _Result())
    release._stamp_review_gate(str(ROOT), dry_run=False)  # must not raise
    assert "review-gate stamp failed" in capsys.readouterr().err


def test_stamp_desc_uses_the_shared_constant(monkeypatch):
    """The gate description is a single-source-of-truth module constant, not an
    inline literal (so a future edit is one place and pinnable)."""
    monkeypatch.setattr(release, "run", lambda *a, **k: "sha1")
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
    assert release._BUMP_COMMIT_GATE_DESC in captured["argv"]


# --- push to main is stamped by construction (structural, not textual) ------

def test_push_main_lands_sha_then_stamps_then_pushes(monkeypatch):
    """_push_main binds the whole gate-satisfying sequence into one operation:
    (1) push the tip to an unprotected staging ref so GitHub has the SHA, (2)
    stamp the gate, (3) push main, (4) delete staging. The stamp must sit AFTER
    the staging push (the SHA must exist to carry a status) and BEFORE the main
    push (the required check must be present). Behavioural, so it survives
    source-layout changes."""
    calls = []
    monkeypatch.setattr(release, "_stamp_review_gate",
                        lambda *a, **k: calls.append("stamp"))
    monkeypatch.setattr(release, "run",
                        lambda cmd, **k: calls.append(("run", tuple(cmd))))

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_sub(cmd, **k):
        calls.append(("subprocess", tuple(cmd)))
        return _Ok()

    monkeypatch.setattr(release.subprocess, "run", fake_sub)
    release._push_main("/repo", dry_run=False)
    # 1. staging push lands the SHA on the remote first, force-pushed (a prior
    #    failed run may have left the scratch ref behind)
    assert calls[0][0] == "run"
    assert "--force" in calls[0][1]
    assert f"HEAD:refs/heads/{release._RELEASE_STAGING_REF}" in calls[0][1]
    # 2. stamp happens only after the SHA exists
    assert calls[1] == "stamp"
    # 3. then the protected push to main
    assert calls[2] == ("run", ("git", "push", "origin", "main"))
    # 4. scratch ref torn down last (via subprocess so a teardown failure can be
    #    surfaced rather than swallowed)
    assert calls[3][0] == "subprocess" and "--delete" in calls[3][1]


def test_no_bare_push_to_main_outside_the_helper():
    """The only `git push origin main` in the pipeline lives inside _push_main.
    This is the structural guarantee that replaces the old 6-line-window grep: a
    new push route CANNOT ship an un-stamped bump commit because there is no bare
    push left to copy (independent AX + maintainability consensus on PR #705)."""
    src = RELEASE.read_text(encoding="utf-8")
    lines = src.splitlines()
    push_idxs = [i for i, ln in enumerate(lines)
                 if '"git", "push", "origin", "main"' in ln]
    assert len(push_idxs) == 1, (
        "there must be exactly ONE push-to-main call and it must be inside "
        "_push_main(); a bare push elsewhere would bypass the gate stamp"
    )
    def_idx = next(i for i, ln in enumerate(lines)
                   if ln.startswith("def _push_main("))
    next_def = next((i for i in range(def_idx + 1, len(lines))
                     if lines[i].startswith("def ")), len(lines))
    assert def_idx < push_idxs[0] < next_def, (
        "the push-to-main call must live in the _push_main() body"
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
