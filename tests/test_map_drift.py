"""Tests for the application-map machinery (ms-104).

Covers two surfaces:

  (A) scripts/check-map-drift.py — the mechanical reconcile (= surface 列挙 +
      両方向 diff, e-3151). We pin the enumeration returns known members and
      that reconcile() correctly reports 書き漏れ (missing) / 幽霊 (phantom)
      including the family-wedge matching rules (cli noun `*`, api method `*`
      prefix). The matching logic is isolated by monkeypatching the module's
      enumerate_* helpers to small fixed sets, so the test doesn't depend on
      Beacon's evolving real surface counts.

  (B) lib/commands.py trigger + box helpers (e-3153a / e-3154 / e-3155):
      - _application_map_box_content / _seed_application_map_box (init box)
      - _auto_fire_map_drift_trigger (commit-count backstop)
      - _fire_map_reconcile_trigger (deploy-time reconcile prompt)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))


def _load_check_map_drift():
    path = REPO_ROOT / "scripts" / "check-map-drift.py"
    spec = importlib.util.spec_from_file_location("check_map_drift", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CMD = _load_check_map_drift()


# --------------------------------------------------------------- (A) enumerate

def test_enumerate_cli_includes_known_leaves():
    cli = CMD.enumerate_cli()
    assert "task_done" in cli
    assert "trek_create" in cli
    # shell-handled top-level commands are included (not in dispatch dict)
    assert "status" in cli
    # internal-only keys are excluded
    assert "auth_check" not in cli
    assert "help_json" not in cli


def test_enumerate_api_includes_known_routes_and_normalizes_params():
    api = CMD.enumerate_api()
    assert "GET /api/projects/{}/search" in api
    # trailnode sub-router prefix is prepended
    assert any(r.startswith("GET /api/trailnode/orgs") for r in api)
    # websocket method is preserved (not collapsed to GET)
    assert "WEBSOCKET /ws/projects/{}" in api


def test_enumerate_skill_includes_beacon_map():
    skills = CMD.enumerate_skills()
    assert "/beacon-map" in skills
    assert "/beacon-session-start" in skills


# ------------------------------------------------------------- (A) wedge parse

def test_parse_wedges_extracts_typed_tokens():
    text = "value `cli:beacon task done` and `api:GET /api/x` `skill:/beacon-map` `file:foo.py`"
    wedges = dict(CMD.parse_wedges(text))
    assert wedges["cli"] == "beacon task done" or ("cli", "beacon task done") in CMD.parse_wedges(text)
    kinds = {t for t, _ in CMD.parse_wedges(text)}
    assert kinds == {"cli", "api", "skill", "file"}


def test_bare_command_in_prose_is_not_a_wedge():
    # No `type:` prefix → not matched (so examples can be written freely).
    text = "run `beacon task done` as an example"
    assert CMD.parse_wedges(text) == []


# --------------------------------------------------------- (A) reconcile logic

@pytest.fixture
def small_surface(monkeypatch):
    """Isolate reconcile()'s matching logic from Beacon's real surface."""
    monkeypatch.setattr(CMD, "enumerate_cli",
                        lambda: {"task_add", "task_done", "trek_create"})
    monkeypatch.setattr(CMD, "enumerate_api",
                        lambda: {"GET /api/x", "POST /api/x/y"})
    monkeypatch.setattr(CMD, "enumerate_skills", lambda: {"/beacon-map"})


def test_reconcile_clean_map_has_no_drift(small_surface):
    text = (
        "- t `cli:beacon task *` `cli:beacon trek create` "
        "`api:* /api/x` `skill:/beacon-map`\n"
    )
    res = CMD.reconcile(text)
    assert res["counts"]["missing"] == 0
    assert res["counts"]["phantom"] == 0


def test_reconcile_detects_missing_cli(small_surface):
    # covers task_* but not trek_create → trek_create is 書き漏れ
    text = "- t `cli:beacon task *` `api:* /api/x` `skill:/beacon-map`\n"
    res = CMD.reconcile(text)
    assert "trek_create" in res["missing"]["cli"]
    assert res["counts"]["missing"] == 1


def test_reconcile_detects_phantom_cli_family(small_surface):
    text = ("- t `cli:beacon task *` `cli:beacon trek create` `cli:beacon ghost *` "
            "`api:* /api/x` `skill:/beacon-map`\n")
    res = CMD.reconcile(text)
    assert any("ghost" in p for p in res["phantom"]["cli_family"])


def test_reconcile_api_family_prefix_covers_subpaths(small_surface):
    # `api:* /api/x` must cover both GET /api/x and POST /api/x/y
    text = ("- t `cli:beacon task *` `cli:beacon trek create` "
            "`api:* /api/x` `skill:/beacon-map`\n")
    res = CMD.reconcile(text)
    assert res["missing"]["api"] == []


def test_reconcile_detects_phantom_api_and_skill(small_surface):
    text = ("- t `cli:beacon task *` `cli:beacon trek create` `api:* /api/x` "
            "`api:GET /api/nope` `skill:/beacon-map` `skill:/beacon-ghost`\n")
    res = CMD.reconcile(text)
    assert "GET /api/nope" in res["phantom"]["api_exact"]
    assert "/beacon-ghost" in res["phantom"]["skill"]


def test_reconcile_file_wedge_existence(small_surface, tmp_path):
    # existing repo file → ok; nonexistent → phantom
    text = ("- t `cli:beacon task *` `cli:beacon trek create` `api:* /api/x` "
            "`skill:/beacon-map` `file:scripts/check-map-drift.py` "
            "`file:scripts/does-not-exist-xyz.py`\n")
    res = CMD.reconcile(text)
    assert "scripts/does-not-exist-xyz.py" in res["phantom"]["file"]
    assert "scripts/check-map-drift.py" not in res["phantom"]["file"]


# ----------------------------------------------------------- (B) box + triggers

import commands  # noqa: E402


def test_box_content_has_header_and_objective():
    body = commands._application_map_box_content("家計簿を自動化する")
    assert "アプリケーション全貌マップ" in body
    assert "家計簿を自動化する" in body
    assert "/beacon-map" in body  # fill instruction


def test_box_content_handles_empty_objective():
    body = commands._application_map_box_content("")
    assert "未設定" in body


def test_seed_box_writes_once_and_is_idempotent(tmp_path, monkeypatch):
    docs = tmp_path / "documents"
    monkeypatch.setattr(commands, "_get_docs_dir", lambda: str(docs))
    commands._seed_application_map_box("obj-A")
    fpath = docs / "application-map.md"
    assert fpath.exists()
    first = fpath.read_text(encoding="utf-8")
    assert "scope: core" in first
    assert "obj-A" in first
    # idempotent: a second call with a different objective must NOT overwrite
    commands._seed_application_map_box("obj-B")
    assert fpath.read_text(encoding="utf-8") == first


class _FakeStore:
    def __init__(self, doc):
        self._doc = doc

    def get_document(self, doc_id):
        return self._doc if doc_id == "application-map" else None


def test_map_drift_fires_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(commands, "get_store",
                        lambda: _FakeStore({"updated_at": "2020-01-01T00:00:00"}))
    commands._auto_fire_map_drift_trigger()
    p = tmp_path / "map-drift.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["kind"] == "map-drift"
    assert data["commit_count"] >= commands._MAP_DRIFT_COMMIT_THRESHOLD


def test_map_drift_does_not_fire_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tmp_path))
    # updated_at = now → 0 commits since → below threshold
    import datetime
    now = datetime.datetime.now().isoformat()
    monkeypatch.setattr(commands, "get_store",
                        lambda: _FakeStore({"updated_at": now}))
    commands._auto_fire_map_drift_trigger()
    assert not (tmp_path / "map-drift.json").exists()


def test_map_drift_does_not_fire_when_map_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(commands, "get_store", lambda: _FakeStore(None))
    commands._auto_fire_map_drift_trigger()
    assert not (tmp_path / "map-drift.json").exists()


def test_map_reconcile_fires_when_map_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(commands, "get_store",
                        lambda: _FakeStore({"updated_at": "2020-01-01T00:00:00"}))
    commands._fire_map_reconcile_trigger()
    p = tmp_path / "map-reconcile.json"
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8"))["kind"] == "map-reconcile"


def test_map_reconcile_skips_when_map_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(commands, "get_store", lambda: _FakeStore(None))
    commands._fire_map_reconcile_trigger()
    assert not (tmp_path / "map-reconcile.json").exists()
