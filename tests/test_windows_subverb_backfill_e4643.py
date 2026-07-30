"""Windows/pipx profession-critical sub-verb backfill (ms-133 e-4643).

The bash dispatcher exposed `phase add/rename/move/remove`, `acquisition
start/done`, and `opportunity describe` but the Python dispatcher (what
Windows/pipx users run) did not register them as argparse subparser choices, so
those users hit `argparse invalid choice`. These pin that the Python side now
(a) PARSES each sub-verb and (b) ROUTES it to the same commands.py engine
subcmd + BEACON_* env the bash `cmd_*` function used — behavioral parity, not
just "the parser stopped erroring".
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

dispatch = importlib.import_module("beacon_cli.dispatch")


@pytest.fixture
def route(monkeypatch):
    """Parse an argv, run its handler, capture the (subcmd, env) it routes to.

    _run_commands_py and _ensure_project are stubbed so no project/cloud is
    touched — we assert only the routing contract."""
    calls: list[tuple[str, dict]] = []

    def fake_run(root, subcmd, env, **kw):
        calls.append((subcmd, dict(env)))
        return 0

    monkeypatch.setattr(dispatch, "_run_commands_py", fake_run)
    monkeypatch.setattr(dispatch, "_ensure_project", lambda: None)

    def _run(argv: list[str]):
        parser = dispatch.build_parser()
        args = parser.parse_args(argv)
        handler = dispatch._HANDLERS[argv[0]]
        rc = handler(ROOT, args)
        return rc, calls

    return _run


def test_phase_add_routes_to_engine(route):
    rc, calls = route(["phase", "add", "opportunity", "Negotiation", "--index", "2"])
    assert rc == 0
    subcmd, env = calls[-1]
    assert subcmd == "phase_add"
    assert env["BEACON_FUNNEL_KIND"] == "opportunity"
    assert env["BEACON_PHASE_NAME"] == "Negotiation"
    assert env["BEACON_PHASE_INDEX"] == "2"


def test_phase_rename_routes_to_engine(route):
    rc, calls = route(["phase", "rename", "account", "Lead", "Prospect"])
    subcmd, env = calls[-1]
    assert subcmd == "phase_rename"
    assert (env["BEACON_FUNNEL_KIND"], env["BEACON_PHASE_OLD"], env["BEACON_PHASE_NEW"]) == (
        "account", "Lead", "Prospect",
    )


def test_phase_move_routes_to_engine(route):
    rc, calls = route(["phase", "move", "opportunity", "Won", "0"])
    subcmd, env = calls[-1]
    assert subcmd == "phase_move"
    assert env["BEACON_PHASE_NAME"] == "Won"
    assert env["BEACON_PHASE_INDEX"] == "0"


@pytest.mark.parametrize("alias", ["remove", "delete", "rm"])
def test_phase_remove_and_aliases_route_to_engine(route, alias):
    rc, calls = route(["phase", alias, "account", "Churned"])
    subcmd, env = calls[-1]
    assert subcmd == "phase_remove"
    assert env["BEACON_FUNNEL_KIND"] == "account"
    assert env["BEACON_PHASE_NAME"] == "Churned"


def test_acquisition_start_sets_in_progress(route):
    rc, calls = route(["acquisition", "start", "acq-1"])
    subcmd, env = calls[-1]
    assert subcmd == "acquisition_status"
    assert env["BEACON_ACQ_ID"] == "acq-1"
    assert env["BEACON_ACQ_STATUS"] == "in_progress"


def test_acquisition_done_sets_done(route):
    rc, calls = route(["acquisition", "done", "acq-9"])
    subcmd, env = calls[-1]
    assert subcmd == "acquisition_status"
    assert env["BEACON_ACQ_STATUS"] == "done"


@pytest.mark.parametrize("alias", ["describe", "desc"])
def test_opportunity_describe_routes_to_engine(route, alias):
    rc, calls = route(["opportunity", alias, "opp-3", "背景メモ"])
    subcmd, env = calls[-1]
    assert subcmd == "opportunity_describe"
    assert env["BEACON_OPP_ID"] == "opp-3"
    assert env["BEACON_OPP_DESCRIPTION"] == "背景メモ"


def test_opportunity_describe_allows_empty_text_to_clear(route):
    """Empty description clears the field (bash parity) — must not error out on
    a missing positional the way a required field would."""
    rc, calls = route(["opportunity", "describe", "opp-3"])
    assert rc == 0
    subcmd, env = calls[-1]
    assert subcmd == "opportunity_describe"
    assert env["BEACON_OPP_DESCRIPTION"] == ""


def test_missing_required_positional_is_usage_error_not_crash(route):
    """`phase add` with no funnel/name prints usage and returns 1 (doesn't route
    a half-formed call to the engine)."""
    rc, calls = route(["phase", "add"])
    assert rc == 1
    assert calls == []
