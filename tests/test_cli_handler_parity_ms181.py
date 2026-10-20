"""The handler-parity guard fires when a front sends a verb to the wrong place.

ms-181 e-7102
=============
Both CLI fronts name the internal handler as a **string** —
``_run_commands_py(root, "recap_targets", …)`` and
``python3 "$COMMANDS_PY" recap_targets``. Get one of those strings wrong and
that front answers a different verb's question: exit code 0, well-formed JSON,
nothing for the caller to notice. ms-184's fork measured that the three guards
which might plausibly have caught it (help/README drift, the verb ledger, the
capability-scope checker) all stayed exactly as green as before.

受入条件 2 asks for that to be measured rather than asserted, so this module
**re-breaks the wiring and watches the guard go red**. Per 受入条件 4 the
baseline for the other three guards was measured in place (green → green under
the same mutation, 2026-10-20) and is recorded in the commit; repeating those
three here would make this module a multi-minute run for no extra signal.

The mutation is applied to a **copy** of the tree, never to the repository
being tested: the suite runs in parallel over one working copy, so a test that
edits a real file hands other workers a half-written file
(``scripts/check-tests-dont-mutate-repo.py`` enforces this, after it cost three
CI rounds to diagnose once).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKER = os.path.join("scripts", "check-cli-handler-parity.py")

#: One verb is enough to prove the mechanism, and keeps this test ~1s instead
#: of the ~45s the full sweep takes. The breadth lives in
#: scripts/ci-strict-drift-guards.sh, which runs the sweep with no --verb.
PROBE_VERB = "recap targets"

#: The wiring to break: the Python front's `recap targets` branch, pointed at
#: the handler that belongs to `recap projects`. Chosen because both verbs
#: exist, both fronts implement them, and the swap is invisible in any help
#: surface — exactly the defect's real shape.
MUTATION = ('_run_commands_py(root, "recap_targets", env)',
            '_run_commands_py(root, "recap_projects", env)')


@pytest.fixture(scope="module")
def tree():
    """A throwaway copy of the source roots the checker reads."""
    tmp = tempfile.mkdtemp(prefix="handler-parity-test-")
    try:
        for sub in ("lib", "scripts", "bin", "beacon_cli"):
            shutil.copytree(os.path.join(REPO, sub), os.path.join(tmp, sub),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tree, *extra):
    proc = subprocess.run(
        [sys.executable, "-B", CHECKER, "--json", "--verb", PROBE_VERB, *extra],
        cwd=tree, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             # Per-invocation cache dir: the macOS system Python keeps bytecode
             # keyed by absolute source path, so a mutated module's .pyc can be
             # served back after the source is restored. -B stops writing, not
             # reading, so isolation is what actually closes it.
             "PYTHONPYCACHEPREFIX": tempfile.mkdtemp(prefix="hp-pyc-")})
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            payload = json.loads(line)
    return proc.returncode, payload, proc.stdout + proc.stderr


def test_the_guard_is_green_on_an_unmutated_tree(tree):
    """Baseline. Without this, a guard that always fails would "pass" below."""
    rc, payload, out = _run(tree)
    assert rc == 0, out
    assert payload["ok"] is True, payload
    assert payload["compared"] == [PROBE_VERB], payload
    assert payload["mismatches"] == []


def test_the_guard_goes_red_when_a_front_calls_the_wrong_handler(tree):
    """受入条件 2: re-break the wiring, watch it fire.

    The restore is in a ``finally`` so an assertion failure cannot leave the
    copy mutated for the next test in this module.
    """
    path = os.path.join(tree, "beacon_cli", "dispatch.py")
    with open(path, encoding="utf-8") as f:
        original = f.read()
    assert original.count(MUTATION[0]) == 1, (
        "the anchor moved — update MUTATION rather than deleting this test, "
        "or the guard stops being measured")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(original.replace(MUTATION[0], MUTATION[1], 1))
        rc, payload, out = _run(tree)
        assert rc == 1, f"the guard did NOT fire on a mis-wired front:\n{out}"
        assert len(payload["mismatches"]) == 1, payload
        row = payload["mismatches"][0]
        assert row["verb"] == PROBE_VERB
        assert row["bash"] == ["recap_targets"]
        assert row["python"] == ["recap_projects"], (
            "the guard fired but reported the wrong handlers — it must name "
            "what each front actually passed, or the message cannot be acted on")
    finally:
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

    rc, payload, out = _run(tree)
    assert rc == 0, f"the tree did not come back green after restore:\n{out}"


def test_the_probe_observes_the_python_front_not_bash_twice(tree):
    """The Python front delegates to bash on macOS unless told not to.

    ``beacon_cli.main`` runs ``bin/beacon`` when bash is available and
    ``BEACON_NO_BASH`` is unset, so a probe that forgets that flag measures
    bash twice and reports flawless agreement with itself — a false green that
    would survive every mutation above. Pinned on the source because the flag
    is the whole reason the two columns are independent.
    """
    with open(os.path.join(tree, CHECKER), encoding="utf-8") as f:
        src = f.read()
    start = src.index("def python_handlers_for_path(")
    end = src.index("def collect(")
    assert '"BEACON_NO_BASH": "1"' in src[start:end], (
        "the Python probe must set BEACON_NO_BASH=1, else it re-measures bash")


def test_the_checker_refuses_a_verb_the_registry_does_not_declare(tree):
    """A typo in --verb must fail loudly, not silently probe nothing.

    Narrowing to zero verbs would report "OK: 0 verbs compared" — green, and
    meaningless. The same shape as the empty-set false green this milestone
    keeps running into.
    """
    rc, _payload, out = _run(tree, "--verb", "recap nonexistent-subverb")
    assert rc != 0, out
    assert "does not declare" in out, out
