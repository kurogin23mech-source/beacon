import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import skill_converter as sc


FIXTURES = Path(__file__).parent / "fixtures" / "skills"


def _fixture_source(name: str, tmp_path: Path) -> Path:
    import shutil
    destination = tmp_path / "sources" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURES / f"{name}.in", destination)
    return destination


def _assert_trees_equal(actual: Path, expected: Path) -> None:
    actual_files = sorted(
        path.relative_to(actual) for path in actual.rglob("*") if path.is_file()
    )
    expected_files = sorted(
        path.relative_to(expected) for path in expected.rglob("*") if path.is_file()
    )
    assert actual_files == expected_files
    for relative in expected_files:
        assert (actual / relative).read_bytes() == (expected / relative).read_bytes()


@pytest.mark.parametrize("name", ["beacon-init", "beacon-dm-send"])
def test_golden_renderers_match_bytewise(name, tmp_path):
    skill = sc.read_canonical_skill(_fixture_source(name, tmp_path))
    assert sc.render_claude(skill) == (
        FIXTURES / f"{name}.claude.expected.md"
    ).read_bytes()

    rendered = tmp_path / name
    sc.render_codex(skill, rendered)
    _assert_trees_equal(rendered, FIXTURES / f"{name}.codex.expected")
    assert not (rendered / "clients").exists()


def test_reader_rejects_extra_frontmatter_and_name_mismatch(tmp_path):
    root = tmp_path / "wrong-folder"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: other\ndescription: x\npriority: high\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(sc.SkillConversionError, match="unsupported field"):
        sc.read_canonical_skill(root)


def test_reader_rejects_unknown_client_metadata(tmp_path):
    source = FIXTURES / "beacon-init.in"
    root = tmp_path / "beacon-init"
    import shutil
    shutil.copytree(source, root)
    (root / "clients" / "claude.yaml").write_text("model_hint: fast\n", encoding="utf-8")
    with pytest.raises(sc.SkillConversionError, match="unsupported field"):
        sc.read_canonical_skill(root)


def test_install_both_is_idempotent_and_records_hashes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("BEACON_SKILL_INSTALL_STATE", str(tmp_path / "state.json"))
    source = _fixture_source("beacon-dm-send", tmp_path)

    first = sc.install_skill(source, targets=("claude", "codex"), home=home)
    assert [row["action"] for row in first] == ["installed", "installed"]
    assert first[0]["warnings"]
    second = sc.install_skill(source, targets=("claude", "codex"), home=home)
    assert [row["action"] for row in second] == ["unchanged", "unchanged"]

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert sorted(state["installs"]) == [
        "claude:beacon-dm-send", "codex:beacon-dm-send"
    ]
    assert all(row["source_hash"] for row in state["installs"].values())
    assert (home / ".claude" / "skills" / "beacon-dm-send.md").is_file()
    assert (home / ".agents" / "skills" / "beacon-dm-send" / "SKILL.md").is_file()


def test_external_edit_requires_force_or_adopt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("BEACON_SKILL_INSTALL_STATE", str(tmp_path / "state.json"))
    source = _fixture_source("beacon-init", tmp_path)
    sc.install_skill(source, targets=("claude",), home=home)
    destination = home / ".claude" / "skills" / "beacon-init.md"
    destination.write_text("user edit\n", encoding="utf-8")

    with pytest.raises(sc.SkillConversionError, match="modified outside Beacon"):
        sc.install_skill(source, targets=("claude",), home=home)
    adopted = sc.install_skill(source, targets=("claude",), home=home, adopt=True)
    assert adopted[0]["action"] == "adopted"
    assert destination.read_text(encoding="utf-8") == "user edit\n"

    destination.write_text("second edit\n", encoding="utf-8")
    forced = sc.install_skill(source, targets=("claude",), home=home, force=True)
    assert forced[0]["action"] == "installed"
    assert destination.read_bytes() == sc.render_claude(sc.read_canonical_skill(source))


def test_dry_run_does_not_write_destination_or_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = tmp_path / "state.json"
    monkeypatch.setenv("BEACON_SKILL_INSTALL_STATE", str(state))
    result = sc.install_skill(
        _fixture_source("beacon-init", tmp_path),
        targets=("claude", "codex"),
        home=home,
        dry_run=True,
    )
    assert [row["action"] for row in result] == ["installed", "installed"]
    assert not home.exists()
    assert not state.exists()


def test_both_target_failure_rolls_back_destinations(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = tmp_path / "state.json"
    monkeypatch.setenv("BEACON_SKILL_INSTALL_STATE", str(state))
    original_replace = os.replace
    destination_suffix = str(Path(".agents") / "skills" / "beacon-init")

    def fail_codex_replace(source, destination):
        if str(destination).endswith(destination_suffix):
            raise OSError("simulated second-target failure")
        return original_replace(source, destination)

    monkeypatch.setattr(sc.os, "replace", fail_codex_replace)
    with pytest.raises(OSError, match="second-target"):
        sc.install_skill(
            _fixture_source("beacon-init", tmp_path),
            targets=("claude", "codex"),
            home=home,
        )
    assert not (home / ".claude" / "skills" / "beacon-init.md").exists()
    assert not (home / ".agents" / "skills" / "beacon-init").exists()
    assert not state.exists()


def test_force_and_adopt_are_mutually_exclusive(tmp_path):
    with pytest.raises(sc.SkillConversionError, match="mutually exclusive"):
        sc.install_skill(
            _fixture_source("beacon-init", tmp_path),
            targets=("claude",),
            home=tmp_path,
            force=True,
            adopt=True,
        )


def test_prune_removes_only_managed_unchanged_install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = tmp_path / "state.json"
    monkeypatch.setenv("BEACON_SKILL_INSTALL_STATE", str(state))
    source = _fixture_source("beacon-init", tmp_path)
    sc.install_skill(source, targets=("claude", "codex"), home=home)

    preview = sc.prune_skill(
        "beacon-init", targets=("claude", "codex"), home=home, dry_run=True
    )
    assert [row["action"] for row in preview] == ["pruned", "pruned"]
    assert (home / ".claude" / "skills" / "beacon-init.md").exists()

    result = sc.prune_skill(
        "beacon-init", targets=("claude", "codex"), home=home
    )
    assert [row["action"] for row in result] == ["pruned", "pruned"]
    assert not (home / ".claude" / "skills" / "beacon-init.md").exists()
    assert not (home / ".agents" / "skills" / "beacon-init").exists()
    assert json.loads(state.read_text(encoding="utf-8"))["installs"] == {}


def test_prune_refuses_modified_destination_without_force(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("BEACON_SKILL_INSTALL_STATE", str(tmp_path / "state.json"))
    source = _fixture_source("beacon-init", tmp_path)
    sc.install_skill(source, targets=("claude",), home=home)
    destination = home / ".claude" / "skills" / "beacon-init.md"
    destination.write_text("user edit\n", encoding="utf-8")

    with pytest.raises(sc.SkillConversionError, match="use --force to prune"):
        sc.prune_skill("beacon-init", targets=("claude",), home=home)
    sc.prune_skill("beacon-init", targets=("claude",), home=home, force=True)
    assert not destination.exists()
