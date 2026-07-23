"""Tests for the doc/skill drift detectors (ms-10 e-722).

Three independent guards live under ``scripts/`` and one new check lives
inside ``cmd_doctor``. They share the same purpose: catch the moment
when a feature commit forgets to update the matching doc / Skill surface.

Each test pair covers a clean (no-drift) state and a drifted state so
the script's binary signal stays trustworthy.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPTS = REPO / "scripts"


# ---------------------------------------------------------------------------
# Module loaders — the drift scripts have hyphenated filenames, so we have to
# load them via importlib rather than `from scripts.check_skill_drift import`.
# ---------------------------------------------------------------------------

def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def skill_drift():
    return _load(SCRIPTS / "check-skill-drift.py")


@pytest.fixture(scope="module")
def cli_drift():
    return _load(SCRIPTS / "check-cli-help-drift.py")


@pytest.fixture(scope="module")
def install_md():
    return _load(SCRIPTS / "check-install-md.py")


# ===========================================================================
# Skill drift  — scripts/check-skill-drift.py
# ===========================================================================

def _make_skill_repo(root: Path, files: dict[str, str]) -> Path:
    """Create a fake skills/ directory under ``root`` with the given files."""
    src = root / "skills"
    src.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (src / name).write_text(body, encoding="utf-8")
    return src


def _make_installed(root: Path, skill_files: dict[str, str]) -> Path:
    """Create a fake ~/.claude/skills/ tree.

    ``skill_files`` is ``{"<skill-name>/<filename>": body}``.
    """
    dst = root / ".claude" / "skills"
    dst.mkdir(parents=True, exist_ok=True)
    for rel, body in skill_files.items():
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return dst


def test_skill_drift_clean_no_findings(tmp_path, skill_drift):
    """When repo and install match exactly, exit 0 and ok=True."""
    src = _make_skill_repo(tmp_path, {"foo.md": "hello"})
    dst = _make_installed(tmp_path, {"foo/SKILL.md": "hello"})
    report = skill_drift.collect_drift(skills_src=src, claude_skills=dst)
    assert report["ok"] is True
    assert report["drift"] == []


def test_skill_drift_content_mismatch_flagged(tmp_path, skill_drift):
    src = _make_skill_repo(tmp_path, {"foo.md": "new body"})
    dst = _make_installed(tmp_path, {"foo/SKILL.md": "old body"})
    report = skill_drift.collect_drift(skills_src=src, claude_skills=dst)
    assert report["ok"] is False
    assert any(d["name"] == "foo" and d["reason"] == "content_diff" for d in report["drift"])


def test_skill_drift_not_installed_flagged(tmp_path, skill_drift):
    src = _make_skill_repo(tmp_path, {"foo.md": "hello"})
    dst = _make_installed(tmp_path, {})  # empty install
    report = skill_drift.collect_drift(skills_src=src, claude_skills=dst)
    assert report["ok"] is False
    assert any(d["name"] == "foo" and d["reason"] == "not_installed" for d in report["drift"])


def test_skill_drift_no_install_dir_at_all(tmp_path, skill_drift):
    src = _make_skill_repo(tmp_path, {"foo.md": "hello"})
    dst = tmp_path / "doesnt-exist"
    report = skill_drift.collect_drift(skills_src=src, claude_skills=dst)
    assert report["no_install_dir"] is True
    assert report["ok"] is False
    assert any(d["reason"] == "not_installed" for d in report["drift"])


def test_skill_drift_companion_resolution(tmp_path, skill_drift):
    """``_foo-bar.md`` should map to ``foo/bar.md`` in install."""
    src = _make_skill_repo(tmp_path, {
        "foo.md": "skill body",
        "_foo-methodology.md": "companion body",
    })
    dst = _make_installed(tmp_path, {
        "foo/SKILL.md": "skill body",
        "foo/methodology.md": "companion body",
    })
    report = skill_drift.collect_drift(skills_src=src, claude_skills=dst)
    assert report["ok"], f"expected clean, got: {report}"


def test_skill_drift_companion_drifted(tmp_path, skill_drift):
    src = _make_skill_repo(tmp_path, {
        "foo.md": "skill body",
        "_foo-methodology.md": "new companion",
    })
    dst = _make_installed(tmp_path, {
        "foo/SKILL.md": "skill body",
        "foo/methodology.md": "old companion",
    })
    report = skill_drift.collect_drift(skills_src=src, claude_skills=dst)
    assert report["ok"] is False
    assert any(d["kind"] == "companion" and "methodology" in d["name"] for d in report["drift"])


def test_skill_drift_orphan_companion(tmp_path, skill_drift):
    """Companion file with no matching skill is flagged as orphan_in_repo."""
    src = _make_skill_repo(tmp_path, {
        "_bogus-thing.md": "companion without owner",
    })
    dst = _make_installed(tmp_path, {})
    report = skill_drift.collect_drift(skills_src=src, claude_skills=dst)
    assert report["ok"] is False
    assert any(d["reason"] == "orphan_in_repo" for d in report["drift"])


def test_skill_drift_recognises_lowercase_skill_md(tmp_path, skill_drift):
    """Backward compat: cmd_skill_install historically wrote lowercase
    ``skill.md`` so the checker must also recognise that placement."""
    src = _make_skill_repo(tmp_path, {"foo.md": "hello"})
    dst = _make_installed(tmp_path, {"foo/skill.md": "hello"})
    report = skill_drift.collect_drift(skills_src=src, claude_skills=dst)
    assert report["ok"], report


def test_skill_drift_warn_mode_exits_zero(tmp_path, skill_drift):
    src = _make_skill_repo(tmp_path, {"foo.md": "new"})
    dst = _make_installed(tmp_path, {"foo/SKILL.md": "old"})
    rc = skill_drift.main([
        "--skills-src", str(src), "--claude-skills", str(dst),
    ])
    assert rc == 0  # warn mode default


def test_skill_drift_strict_mode_exits_one(tmp_path, skill_drift):
    src = _make_skill_repo(tmp_path, {"foo.md": "new"})
    dst = _make_installed(tmp_path, {"foo/SKILL.md": "old"})
    rc = skill_drift.main([
        "--strict", "--skills-src", str(src), "--claude-skills", str(dst),
    ])
    assert rc == 1


def test_skill_drift_json_mode(tmp_path, skill_drift, capsys):
    src = _make_skill_repo(tmp_path, {"foo.md": "x"})
    dst = _make_installed(tmp_path, {"foo/SKILL.md": "x"})
    skill_drift.main([
        "--json", "--skills-src", str(src), "--claude-skills", str(dst),
    ])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is True


# ===========================================================================
# CLI help drift  — scripts/check-cli-help-drift.py
# ===========================================================================

def test_cli_drift_real_repo_is_aligned(cli_drift):
    """Baseline / regression: the current repo state must report ok=True."""
    report = cli_drift.collect_drift()
    assert report["ok"], (
        "CLI help drift detected in current repo state — "
        f"{report['missing_from_bin_help']=} "
        f"{report['missing_from_help_json']=} "
        f"{report['missing_from_readme']=}"
    )


def test_cli_drift_detects_new_subcommand_missing_from_help_json(tmp_path, cli_drift):
    """A new subcommand added to bin/beacon and README but not to
    cmd_help_json must be flagged."""
    bin_text = """#!/usr/bin/env bash
cat <<EOF
  beacon brandnew foo  Do something fresh
EOF
"""
    commands_text = '''
def cmd_help_json():
    import json
    print(json.dumps({"version": "x", "commands": []}))
'''
    readme_text = """## CLI Commands

### New

| Command | Description |
|---------|-------------|
| `beacon brandnew foo` | Do something fresh |

## Next Section
"""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "beacon").write_text(bin_text)
    (tmp_path / "commands.py").write_text(commands_text)
    (tmp_path / "README.md").write_text(readme_text)
    report = cli_drift.collect_drift(
        bin_path=tmp_path / "bin" / "beacon",
        commands_path=tmp_path / "commands.py",
        readme_path=tmp_path / "README.md",
    )
    assert "brandnew foo" in report["missing_from_help_json"]


def test_cli_drift_detects_new_subcommand_missing_from_readme(tmp_path, cli_drift):
    bin_text = """#!/usr/bin/env bash
cat <<EOF
  beacon brandnew foo  Do something fresh
EOF
"""
    commands_text = '''
def cmd_help_json():
    import json
    print(json.dumps({"version": "x", "commands": [
        {"command": "beacon brandnew foo", "flags": [], "description": "x"}
    ]}))
'''
    readme_text = """## CLI Commands

### Old

| Command | Description |
|---------|-------------|
| `beacon old thing` | Older |

## Next Section
"""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "beacon").write_text(bin_text)
    (tmp_path / "commands.py").write_text(commands_text)
    (tmp_path / "README.md").write_text(readme_text)
    report = cli_drift.collect_drift(
        bin_path=tmp_path / "bin" / "beacon",
        commands_path=tmp_path / "commands.py",
        readme_path=tmp_path / "README.md",
    )
    assert "brandnew foo" in report["missing_from_readme"]
    # "old thing" is documented in README but absent from the cmd_help_json
    # registry. ms-120 e-3897 made `beacon --help` RENDER that registry, so
    # bin help and the registry share one source: "missing from help" now means
    # "absent from the canonical registry", surfaced as missing_from_help_json
    # (not missing_from_bin_help, which is reserved for registry commands the
    # renderer drops). The README/registry gap is still detected — just in the
    # bucket that matches the post-e-3897 single-source model.
    assert "old thing" in report["missing_from_help_json"]


def test_cli_drift_warn_vs_strict_exit_codes(tmp_path, cli_drift):
    bin_text = "  beacon brandnew foo  Stuff\n"
    commands_text = '''
def cmd_help_json():
    import json
    print(json.dumps({"version": "x", "commands": []}))
'''
    readme_text = "## CLI Commands\n\n### x\n\n## Next\n"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "beacon").write_text(bin_text)
    (tmp_path / "commands.py").write_text(commands_text)
    (tmp_path / "README.md").write_text(readme_text)
    rc_warn = cli_drift.main([
        "--bin", str(tmp_path / "bin" / "beacon"),
        "--commands", str(tmp_path / "commands.py"),
        "--readme", str(tmp_path / "README.md"),
    ])
    rc_strict = cli_drift.main([
        "--strict",
        "--bin", str(tmp_path / "bin" / "beacon"),
        "--commands", str(tmp_path / "commands.py"),
        "--readme", str(tmp_path / "README.md"),
    ])
    assert rc_warn == 0
    assert rc_strict == 1


def test_cli_drift_extract_verb_ignores_placeholders(cli_drift):
    """Positional / flag tokens must not be treated as subverbs."""
    assert cli_drift._extract_verb("beacon milestone add \"title\"") == "milestone add"
    assert cli_drift._extract_verb("beacon task add \"desc\" [-m ms-id]") == "task add"
    assert cli_drift._extract_verb("beacon status") == "status"
    assert cli_drift._extract_verb("beacon") == ""
    assert cli_drift._extract_verb("not a beacon line") is None
    # Description words after the verb must not leak in (the old bug).
    assert cli_drift._extract_verb("beacon doctor Check PATH, hooks") == "doctor"


# ===========================================================================
# INSTALL.md static validation  — scripts/check-install-md.py
# ===========================================================================

def test_install_md_real_file_passes(install_md):
    """Regression: current INSTALL.md must validate cleanly."""
    report = install_md.collect_findings()
    assert report["ok"], f"INSTALL.md regressed: {report['findings']}"


def test_install_md_flags_deprecated_build_arg(tmp_path, install_md):
    bad = tmp_path / "INSTALL.md"
    bad.write_text("""# Install

```bash
gcloud run deploy beacon-api-prod \\
  --source . \\
  --region asia-northeast1 \\
  --build-arg=CACHE_BUST=$(date +%s)
```
""")
    report = install_md.collect_findings(path=bad)
    assert report["ok"] is False
    kinds = {(f["rule"], f["kind"], f["flag"]) for f in report["findings"]}
    assert ("gcloud run deploy", "forbidden_flag", "--build-arg") in kinds


def test_install_md_flags_missing_required_flag(tmp_path, install_md):
    bad = tmp_path / "INSTALL.md"
    bad.write_text("""# Install

```bash
gcloud run deploy beacon-api-prod --source . --region asia-northeast1
```
""")
    report = install_md.collect_findings(path=bad)
    assert report["ok"] is False
    assert any(f["kind"] == "missing_required_flag" for f in report["findings"])


def test_install_md_flags_brew_install_missing_positional(tmp_path, install_md):
    bad = tmp_path / "INSTALL.md"
    bad.write_text("""# Install

```bash
brew install
```
""")
    report = install_md.collect_findings(path=bad)
    assert report["ok"] is False
    assert any(f["rule"] == "brew install" and f["kind"] == "missing_positional"
               for f in report["findings"])


def test_install_md_pipx_install_with_pkg_passes(tmp_path, install_md):
    good = tmp_path / "INSTALL.md"
    good.write_text("""```bash
pipx install beacon
```
""")
    report = install_md.collect_findings(path=good)
    assert report["ok"], report


def test_install_md_strict_vs_warn_exit_codes(tmp_path, install_md):
    bad = tmp_path / "INSTALL.md"
    bad.write_text("```bash\ngcloud run deploy x --build-arg=Y\n```\n")
    rc_warn = install_md.main(["--install-md", str(bad)])
    rc_strict = install_md.main(["--strict", "--install-md", str(bad)])
    assert rc_warn == 0
    assert rc_strict == 1


def test_install_md_joins_backslash_continuations(tmp_path, install_md):
    """A multi-line gcloud command split with backslashes must be
    normalised into a single command before flag detection runs."""
    f = tmp_path / "INSTALL.md"
    f.write_text("""```bash
gcloud run deploy x \\
  --source . \\
  --build-arg=BAD=1
```
""")
    report = install_md.collect_findings(path=f)
    assert report["ok"] is False
    assert any(f["flag"] == "--build-arg" for f in report["findings"])


# ===========================================================================
# beacon doctor check #6 (skills drift)
# ===========================================================================

def test_doctor_includes_skills_drift_section():
    """cmd_doctor's docstring lists the new check #6, and its body runs
    the drift script. We exercise it by calling the doctor subprocess
    in a way that triggers the section's code path (the script exists
    in this repo). A clean repo state must still exit 0 from doctor."""
    # Run doctor against the worktree. Doctor may emit other unrelated
    # warnings (auth token, hooks), but it must NOT crash and the skills-drift
    # line, if present, must obey the new format.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "lib") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(REPO / "lib" / "commands.py"), "doctor"],
        cwd=str(REPO),
        capture_output=True, text=True, env=env, timeout=30,
    )
    # Doctor exits 0 (clean) or 1 (warnings) — never crash.
    assert proc.returncode in (0, 1), proc.stderr
    # If skills are out of sync, the line must use the agreed prefix and
    # mention the repair command.
    if "[skills-drift]" in proc.stdout:
        assert "beacon skill install" in proc.stdout


def test_doctor_docstring_mentions_skills_drift():
    """The docstring is part of the user-visible contract — keep it
    in sync with the implementation."""
    spec = importlib.util.spec_from_file_location(
        "_beacon_commands_for_doc",
        REPO / "lib" / "commands.py",
    )
    assert spec is not None and spec.loader is not None
    # Add lib/ to sys.path so commands.py can import siblings (auth, etc).
    lib_path = str(REPO / "lib")
    added = lib_path not in sys.path
    if added:
        sys.path.insert(0, lib_path)
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if added:
            try:
                sys.path.remove(lib_path)
            except ValueError:
                pass
    doc = mod.cmd_doctor.__doc__ or ""
    assert "skills/" in doc
    assert "e-722" in doc, "Doctor docstring should reference the new check's task id"
