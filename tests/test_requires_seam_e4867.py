"""ms-127 e-4867: the bin/lib/cmd_*.sh `# requires-fn/var:` seam is a verified
contract, not a comment that can silently rot.

The god-module split moves cmd_<verb>() bodies into sourced family files that
depend on helpers defined in bin/beacon (the dispatcher). Each family file
declares those cross-file deps machine-readably. This test locks in that the
drift checker (1) passes when the real family files' declarations match
bin/beacon, and (2) fails when a declared symbol is absent.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "_cli_help_drift_e4867", ROOT / "scripts" / "check-cli-help-drift.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_real_family_files_declarations_are_satisfied():
    """Every `# requires-fn/var:` in the real bin/lib/cmd_*.sh resolves to a
    function / variable actually present in bin/beacon."""
    mod = _load_checker()
    r = mod.collect_requires_drift()
    assert r["ok"], (
        f"unsatisfied requires seam: fn={r['missing_requires_fn']} "
        f"var={r['missing_requires_var']}"
    )


def test_broken_requires_fn_is_caught(tmp_path):
    """A family file that declares a non-existent helper fails the guard."""
    mod = _load_checker()
    # minimal fake dispatcher + a family file with a bogus requires-fn.
    bin_dir = tmp_path / "bin"
    (bin_dir / "lib").mkdir(parents=True)
    (bin_dir / "beacon").write_text(
        "#!/usr/bin/env bash\n"
        "COMMANDS_PY=x\n"
        "ensure_project() { :; }\n",
        encoding="utf-8",
    )
    (bin_dir / "lib" / "cmd_fake.sh").write_text(
        "# requires-fn: ensure_project no_such_helper\n"
        "# requires-var: COMMANDS_PY MISSING_VAR\n"
        "cmd_fake() { ensure_project; }\n",
        encoding="utf-8",
    )
    r = mod.collect_requires_drift(bin_dir / "beacon")
    assert not r["ok"]
    assert "cmd_fake.sh: no_such_helper" in r["missing_requires_fn"]
    assert "cmd_fake.sh: MISSING_VAR" in r["missing_requires_var"]


def test_satisfied_requires_passes_on_fixture(tmp_path):
    """A family file whose declarations all resolve passes the guard."""
    mod = _load_checker()
    bin_dir = tmp_path / "bin"
    (bin_dir / "lib").mkdir(parents=True)
    (bin_dir / "beacon").write_text(
        "#!/usr/bin/env bash\n"
        "export BEACON_INVOCATION_CWD=y\n"
        "COMMANDS_PY=x\n"
        "ensure_project() { :; }\n"
        "_guard_flag() { :; }\n",
        encoding="utf-8",
    )
    (bin_dir / "lib" / "cmd_ok.sh").write_text(
        "# requires-fn: ensure_project _guard_flag\n"
        "# requires-var: COMMANDS_PY BEACON_INVOCATION_CWD\n"
        "cmd_ok() { ensure_project; }\n",
        encoding="utf-8",
    )
    r = mod.collect_requires_drift(bin_dir / "beacon")
    assert r["ok"], r
