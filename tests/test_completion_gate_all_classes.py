"""ms-142 T3 / e-5158 — every target-class's terminal transition is gated.

T2 declared each class's state model; T3 guarantees the anti-self-close GATE
EXISTS for every class (class-engine ideal §5). milestone / operation already
route through the dev spine and opportunity through the sales judge flow; the
two previously-ungated classes — acquisition and descriptor-defined targets —
get the lightweight structural ban (Scope B, leader Q2 ruling): an AI session
cannot complete them directly without a human / override signal.

These pin the WIRING (the verb actually invokes the ban), complementing
``test_attainment_gate_ban`` (which pins the decision function) and
``test_target_state`` (which pins the state-model DECLARATION of the gate). The
ban fires BEFORE ``load_project`` in both verbs, so no project fixture is needed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import cmd_acquisition  # noqa: E402
import cmd_target  # noqa: E402


class _Env:
    """Context-manager env swap so tests don't pollute global env."""

    def __init__(self, env):
        self._wanted = env
        self._prior = {}

    def __enter__(self):
        for k, v in self._wanted.items():
            self._prior[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, v in self._prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_AI_SESSION = {"BEACON_TARGET_COMPLETE_USER_OVERRIDE": None,
               "BEACON_SESSION_KIND": None}


# ---------------------------------------------------------------------------
# acquisition — reaching `done` is gated.
# ---------------------------------------------------------------------------

def test_acquisition_done_refused_for_ai_session():
    with _Env({**_AI_SESSION, "BEACON_ACQ_ID": "acq-1",
               "BEACON_ACQ_STATUS": "done"}):
        with pytest.raises(SystemExit) as exc:
            cmd_acquisition.cmd_acquisition_status()
        assert exc.value.code == 2


def test_acquisition_done_allowed_under_override(monkeypatch):
    # With the override the ban must NOT fire — the handler proceeds past it into
    # load_project (which we stub to a sentinel to prove the ban was cleared).
    monkeypatch.setattr(cmd_acquisition, "load_project",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("past-ban")))
    with _Env({"BEACON_TARGET_COMPLETE_USER_OVERRIDE": "1",
               "BEACON_SESSION_KIND": None,
               "BEACON_ACQ_ID": "acq-1", "BEACON_ACQ_STATUS": "done"}):
        with pytest.raises(RuntimeError, match="past-ban"):
            cmd_acquisition.cmd_acquisition_status()


def test_acquisition_non_terminal_skips_the_gate(monkeypatch):
    # A non-terminal status (in_progress) is not a completion claim — the ban must
    # be skipped even for an AI session (proven by reaching the stubbed load_project).
    monkeypatch.setattr(cmd_acquisition, "load_project",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("past-ban")))
    with _Env({**_AI_SESSION, "BEACON_ACQ_ID": "acq-1",
               "BEACON_ACQ_STATUS": "in_progress"}):
        with pytest.raises(RuntimeError, match="past-ban"):
            cmd_acquisition.cmd_acquisition_status()


# ---------------------------------------------------------------------------
# descriptor-defined class — `beacon target close` is gated.
# ---------------------------------------------------------------------------

def test_descriptor_close_refused_for_ai_session():
    with _Env({**_AI_SESSION, "BEACON_TARGET_CLASS": "matter",
               "BEACON_TARGET_ID": "mat-1", "BEACON_REASON": ""}):
        with pytest.raises(SystemExit) as exc:
            cmd_target.cmd_target_close()
        assert exc.value.code == 2


def test_descriptor_close_allowed_under_human_session(monkeypatch):
    monkeypatch.setattr(cmd_target, "load_project",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("past-ban")))
    with _Env({"BEACON_TARGET_COMPLETE_USER_OVERRIDE": None,
               "BEACON_SESSION_KIND": "human",
               "BEACON_TARGET_CLASS": "matter", "BEACON_TARGET_ID": "mat-1",
               "BEACON_REASON": ""}):
        with pytest.raises(RuntimeError, match="past-ban"):
            cmd_target.cmd_target_close()
