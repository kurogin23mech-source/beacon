"""ms-128 / e-4289: idempotent send — api_client HTTP-body contract.

``post_bus_event`` is what actually talks to the server. These tests pin the
request-body it builds (the server-facing half of the idempotency contract),
independent of the CLI env plumbing exercised in test_bus_cli.py:

  * an explicit ``is_retry=True`` seeds ``body["is_retry"]`` so the server
    takes the dedup path (return stored event) instead of creating a dup.
  * a normal first send omits ``is_retry`` (the server creates the event) but
    always carries a ``client_event_id`` so a later retry can reference it.
  * an *ambiguous* transport failure (ConnectionError/TimeoutError) is retried
    with the SAME key + is_retry — a definite server error (RuntimeError) is
    NOT retried (not ambiguous).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import api_client  # noqa: E402


def _client_capturing(posts):
    """An ApiClient whose .post records each body and returns a stub event."""
    c = api_client.ApiClient.__new__(api_client.ApiClient)

    def _post(path, body=None, *a, **k):
        posts.append(dict(body or {}))
        return {"event_id": "e-stub", "client_event_id": (body or {}).get("client_event_id")}

    c.post = _post  # type: ignore[assignment]
    return c


def test_is_retry_seeds_body():
    posts: list[dict] = []
    c = _client_capturing(posts)
    c.post_bus_event("p", "dm", payload={"text": "hi"},
                     client_event_id="ce-key", is_retry=True)
    assert posts[-1]["client_event_id"] == "ce-key"
    assert posts[-1]["is_retry"] is True


def test_first_send_has_key_but_no_retry_flag():
    posts: list[dict] = []
    c = _client_capturing(posts)
    c.post_bus_event("p", "dm", payload={"text": "hi"})
    # A key is always minted so a later retry can reuse it...
    assert posts[-1]["client_event_id"].startswith("ce-")
    # ...but a first send is not a retry.
    assert "is_retry" not in posts[-1]


def test_ambiguous_failure_retries_same_key_with_is_retry():
    posts: list[dict] = []
    c = api_client.ApiClient.__new__(api_client.ApiClient)
    calls = {"n": 0}

    def _post(path, body=None, *a, **k):
        posts.append(dict(body or {}))
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("ambiguous transport drop")
        return {"event_id": "e-stub"}

    c.post = _post  # type: ignore[assignment]
    # sleep is real (min(2**attempt,4)); keep it from slowing the test.
    import time as _t
    orig_sleep = _t.sleep
    _t.sleep = lambda *_a, **_k: None
    try:
        c.post_bus_event("p", "dm", payload={"text": "hi"}, max_retries=2)
    finally:
        _t.sleep = orig_sleep
    assert len(posts) == 2, "ambiguous failure should retry once"
    # Same key both times; the retry carries is_retry so the server dedups.
    assert posts[0]["client_event_id"] == posts[1]["client_event_id"]
    assert "is_retry" not in posts[0]
    assert posts[1]["is_retry"] is True


def test_definite_server_error_is_not_retried():
    posts: list[dict] = []
    c = api_client.ApiClient.__new__(api_client.ApiClient)

    def _post(path, body=None, *a, **k):
        posts.append(dict(body or {}))
        raise RuntimeError("403: forbidden")  # definite, not ambiguous

    c.post = _post  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        c.post_bus_event("p", "dm", payload={"text": "hi"})
    assert len(posts) == 1, "a definite server response must not be resent"
