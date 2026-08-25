"""ms-153 e-5549 (SPEC 方針3): ``beacon status --json`` — the payload
/beacon-session-start reads — carries the assembled ROOT target under ``root``,
so session-start is a root-target ASSEMBLER (read = from root).

The root composes the adopted target-classes' state projection + deliverable
projection (``projection``) with the root-owned narrative (``narrative``),
keeping the 方針2 2-split structural. This test pins the WIRING (status emits
root); the assembly logic itself is unit-tested in ``test_root_target``.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_milestone  # noqa: E402


def _write_project(tmp_path, data):
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def dev_proj(tmp_path, monkeypatch):
    _write_project(tmp_path, {
        "name": "Proj",
        "objective": "大目的テキスト",
        "summary": "経緯テキスト",
        "profession": "dev",
        "milestones": [
            {"id": "ms-1", "status": "done", "label": "A", "entries": []},
            {"id": "ms-2", "status": "in_progress", "label": "B", "entries": []},
        ],
    })
    monkeypatch.chdir(tmp_path)
    for k in ("BEACON_JSON", "BEACON_TARGET_CLASS", "BEACON_MS_IDS", "BEACON_ALL"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _status_json(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_milestone.cmd_milestone_list()
    return json.loads(capsys.readouterr().out)


def test_status_json_carries_assembled_root(dev_proj, monkeypatch, capsys):
    payload = _status_json(monkeypatch, capsys)
    assert "root" in payload
    root = payload["root"]
    assert root["id"] == "root"
    assert root["kind"] == "root"
    assert root["label"] == "Proj"
    # 2-split present (方針2): synthesized projection + owned narrative
    assert root["projection"]["counts"] == {"total": 2, "done": 1, "open": 1}
    assert root["narrative"]["objective"] == "大目的テキスト"
    assert root["narrative"]["summary"] == "経緯テキスト"
    # arm mapping (方針: phase-less / evidence-less)
    assert root["arms"]["kind"] == "root"
    assert root["arms"]["phase_ball"] is None
    assert root["arms"]["evidence_arms"] == []


def test_root_counts_agree_with_targets_projection(dev_proj, monkeypatch, capsys):
    """root.projection.targets is the same per-class projection as targets[],
    so the assembler does not diverge from the legacy sibling field."""
    payload = _status_json(monkeypatch, capsys)
    assert payload["root"]["projection"]["targets"] == payload["targets"]
    assert payload["root"]["work_items_total"] == len(payload["targets"])


def test_status_json_read_only(dev_proj, monkeypatch, capsys):
    """Assembling the root is a pure read — status must not rewrite project.json."""
    before = (dev_proj / ".beacon" / "project.json").read_text(encoding="utf-8")
    _status_json(monkeypatch, capsys)
    after = (dev_proj / ".beacon" / "project.json").read_text(encoding="utf-8")
    assert after == before
