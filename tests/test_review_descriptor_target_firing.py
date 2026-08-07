"""Data-defined (descriptor) targets fire review at completion (ms-119 / e-4087).

A milestone going done fires the 目的達成 + 思想 review-due trigger; a descriptor
target (ms-122 記述子で定義した職種の対象) reaching a terminal phase or being
closed is the SAME completion claim and must fire the same review — otherwise the
"review is a first-class, kind-neutral capability" promise leaks for exactly the
data-defined targets ms-122/124 made first-class.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import cmd_target  # noqa: E402  (ms-127 e-4852: target family moved here)
import review_spine  # noqa: E402


CONTRACT = {
    "kind": "contract", "label": "契約", "profession": "backoffice",
    "type": "single-shot", "id_prefix": "ctr-", "collection": "contracts",
    "decomposition": {"id_field": "id", "arms": ["clauses"]},
    "fields": [{"key": "counterparty", "label": "相手方"}],
    "phases": [
        {"key": "drafting", "label": "起草"},
        {"key": "review", "label": "レビュー"},
        {"key": "signed", "label": "締結", "terminal": True},
    ],
}


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "milestones": [], "operations": [],
        "target_classes": [CONTRACT],
    }, ensure_ascii=False))
    monkeypatch.setenv("BEACON_PROJECT_FILE",
                       str(tmp_path / ".beacon" / "project.json"))
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    # descriptor targets have no doc store spec by default → philosophy off.
    monkeypatch.setattr(cmd_target, "_spec_exists_for_descriptor_target",
                        lambda tid: False)
    return tmp_path


def _clear(monkeypatch):
    for k in ("BEACON_TARGET_CLASS", "BEACON_TARGET_ID", "BEACON_LABEL",
              "BEACON_TO_PHASE", "BEACON_REASON", "BEACON_FIELD"):
        monkeypatch.delenv(k, raising=False)


def _triggers(proj):
    d = proj / ".beacon" / "triggers"
    return {p.name: json.loads(p.read_text()) for p in d.glob("*.json")} if d.exists() else {}


def _create(proj, monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_CLASS", "contract")
    monkeypatch.setenv("BEACON_LABEL", "ACME 契約")
    commands.cmd_target_create()
    out = capsys.readouterr().out
    # id is printed like "作成: [ct-1] ..."
    tid = out.split("[", 1)[1].split("]", 1)[0]
    return tid


# --- pure binding -----------------------------------------------------------

def test_completion_binding_is_kind_neutral():
    b = review_spine.review_bindings_for_completion(has_spec=False)
    assert [x["review"] for x in b] == [review_spine.REVIEW_ATTAINMENT]
    b2 = review_spine.review_bindings_for_completion(has_spec=True)
    assert review_spine.REVIEW_PHILOSOPHY in [x["review"] for x in b2]


# --- CLI wiring -------------------------------------------------------------

def test_close_fires_review_due(proj, monkeypatch, capsys):
    tid = _create(proj, monkeypatch, capsys)
    assert not _triggers(proj)  # create alone fires nothing
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_CLASS", "contract")
    monkeypatch.setenv("BEACON_TARGET_ID", tid)
    monkeypatch.setenv("BEACON_REASON", "closed")
    commands.cmd_target_close()
    trigs = _triggers(proj)
    assert f"review-due-{tid}.json" in trigs
    t = trigs[f"review-due-{tid}.json"]
    assert review_spine.REVIEW_ATTAINMENT in t["bindings"]
    assert t["new_state"] == "done"


def test_advance_to_terminal_phase_fires_review_due(proj, monkeypatch, capsys):
    tid = _create(proj, monkeypatch, capsys)
    # advance drafting -> review: NOT a completion, no trigger.
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_CLASS", "contract")
    monkeypatch.setenv("BEACON_TARGET_ID", tid)
    monkeypatch.setenv("BEACON_TO_PHASE", "review")
    commands.cmd_target_advance()
    assert not _triggers(proj)
    # advance review -> signed (terminal): completion claim, fires.
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_CLASS", "contract")
    monkeypatch.setenv("BEACON_TARGET_ID", tid)
    monkeypatch.setenv("BEACON_TO_PHASE", "signed")
    commands.cmd_target_advance()
    trigs = _triggers(proj)
    assert f"review-due-{tid}.json" in trigs
    assert review_spine.REVIEW_ATTAINMENT in trigs[f"review-due-{tid}.json"]["bindings"]


def test_philosophy_binds_when_descriptor_target_has_spec(proj, monkeypatch, capsys):
    monkeypatch.setattr(cmd_target, "_spec_exists_for_descriptor_target",
                        lambda tid: True)
    tid = _create(proj, monkeypatch, capsys)
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_CLASS", "contract")
    monkeypatch.setenv("BEACON_TARGET_ID", tid)
    commands.cmd_target_close()
    t = _triggers(proj)[f"review-due-{tid}.json"]
    assert review_spine.REVIEW_PHILOSOPHY in t["bindings"]
