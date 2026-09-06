"""ms-150 (全 target 一律 adoptable) — the `beacon target-class adopt` verb.

Adopting a built-in / catalog target-class into a project's adopted set is the
user-facing path that makes the axis inversion usable: a sales project can adopt
``milestone`` and then OWN it (enumerate + pass the create gate) via the same M:N
route ``release`` proved. These pin the four load-bearing behaviours:

  * adopt materialises the profession defaults first (a legacy project must not
    LOSE its built-ins when it adopts one more — the Cairn case);
  * adopt is idempotent (re-adopting is a no-op, not a duplicate);
  * a kind that is neither a catalog built-in nor a declared descriptor is refused;
  * ``--profession-defaults`` backfills a stale seed (a pre-ms-150 dev project whose
    copied set is only ``["release"]``) up to the full built-in default set.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_target  # noqa: E402
import occupation  # noqa: E402
import target_descriptor as td  # noqa: E402


@pytest.fixture
def project(monkeypatch):
    """A mutable in-memory project the adopt verb reads/writes, with the CLI's
    load/save monkeypatched onto it so the test is hermetic (no store / cwd)."""
    holder = {"data": None}

    def _load():
        return holder["data"]

    def _save(data, op=None):
        holder["data"] = data

    monkeypatch.setattr(cmd_target, "load_project", _load)
    monkeypatch.setattr(cmd_target, "save_project", _save)
    return holder


def _run_adopt(monkeypatch, *, kind="", defaults=False, json_mode=False):
    monkeypatch.setenv("BEACON_TC_KIND", kind)
    monkeypatch.setenv("BEACON_TC_PROFESSION_DEFAULTS", "1" if defaults else "0")
    monkeypatch.setenv("BEACON_JSON", "1" if json_mode else "0")
    cmd_target.cmd_target_class_adopt()


def test_legacy_sales_adopting_milestone_keeps_its_own_builtins(project, monkeypatch):
    # The Cairn case: a legacy sales project (no adopted key) adopts milestone. The
    # verb must SEED the sales defaults first, then append milestone — otherwise the
    # project would end up owning ONLY milestone (its opportunities/accounts lost).
    data = occupation.build_new_project("Cairn", "o", "sales")
    data.pop("adopted_target_classes", None)  # legacy: no copied set
    project["data"] = data

    _run_adopt(monkeypatch, kind="milestone")

    assert project["data"][td.ADOPTED_TARGET_CLASSES_KEY] == [
        "opportunity", "account", "acquisition", "milestone"]
    # and it now OWNS milestone + passes the create gate
    assert "milestone" in occupation.owned_target_classes(project["data"])
    occupation.assert_target_class_owned(project["data"], "milestone")  # no raise


def test_adopt_is_idempotent(project, monkeypatch):
    data = occupation.build_new_project("D", "o", "dev")  # already owns milestone
    project["data"] = data
    before = list(project["data"][td.ADOPTED_TARGET_CLASSES_KEY])
    _run_adopt(monkeypatch, kind="milestone")  # already adopted
    assert project["data"][td.ADOPTED_TARGET_CLASSES_KEY] == before  # unchanged


def test_adopt_rejects_unknown_kind(project, monkeypatch):
    project["data"] = occupation.build_new_project("D", "o", "dev")
    with pytest.raises(SystemExit):
        _run_adopt(monkeypatch, kind="bogus")


def test_adopt_requires_a_kind_without_defaults(project, monkeypatch):
    project["data"] = occupation.build_new_project("D", "o", "dev")
    with pytest.raises(SystemExit):
        _run_adopt(monkeypatch, kind="")  # neither a kind nor --profession-defaults


def test_profession_defaults_backfills_a_stale_seed(project, monkeypatch):
    # A pre-ms-150 dev project whose copied set is only ["release"] (the old seed).
    # Under the strict single-read ownership, it owns ONLY release until backfilled;
    # --profession-defaults unions the full dev default set in.
    data = occupation.build_new_project("D", "o", "dev")
    data["adopted_target_classes"] = ["release"]
    project["data"] = data
    assert set(occupation.owned_target_classes(data)) == {"release"}  # the regression

    _run_adopt(monkeypatch, defaults=True)

    assert project["data"][td.ADOPTED_TARGET_CLASSES_KEY] == [
        "release", "milestone", "operation"]
    assert set(occupation.owned_target_classes(project["data"])) == {
        "release", "milestone", "operation"}


def test_dev_can_adopt_a_sales_class_symmetry(project, monkeypatch):
    # M:N is symmetric: a dev project can adopt opportunity too.
    project["data"] = occupation.build_new_project("D", "o", "dev")
    _run_adopt(monkeypatch, kind="opportunity")
    assert "opportunity" in occupation.owned_target_classes(project["data"])
    occupation.assert_target_class_owned(project["data"], "opportunity")  # no raise


# --- ms-150 AX / maintainability review fixes -------------------------------

def test_kind_and_profession_defaults_are_mutually_exclusive(project, monkeypatch):
    # AX1: passing both a <kind> and --profession-defaults used to silently ignore
    # the kind (backfill won) — now it is a hard error, not a silent no-op.
    project["data"] = occupation.build_new_project("D", "o", "sales")
    with pytest.raises(SystemExit):
        _run_adopt(monkeypatch, kind="milestone", defaults=True)


def test_json_shape_is_consistent_across_modes(project, monkeypatch, capsys):
    # AX4: every JSON output is {"added", "adopted"} — the already-adopted case no
    # longer returns a different {"kind","already_adopted"} shape that broke automation.
    import json as _json
    project["data"] = occupation.build_new_project("D", "o", "dev")  # already owns milestone
    _run_adopt(monkeypatch, kind="milestone", json_mode=True)  # already adopted
    out = _json.loads(capsys.readouterr().out.strip())
    assert set(out.keys()) == {"added", "adopted"} and out["added"] == []
    # a fresh adoption uses the same shape
    _run_adopt(monkeypatch, kind="opportunity", json_mode=True)
    out2 = _json.loads(capsys.readouterr().out.strip())
    assert set(out2.keys()) == {"added", "adopted"} and out2["added"] == ["opportunity"]


def test_adopting_a_data_defined_class_is_a_noop_not_a_ghost(project, monkeypatch):
    # M4: a data-defined class is already owned via target_classes; adopting it must
    # NOT write a ghost entry into adopted_target_classes (which resolve_adopted skips).
    declared = {"kind": "contract", "label": "契約", "profession": "legal",
                "type": "single-shot", "id_prefix": "ct-", "collection": "contracts",
                "phases": [{"key": "draft"}]}
    data = {"name": "p", "profession": "legal", "milestones": [],
            "adopted_target_classes": [], "target_classes": [declared]}
    project["data"] = data
    _run_adopt(monkeypatch, kind="contract")  # no-op, no raise
    # contract is still owned (via target_classes) but NOT written to the adopted set
    assert "contract" in occupation.owned_target_classes(project["data"])
    assert "contract" not in project["data"]["adopted_target_classes"]
