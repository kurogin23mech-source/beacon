"""Cloud write persist verification (ms-166 e-6036).

A whole-document cloud PUT can return 2xx yet silently NOT persist under transient server
load — observed 2026-09-03: ``beacon milestone add`` printed "Added milestone" but neither
the cloud document nor the local mirror had it, and a plain re-run fixed it. That is a
"write reports success but does not persist" silent non-function (same family as the
decision-swallow / who.agent silent bugs this MS sweeps).

``verify_cloud_write_persisted`` closes it: after the write, the caller (which knows what it
wrote) checks a predicate against a FRESH cloud read; an absent write, or an unverifiable
one, is a non-zero exit with a retry hint — never a silent success.
"""
from __future__ import annotations

import os
import sys

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)

import commands_shared as cs  # noqa: E402


def _ms_present(ms_id):
    return lambda d: any(m.get("id") == ms_id for m in d.get("milestones", []))


def test_readback_missing_is_detected_not_silent(monkeypatch):
    # cloud mode + the write did NOT land (fresh read lacks the ms) → SystemExit(1),
    # NOT a silent success (the exact milestone-add reproduction the AC asks to detect).
    monkeypatch.setattr(cs, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cs, "load_project", lambda: {"milestones": []})
    with pytest.raises(SystemExit) as ei:
        cs.verify_cloud_write_persisted(_ms_present("ms-167"), what="milestone ms-167")
    assert ei.value.code == 1


def test_readback_present_passes(monkeypatch):
    # cloud mode + the write DID land → no raise (success is confirmed, not assumed).
    monkeypatch.setattr(cs, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cs, "load_project",
                        lambda: {"milestones": [{"id": "ms-167"}]})
    cs.verify_cloud_write_persisted(_ms_present("ms-167"), what="milestone ms-167")


def test_local_mode_is_noop_no_readback(monkeypatch):
    # local mode: the write is synchronous and save_project raises on failure, so there is
    # nothing async to verify — the helper must NOT even read back (no extra I/O), and must
    # not evaluate the predicate (here it would raise if it did).
    monkeypatch.setattr(cs, "_is_cloud_mode", lambda: False)
    read_calls = []
    monkeypatch.setattr(cs, "load_project",
                        lambda: read_calls.append(1) or {"milestones": []})

    def _predicate_that_would_fail(_d):
        raise AssertionError("predicate must not be evaluated in local mode")

    cs.verify_cloud_write_persisted(_predicate_that_would_fail, what="x")
    assert read_calls == []  # no cloud read-back in local mode


def test_unverifiable_readback_is_not_silent_success(monkeypatch):
    # cloud read-back itself errors (offline / API error): we must NOT claim success — an
    # unverifiable write exits non-zero with a retry hint (the silent-success is the bug).
    monkeypatch.setattr(cs, "_is_cloud_mode", lambda: True)

    def _boom():
        raise ConnectionError("offline")

    monkeypatch.setattr(cs, "load_project", _boom)
    with pytest.raises(SystemExit) as ei:
        cs.verify_cloud_write_persisted(lambda _d: True, what="milestone ms-167")
    assert ei.value.code == 1


def test_systemexit_from_readback_propagates(monkeypatch):
    # If load_project itself raises SystemExit (e.g. a downstream credential guard), that
    # must propagate unchanged (not be reframed as a generic verify failure).
    monkeypatch.setattr(cs, "_is_cloud_mode", lambda: True)

    def _exit():
        raise SystemExit(3)

    monkeypatch.setattr(cs, "load_project", _exit)
    with pytest.raises(SystemExit) as ei:
        cs.verify_cloud_write_persisted(lambda _d: True, what="x")
    assert ei.value.code == 3
