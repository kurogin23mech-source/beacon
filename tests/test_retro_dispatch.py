"""Win parity coverage for `beacon retro` (ms-61 / e-2348).

Mirrors ``tests/test_ms55_dispatch.py``'s strategy: patch ``subprocess.call``
inside ``beacon_cli.dispatch`` so we capture the (cmd, env) tuple that *would
have* been forwarded to ``commands.py``. We then assert each argv path produces
the env contract that ``bin/beacon`` already uses on macOS/Linux for the
``retro)`` case (lines 4119-4151) and ``cmd_retro`` (lines 1421-1474).

Verbs covered:

  * ``beacon retro`` / ``beacon retro --prepare`` / ``beacon retro --catch-up``
    → ``retro_prepare`` (env: BEACON_SINCE / BEACON_UNTIL / BEACON_RETRO_CATCH_UP)
  * ``beacon retro --since X --until Y``
    → ``retro_prepare`` (explicit dates pass through verbatim)
  * ``beacon retro save --week W [--content T] [--json]``
    → ``retro_save`` (env: BEACON_RETRO_WEEK / BEACON_CONTENT / BEACON_JSON)
  * ``beacon retro save`` (missing --week) → rc=1, no subprocess call
  * ``beacon retro done`` → ``retro_done`` (no env)

Why this matters: dolphin (Win) hit ``argparse: invalid choice: 'retro'`` on
the Windows beacon.exe path before this PR, making /beacon-retro Skill
unusable end-to-end on Windows. The parser registration + handler routing
is what this test pins.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import dispatch, main as main_mod  # noqa: E402


@pytest.fixture
def captured_call(monkeypatch):
    box: Dict[str, Any] = {"cmd": None, "env": None}

    def fake_call(cmd, env=None, **kwargs):
        box["cmd"] = list(cmd)
        box["env"] = dict(env) if env is not None else None
        return 0

    monkeypatch.setattr(dispatch.subprocess, "call", fake_call)
    return box


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "test", "milestones": []}'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    yield tmp_path


@pytest.fixture
def no_bash(monkeypatch):
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)


# ---------------------------------------------------------------------------
# Parser registration — the regression that bit dolphin
# ---------------------------------------------------------------------------

def test_retro_subparser_is_registered():
    """Pre-PR, `argparse` raised ``invalid choice: 'retro'``."""
    parser = dispatch.build_parser()
    args = parser.parse_args(["retro", "--prepare"])
    assert args.command == "retro"
    assert getattr(args, "retro_prepare_flag", False) is True


def test_retro_save_subparser_is_registered():
    parser = dispatch.build_parser()
    args = parser.parse_args(["retro", "save", "--week", "2026-W23"])
    assert args.command == "retro"
    assert args.retro_cmd == "save"
    assert args.week == "2026-W23"


def test_retro_done_subparser_is_registered():
    parser = dispatch.build_parser()
    args = parser.parse_args(["retro", "done"])
    assert args.command == "retro"
    assert args.retro_cmd == "done"


def test_retro_is_in_handlers_dict():
    """Without this, dispatch() would print `Unknown command` even with the parser."""
    assert "retro" in dispatch._HANDLERS
    assert dispatch._HANDLERS["retro"] is dispatch._handle_retro


# ---------------------------------------------------------------------------
# retro --prepare (top-level)
# ---------------------------------------------------------------------------

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_retro_default_prepare_routes_to_retro_prepare(
    project_dir, no_bash, captured_call,
):
    rc = main_mod.main(["retro"])
    assert rc == 0
    env = captured_call["env"]
    assert captured_call["cmd"][-1] == "retro_prepare"
    # Default since/until are auto-computed and must look like ISO dates.
    assert ISO_DATE.match(env["BEACON_SINCE"]), env["BEACON_SINCE"]
    assert ISO_DATE.match(env["BEACON_UNTIL"]), env["BEACON_UNTIL"]
    assert env["BEACON_RETRO_CATCH_UP"] == ""


def test_retro_prepare_flag_is_explicit_noop(
    project_dir, no_bash, captured_call,
):
    """`--prepare` is accepted for parity with the bash CLI."""
    rc = main_mod.main(["retro", "--prepare"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "retro_prepare"


def test_retro_catch_up_sets_env_flag(
    project_dir, no_bash, captured_call,
):
    rc = main_mod.main(["retro", "--catch-up"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_RETRO_CATCH_UP"] == "1"
    assert captured_call["cmd"][-1] == "retro_prepare"


def test_retro_explicit_since_until_pass_through(
    project_dir, no_bash, captured_call,
):
    rc = main_mod.main([
        "retro",
        "--since", "2026-06-01",
        "--until", "2026-06-07",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_SINCE"] == "2026-06-01"
    assert env["BEACON_UNTIL"] == "2026-06-07"
    assert captured_call["cmd"][-1] == "retro_prepare"


# ---------------------------------------------------------------------------
# retro save
# ---------------------------------------------------------------------------

def test_retro_save_with_week_routes_to_retro_save(
    project_dir, no_bash, captured_call,
):
    rc = main_mod.main([
        "retro", "save", "--week", "2026-W23",
        "--content", "weekly notes",
        "--json",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_RETRO_WEEK"] == "2026-W23"
    assert env["BEACON_CONTENT"] == "weekly notes"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "retro_save"


def test_retro_save_with_stdin_flag(
    project_dir, no_bash, captured_call,
):
    """`--stdin` is accepted; commands.py reads via _resolve_content_input."""
    rc = main_mod.main([
        "retro", "save", "--week", "2026-W23", "--stdin",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_RETRO_WEEK"] == "2026-W23"
    # --content not passed; commands.py reads from stdin (inherited).
    assert env["BEACON_CONTENT"] == ""
    assert captured_call["cmd"][-1] == "retro_save"


def test_retro_save_without_week_fails(
    project_dir, no_bash, captured_call, capsys,
):
    rc = main_mod.main(["retro", "save", "--content", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "week" in err.lower()
    # Critical: no subprocess fired (= we caught it before commands.py).
    assert captured_call["cmd"] is None


# ---------------------------------------------------------------------------
# retro done
# ---------------------------------------------------------------------------

def test_retro_done_routes_to_retro_done(
    project_dir, no_bash, captured_call,
):
    rc = main_mod.main(["retro", "done"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "retro_done"


# ---------------------------------------------------------------------------
# help surface
# ---------------------------------------------------------------------------

def test_retro_help_does_not_invoke_subprocess(
    project_dir, no_bash, captured_call, capsys,
):
    rc = main_mod.main(["retro", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "beacon retro" in out
    assert "save" in out
    assert "done" in out
    assert captured_call["cmd"] is None
