"""ms-8 e-85: trunk/branch routing + auto-version pure helpers.

These exercises only touch the pure helpers in `server/trailnode.py` —
no Firestore stub required. The Firestore-touching code (`_compute_route`,
`_query_existing_versions`, `_query_latest_trunk_version`) is covered by
end-to-end integration once the server is deployed; this file pins down
the deterministic behaviour we rely on at the boundary.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from fastapi import HTTPException  # noqa: E402

import trailnode  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# _parse_capability_ref
# ──────────────────────────────────────────────────────────────────────────


def test_parse_ref_no_version_no_branch():
    out = trailnode._parse_capability_ref("acme/foo")
    assert out == {
        "org_slug": "acme",
        "name": "foo",
        "branch": None,
        "version": None,
    }


def test_parse_ref_pinned_legacy_semver():
    out = trailnode._parse_capability_ref("acme/foo@0.12.0")
    assert out == {
        "org_slug": "acme",
        "name": "foo",
        "branch": None,
        "version": "0.12.0",
    }


def test_parse_ref_pinned_ms8_date_version():
    out = trailnode._parse_capability_ref("acme/foo@2026-06-06.3")
    assert out == {
        "org_slug": "acme",
        "name": "foo",
        "branch": None,
        "version": "2026-06-06.3",
    }


def test_parse_ref_branch_latest():
    out = trailnode._parse_capability_ref("acme/foo:bob")
    assert out == {
        "org_slug": "acme",
        "name": "foo",
        "branch": "bob",
        "version": None,
    }


def test_parse_ref_branch_pinned():
    out = trailnode._parse_capability_ref("acme/foo:bob@2026-06-06.2")
    assert out == {
        "org_slug": "acme",
        "name": "foo",
        "branch": "bob",
        "version": "2026-06-06.2",
    }


def test_parse_ref_rejects_uppercase_org():
    with pytest.raises(HTTPException) as exc:
        trailnode._parse_capability_ref("ACME/foo")
    assert exc.value.status_code == 400


def test_parse_ref_rejects_invalid_branch_chars():
    with pytest.raises(HTTPException) as exc:
        trailnode._parse_capability_ref("acme/foo:Bob")
    assert exc.value.status_code == 400


def test_parse_ref_rejects_malformed_version():
    with pytest.raises(HTTPException) as exc:
        trailnode._parse_capability_ref("acme/foo@whatever")
    assert exc.value.status_code == 400


def test_parse_ref_rejects_missing_name():
    with pytest.raises(HTTPException) as exc:
        trailnode._parse_capability_ref("acme/")
    assert exc.value.status_code == 400


# ──────────────────────────────────────────────────────────────────────────
# _sanitize_branch_identity
# ──────────────────────────────────────────────────────────────────────────


def test_sanitize_branch_identity_simple_email():
    assert trailnode._sanitize_branch_identity("alice@example.com") == "alice"


def test_sanitize_branch_identity_normalizes_case():
    assert trailnode._sanitize_branch_identity("Alice@Example.com") == "alice"


def test_sanitize_branch_identity_replaces_dots_with_hyphens():
    assert (
        trailnode._sanitize_branch_identity("alice.smith@example.com")
        == "alice-smith"
    )


def test_sanitize_branch_identity_collapses_runs_of_hyphens():
    # `+` and `.` both become `-`, so consecutive non-alnum chars collapse
    # into a single hyphen (no double-dash artifact).
    assert (
        trailnode._sanitize_branch_identity("alice+tag.x@example.com")
        == "alice-tag-x"
    )


def test_sanitize_branch_identity_strips_edge_hyphens():
    assert trailnode._sanitize_branch_identity(".alice.@example.com") == "alice"


def test_sanitize_branch_identity_empty_email():
    assert trailnode._sanitize_branch_identity("") == "anon"


def test_sanitize_branch_identity_all_invalid_chars():
    # Local part is all special chars → result collapses to empty → "anon"
    assert trailnode._sanitize_branch_identity("...@example.com") == "anon"


# ──────────────────────────────────────────────────────────────────────────
# _compute_next_version_from_existing
# ──────────────────────────────────────────────────────────────────────────


def test_compute_next_version_empty_history():
    assert (
        trailnode._compute_next_version_from_existing("2026-06-06", [])
        == "2026-06-06.1"
    )


def test_compute_next_version_continues_same_day_sequence():
    assert (
        trailnode._compute_next_version_from_existing(
            "2026-06-06", ["2026-06-06.1", "2026-06-06.2"]
        )
        == "2026-06-06.3"
    )


def test_compute_next_version_ignores_other_dates():
    assert (
        trailnode._compute_next_version_from_existing(
            "2026-06-06", ["2026-06-05.7", "2026-06-04.9"]
        )
        == "2026-06-06.1"
    )


def test_compute_next_version_ignores_legacy_semver():
    # Legacy SemVer versions sharing the namespace must not poison the N
    # counter for the new ms-8 date.N stream.
    assert (
        trailnode._compute_next_version_from_existing(
            "2026-06-06", ["0.12.0", "0.11.1-rc.1"]
        )
        == "2026-06-06.1"
    )


def test_compute_next_version_max_not_count():
    # If history is sparse (e.g. .1 and .5, with .2-.4 deleted) we pick
    # max+1, not len()+1, so versions never collide.
    assert (
        trailnode._compute_next_version_from_existing(
            "2026-06-06", ["2026-06-06.1", "2026-06-06.5"]
        )
        == "2026-06-06.6"
    )


def test_compute_next_version_skips_malformed_tail():
    # A garbage tail (e.g. someone hand-edited Firestore) must not crash;
    # it just doesn't contribute to the max.
    assert (
        trailnode._compute_next_version_from_existing(
            "2026-06-06", ["2026-06-06.broken", "2026-06-06.2"]
        )
        == "2026-06-06.3"
    )


# ──────────────────────────────────────────────────────────────────────────
# _slug_for + _gcs_object_key_for
# ──────────────────────────────────────────────────────────────────────────


def test_slug_for_trunk_matches_legacy_shape():
    # Trunk slug keeps the ms-6 layout so legacy reads (without a branch
    # field) keep hitting the right document id by coincidence.
    assert (
        trailnode._slug_for("acme", "foo", None, "2026-06-06.1")
        == "acme__foo__2026-06-06.1"
    )


def test_slug_for_branch_has_branch_marker():
    # The `__b__` infix is the only thing distinguishing branch slugs from
    # legacy 3-segment ones — must be present.
    slug = trailnode._slug_for("acme", "foo", "bob", "2026-06-06.1")
    assert slug == "acme__foo__b__bob__2026-06-06.1"
    assert "__b__" in slug


def test_gcs_object_key_for_trunk_unchanged():
    # Trunk artifacts must keep the pre-ms-8 path so legacy artifacts
    # remain reachable without a migration step.
    assert (
        trailnode._gcs_object_key_for("acme", "foo", None, "2026-06-06.1")
        == f"{trailnode._GCS_PREFIX}acme/foo/2026-06-06.1.tar.gz"
    )


def test_gcs_object_key_for_branch_uses_branches_subpath():
    assert (
        trailnode._gcs_object_key_for("acme", "foo", "bob", "2026-06-06.1")
        == f"{trailnode._GCS_PREFIX}acme/foo/branches/bob/2026-06-06.1.tar.gz"
    )


# ──────────────────────────────────────────────────────────────────────────
# Regex guards (sanity)
# ──────────────────────────────────────────────────────────────────────────


def test_version_pattern_date_accepts_canonical_form():
    assert trailnode._VERSION_PATTERN_DATE.match("2026-06-06.1") is not None
    assert trailnode._VERSION_PATTERN_DATE.match("2026-12-31.999") is not None


def test_version_pattern_date_rejects_semver():
    assert trailnode._VERSION_PATTERN_DATE.match("0.12.0") is None
    assert trailnode._VERSION_PATTERN_DATE.match("2026-06-06") is None


def test_version_pattern_legacy_accepts_semver():
    assert trailnode._VERSION_PATTERN_LEGACY.match("0.12.0") is not None
    assert trailnode._VERSION_PATTERN_LEGACY.match("1.0.0-rc.1") is not None
