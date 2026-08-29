"""Unit tests for the application-map → deliverable-changelog backfill parser
(ms-161 e-5851). Drives ``parse_map`` on a fixture (no I/O, no cloud): asserts
one entry per bullet, 大節→area tag / 小節→category, wedges captured verbatim,
the seed marker added, and that the derived render introduces NO new drift
(parity) vs the source — the honest "楔維持" contract."""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

_spec = importlib.util.spec_from_file_location(
    "seed_app_map",
    os.path.join(REPO, "scripts", "seed-application-map-deliverables.py"))
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


FIXTURE = """---
scope: core
---
# Beacon アプリケーション全貌マップ

> intro prose, ignored.

## A. 見失わない — 現在地と進捗の可視化

### A1. 状態を一望する
- いま何が進行中かを1画面で把握できる `cli:beacon status` `api:GET /api/projects`
- 過去の記録を探せる `cli:beacon search`

### A2. マイルストーンを管理する
- 目的地を立て状態を回せる `cli:beacon milestone *`

## B. 透明にする

### B1. 設計文書を残す
- 設計原則を記録できる `skill:/beacon-spec` `file:lib/commands.py`
"""


def test_parse_one_entry_per_bullet():
    entries = seed.parse_map(FIXTURE)
    assert len(entries) == 4  # 2 + 1 + 1 bullets; intro prose ignored


def test_parse_area_category_and_source():
    entries = seed.parse_map(FIXTURE)
    e0 = entries[0]
    assert e0["category"] == "A1. 状態を一望する"
    assert e0["source"] == {"target_id": "root", "kind": "root"}
    # area rides as an area: tag (大節)
    assert "area:A. 見失わない — 現在地と進捗の可視化" in e0["tags"]
    # summary is the 散文 with wedges stripped
    assert e0["summary"] == "いま何が進行中かを1画面で把握できる"


def test_parse_captures_wedges_verbatim():
    entries = seed.parse_map(FIXTURE)
    e0 = entries[0]
    assert "cli:beacon status" in e0["tags"]
    assert "api:GET /api/projects" in e0["tags"]
    # a later section's file/skill wedges too
    b1 = entries[-1]
    assert "skill:/beacon-spec" in b1["tags"]
    assert "file:lib/commands.py" in b1["tags"]


def test_every_entry_has_seed_marker():
    entries = seed.parse_map(FIXTURE)
    assert all(seed.SEED_MARKER in e["tags"] for e in entries)


def test_second_area_switches_category_group():
    entries = seed.parse_map(FIXTURE)
    cats = [e["category"] for e in entries]
    assert cats == ["A1. 状態を一望する", "A1. 状態を一望する",
                    "A2. マイルストーンを管理する", "B1. 設計文書を残す"]


def test_derived_render_has_parity_with_source():
    """The derived render must introduce no NEW drift vs the fixture source —
    the wedge machine-check is preserved by derivation (e-5851)."""
    entries = seed.parse_map(FIXTURE)
    result = seed._reconcile_render(entries, source_text=FIXTURE)
    assert result["parity"] is True
    assert result["new_missing"] == []
    assert result["new_phantom"] == []


def test_already_seeded_guard():
    import deliverable_changelog as dc
    data = {"name": "P", "profession": "dev"}
    assert seed.already_seeded(data) is False
    dc.append_deliverable(data, {
        "source": {"target_id": "root", "kind": "root"},
        "category": "c", "title": "t", "summary": "s",
        "tags": [seed.SEED_MARKER]})
    assert seed.already_seeded(data) is True
