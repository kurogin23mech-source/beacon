"""CLI help renders from a single registry, everywhere (ms-120 e-3897).

AX 原則 1/3 (ヘルプが信頼できないと AI は毎回ソースを読む) + 原則 6 (乖離は
検出でなく構造で不能に): `beacon --help`, `beacon help --json`, and every
subcommand's `--help` all render from lib/commands.py `_help_registry()`. There
is no second hand-maintained help text to drift against.

These pin the two guarantees:
  1. Structural — every registry command actually appears in the rendered
     top-level help (a broken renderer that drops commands fails here).
  2. Behavioral — `--help` returns real usage on subcommands whose parser used
     to mis-read it (`task update --help` → "Entry not found: --help", the
     e-3897 named breakage; `milestone start --help` → "Milestone not found").
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "beacon"
COMMANDS_PY = ROOT / "lib" / "commands.py"
BASH = shutil.which("bash")

# Import the drift checker's registry/render parsers as the structural oracle.
sys.path.insert(0, str(ROOT / "scripts"))
_drift = __import__("check-cli-help-drift".replace("-", "_")) if False else None


def _load_checker():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_cli_help_drift", ROOT / "scripts" / "check-cli-help-drift.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_every_registry_command_is_rendered_in_top_help():
    """Structural single-source guarantee: nothing in the registry is dropped
    from what `beacon --help` prints. Empty diff = one source, rendered whole."""
    mod = _load_checker()
    rendered = mod.parse_bin_beacon()          # verbs printed by `beacon --help`
    registry = mod.parse_help_json()           # verbs in _help_registry()
    missing = {v for v in (registry - rendered) if v}
    assert missing == set(), (
        "Registry commands missing from rendered `beacon --help` "
        "(renderer dropped them, ms-120 e-3897): " + ", ".join(sorted(missing))
    )


def test_drift_checker_passes_strict():
    """The four-surface drift gate stays green in CI (strict) mode."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-cli-help-drift.py"), "--strict"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_usage_has_no_second_help_heredoc():
    """`usage()` must not reintroduce a hand-maintained command list; it renders
    the registry. Guards against a future edit re-adding a divergent heredoc."""
    src = BIN.read_text(encoding="utf-8")
    # locate the usage() body
    start = src.index("usage() {")
    body = src[start:start + 400]
    assert "help_render" in body, "usage() must render from the registry"
    assert "cat <<'EOF'" not in body, (
        "usage() reintroduced a heredoc — that is a second help source (e-3897)"
    )


# --- behavioral (need bash) -------------------------------------------------

pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "t", "milestones": [{"id": "ms-1", "title": "x", '
        '"status": "todo"}]}',
        encoding="utf-8",
    )
    return tmp_path


def _run(proj, *args):
    return subprocess.run(
        [BASH, str(BIN), *args], cwd=proj, capture_output=True, text=True
    )


@pytestmark_bash
@pytest.mark.parametrize(
    "argv,needle",
    [
        (["task", "update", "--help"], "Usage: beacon task update"),
        (["milestone", "start", "--help"], "Usage: beacon milestone start"),
        (["trek", "list", "--help"], "Usage: beacon trek list"),
        (["status", "-h"], "Usage: beacon status"),
    ],
)
def test_subcommand_help_returns_real_usage(proj, argv, needle):
    r = _run(proj, *argv)
    assert r.returncode == 0, r.stderr
    assert needle in r.stdout
    # the historical breakages must not resurface
    low = (r.stdout + r.stderr).lower()
    assert "not found" not in low
    assert "entry not found" not in low


@pytestmark_bash
def test_noun_help_lists_subcommands(proj):
    r = _run(proj, "milestone", "--help")
    assert r.returncode == 0
    assert "beacon milestone add" in r.stdout
    assert "beacon milestone start" in r.stdout


@pytestmark_bash
def test_top_help_renders_from_registry(proj):
    r = _run(proj, "--help")
    assert r.returncode == 0
    # a spread of nouns from the registry must be present
    for token in ("beacon status", "beacon milestone start", "beacon task add"):
        assert token in r.stdout, token
