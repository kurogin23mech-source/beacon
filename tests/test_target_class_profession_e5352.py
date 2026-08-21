"""ms-146 e-5352: a target-class file shared between projects must work in a
project whose profession differs.

The failure this prevents is a SILENT half-working state. A class whose declared
profession does not match the project still registers, and
`beacon target instances --class <kind>` still finds it — so it looks fine. But
the profession-scoped reads (`beacon status`, the shared Target projection, the
切り上げシグナル) filter by owning profession, so the class is invisible in exactly
the places the owner actually looks. Sharing class files across projects is the
POINT of declaring classes as data, so an arriving mismatch is the normal case,
not the exception.
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
# The mismatch is announced, not silently accepted.
# ---------------------------------------------------------------------------

def test_add_warns_when_the_file_profession_does_not_match(president,
                                                           monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TC_STDIN", "1")
    _feed_stdin(monkeypatch, SHARED_FILE)
    cmd_target.cmd_target_class_add()
    err = capsys.readouterr().err
    assert "'dev'" in err and "'president'" in err
    assert "beacon status" in err, "must say WHERE it will be missing"
    assert "target-class update --kind undertaking --profession president" in err, \
        "must name the one-line fix"


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

def test_update_can_correct_the_owning_profession(tmp_path, monkeypatch,
                                                  capsys):
    """Changing the OWNER is not a field edit, so the additive-only rule does not
    apply: it orphans nothing — every record keeps its collection and its shape."""
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
    assert occupation.project_targets(data_before) == [], "invisible before"

    monkeypatch.setenv("BEACON_TC_KIND", "undertaking")
    monkeypatch.setenv("BEACON_TC_PROFESSION", "president")
    cmd_target.cmd_target_class_update()

    assert "president に変更" in capsys.readouterr().out
    data_after = json.loads((tmp_path / ".beacon" / "project.json")
                            .read_text(encoding="utf-8"))
    assert td.get_descriptor(data_after, "undertaking")["profession"] \
        == "president"
    # the instance survives untouched AND is now visible to the shared frame
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
