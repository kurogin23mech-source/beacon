"""Cloud-mode round-trip tests for lib/claims.py (ms-55 e-1730).

The local-mode helpers (record_local_claim / release_local_claim /
list_local_claims) are covered in tests/test_claims.py. This file
focuses on the cloud-mode path added by e-1730:

  * `record_claim` calls the registered provider and writes to a mock
    server via `client.save_active_claim`
  * `release_claim` propagates the cloud + local outcome correctly
  * `list_claims` returns the server records when the provider is
    active, filtered by mine / target_kind / target_id
  * A misbehaving provider degrades to local-only without raising

Strategy: substitute a stub ApiClient that records calls and stores
state in-memory. Drives ``register_cloud_provider`` with a closure
that returns the stub. No real HTTP traffic.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import claims  # noqa: E402


class _StubApi:
    """In-memory mock of api_client.ApiClient's claim methods.

    Records every call so tests can assert ordering / payload shape.
    Optionally raises to exercise the fall-back paths.
    """

    def __init__(self, *, project_id: str = "proj-1",
                 raise_on_save: bool = False,
                 raise_on_delete: bool = False,
                 raise_on_list: bool = False):
        self.project_id = project_id
        self.store: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.raise_on_save = raise_on_save
        self.raise_on_delete = raise_on_delete
        self.raise_on_list = raise_on_list

    def save_active_claim(self, project_id: str, claim_id: str, payload: dict):
        self.calls.append(("save", project_id, claim_id, payload))
        if self.raise_on_save:
            raise RuntimeError("simulated save failure")
        self.store[claim_id] = {**payload, "claim_id": claim_id}
        return {"claim_id": claim_id, "status": "saved"}

    def delete_active_claim(self, project_id: str, claim_id: str):
        self.calls.append(("delete", project_id, claim_id))
        if self.raise_on_delete:
            raise RuntimeError("simulated delete failure")
        existed = claim_id in self.store
        self.store.pop(claim_id, None)
        return {"claim_id": claim_id, "deleted": existed}

    def list_active_claims(self, project_id: str):
        self.calls.append(("list", project_id))
        if self.raise_on_list:
            raise RuntimeError("simulated list failure")
        # Server returns oldest-first per server endpoint contract.
        return sorted(self.store.values(), key=lambda r: r.get("issued_at") or "")


@pytest.fixture(autouse=True)
def _reset_provider():
    """Always clear the cloud provider between tests so leaks don't
    cross-contaminate. The fixture replaces it with None on enter and
    exit; tests that want cloud mode explicitly call register_cloud_provider.
    """
    claims.register_cloud_provider(None)
    yield
    claims.register_cloud_provider(None)


@pytest.fixture
def stub_provider(tmp_path):
    """Register an in-memory stub api as the cloud provider.

    Sets BEACON_DIR to tmp_path so the local cache lives in an isolated
    location (= we can also assert the local mirror was kept in sync).
    """
    stub = _StubApi()
    claims.register_cloud_provider(lambda: (stub, stub.project_id))
    os.environ["BEACON_DIR"] = str(tmp_path)
    yield stub
    os.environ.pop("BEACON_DIR", None)


# ---------------------------------------------------------------------------
# Cloud mode active — round-trip
# ---------------------------------------------------------------------------

def _payload(claim_id, target_kind="task", target_id="e-1", sid="sv-A",
             issued_at="2026-06-15T00:00:00Z"):
    return claims.build_claim_payload(
        claim_kind=claims.CLAIM_KIND_CLAIM,
        from_session_id=sid,
        target_kind=target_kind,
        target_id=target_id,
        intent="test",
        claim_id=claim_id,
        issued_at=issued_at,
    )


def test_record_claim_round_trips_to_cloud(stub_provider):
    p = _payload("cl-1")
    claims.record_claim(p)
    # cloud got the save
    save_calls = [c for c in stub_provider.calls if c[0] == "save"]
    assert len(save_calls) == 1
    assert save_calls[0][1] == "proj-1"
    assert save_calls[0][2] == "cl-1"
    assert save_calls[0][3]["claim_id"] == "cl-1"
    # local mirror also got it (= offline survival)
    local = claims.list_local_claims()
    assert any(r["claim_id"] == "cl-1" for r in local)


def test_release_claim_round_trips_and_falls_through(stub_provider):
    p = _payload("cl-2")
    claims.record_claim(p)
    assert claims.release_claim("cl-2") is True
    # cloud got the delete
    delete_calls = [c for c in stub_provider.calls if c[0] == "delete"]
    assert delete_calls == [("delete", "proj-1", "cl-2")]
    # local cache was cleared
    assert all(r["claim_id"] != "cl-2" for r in claims.list_local_claims())


def test_list_claims_returns_server_view(stub_provider):
    # Two claims from different sessions; cloud is the truth.
    claims.record_claim(_payload("cl-A", sid="sv-A", issued_at="2026-06-15T01:00:00Z"))
    claims.record_claim(_payload("cl-B", sid="sv-B", issued_at="2026-06-15T02:00:00Z"))
    listed = claims.list_claims()
    assert [r["claim_id"] for r in listed] == ["cl-A", "cl-B"]
    # mine filter works against the server snapshot
    mine = claims.list_claims(mine="sv-B")
    assert [r["claim_id"] for r in mine] == ["cl-B"]
    # target filter works too
    only_e1 = claims.list_claims(target_kind="task", target_id="e-1")
    assert len(only_e1) == 2  # both claims were on task:e-1 in _payload defaults


def test_list_filters_drop_non_matching(stub_provider):
    claims.record_claim(_payload("cl-X", target_kind="ms", target_id="ms-1"))
    claims.record_claim(_payload("cl-Y", target_kind="task", target_id="e-9"))
    only_ms = claims.list_claims(target_kind="ms")
    assert [r["claim_id"] for r in only_ms] == ["cl-X"]


# ---------------------------------------------------------------------------
# Fall-back behavior
# ---------------------------------------------------------------------------

def test_no_provider_means_local_only(tmp_path):
    """Without a registered provider, all paths use the local store."""
    os.environ["BEACON_DIR"] = str(tmp_path)
    try:
        claims.record_claim(_payload("cl-local"))
        listed = claims.list_claims()
        assert [r["claim_id"] for r in listed] == ["cl-local"]
        assert claims.release_claim("cl-local") is True
        assert claims.list_claims() == []
    finally:
        os.environ.pop("BEACON_DIR", None)


def test_cloud_save_failure_keeps_local(tmp_path):
    """If the cloud round-trip raises, local cache is still written."""
    stub = _StubApi(raise_on_save=True)
    claims.register_cloud_provider(lambda: (stub, stub.project_id))
    os.environ["BEACON_DIR"] = str(tmp_path)
    try:
        # Should not raise — record_claim is best-effort cloud, mandatory local.
        claims.record_claim(_payload("cl-resilient"))
        # local cache has it
        assert any(r["claim_id"] == "cl-resilient" for r in claims.list_local_claims())
    finally:
        os.environ.pop("BEACON_DIR", None)


def test_cloud_list_failure_falls_back_to_local(tmp_path):
    """When the server list fails, we read from the local cache."""
    stub = _StubApi(raise_on_list=True)
    claims.register_cloud_provider(lambda: (stub, stub.project_id))
    os.environ["BEACON_DIR"] = str(tmp_path)
    try:
        # Seed local directly so we have something to fall back to.
        claims.record_local_claim(_payload("cl-fallback"))
        listed = claims.list_claims()
        assert [r["claim_id"] for r in listed] == ["cl-fallback"]
    finally:
        os.environ.pop("BEACON_DIR", None)


def test_misbehaving_provider_degrades_safely(tmp_path):
    """Provider that raises during resolution → cloud disabled, local
    operates normally. No exception leaks to the caller."""

    def _bad_provider():
        raise RuntimeError("provider blew up")

    claims.register_cloud_provider(_bad_provider)
    os.environ["BEACON_DIR"] = str(tmp_path)
    try:
        claims.record_claim(_payload("cl-degrade"))
        listed = claims.list_claims()
        assert [r["claim_id"] for r in listed] == ["cl-degrade"]
    finally:
        os.environ.pop("BEACON_DIR", None)


def test_provider_returning_none_falls_through(tmp_path):
    """Provider returning None means "local mode" — same as no provider."""
    claims.register_cloud_provider(lambda: None)
    os.environ["BEACON_DIR"] = str(tmp_path)
    try:
        claims.record_claim(_payload("cl-none"))
        assert [r["claim_id"] for r in claims.list_claims()] == ["cl-none"]
    finally:
        os.environ.pop("BEACON_DIR", None)


def test_provider_returning_no_project_id_falls_through(tmp_path):
    """An empty project_id is a misconfig — fall back to local."""
    stub = _StubApi()
    claims.register_cloud_provider(lambda: (stub, ""))
    os.environ["BEACON_DIR"] = str(tmp_path)
    try:
        claims.record_claim(_payload("cl-empty-pid"))
        # cloud was NOT called
        assert not stub.calls
        # local has the record
        assert [r["claim_id"] for r in claims.list_claims()] == ["cl-empty-pid"]
    finally:
        os.environ.pop("BEACON_DIR", None)


# ---------------------------------------------------------------------------
# Public API surface — pin the new functions so a rename doesn't sneak by.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name", ["record_claim", "release_claim", "list_claims",
             "register_cloud_provider"],
)
def test_public_api_exported(name: str) -> None:
    assert hasattr(claims, name), f"lib/claims.py missing {name!r}"
