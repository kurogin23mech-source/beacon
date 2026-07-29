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


# --- ms-126 e-4223: bash ↔ Python flag parity -------------------------------


def test_required_flag_parity_holds():
    """The curated priority-flag contract is present on BOTH surfaces today."""
    mod = _load_checker()
    parity = mod.collect_flag_parity()
    assert parity["ok"], parity


def test_python_extractor_sees_priority_flags():
    """argparse introspection finds --priority/--untriaged on the real dispatcher."""
    mod = _load_checker()
    py = mod.python_verb_flags()
    assert {"--priority", "--untriaged"} <= py["milestone add"]
    assert {"--priority", "--untriaged"} <= py["task add"]
    assert "--priority" in py["milestone update"]
    assert "--priority" in py["task update"]


def test_bash_extractor_sees_priority_flags():
    """The bash cmd_* slicer finds the same flags in bin/beacon, including the
    --a|--b alias groups (so `--acceptance-criteria|--ac` doesn't mask siblings)."""
    mod = _load_checker()
    assert {"--priority", "--untriaged"} <= mod.bash_verb_flags("milestone add")
    assert {"--priority", "--untriaged"} <= mod.bash_verb_flags("task add")
    assert "--priority" in mod.bash_verb_flags("milestone update")
    assert "--priority" in mod.bash_verb_flags("task update")


def test_flag_parity_detects_bash_omission(tmp_path):
    """A guard that can't fail is useless: prove it fires when a required flag is
    dropped from the bash surface. A synthetic bin/beacon whose cmd_task_update()
    lacks --priority must surface as missing_from_bash_flags (Python still has
    it, so only the bash side is flagged — per-surface attribution works)."""
    mod = _load_checker()
    fake_bin = tmp_path / "beacon"
    fake_bin.write_text(
        "#!/usr/bin/env bash\n"
        "cmd_milestone_add() {\n"
        "  case \"$1\" in\n"
        "    --priority) priority=\"${2:-}\"; shift 2 ;;\n"
        "    --untriaged) allow_untriaged=1; shift ;;\n"
        "  esac\n"
        "}\n"
        "cmd_milestone_update() {\n"
        "  case \"$1\" in\n"
        "    --priority) priority=\"${2:-}\"; shift 2 ;;\n"
        "  esac\n"
        "}\n"
        "cmd_task_add() {\n"
        "  case \"$1\" in\n"
        "    --priority) priority=\"${2:-}\"; shift 2 ;;\n"
        "    --untriaged) allow_untriaged=1; shift ;;\n"
        "  esac\n"
        "}\n"
        "cmd_task_update() {\n"
        "  case \"$1\" in\n"
        "    --status) status=\"${2:-}\"; shift 2 ;;\n"  # --priority intentionally absent
        "  esac\n"
        "}\n",
        encoding="utf-8",
    )
    parity = mod.collect_flag_parity(bin_path=fake_bin)
    assert not parity["ok"]
    assert "task update --priority" in parity["missing_from_bash_flags"]
    # Python still registers it → the bash-only omission is not misattributed.
    assert "task update --priority" not in parity["missing_from_python_flags"]


def test_python_seam_is_injectable():
    """Maint finding: the Python side must have a *real* injection seam, not an
    illusory path arg (import_module caches, so a path can't swap the module).
    Passing a synthetic parser missing --priority on `milestone add` must be
    detected — proving python_verb_flags(parser=...) actually introspects it."""
    import argparse

    mod = _load_checker()
    p = argparse.ArgumentParser(add_help=False)
    sub = p.add_subparsers()
    ms = sub.add_parser("milestone", add_help=False)
    ms_sub = ms.add_subparsers()
    ms_add = ms_sub.add_parser("add", add_help=False)
    ms_add.add_argument("--untriaged", action="store_true")  # --priority omitted
    flags = mod.python_verb_flags(parser=p)
    assert "--untriaged" in flags["milestone add"]
    assert "--priority" not in flags["milestone add"]  # the injected parser is seen


def test_flag_parity_reports_missing_bash_function_distinctly(tmp_path):
    """AX finding: a *missing function* is a different fix than a *missing flag*.
    A synthetic bin/beacon with no cmd_task_update() at all must surface in
    missing_bash_functions — NOT as 'task update --priority' in the flag list
    (which would misdirect the fix to 'add a case to a loop' that doesn't exist)."""
    mod = _load_checker()
    fake_bin = tmp_path / "beacon"
    fake_bin.write_text(
        "#!/usr/bin/env bash\n"
        "cmd_milestone_add() { case \"$1\" in --priority) :;; --untriaged) :;; esac; }\n"
        "cmd_milestone_update() { case \"$1\" in --priority) :;; esac; }\n"
        "cmd_task_add() { case \"$1\" in --priority) :;; --untriaged) :;; esac; }\n"
        # cmd_task_update() intentionally ABSENT
        , encoding="utf-8",
    )
    parity = mod.collect_flag_parity(bin_path=fake_bin)
    assert not parity["ok"]
    assert "task update" in parity["missing_bash_functions"]
    assert "task update --priority" not in parity["missing_from_bash_flags"]


def test_bash_slice_start_is_anchored_not_substring(tmp_path):
    """Maint finding: the function-header search must be line-anchored so a bare
    mention of the name (comment / usage string) earlier in the file can't be
    mistaken for the definition and slice the wrong region (a false pass)."""
    mod = _load_checker()
    fake_bin = tmp_path / "beacon"
    fake_bin.write_text(
        "#!/usr/bin/env bash\n"
        "# NOTE: cmd_task_update() parses --status; see below.\n"  # decoy mention
        "cmd_other() {\n"
        "  case \"$1\" in\n"
        "    --priority) shift 2 ;;\n"                              # wrong region
        "  esac\n"
        "}\n"
        "cmd_task_update() {\n"
        "  case \"$1\" in\n"
        "    --priority) shift 2 ;;\n"
        "    --status) shift 2 ;;\n"
        "  esac\n"
        "}\n",
        encoding="utf-8",
    )
    flags = mod.bash_verb_flags("task update", bin_path=fake_bin)
    # Must read the REAL definition (has --priority + --status), not latch onto
    # the decoy comment mention and slice cmd_other's region instead.
    assert flags == {"--priority", "--status"}


def test_bash_verb_flags_returns_none_when_function_absent(tmp_path):
    """The extractor signals function-not-found as None (distinct from an empty
    flag set), so callers can tell 'no such function' from 'function has no
    matching flags'."""
    mod = _load_checker()
    fake_bin = tmp_path / "beacon"
    fake_bin.write_text("#!/usr/bin/env bash\ncmd_other() { :; }\n", encoding="utf-8")
    assert mod.bash_verb_flags("task update", bin_path=fake_bin) is None


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
def test_bash_only_command_keeps_its_own_help(proj):
    """`bus` isn't in the registry — the central --help reservation must fall
    through to its bespoke bash help, not override it with the top-level help
    (ms-120 e-3897 regression guard)."""
    r = _run(proj, "bus", "--help")
    assert r.returncode == 0
    assert "beacon bus" in r.stdout
    # must NOT be the generic top-level banner
    assert "Usage: beacon <command>" not in r.stdout


@pytestmark_bash
def test_top_help_renders_from_registry(proj):
    r = _run(proj, "--help")
    assert r.returncode == 0
    # a spread of nouns from the registry must be present
    for token in ("beacon status", "beacon milestone start", "beacon task add"):
        assert token in r.stdout, token
