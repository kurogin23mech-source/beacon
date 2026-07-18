"""Tests for `beacon migrate target-labels` — the ms-109 e-3695 wiring that
gives the two ``backfill_target_labels`` sweeps a production caller.

Before this verb the sweeps existed but nothing ran them (fable review A-1),
so no stored project.json was ever migrated. These tests pin:

  1. the command stamps the canonical ``label`` on legacy-only records across
     both occupations (development milestones/operations + sales
     accounts/opportunities) in one pass;
  2. it records ``work_model.BACKFILL_MARKER`` — the execution trail the
     contract step (e-3626) gates on — even when nothing needed stamping;
  3. it is additive (legacy keys survive) and idempotent (a re-run stamps 0).

load_project / save_project are monkeypatched to an in-memory dict so the test
stays store-agnostic and network-free.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402
import work_model  # noqa: E402


@pytest.fixture
def wired_project(monkeypatch):
    """Monkeypatch load/save to an in-memory project and return a mutable
    holder so the test can inspect what was saved."""
    state = {"data": None, "saved": False}

    def fake_load():
        return state["data"]

    def fake_save(data, op=None):
        state["saved"] = True
        state["op"] = op
        state["data"] = data

    monkeypatch.setattr(commands, "load_project", fake_load)
    monkeypatch.setattr(commands, "save_project", fake_save)
    return state


def test_stamps_both_occupations(wired_project, capsys):
    wired_project["data"] = {
        "name": "p",
        "milestones": [{"id": "ms-1", "title": "MVP"},
                       {"id": "ms-2", "label": "Already"}],
        "operations": [{"id": "op-1", "title": "Watch"}],
        "accounts": [{"id": "a-1", "name": "Acme"}],
        "opportunities": [{"id": "o-1", "title": "Acme Q3"}],
    }

    commands.cmd_migrate_target_labels()

    data = wired_project["data"]
    assert wired_project["saved"] is True
    # dev: ms-1 + op-1 (ms-2 already canonical) = 2; sales: a-1 + o-1 = 2
    assert data["milestones"][0]["label"] == "MVP"
    assert data["operations"][0]["label"] == "Watch"
    assert data["accounts"][0]["label"] == "Acme"
    assert data["opportunities"][0]["label"] == "Acme Q3"
    # legacy keys survive (additive)
    assert data["milestones"][0]["title"] == "MVP"
    assert data["accounts"][0]["name"] == "Acme"

    marker = data[work_model.BACKFILL_MARKER]
    assert marker["dev_count"] == 2
    assert marker["sales_count"] == 2
    assert marker["at"]
    assert marker["version"] == commands.__version__
    assert work_model.target_labels_backfilled(data) is True

    out = capsys.readouterr().out
    assert "4 record(s)" in out
    assert "e-3626" in out


def test_records_trail_even_when_nothing_to_stamp(wired_project, capsys):
    wired_project["data"] = {
        "name": "p",
        "milestones": [{"id": "ms-1", "label": "Canon", "title": "Canon"}],
    }

    commands.cmd_migrate_target_labels()

    data = wired_project["data"]
    marker = data[work_model.BACKFILL_MARKER]
    assert marker["dev_count"] == 0
    assert marker["sales_count"] == 0
    assert work_model.target_labels_backfilled(data) is True
    assert "nothing to stamp" in capsys.readouterr().out


def test_idempotent_rerun(wired_project, capsys):
    wired_project["data"] = {
        "name": "p",
        "milestones": [{"id": "ms-1", "title": "MVP"}],
    }

    commands.cmd_migrate_target_labels()
    capsys.readouterr()  # drain
    commands.cmd_migrate_target_labels()

    data = wired_project["data"]
    assert data[work_model.BACKFILL_MARKER]["dev_count"] == 0
    assert "already complete" in capsys.readouterr().out


def test_op_records_counts(wired_project):
    wired_project["data"] = {
        "name": "p",
        "milestones": [{"id": "ms-1", "title": "MVP"}],
    }
    commands.cmd_migrate_target_labels()
    assert wired_project["op"]["action"] == "migrate_target_labels"
    assert wired_project["op"]["dev_count"] == 1
    assert wired_project["op"]["sales_count"] == 0
