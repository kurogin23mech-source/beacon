"""Tests for the deploy-marker writer in lib/commands.py (ms-105 e-4607).

`beacon deploy record` force-moves the `deployed-prod` git tag so the
deploy-health monitor has a token-free truth source for "what prod should
serve". These pin the gate predicate and that a failed tag/push is surfaced
(not silently swallowed) — an AX review 2026-07-30 finding.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import commands  # noqa: E402
import cmd_deploy  # noqa: E402  (ms-127 e-4815: deploy handlers live here)


# --- _is_default_prod_backend (the marker-move gate) -----------------------

def test_gate_true_for_prod_default_and_empty_backend():
    assert cmd_deploy._is_default_prod_backend("prod", "") is True
    assert cmd_deploy._is_default_prod_backend("prod", "default") is True


def test_gate_false_for_other_backends():
    assert cmd_deploy._is_default_prod_backend("prod", "aws-ga") is False
    assert cmd_deploy._is_default_prod_backend("prod", "trailnode") is False


def test_gate_false_for_non_prod_environment():
    assert cmd_deploy._is_default_prod_backend("staging", "") is False
    assert cmd_deploy._is_default_prod_backend("", "default") is False


# --- _update_deployed_prod_marker (tag + push, best-effort but observable) --

def _ok(*a, **k):
    from types import SimpleNamespace
    return SimpleNamespace(stdout="", stderr="", returncode=0)


def test_marker_success_reports_updated():
    with mock.patch("subprocess.run", side_effect=_ok):
        r = cmd_deploy._update_deployed_prod_marker("abc1234", json_mode=True)
    assert r["updated"] is True and r["error"] is None and r["rev"] == "abc1234"


def test_marker_empty_rev_is_noop():
    r = cmd_deploy._update_deployed_prod_marker("", json_mode=True)
    assert r["updated"] is False and "no rev" in r["error"]


def test_marker_push_failure_is_surfaced_not_swallowed():
    # tag succeeds, push raises → result carries the error even in json_mode
    # (the AX finding: a --json caller must be able to detect a failed push).
    calls = {"n": 0}

    def flaky(cmd, **kw):
        calls["n"] += 1
        if "push" in cmd:
            raise RuntimeError("no upstream")
        return _ok()
    with mock.patch("subprocess.run", side_effect=flaky):
        r = cmd_deploy._update_deployed_prod_marker("abc1234", json_mode=True)
    assert r["updated"] is False
    assert r["error"] and "push failed" in r["error"]


def test_marker_tag_failure_is_surfaced():
    def flaky(cmd, **kw):
        if "tag" in cmd:
            raise RuntimeError("bad object")
        return _ok()
    with mock.patch("subprocess.run", side_effect=flaky):
        r = cmd_deploy._update_deployed_prod_marker("abc1234", json_mode=True)
    assert r["updated"] is False
    assert r["error"] and "tag failed" in r["error"]
