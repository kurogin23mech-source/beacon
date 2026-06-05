"""ms-3 e-32: cross-org leak guard for the manifest list endpoint.

The list endpoint (`GET /api/trailnode/capabilities/manifests`) must never
expose a manifest — or even its bare id via `deleted_ids` — for an org the
caller is not a member of. We unit-test the pure filter function so the
guarantee is enforceable without a Firestore stub.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import trailnode


def _make(org: str, name: str = "x", version: str = "0.1.0") -> dict:
    return {"id": f"{org}/{name}@{version}", "org_slug": org}


def test_filter_keeps_only_member_orgs():
    manifests = [_make("alice"), _make("bob"), _make("carol")]
    out = trailnode._filter_manifests_by_membership(manifests, {"alice", "carol"})
    assert {m["id"] for m in out} == {"alice/x@0.1.0", "carol/x@0.1.0"}


def test_filter_excludes_all_when_no_member_slugs():
    manifests = [_make("alice"), _make("bob")]
    assert trailnode._filter_manifests_by_membership(manifests, set()) == []


def test_filter_drops_manifests_without_org_slug():
    # Defensive: legacy docs predating ms-6 have no org_slug field. They
    # must not leak through even if the caller is a member of *something*.
    manifests = [{"id": "ancient/x@0.1.0"}, _make("alice")]
    out = trailnode._filter_manifests_by_membership(manifests, {"alice", "ancient"})
    assert {m["id"] for m in out} == {"alice/x@0.1.0"}


def test_filter_handles_empty_input():
    assert trailnode._filter_manifests_by_membership([], {"alice"}) == []


def test_non_member_gets_no_ids_from_deleted_set():
    # `deleted_ids` is built by running the same filter over deleted docs
    # and projecting `id`. We verify that path here too: a non-member must
    # not learn that `bob/x@0.1.0` ever existed.
    deleted = [_make("alice"), _make("bob"), _make("eve")]
    visible = trailnode._filter_manifests_by_membership(deleted, {"alice"})
    assert [d["id"] for d in visible] == ["alice/x@0.1.0"]
