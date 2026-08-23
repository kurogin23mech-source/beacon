"""ms-146 e-5352 → ms-147 e-5375: a target-class file shared between projects
must work in a project whose profession differs.

ORIGINAL premise (e-5352): a class whose declared profession did not match the
project registered but was INVISIBLE to the profession-scoped reads (`beacon
status`, the shared Target projection, the 切り上げシグナル), so e-5352 warned on
the mismatch and offered `--profession` to correct the owner and restore
visibility.

e-5375 removed profession as a wiring authority: enumeration reads the project's
adopted/effective set, NOT each descriptor's stamp (`occupation._descriptors_
owned_by`). So a mismatched stamp is now HARMLESS — the class surfaces regardless
of it. The stamp is pure provenance (where the class was authored). These tests
are updated to that reality: no invisibility, no mismatch warning; `--profession`
survives as a provenance relabel that no longer changes visibility.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_target  # noqa: E402
import occupation  # noqa: E402
import target_descriptor as td  # noqa: E402


SHARED_FILE = {
    "kind": "undertaking",
    "label": "やること",
    "profession": "dev",          # the file was authored in a dev project
    "type": "single-shot",
    "id_prefix": "ut-",
    "collection": "undertakings",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [],
    "phases": [{"key": "started", "label": "着手"},
               {"key": "enough", "label": "十分やった", "terminal": True}],
}

_ENV = ("BEACON_TC_KIND", "BEACON_TC_LABEL", "BEACON_TC_TYPE",
        "BEACON_TC_ID_PREFIX", "BEACON_TC_COLLECTION", "BEACON_TC_PROFESSION",
        "BEACON_TC_STDIN", "BEACON_TC_FIELDS", "BEACON_TC_REQUIRED_FIELDS",
        "BEACON_TC_WI_FIELDS", "BEACON_TC_REQUIRED_WI_FIELDS",
        "BEACON_TC_EV_FIELDS", "BEACON_TC_REQUIRED_EV_FIELDS",
        "BEACON_TC_PHASE_FIELDS", "BEACON_TC_REQUIRED_PHASE_FIELDS",
        "BEACON_TC_REMOVE_FIELDS", "BEACON_TC_RENAME_FIELDS",
        "BEACON_TC_BUDGET_TRACKING", "BEACON_TC_STALL_SIGNAL",
        "BEACON_TC_PHASES", "BEACON_TC_TERMINAL_PHASES")


def _write(tmp_path, *, profession, classes=(), undertakings=()):
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": profession, "milestones": [],
        "target_classes": list(classes),
        "undertakings": list(undertakings),
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def president(tmp_path, monkeypatch):
    _write(tmp_path, profession="president")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _classes(proj_path):
    return json.loads((proj_path / ".beacon" / "project.json")
                      .read_text(encoding="utf-8"))["target_classes"]


def _feed_stdin(monkeypatch, payload):
    import io as _io
    monkeypatch.setattr(sys, "stdin",
                        _io.StringIO(json.dumps(payload, ensure_ascii=False)))


# ---------------------------------------------------------------------------
# e-5375: the mismatch is no longer warned about — it is harmless provenance.
# ---------------------------------------------------------------------------

def test_add_does_not_warn_on_a_provenance_mismatch(president,
                                                    monkeypatch, capsys):
    # e-5375: a dev-authored file added to a president project no longer triggers
    # the "invisible in beacon status" warning, because the stamp does not gate
    # visibility any more. The class registers and surfaces on its own.
    monkeypatch.setenv("BEACON_TC_STDIN", "1")
    _feed_stdin(monkeypatch, SHARED_FILE)
    cmd_target.cmd_target_class_add()
    err = capsys.readouterr().err
    assert "⚠" not in err, "a provenance mismatch is harmless — no warning"


def test_a_mismatched_class_still_surfaces_in_the_shared_frame(president,
                                                               monkeypatch):
    # The positive proof of e-5375: the dev-stamped class added to a president
    # project is enumerated by the shared Target projection despite the stamp.
    monkeypatch.setenv("BEACON_TC_STDIN", "1")
    _feed_stdin(monkeypatch, SHARED_FILE)
    cmd_target.cmd_target_class_add()
    data = json.loads((president / ".beacon" / "project.json")
                      .read_text(encoding="utf-8"))
    data["undertakings"] = [{"id": "ut-1", "label": "x", "kind": "undertaking",
                             "phase": "started", "status": "todo",
                             "work_items": [], "evidence": [],
                             "phase_history": []}]
    assert [t["id"] for t in occupation.project_targets(data)] == ["ut-1"]


def test_the_class_still_registers_despite_the_mismatch(president, monkeypatch):
    """A warning, not an error: a mismatch is the normal arrival state for a
    shared file, so refusing it would block the very use case."""
    monkeypatch.setenv("BEACON_TC_STDIN", "1")
    _feed_stdin(monkeypatch, SHARED_FILE)
    cmd_target.cmd_target_class_add()
    assert [c["kind"] for c in _classes(president)] == ["undertaking"]


def test_no_warning_when_the_professions_agree(president, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TC_STDIN", "1")
    _feed_stdin(monkeypatch, dict(SHARED_FILE, profession="president"))
    cmd_target.cmd_target_class_add()
    assert "⚠" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Adopting a shared file without hand-editing it.
# ---------------------------------------------------------------------------

def test_stdin_add_accepts_a_profession_override(president, monkeypatch,
                                                 capsys):
    monkeypatch.setenv("BEACON_TC_STDIN", "1")
    monkeypatch.setenv("BEACON_TC_PROFESSION", "president")
    _feed_stdin(monkeypatch, SHARED_FILE)
    cmd_target.cmd_target_class_add()
    assert _classes(president)[0]["profession"] == "president"
    assert "⚠" not in capsys.readouterr().err


def test_the_override_does_not_disturb_the_rest_of_the_file(president,
                                                            monkeypatch):
    monkeypatch.setenv("BEACON_TC_STDIN", "1")
    monkeypatch.setenv("BEACON_TC_PROFESSION", "president")
    _feed_stdin(monkeypatch, SHARED_FILE)
    cmd_target.cmd_target_class_add()
    got = _classes(president)[0]
    assert got["kind"] == "undertaking"
    assert got["id_prefix"] == "ut-"
    assert [p["key"] for p in got["phases"]] == ["started", "enough"]


# ---------------------------------------------------------------------------
# Repairing a class that is already registered wrong.
# ---------------------------------------------------------------------------

def test_update_can_relabel_the_provenance_profession(tmp_path, monkeypatch,
                                                      capsys):
    """e-5375: editing the stamp is a pure PROVENANCE relabel. It orphans nothing
    (every record keeps its collection / id / shape) AND — unlike pre-e5375 — the
    class is already visible before the edit, because the stamp never gated it."""
    rec = {"id": "ut-1", "label": "テスト", "kind": "undertaking",
           "phase": "started", "status": "todo", "work_items": [],
           "evidence": [], "phase_history": []}
    _write(tmp_path, profession="president", classes=[SHARED_FILE],
           undertakings=[rec])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)

    data_before = json.loads((tmp_path / ".beacon" / "project.json")
                             .read_text(encoding="utf-8"))
    # e-5375: the dev-stamped class is ALREADY visible — the stamp is provenance.
    assert [t["id"] for t in occupation.project_targets(data_before)] == ["ut-1"]

    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_PROFESSION", "president")
    cmd_target.cmd_target_class_update()

    assert "president に更新" in capsys.readouterr().out
    data_after = json.loads((tmp_path / ".beacon" / "project.json")
                            .read_text(encoding="utf-8"))
    assert td.get_descriptor(data_after, "undertaking")["profession"] \
        == "president"
    # the instance survives untouched AND stays visible to the shared frame
    assert data_after["undertakings"] == [rec]
    assert [t["id"] for t in occupation.project_targets(data_after)] == ["ut-1"]


def test_profession_alone_is_a_valid_update(president, monkeypatch):
    """No field flags is normally an error ("nothing to add"); a profession fix
    must not trip that guard."""
    _write(president, profession="president", classes=[SHARED_FILE])
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_PROFESSION", "president")
    cmd_target.cmd_target_class_update()   # must not SystemExit
    assert _classes(president)[0]["profession"] == "president"


def test_an_update_with_nothing_at_all_is_still_refused(president, monkeypatch):
    _write(president, profession="president", classes=[SHARED_FILE])
    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    with pytest.raises(SystemExit) as e:
        cmd_target.cmd_target_class_update()
    assert e.value.code != 0
