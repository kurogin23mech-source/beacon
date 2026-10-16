"""The "no test writes to the checkout" guard must be able to go red (ms-180).

`scripts/check-tests-dont-mutate-repo.py` enforces the rule that cost three CI
rounds to localise: an inversion test edited `bin/beacon` in place, and the
other xdist workers executed the half-written file, failing a different
unrelated test each run.

A guard whose green is read as safety has to be shown going red on the very
defect it exists to find — otherwise a loose match (a renamed constant, a
spelling it does not know, a write form it does not model) leaves a guard that
can only ever pass. So each shape the guard claims to model is planted here and
required to be reported, and the real suite is required to be clean.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-tests-dont-mutate-repo.py"


def _guard():
    spec = importlib.util.spec_from_file_location("_no_repo_mutation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _guard()

HEADER = (
    "import shutil\n"
    "from pathlib import Path\n"
    "REPO = Path(__file__).resolve().parents[1]\n"
    "BIN_BEACON = REPO / 'bin' / 'beacon'\n"
)

PLANTED = [
    ("write_text", "BIN_BEACON.write_text('x')"),
    ("write_bytes", "BIN_BEACON.write_bytes(b'x')"),
    ("unlink", "BIN_BEACON.unlink()"),
    ("touch", "BIN_BEACON.touch()"),
    ("open-w", "open(BIN_BEACON, 'w').write('x')"),
    ("open-a", "open(BIN_BEACON, 'a').write('x')"),
    ("shutil-copy", "shutil.copy('/etc/hosts', BIN_BEACON)"),
    ("shutil-move", "shutil.move('/etc/hosts', BIN_BEACON)"),
]


@pytest.mark.parametrize("label, statement", PLANTED,
                         ids=[p[0] for p in PLANTED])
def test_guard_catches_each_modelled_write_shape(tmp_path, label, statement):
    """Every write form the guard's docstring claims to model must be caught."""
    planted = tmp_path / "test_planted.py"
    planted.write_text(f"{HEADER}\n\ndef test_x():\n    {statement}\n",
                       encoding="utf-8")

    findings = guard.audit_file(planted)

    assert findings, (
        f"the guard did NOT catch `{statement}` — a write shape it claims to "
        f"model. A guard that stays green on the defect it exists to find is "
        f"false safety."
    )
    assert any("BIN_BEACON" in what for _line, what, _bound in findings), (
        f"the guard went red but did not name BIN_BEACON: {findings}")


def test_guard_does_not_flag_scratch_paths(tmp_path):
    """Writing under `tmp_path` is how tests are supposed to work.

    If the guard flagged those too, the only way to get it green would be to
    switch it off — and a guard that is switched off protects nothing.
    """
    planted = tmp_path / "test_scratch.py"
    planted.write_text(
        "from pathlib import Path\n"
        "def test_x(tmp_path):\n"
        "    target = tmp_path / 'beacon'\n"
        "    target.write_text('x')\n",
        encoding="utf-8")
    assert guard.audit_file(planted) == []


def test_the_real_suite_does_not_write_to_the_checkout():
    """The rule, applied to this repo."""
    proc = subprocess.run([sys.executable, str(SCRIPT)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        f"a test writes to the checkout:\n{proc.stdout}\n{proc.stderr}")


def test_the_guard_is_wired_into_the_strict_ci_gate():
    """A guard nobody runs is documentation."""
    gate = (REPO / "scripts" / "ci-strict-drift-guards.sh").read_text(
        encoding="utf-8")
    assert "check-tests-dont-mutate-repo.py" in gate


def test_the_guard_needs_no_pytest_to_run():
    """`ci-strict-drift-guards.sh` is also called from the lint-docs job, which
    has no pytest installed."""
    assert "import pytest" not in SCRIPT.read_text(encoding="utf-8")
