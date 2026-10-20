"""Every path that records a PR raises its review gate (ms-181 e-7103).

The two-sided hole
==================
**(a) 作成直後は関門が立たなかった.** ``beacon pr add`` fired the PR-open review
triggers; ``beacon pr create`` called ``core.pr_add`` directly and fired
nothing. So a PR made through the wrapper had no gate standing on it, and
``beacon pr approve`` would pass for want of a review nobody had requested. The
backstop (``_auto_fire_pr_open_review_triggers_for_open_prs``) only runs from
``beacon trigger tick``, so the window stayed open from creation until the next
tick. Measured 2026-10-19: PRs #806/#803 had no gate before a tick and both had
ax/maintainability gates after one.

**(b) 実施しても CI の印が押されなかった.** ``beacon review done`` left the
GitHub ``beacon-review-gate`` check pending unless the operator remembered
``BEACON_REVIEW_GATE_CI=1``. Measured twice — ms-185's fork on 2026-10-19 and
again while opening PR #811 on 2026-10-20: the stamp only landed on a re-run
with the variable set.

Both sides are the same gate, which is why they are one task: fixing only (a)
leaves "reviewed but can never merge", fixing only (b) leaves "merges with no
review".

受入条件 4 asks that removing the fire from **one** path goes red, and that the
measurement not be rescued by the tick backstop. Every test here drives the
recording path directly and never calls ``beacon trigger tick``.
"""

import ast
import os

import pytest

import cmd_pr
import commands
import commands_shared

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PR_URL = "https://github.com/kurogin23mech-source/beacon/pull/9001"


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 受入条件 1 / 3: one place, reached by every recording path
# ---------------------------------------------------------------------------

def _calls_in(func_name: str, src: str) -> set:
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == func_name)
    return {
        (node.func.attr if isinstance(node.func, ast.Attribute)
         else getattr(node.func, "id", ""))
        for node in ast.walk(fn) if isinstance(node, ast.Call)
    }


def test_both_recording_paths_go_through_the_same_place():
    """受入条件 3: neither verb may keep its own memory of what to fire.

    Structural because that IS the guarantee: two call sites each remembering
    "what else recording a PR implies" is the defect, not the forgotten line.
    A third verb added later calls the one helper and is right by construction.
    """
    src = _read("lib/cmd_pr.py")
    for verb in ("cmd_pr_add", "cmd_pr_create"):
        assert "_after_pr_recorded" in _calls_in(verb, src), (
            f"{verb} does not route through _after_pr_recorded — it is "
            "remembering the post-record steps on its own again")

    # ...and the fire itself lives in exactly that one place.
    owners = {name for name in ("cmd_pr_add", "cmd_pr_create",
                                "_after_pr_recorded")
              if "_fire_pr_open_review_triggers" in _calls_in(name, src)}
    assert owners == {"_after_pr_recorded"}, (
        f"the PR-open fire is called from {owners or 'nowhere'}; it must be "
        "called from _after_pr_recorded only")


def test_the_one_place_fires_every_registered_pr_review(monkeypatch):
    """受入条件 1: the shared helper really raises the gate.

    Pairs with the structural test above: that one proves both verbs reach
    here, this one proves arriving here is worth something. Either alone is
    satisfiable by a helper that does nothing.
    """
    fired = {}

    def _fake_fire(number, title, url, pr_repo=None):
        fired["args"] = (number, title, url, pr_repo)

    monkeypatch.setattr(cmd_pr, "_fire_pr_open_review_triggers", _fake_fire)
    monkeypatch.setattr(cmd_pr, "_derive_pr_intent_decision",
                        lambda *a, **k: None)

    cmd_pr._after_pr_recorded(PR_URL, "t", "why", pr_repo="o/r")
    assert fired["args"] == ("9001", "t", PR_URL, "o/r")


def test_the_guard_pair_actually_goes_red_when_the_fire_is_removed(tmp_path):
    """受入条件 4, watched failing — with the tick backstop absent.

    The first draft of this test monkeypatched the helper to a no-op and then
    called the monkeypatched helper, asserting nothing was fired. That is a
    tautology: it passes whether or not the real code fires anything. So the
    inversion is done for real — the call is cut out of a **copy** of the tree
    and the two guards above are expected to fail there.

    A copy, not the repo: the suite runs in parallel over one working tree, so
    editing a real file hands other workers a half-written module
    (``scripts/check-tests-dont-mutate-repo.py`` enforces this).

    No ``beacon trigger tick`` anywhere near this: the backstop would put the
    gate up a moment later and make a missing call look like a timing quirk.
    """
    import shutil
    import subprocess
    import sys

    tree = tmp_path / "tree"
    for sub in ("lib", "tests"):
        shutil.copytree(os.path.join(REPO, sub), tree / sub,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(os.path.join(REPO, "tests", "conftest.py"),
                 tree / "tests" / "conftest.py")

    target = tree / "lib" / "cmd_pr.py"
    src = target.read_text(encoding="utf-8")
    anchor = "    _fire_pr_open_review_triggers(number, title, url, pr_repo=pr_repo)\n"
    assert src.count(anchor) == 1, (
        "the fire call moved — update this inversion rather than dropping it, "
        "or 受入条件 4 stops being measured")
    target.write_text(src.replace(anchor, "    pass\n", 1), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:randomly",
         "-p", "no:cacheprovider",
         "tests/test_pr_review_gate_both_paths_ms181.py",
         "-k", "the_same_place or fires_every_registered"],
        cwd=str(tree), capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONPYCACHEPREFIX": str(tmp_path / "pyc")})
    assert proc.returncode != 0, (
        "the fire was cut out of the one shared place and the guards STILL "
        "passed — they do not measure it:\n" + proc.stdout[-2000:])


# ---------------------------------------------------------------------------
# 受入条件 2: no window where approve passes with an empty gate
# ---------------------------------------------------------------------------

def test_approve_is_refused_while_a_review_is_owed(monkeypatch, capsys):
    """Straight after recording — no tick in between — approve must stop.

    This is the half that matters to a caller: the gate existing is only
    useful if the terminal operation actually waits for it.
    """
    monkeypatch.setattr(cmd_pr, "_pending_review_types_for_pr",
                        lambda number, repo: ["ax", "maintainability"])
    monkeypatch.delenv("BEACON_PR_REVIEW_OVERRIDE", raising=False)

    with pytest.raises(SystemExit) as exc:
        cmd_pr._review_gate_check("9001", action="approve", pr_repo="o/r")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "独立レビューが未実施" in err
    assert "ax" in err and "maintainability" in err


def test_firing_at_record_time_is_what_approve_then_reads(tmp_path,
                                                          monkeypatch):
    """Close the seam between "the gate was raised" and "approve sees it".

    The two tests around this one stub ``_pending_review_types_for_pr``, so on
    their own they leave open the possibility that firing writes something
    approve never reads. Here the REAL fire runs against a temp trigger
    directory and the REAL reader is asked — no tick, no stubs in between.
    """
    triggers = tmp_path / "triggers"
    monkeypatch.setattr(commands_shared, "_get_triggers_dir",
                        lambda: str(triggers))
    monkeypatch.setattr(cmd_pr, "_derive_pr_intent_decision",
                        lambda *a, **k: None)

    before = commands_shared._pending_review_types_for_pr("9001", "o/r")
    assert before == [], (
        "a PR nobody recorded already owes reviews — this probe is not "
        "starting from a clean gate")

    cmd_pr._after_pr_recorded(PR_URL, "t", "why", pr_repo="o/r")

    after = commands_shared._pending_review_types_for_pr("9001", "o/r")
    assert after, (
        "recording the PR raised no gate that approve can see — the fire and "
        "the reader are not looking at the same thing")
    assert "ax" in after, after


def test_approve_passes_once_nothing_is_owed(monkeypatch):
    """The other direction, so "always refuses" cannot satisfy the test above."""
    monkeypatch.setattr(cmd_pr, "_pending_review_types_for_pr",
                        lambda number, repo: [])
    cmd_pr._review_gate_check("9001", action="approve", pr_repo="o/r")


# ---------------------------------------------------------------------------
# 受入条件 5 / 6: the CI stamp no longer depends on remembering an env var
# ---------------------------------------------------------------------------

def test_the_stamp_does_not_require_remembering_an_env_var(monkeypatch):
    """受入条件 5: with no env var set, the repo's own setting decides.

    The authority on "does this repository use the gate" is the GitHub repo
    variable — the workflow enables itself from it. Asking that instead of the
    operator's shell is how the env var stopped being something to remember.
    """
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)
    calls = []

    class _Proc:
        returncode = 0
        stdout = "1\n"

    def _fake_run(argv, **kw):
        calls.append(argv)
        return _Proc()

    monkeypatch.setattr(commands.subprocess, "run", _fake_run)
    assert commands._review_gate_is_in_use() is True
    assert calls and calls[0][:3] == ["gh", "variable", "get"], calls


def test_a_repo_that_does_not_use_the_gate_is_left_alone(monkeypatch):
    """Not stamping elsewhere is the property the env var was protecting.

    Removing the env var must not start pushing commit statuses onto repos that
    never opted in — otherwise 受入条件 5 would be bought by breaking them.
    """
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)

    class _Proc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(commands.subprocess, "run",
                        lambda argv, **kw: _Proc())
    assert commands._review_gate_is_in_use() is False


def test_an_unreadable_setting_does_not_silently_claim_the_gate(monkeypatch):
    """gh missing / unauthenticated / offline → do not stamp.

    Returning True here would mean claiming a stamp was pushed when it was
    not. The caller prints that it did not stamp, so this stays visible rather
    than becoming a silent pending.
    """
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)

    def _boom(argv, **kw):
        raise OSError("gh not found")

    monkeypatch.setattr(commands.subprocess, "run", _boom)
    assert commands._review_gate_is_in_use() is False


@pytest.mark.parametrize("value,expected", [("1", True), ("0", False)])
def test_the_env_var_still_works_as_an_explicit_override(value, expected,
                                                         monkeypatch):
    """Kept for CI itself and for environments that cannot read the variable.

    It is no longer something to remember; it is something to reach for.
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", value)

    def _boom(argv, **kw):
        raise AssertionError("an explicit override must not consult gh")

    monkeypatch.setattr(commands.subprocess, "run", _boom)
    assert commands._review_gate_is_in_use() is expected
