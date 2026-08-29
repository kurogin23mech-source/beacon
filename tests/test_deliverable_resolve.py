"""Unit tests for the deliverable-projection RESOLVER (ms-155 e-5602).

e-5599 built the PURE union (``occupation.project_deliverables``) that carries
deliverable POINTERS (``{target_class, kind, label, projector, ref}``). The
independent philosophy review of PR #677 flagged that no consumer resolved those
pointers to content — the docstring delegated resolution to a "session-start
assembler" that was never written. ``deliverable_resolve`` is that missing I/O
layer. These tests pin:

  * a ``"doc"`` deliverable resolves ``ref`` to the real document body.
  * a stale / empty ``"doc"`` ref surfaces LOUDLY (found=False + error), never
    a crash or silent miss.
  * a ``"rollup"`` deliverable computes a real count+labels summary over the
    producing class's DELIVERED Targets (closing the sibling finding that rollup
    was an allowed projector with no resolver → hollow deliverable).
  * ``resolve_project_deliverables`` is the I/O counterpart of the pure union
    (same entries, each resolved) and tolerates ``None`` like its pure sibling.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import deliverable_resolve as dr  # noqa: E402
import target_descriptor as td  # noqa: E402


# ---------------------------------------------------------------------------
# doc projector — resolve ref to the real document content.
# ---------------------------------------------------------------------------

def _doc_spec():
    return {"target_class": "milestone", "kind": "feature-map", "label": "機能",
            "projector": td.PROJECTOR_DOC, "ref": "application-map"}


def test_doc_resolves_ref_to_document_body():
    fake_doc = {"title": "アプリケーション全貌マップ",
                "content": "# map\n- feature A\n- feature B",
                "updated_at": "2026-08-26T00:00:00"}
    store = mock.Mock()
    store.get_document.return_value = fake_doc
    with mock.patch.object(dr, "get_store", return_value=store):
        out = dr.resolve_deliverable_content({}, _doc_spec())
    store.get_document.assert_called_once_with("application-map")
    r = out["resolved"]
    assert r["found"] is True
    assert r["strategy"] == td.PROJECTOR_DOC
    assert r["title"] == "アプリケーション全貌マップ"
    assert "feature A" in r["content"]
    # the pointer is preserved alongside the resolved value.
    assert out["projector"] == td.PROJECTOR_DOC and out["ref"] == "application-map"


def test_doc_missing_ref_target_surfaces_loudly():
    store = mock.Mock()
    store.get_document.return_value = {}   # local store returns {} for a miss
    with mock.patch.object(dr, "get_store", return_value=store):
        out = dr.resolve_deliverable_content({}, _doc_spec())
    r = out["resolved"]
    assert r["found"] is False
    assert "not found" in r["error"]


def test_doc_empty_ref_does_not_hit_store():
    spec = _doc_spec()
    spec["ref"] = ""
    store = mock.Mock()
    with mock.patch.object(dr, "get_store", return_value=store):
        out = dr.resolve_deliverable_content({}, spec)
    store.get_document.assert_not_called()
    assert out["resolved"]["found"] is False
    assert "empty ref" in out["resolved"]["error"]


# ---------------------------------------------------------------------------
# rollup projector — compute count+labels over the class's DELIVERED Targets.
# ---------------------------------------------------------------------------

def _rollup_spec():
    return {"target_class": "opportunity", "kind": "pipeline", "label": "成約",
            "projector": td.PROJECTOR_ROLLUP, "ref": ""}


def test_rollup_counts_only_delivered_targets():
    rows = [
        {"kind": "opportunity", "label": "A社", "status": "won"},
        {"kind": "opportunity", "label": "B社", "status": "in_progress"},
        {"kind": "opportunity", "label": "C社", "status": "成約"},
        {"kind": "milestone", "label": "別クラス", "status": "done"},  # excluded
    ]
    with mock.patch.object(dr._occ, "project_targets", return_value=rows):
        out = dr.resolve_deliverable_content({}, _rollup_spec())
    r = out["resolved"]
    assert r["found"] is True
    assert r["strategy"] == td.PROJECTOR_ROLLUP
    assert r["count_total"] == 3          # only opportunity rows
    assert r["count_delivered"] == 2      # won + 成約
    assert r["labels"] == ["A社", "C社"]


def test_rollup_label_cap_truncates_but_count_is_exact():
    rows = [{"kind": "opportunity", "label": f"deal{i}", "status": "won"}
            for i in range(dr._ROLLUP_LABEL_CAP + 5)]
    with mock.patch.object(dr._occ, "project_targets", return_value=rows):
        out = dr.resolve_deliverable_content({}, _rollup_spec())
    r = out["resolved"]
    assert r["count_delivered"] == dr._ROLLUP_LABEL_CAP + 5   # exact
    assert len(r["labels"]) == dr._ROLLUP_LABEL_CAP           # capped
    assert r["labels_truncated"] is True


# ---------------------------------------------------------------------------
# changelog projector — the produced value IS the root deliverable-changelog,
# summarised to its current-state map (ms-161 e-5825). milestone→機能 rides this.
# ---------------------------------------------------------------------------

def _changelog_spec():
    return {"target_class": "milestone", "kind": "feature-map", "label": "機能",
            "projector": td.PROJECTOR_CHANGELOG, "ref": ""}


def test_changelog_resolves_to_derived_map():
    import deliverable_changelog as dc
    data = {"name": "P", "profession": "dev"}
    dc.append_deliverable(data, {
        "source": {"target_id": "ms-1", "kind": "milestone"},
        "category": "feature-map", "title": "claim", "summary": "二重取り防止"})
    out = dr.resolve_deliverable_content(data, _changelog_spec())
    r = out["resolved"]
    assert r["found"] is True
    assert r["strategy"] == td.PROJECTOR_CHANGELOG
    assert r["count_active"] == 1
    assert r["categories"] == [{"category": "feature-map", "count": 1}]
    # the derived dev render (application-map-flavoured) is carried for humans
    assert "アプリケーション全貌マップ" in r["rendered"]
    assert "二重取り防止" in r["rendered"]


def test_changelog_empty_log_resolves_found_not_a_miss():
    out = dr.resolve_deliverable_content({"profession": "dev"}, _changelog_spec())
    r = out["resolved"]
    assert r["found"] is True          # empty is a valid empty map, not a failure
    assert r["count_active"] == 0


# ---------------------------------------------------------------------------
# dispatch + union.
# ---------------------------------------------------------------------------

def test_unknown_projector_is_defensive_not_a_crash():
    spec = {"target_class": "x", "kind": "y", "projector": "bogus", "ref": ""}
    out = dr.resolve_deliverable_content({}, spec)
    assert out["resolved"]["found"] is False
    assert "bogus" in out["resolved"]["error"]


def test_every_allowlisted_projector_has_a_resolver():
    """Forcing function (ms-161 maintainability review PR#694): every projector in
    DELIVERABLE_PROJECTORS must have a resolver branch in resolve_deliverable_content.
    Without this, adding a projector to the allowlist but forgetting the elif branch
    silently returns 'no resolver' (found=False) — a drift the master validator does
    not catch. A resolver may legitimately return found=False (e.g. a doc whose ref
    is missing), but it must NOT be the 'no resolver for projector' sentinel."""
    import store as _store
    for projector in td.DELIVERABLE_PROJECTORS:
        spec = {"target_class": "milestone", "kind": "feature-map",
                "projector": projector, "ref": "application-map"}
        # doc resolver reaches the store; give it a benign one so this stays a pure
        # dispatch check (we assert on the 'no resolver' sentinel, not doc content).
        fake_store = mock.Mock()
        fake_store.get_document.return_value = {"title": "m", "content": "", "updated_at": ""}
        with mock.patch.object(dr, "get_store", return_value=fake_store):
            out = dr.resolve_deliverable_content({"profession": "dev"}, spec)
        err = out["resolved"].get("error", "") or ""
        assert "no resolver" not in err, \
            f"projector {projector!r} is allowlisted but has no resolver branch"


def test_resolve_project_deliverables_is_io_counterpart_of_pure_union():
    pure = [_doc_spec()]
    fake_doc = {"title": "map", "content": "body", "updated_at": ""}
    store = mock.Mock()
    store.get_document.return_value = fake_doc
    with mock.patch.object(dr._occ, "project_deliverables", return_value=pure), \
         mock.patch.object(dr, "get_store", return_value=store):
        out = dr.resolve_project_deliverables({"any": "data"})
    assert len(out) == 1
    assert out[0]["resolved"]["found"] is True
    assert out[0]["resolved"]["content"] == "body"


def test_resolve_project_deliverables_none_tolerant():
    assert dr.resolve_project_deliverables(None) == []
