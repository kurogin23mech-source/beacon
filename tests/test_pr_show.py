"""Unit tests for `beacon pr show` — the read path /review uses to fetch
PR intent and commits for intent-vs-implementation checking (e-608).

The command supports three identifier forms:
    - entry id (e-NNN)
    - PR number (digits)
    - URL (https://github.com/.../pull/N)

These tests exercise resolution + payload shape. We patch load_project to
inject a synthetic project so we don't touch the user's .beacon/.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_pr  # noqa: E402  (ms-127 e-4856: pr family moved here)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_with_one_pr():
    """Two milestones; ms-42 has one recorded PR entry with intent + commits."""
    return {
        "name": "t",
        "summary": "",
        "milestones": [
            {"id": "ms-1", "title": "old", "status": "done", "entries": []},
            {
                "id": "ms-42",
                "title": "current",
                "status": "in_progress",
                "entries": [
                    {
                        "id": "e-700",
                        "type": "pr",
                        "description": "feat(cli): beacon update",
                        "status": "in_review",
                        "meta": {
                            "url": "https://github.com/kurogin23mech-source/beacon/pull/42",
                            "pr_number": 42,
                            "author": "alice",
                            "intent": "Close UC6-K1 by one-shotting brew upgrade",
                            "pr_status": "in_review",
                            "review_status": "pending",
                            "review_rationale": None,
                        },
                        "entries": [
                            {"id": "e-701", "type": "commit",
                             "description": "feat: thing",
                             "meta": {"hash": "abc1234"}},
                            {"id": "e-702", "type": "commit",
                             "description": "test: thing",
                             "meta": {"hash": "def5678"}},
                        ],
                    },
                ],
            },
        ],
    }


def _run_pr_show(monkeypatch, ident: str, json_mode: bool = True):
    """Invoke cmd_pr_show with the given identifier; return parsed payload."""
    monkeypatch.setattr(cmd_pr, "load_project", _project_with_one_pr)
    monkeypatch.setenv("BEACON_PR_IDENT", ident)
    monkeypatch.setenv("BEACON_JSON", "1" if json_mode else "")

    buf = io.StringIO()
    err = io.StringIO()
    code = None
    try:
        with redirect_stdout(buf), redirect_stderr(err):
            commands.cmd_pr_show()
    except SystemExit as e:
        code = e.code
    return code, buf.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_resolve_by_entry_id(monkeypatch):
    code, out, err = _run_pr_show(monkeypatch, "e-700")
    assert code is None
    payload = json.loads(out)
    assert payload["entry_id"] == "e-700"
    assert payload["ms_id"] == "ms-42"
    assert payload["intent"] == "Close UC6-K1 by one-shotting brew upgrade"


def test_resolve_by_pr_number(monkeypatch):
    code, out, err = _run_pr_show(monkeypatch, "42")
    assert code is None
    payload = json.loads(out)
    assert payload["entry_id"] == "e-700"
    assert payload["pr_number"] == 42


def test_resolve_by_url(monkeypatch):
    code, out, err = _run_pr_show(
        monkeypatch, "https://github.com/kurogin23mech-source/beacon/pull/42",
    )
    assert code is None
    payload = json.loads(out)
    assert payload["entry_id"] == "e-700"


def test_resolve_unknown_exits_1(monkeypatch):
    code, out, err = _run_pr_show(monkeypatch, "e-999")
    assert code == 1
    assert "no PR entry matches" in err


# ---------------------------------------------------------------------------
# Payload shape — what /review actually needs
# ---------------------------------------------------------------------------

def test_payload_contains_intent_and_commits(monkeypatch):
    code, out, err = _run_pr_show(monkeypatch, "e-700")
    payload = json.loads(out)
    # /review needs intent for intent-vs-implementation check (e-608)
    assert "intent" in payload
    # /review needs the commit list to scope its diff inspection
    assert len(payload["commits"]) == 2
    assert payload["commits"][0]["hash"] == "abc1234"
    assert payload["commits"][0]["message"] == "feat: thing"
    # Review state must be exposed for round-trip visualisation (e-609)
    assert payload["review_status"] == "pending"


def test_payload_handles_pr_without_intent(monkeypatch):
    """A PR with no recorded intent must surface intent="" — *not* missing —
    so /review can detect and warn."""
    proj = _project_with_one_pr()
    proj["milestones"][1]["entries"][0]["meta"]["intent"] = ""
    monkeypatch.setattr(cmd_pr, "load_project", lambda: proj)
    monkeypatch.setenv("BEACON_PR_IDENT", "e-700")
    monkeypatch.setenv("BEACON_JSON", "1")

    buf = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        commands.cmd_pr_show()
    payload = json.loads(buf.getvalue())
    assert payload["intent"] == ""


def test_text_mode_warns_when_intent_missing(monkeypatch):
    proj = _project_with_one_pr()
    proj["milestones"][1]["entries"][0]["meta"]["intent"] = ""
    monkeypatch.setattr(cmd_pr, "load_project", lambda: proj)
    monkeypatch.setenv("BEACON_PR_IDENT", "e-700")
    monkeypatch.setenv("BEACON_JSON", "")

    buf = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        commands.cmd_pr_show()
    out = buf.getvalue()
    assert "none recorded" in out
    assert "/review cannot" in out
