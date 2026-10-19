"""The single-Skill-parser guard must be able to go red (ms-180 follow-up).

The first version of this check compared source text against ``'get("skill")'``.
An independent maintainability review planted ``tool_input.get('skill')`` — the
same read, single quotes — and the check stayed green: a guard that could only
ever pass, whose green was nonetheless read as "the single-parser property is
structurally held".

So every spelling of the read that the guard claims to catch is planted here and
required to be reported. This is the test-the-test; the guard itself runs from
`scripts/ci-strict-drift-guards.sh`.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-single-skill-parser.py"


def _guard():
    spec = importlib.util.spec_from_file_location("_single_skill_parser", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _guard()

PLANTED = [
    ("get-double", 'x = (tool_input or {}).get("skill")'),
    ("get-single", "x = (tool_input or {}).get('skill')"),
    ("subscript-double", 'x = tool_input["skill"]'),
    ("subscript-single", "x = tool_input['skill']"),
    ("get-on-a-call", 'x = payload.get("tool_input", {}).get("skill")'),
    ("nested", 'x = {"a": tool_input.get("skill")}'),
]


@pytest.mark.parametrize("label, statement", PLANTED, ids=[p[0] for p in PLANTED])
def test_the_guard_catches_every_spelling_of_the_read(tmp_path, label, statement):
    planted = tmp_path / "planted.py"
    planted.write_text(f"def f(tool_input, payload):\n    {statement}\n    return x\n",
                       encoding="utf-8")
    found = guard.audit_file(planted)
    assert found, (
        f"the guard did NOT catch `{statement}` — a spelling it claims to "
        f"model. A quote style must not decide whether a guard fires."
    )


def test_the_guard_ignores_other_keys():
    """It must not fire on ordinary dictionary reads, or the only way to get it
    green would be to switch it off."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "other.py"
        path.write_text(
            'def f(d):\n'
            '    a = d.get("name")\n'
            '    b = d.get("command")\n'
            '    c = d["tool_name"]\n'
            '    return a, b, c\n', encoding="utf-8")
        assert guard.audit_file(path) == []


def test_the_guard_goes_red_on_a_planted_second_reader(tmp_path):
    """End to end, through the script's own census: a copy of the repo tree with
    a second reader in an interrupt must exit 1 and name that file."""
    fake = tmp_path / "repo"
    (fake / "lib").mkdir(parents=True)
    (fake / "bin").mkdir(parents=True)
    shutil.copy(REPO / "lib" / "tool_effect.py", fake / "lib" / "tool_effect.py")
    (fake / "lib" / "tool_record_hook.py").write_text(
        "def handle(payload):\n"
        "    tool_input = payload.get('tool_input')\n"
        "    return tool_input.get('skill')  # planted second reader\n",
        encoding="utf-8")

    proc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(fake)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 1, (
        f"the census passed with a second reader present:\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert "tool_record_hook.py" in proc.stderr, proc.stderr


def test_the_guard_refuses_to_pass_when_it_loses_its_anchor(tmp_path):
    """"Nobody reads it" is not success. If the owner stops reading the key, the
    census would otherwise pass by finding nothing — the exact failure this
    guard exists to make impossible."""
    fake = tmp_path / "repo"
    (fake / "lib").mkdir(parents=True)
    (fake / "lib" / "tool_effect.py").write_text(
        "def ledger_name(tool_name, tool_input):\n    return tool_name\n",
        encoding="utf-8")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(fake)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 1, (
        f"the census passed with no owner read at all:\n{proc.stdout}\n{proc.stderr}")
    assert "anchor" in proc.stderr


def test_the_real_tree_has_exactly_one_reader():
    proc = subprocess.run([sys.executable, str(SCRIPT)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


def test_the_guard_is_wired_into_the_strict_ci_gate():
    """A guard nobody runs is documentation.

    This checks only ONE end of "wired in" — that the gate script calls the
    guard. It passed for weeks while the guard never actually ran, because the
    workflow calling the script filtered pull requests by path and the guard's
    trees were not in the list. The other end — that the gate fires for the
    trees the guard walks — is
    `tests/test_drift_guards_actually_run_ms180.py::test_the_pr_gate_fires_for_every_pull_request`.
    Do not read this test alone as "the guard runs" (ms-180 follow-up).
    """
    gate = (REPO / "scripts" / "ci-strict-drift-guards.sh").read_text(
        encoding="utf-8")
    assert "check-single-skill-parser.py" in gate


def test_the_guard_needs_no_pytest_to_run():
    assert "import pytest" not in SCRIPT.read_text(encoding="utf-8")
