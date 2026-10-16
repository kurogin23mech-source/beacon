"""The unset-positional census must be able to go red (ms-180).

`scripts/check-cli-unset-positional.py` asserts that no `beacon` invocation
dies from an unset positional parameter under `set -u`. A guard like that is
only worth its green: if a loose match let an unguarded reference through, the
green would be read as safety and would be worse than having no guard at all.

It **plants** an unguarded `$1` in a reachable arm of a COPY of the
dispatcher and requires the census to catch it, naming that very argv.

The census run against the real `bin/beacon` lives in
`scripts/ci-strict-drift-guards.sh`, not here: it spawns ~500 subprocesses, and
the CI job that calls the drift guards has no pytest at all. Running it in both
places would just pay for the same probe twice.

The planted copy is a real install layout — `lib/`, `bin/lib/`, `scripts/`
are symlinked next to the patched `bin/beacon` — because `bin/beacon` resolves
its own library root from `$0`. Probing a lone file would exercise nothing.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-cli-unset-positional.py"


def _census_module():
    spec = importlib.util.spec_from_file_location("check_cli_unset_positional",
                                                  SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_install(tmp_path: pathlib.Path, patched_dispatcher: str) -> pathlib.Path:
    """An install root whose bin/beacon is `patched_dispatcher`."""
    root = tmp_path / "install"
    (root / "bin").mkdir(parents=True)
    for name in ("lib", "scripts", "plugins", "skills", "channel"):
        source = REPO / name
        if source.exists():
            os.symlink(source, root / name)
    os.symlink(REPO / "bin" / "lib", root / "bin" / "lib")
    dispatcher = root / "bin" / "beacon"
    dispatcher.write_text(patched_dispatcher, encoding="utf-8")
    dispatcher.chmod(0o755)
    return dispatcher


@pytest.mark.parametrize("probed_argv, arm", [
    # `beacon status` with no further arguments — the census probes the bare
    # verb path, so an unguarded reference in this arm must be caught.
    ("beacon status", '    status)\n        echo "planted $1 $2"\n        ;;\n'),
    # `beacon note list` — a two-token verb path, to show the catch is not
    # limited to single-token verbs.
    ("beacon note list", '    note)\n        echo "planted $7"\n        ;;\n'),
])
def test_census_catches_a_planted_unguarded_reference(tmp_path, probed_argv, arm):
    """Plant the defect the guard exists to find; the guard must report it.

    Without this, a census that silently probed nothing — a changed registry
    shape, a skipped verb, a swallowed error — would still print OK.
    """
    original = (REPO / "bin" / "beacon").read_text(encoding="utf-8")
    # Insert the planted arm ahead of the existing dispatch arms so it wins.
    needle = "# --- Main dispatch ---\ncase \"${1:-}\" in\n"
    assert needle in original, "main dispatch header moved; update this test"
    patched = original.replace(needle, needle + arm, 1)

    dispatcher = _fake_install(tmp_path, patched)
    census = _census_module()
    probed, findings = census.run_census(dispatcher, tmp_path / "scratch",
                                         flags_per_command=0)

    assert probed > 0, "the census probed nothing — it cannot have found anything"
    assert findings, (
        f"the census did NOT catch a planted unguarded reference in the "
        f"{probed_argv!r} arm. A guard that stays green on the very defect it "
        f"exists to find is false safety ({probed} probes ran)."
    )
    caught = {argv for argv, _rc, _line in findings}
    assert probed_argv in caught, (
        f"the census went red, but not on {probed_argv!r} — it reported "
        f"{sorted(caught)}. The planted arm is the one that must be named."
    )
    assert all("unbound variable" in line for _a, _rc, line in findings)
