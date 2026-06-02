"""Unit tests for StoreApi lost-update conflict guard (e-841).

The CLI mutates the whole project document and PUTs it back wholesale. If the
cloud changed between load and save, a blind PUT would clobber the concurrent
change. StoreApi.save_project() must detect that and raise ConflictError.
"""

import sys
import os
import copy

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from store_api import StoreApi, ConflictError


class FakeClient:
    """In-memory stand-in for ApiClient.

    Mimics the HTTP serialization boundary by deep-copying on every get/put so
    callers never share mutable references with the stored document.
    """

    def __init__(self, doc):
        self._doc = copy.deepcopy(doc)
        self.put_calls = []

    def get_project(self, project_id):
        return copy.deepcopy(self._doc)

    def put_project(self, project_id, data):
        self.put_calls.append(copy.deepcopy(data))
        self._doc = copy.deepcopy(data)
        return {"status": "ok"}


def _make_store(doc):
    s = StoreApi("http://test", "pid", "")
    s._client = FakeClient(doc)
    StoreApi._load_baseline.pop("pid", None)  # isolate class-level state
    return s


def test_save_after_clean_load_succeeds():
    """No concurrent change -> save goes through."""
    s = _make_store({"name": "p", "summary": "v0", "milestones": []})
    data = s.load_project()
    data["summary"] = "mine"
    s.save_project(data)
    assert len(s._client.put_calls) == 1
    assert s._client.put_calls[-1]["summary"] == "mine"


def test_save_detects_concurrent_change():
    """Cloud changed after load -> ConflictError, and our PUT is suppressed."""
    s = _make_store({"name": "p", "summary": "v0", "milestones": []})
    data = s.load_project()
    # Another writer updates the cloud doc after our load.
    s._client._doc = {"name": "p", "summary": "v1-by-other", "milestones": []}
    data["summary"] = "v0-mine"
    with pytest.raises(ConflictError):
        s.save_project(data)
    assert s._client.put_calls == []  # nothing clobbered


def test_save_without_prior_load_writes():
    """No baseline recorded (no load this invocation) -> no false positive."""
    s = _make_store({"name": "p", "milestones": []})
    StoreApi._load_baseline.pop("pid", None)
    s.save_project({"name": "p", "summary": "x", "milestones": []})
    assert len(s._client.put_calls) == 1


def test_baseline_refreshes_after_save():
    """A successful save updates the baseline so a follow-up save still works."""
    s = _make_store({"name": "p", "summary": "v0", "milestones": []})
    data = s.load_project()
    data["summary"] = "first"
    s.save_project(data)
    data["summary"] = "second"
    s.save_project(data)  # must not raise — baseline tracked our own write
    assert len(s._client.put_calls) == 2
    assert s._client.put_calls[-1]["summary"] == "second"
