"""Unit tests for the application-map write-through (ms-161 e-5851 / 受入条件4).

Assert ``sync_application_map`` (a) refreshes the doc from the derived render when
the project is dev AND the doc exists, (b) no-ops for non-dev projects, (c) no-ops
when the doc is absent (refresh-not-create), and (d) the generated body carries the
"手編集禁止" banner + the derived map's wedges. The store is fully mocked — no I/O.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import deliverable_changelog as dc  # noqa: E402
import deliverable_doc_sync as sync  # noqa: E402


def _dev_data():
    data = {"name": "P", "profession": "dev"}
    dc.append_deliverable(data, {
        "source": {"target_id": "root", "kind": "root"},
        "category": "状態を一望する", "title": "status",
        "summary": "1画面で把握できる",
        "tags": ["area:見失わない", "cli:beacon status"]})
    return data


def test_build_body_has_banner_and_wedges():
    body = sync.build_application_map_body(_dev_data())
    assert "自動生成です（手編集禁止）" in body       # generated banner
    assert "`cli:beacon status`" in body            # derived map's wedge survives
    assert "アプリケーション全貌マップ" in body


def test_sync_refreshes_existing_dev_doc():
    data = _dev_data()
    calls = {}

    def _rewrite(doc_id, body):
        calls["doc_id"] = doc_id
        calls["body"] = body
        return True

    with mock.patch.object(sync, "get_store") as gs, \
         mock.patch.object(sync, "rewrite_document_body", side_effect=_rewrite):
        gs.return_value.get_document.return_value = {"title": "map", "scope": "core"}
        assert sync.sync_application_map(data) is True
    assert calls["doc_id"] == sync.APPLICATION_MAP_DOC_ID
    assert "`cli:beacon status`" in calls["body"]


def test_sync_noop_for_non_dev():
    data = {"name": "P", "profession": "sales"}
    with mock.patch.object(sync, "get_store") as gs, \
         mock.patch.object(sync, "rewrite_document_body") as rw:
        assert sync.sync_application_map(data) is False
        gs.return_value.get_document.assert_not_called()   # never even looked
        rw.assert_not_called()


def test_sync_noop_when_doc_absent():
    """Refresh-not-create: an unmigrated project (no application-map doc yet) is a
    clean no-op, so the write-through never fabricates the doc."""
    data = _dev_data()
    with mock.patch.object(sync, "get_store") as gs, \
         mock.patch.object(sync, "rewrite_document_body") as rw:
        gs.return_value.get_document.return_value = {}   # not found
        assert sync.sync_application_map(data) is False
        rw.assert_not_called()


# --- rewrite_document_body: the actual write seam (local backend, temp dir) ------

def test_rewrite_document_body_preserves_frontmatter(tmp_path):
    """The body-rewrite helper overwrites the body but KEEPS the doc's title +
    frontmatter (scope / milestone / target) — a generated refresh must not strip
    a doc's identity. Exercised against the local file backend in a temp dir."""
    import commands_shared as cs
    existing = {"title": "全貌マップ", "scope": "core",
                "milestone": "ms-104", "target": "ms-104", "content": "OLD BODY"}
    with mock.patch.object(cs, "get_store") as gs, \
         mock.patch.object(cs, "_is_cloud_mode", return_value=False), \
         mock.patch.object(cs, "_get_docs_dir", return_value=str(tmp_path)):
        gs.return_value.get_document.return_value = existing
        wrote = cs.rewrite_document_body("application-map", "# NEW BODY\n- x")
    assert wrote is True
    written = (tmp_path / "application-map.md").read_text(encoding="utf-8")
    assert "# NEW BODY" in written and "OLD BODY" not in written
    # frontmatter preserved
    assert "scope: core" in written
    assert "ms-104" in written


def test_rewrite_document_body_absent_is_false(tmp_path):
    import commands_shared as cs
    with mock.patch.object(cs, "get_store") as gs:
        gs.return_value.get_document.return_value = {}
        assert cs.rewrite_document_body("nope", "body") is False
