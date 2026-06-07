"""Wheel-layout (Windows / pipx) regression tests for ``beacon skill install``.

This suite covers the ms-44 e-777 + Phase 2 work (PowerShell-native skill
install):

  1. The built wheel contains the skills/ directory remapped under
     ``beacon_cli/_bundled_skills/`` so that pipx-installed beacons can find
     them. (Without this the wheel ships skills-less and ``beacon skill
     install`` produces a broken install on every non-source-checkout user.)

  2. ``cmd_skill_install`` resolves the skills source via
     ``_resolve_skills_src`` — preferring the source layout (regression
     guard for the existing macOS path) and falling back to
     ``_bundled_skills/`` when only the wheel layout is on disk.

  3. ``_resolve_hook_command`` produces a Claude-Code-friendly command
     string that does NOT require bash. Pipx installs get the absolute
     console-script entry-point; source/brew installs keep the existing
     .sh path. The ``python -m beacon_cli.hooks.<name>`` fallback is
     reached when neither is present.

  4. ``_install_claude_hooks`` registers a cross-platform command in
     ``~/.claude/settings.json`` and idempotently dedups stale bash paths
     when a wheel install replaces a previous source install.

The tests deliberately avoid spinning up a real ``pipx`` — they exercise
the resolution logic directly by manipulating ``sys.executable`` /
``shutil.which`` / a tmp_path mirror of the wheel layout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = _REPO_ROOT / "lib"
sys.path.insert(0, str(_LIB))


# ---------------------------------------------------------------------------
# Wheel contents
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    """Build the wheel once per test module via ``python -m build``.

    Skipped when the ``build`` package isn't importable (CI minimal image).
    The wheel is placed in a tmp dir so we don't depend on / mutate the
    user's ``dist/``.
    """
    try:
        import build  # noqa: F401
    except ImportError:
        pytest.skip("python `build` package not available")

    outdir = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        pytest.skip(f"wheel build failed: {proc.stderr[-1500:]}")

    wheels = list(outdir.glob("beacon-*.whl"))
    assert wheels, "no wheel produced"
    return wheels[0]


def test_wheel_bundles_skills_directory(built_wheel):
    """The wheel must ship every .md file under skills/ as a
    ``beacon_cli/_bundled_skills/<name>.md`` entry."""
    with zipfile.ZipFile(built_wheel) as z:
        names = z.namelist()
    bundled = [
        n for n in names
        if n.startswith("beacon_cli/_bundled_skills/") and n.endswith(".md")
    ]
    # Sanity: source has 20+ skills today; we keep this loose to survive
    # legitimate growth/pruning.
    assert len(bundled) >= 20, (
        f"expected 20+ bundled skill files, got {len(bundled)}: "
        f"{sorted(bundled)[:5]}..."
    )

    # Spot-check the two skills most critical to the Beacon dev loop.
    bundled_set = set(bundled)
    assert "beacon_cli/_bundled_skills/beacon-log.md" in bundled_set
    assert "beacon_cli/_bundled_skills/beacon-session-start.md" in bundled_set


def test_wheel_bundles_companion_files(built_wheel):
    """Companion files (`_<skill>-<suffix>.md`) must also ship — they
    document long-form methodology consumed at Skill runtime."""
    with zipfile.ZipFile(built_wheel) as z:
        names = z.namelist()
    companions = [
        n for n in names
        if n.startswith("beacon_cli/_bundled_skills/_") and n.endswith(".md")
    ]
    assert len(companions) >= 1, (
        f"expected companion files, got {companions}"
    )


def test_wheel_exposes_cross_platform_hook_entry_points(built_wheel):
    """The wheel's ``entry_points.txt`` must register the three hook
    console-scripts so pipx install puts them on PATH.

    Without these entry-points Claude Code on Windows would have to point
    at the bash .sh files (which aren't shipped in the wheel anyway), and
    PostToolUse / PostCompact hooks would silently fail."""
    with zipfile.ZipFile(built_wheel) as z:
        # The dist-info dir name changes per version — discover it.
        info_dirs = [n for n in z.namelist() if n.endswith(".dist-info/entry_points.txt")]
        assert info_dirs, "no entry_points.txt in wheel"
        text = z.read(info_dirs[0]).decode("utf-8")

    assert "beacon-hook-post-commit = beacon_cli.hooks.post_commit:main" in text
    assert "beacon-hook-postcompact = beacon_cli.hooks.postcompact:main" in text
    assert "beacon-hook-save = beacon_cli.hooks.save_hook:main" in text


# ---------------------------------------------------------------------------
# _resolve_skills_src — source vs wheel layout fallback
# ---------------------------------------------------------------------------


def test_resolve_skills_src_prefers_source_layout():
    """When running from a source checkout, ``_resolve_skills_src`` must
    return the source ``skills/`` directory verbatim (regression guard
    for the existing macOS workflow)."""
    import commands  # type: ignore

    src = commands._resolve_skills_src()
    assert src, "source layout should always resolve"
    assert Path(src).resolve() == (_REPO_ROOT / "skills").resolve()


def test_resolve_skills_src_falls_back_to_bundled(tmp_path, monkeypatch):
    """Simulate a wheel install: commands.py lives at
    ``<pkg>/_bundled_lib/commands.py`` with skills at
    ``<pkg>/_bundled_skills/``. _resolve_skills_src must pick the latter
    when the source ``../skills`` is absent."""
    import commands  # type: ignore

    # Build a fake wheel-style layout:
    #   tmp/beacon_cli/_bundled_lib/commands.py  (just a placeholder file)
    #   tmp/beacon_cli/_bundled_skills/beacon-log.md
    pkg = tmp_path / "beacon_cli"
    bundled_lib = pkg / "_bundled_lib"
    bundled_lib.mkdir(parents=True)
    (bundled_lib / "commands.py").write_text("# fake")
    bundled_skills = pkg / "_bundled_skills"
    bundled_skills.mkdir()
    (bundled_skills / "beacon-log.md").write_text("# log")

    # Trick _resolve_skills_src into thinking it lives in the fake bundled lib.
    fake_commands_py = bundled_lib / "commands.py"

    # ``_resolve_skills_src`` uses ``os.path.dirname(os.path.abspath(__file__))``
    # via the real commands module. We patch __file__ for the duration.
    monkeypatch.setattr(commands, "__file__", str(fake_commands_py))
    src = commands._resolve_skills_src()
    assert src, "wheel layout fallback must resolve"
    assert Path(src).resolve() == bundled_skills.resolve()


def test_resolve_skills_src_returns_empty_when_neither_exists(tmp_path, monkeypatch):
    """If neither layout is present (truly broken install), the resolver
    must return empty string so ``cmd_skill_install`` can produce a
    targeted error message instead of crashing."""
    import commands  # type: ignore

    pkg = tmp_path / "beacon_cli" / "_bundled_lib"
    pkg.mkdir(parents=True)
    (pkg / "commands.py").write_text("# fake")
    fake_commands_py = pkg / "commands.py"
    monkeypatch.setattr(commands, "__file__", str(fake_commands_py))

    assert commands._resolve_skills_src() == ""


# ---------------------------------------------------------------------------
# _resolve_hook_command — cross-platform contract
# ---------------------------------------------------------------------------


def test_resolve_hook_command_prefers_entry_point(monkeypatch, tmp_path):
    """When ``beacon-hook-post-commit`` is on PATH, return bare name (e-1170).

    Pre-e-1170 this test asserted the absolute resolved path. The new
    contract: shutil.which is used to *validate* that the entry-point
    exists at install time, but settings.json gets only the bare name so
    Claude Code re-resolves via PATH at hook fire time (survives install
    relocation: pipx → pip --user, ~/.local/bin → AppData/.../Scripts, etc).
    """
    import commands  # type: ignore

    fake_entry = tmp_path / "beacon-hook-post-commit"
    fake_entry.write_text("#!/usr/bin/env python\n")
    fake_entry.chmod(0o755)

    monkeypatch.setattr(
        commands.shutil, "which",
        lambda name: str(fake_entry) if name == "beacon-hook-post-commit" else None,
    )
    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    assert cmd == "beacon-hook-post-commit"


def test_resolve_hook_command_falls_back_to_bash_when_no_entry_point(monkeypatch):
    """Source / brew install: no console-script on PATH, but the .sh is
    present in bin/.

    - Off Windows: return the bash path so existing macOS/Linux users keep
      their working hook.
    - On Windows: a bare .sh cannot be executed by Claude Code's hook runner,
      so resolution skips it and returns the ``python -m`` form instead
      (ms-44 e-853)."""
    import commands  # type: ignore

    monkeypatch.setattr(commands.shutil, "which", lambda name: None)
    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    if os.name == "nt":
        assert "beacon_cli.hooks.post_commit" in cmd
        assert not cmd.lower().endswith(".sh")
    else:
        # In this repo the .sh lives at bin/beacon-post-commit-hook.sh
        assert cmd.endswith("beacon-post-commit-hook.sh")
        assert os.path.exists(cmd)


def _force_wheel_only(monkeypatch, commands_mod):
    """Make the resolver skip all source/brew/test-stub paths."""
    monkeypatch.setattr(commands_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(commands_mod, "_find_hook", lambda name: "/nonexistent/" + name)
    # Also wipe the module-level constants so the 2nd resolution layer
    # (which honors monkeypatched CLAUDE_*_SCRIPT for backward compat with
    # test_install_hooks.py) doesn't intercept us.
    monkeypatch.setattr(commands_mod, "CLAUDE_HOOK_SCRIPT", "/nonexistent/x.sh")
    monkeypatch.setattr(commands_mod, "CLAUDE_POSTCOMPACT_HOOK_SCRIPT", "/nonexistent/y.sh")
    monkeypatch.setattr(commands_mod, "CLAUDE_SAVE_HOOK_SCRIPT", "/nonexistent/z.sh")
    # ms-44 e-854: Stop hook (context-usage monitor) added to the mapping.
    monkeypatch.setattr(
        commands_mod, "CLAUDE_CONTEXT_MONITOR_HOOK_SCRIPT", "/nonexistent/ctx.sh"
    )


def test_resolve_hook_command_python_module_fallback(monkeypatch):
    """When neither the entry-point nor the bash .sh is available
    (degenerate CI install), the resolver returns a ``python -m`` command
    that any environment with the wheel importable can run."""
    import commands  # type: ignore
    _force_wheel_only(monkeypatch, commands)

    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    assert "beacon_cli.hooks.post_commit" in cmd
    assert "-m" in cmd


def test_resolve_hook_command_handles_postcompact_and_save(monkeypatch):
    """The mapping covers all three hooks (commit, postcompact, save).
    A typo / missing mapping must NOT crash."""
    import commands  # type: ignore
    _force_wheel_only(monkeypatch, commands)

    for base, mod in [
        ("beacon-postcompact.sh", "beacon_cli.hooks.postcompact"),
        ("beacon-save-hook.sh", "beacon_cli.hooks.save_hook"),
        # ms-44 e-854: Stop hook (context-usage monitor) Python port.
        ("context-usage-monitor.sh", "beacon_cli.hooks.context_monitor"),
    ]:
        cmd = commands._resolve_hook_command(base)
        assert mod in cmd

    # Unknown hook → empty string, not crash.
    assert commands._resolve_hook_command("not-a-real-hook.sh") == ""


# ---------------------------------------------------------------------------
# _install_claude_hooks — settings.json end state
# ---------------------------------------------------------------------------


def test_install_claude_hooks_writes_cross_platform_command(tmp_path, monkeypatch):
    """When the resolved hook is a pipx-style entry-point present on PATH,
    the bare entry-point name lands in settings.json (e-1170).

    Pre-e-1170 this asserted the absolute path of the resolved entry-point.
    The new contract: shutil.which is used to *validate* presence at install
    time, but settings.json carries only the bare name so Claude Code re-
    resolves via PATH at hook fire time. Survives install relocation
    (pipx → pip --user, ~/.local/bin → AppData/.../Scripts, etc.).
    """
    import commands  # type: ignore

    # Simulate pipx-installed entry-points
    fake_post = tmp_path / "beacon-hook-post-commit"
    fake_post.write_text("#!/usr/bin/env python\n")
    fake_post.chmod(0o755)
    fake_pc = tmp_path / "beacon-hook-postcompact"
    fake_pc.write_text("#!/usr/bin/env python\n")
    fake_pc.chmod(0o755)

    which_map = {
        "beacon-hook-post-commit": str(fake_post),
        "beacon-hook-postcompact": str(fake_pc),
    }
    monkeypatch.setattr(
        commands.shutil, "which", lambda name: which_map.get(name)
    )

    settings_path = tmp_path / "settings.json"
    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    commands._install_claude_hooks(cmd, str(settings_path))

    data = json.loads(settings_path.read_text())
    bash_cmds = [
        h.get("command")
        for entry in data["hooks"].get("PostToolUse", [])
        if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    ]
    # e-1170: bare entry-point name, not absolute path.
    assert "beacon-hook-post-commit" in bash_cmds, data

    pc_cmds = [
        h.get("command")
        for entry in data["hooks"].get("PostCompact", [])
        for h in entry.get("hooks", [])
    ]
    assert "beacon-hook-postcompact" in pc_cmds, data


def test_install_claude_hooks_registers_stop_hook_cross_platform(tmp_path, monkeypatch):
    """ms-44 e-854: the Stop hook (context-usage threshold monitor) must be
    registered when `beacon skill install` runs, just like PostToolUse and
    PostCompact. Without this, Windows users have to manually configure
    settings.json — the previous gap that left context-usage notifications
    silently broken on Win.

    The cross-platform contract: when ``beacon-hook-context-monitor`` is
    available as an entry-point, settings.json carries the bare name
    (Claude Code re-resolves via PATH). On a bash-only source layout it
    carries the absolute .sh path. On Windows .sh resolution is BLOCKED by
    _hook_unusable_on_windows() so the resolver always falls through to
    the Python module — no .sh ever leaks into Win settings.json."""
    import commands  # type: ignore

    # Simulate pipx-installed entry-points for all three hook flavors.
    fake_post = tmp_path / "beacon-hook-post-commit"
    fake_post.write_text("#!/usr/bin/env python\n")
    fake_post.chmod(0o755)
    fake_pc = tmp_path / "beacon-hook-postcompact"
    fake_pc.write_text("#!/usr/bin/env python\n")
    fake_pc.chmod(0o755)
    fake_ctx = tmp_path / "beacon-hook-context-monitor"
    fake_ctx.write_text("#!/usr/bin/env python\n")
    fake_ctx.chmod(0o755)

    which_map = {
        "beacon-hook-post-commit": str(fake_post),
        "beacon-hook-postcompact": str(fake_pc),
        "beacon-hook-context-monitor": str(fake_ctx),
    }
    monkeypatch.setattr(
        commands.shutil, "which", lambda name: which_map.get(name)
    )

    settings_path = tmp_path / "settings.json"
    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    commands._install_claude_hooks(cmd, str(settings_path))

    data = json.loads(settings_path.read_text())
    stop_cmds = [
        h.get("command")
        for entry in data["hooks"].get("Stop", [])
        for h in entry.get("hooks", [])
    ]
    # e-1170 contract: bare entry-point name, not absolute path.
    assert "beacon-hook-context-monitor" in stop_cmds, data


def test_install_claude_hooks_dedups_stale_bash_path_on_upgrade(tmp_path, monkeypatch):
    """When a user upgrades from a source install (bash .sh in
    ``~/.claude/settings.json``) to a pipx install, the new install must
    REMOVE the stale .sh entry and ADD the entry-point so Claude Code
    doesn't keep running a no-longer-existing file."""
    import commands  # type: ignore

    pre = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{
                        "type": "command",
                        # Old bash path the user no longer has.
                        "command": "/old/path/beacon-post-commit-hook.sh",
                    }],
                }
            ],
        }
    }
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(pre))

    # Pretend the pipx entry-point is now on PATH.
    fake_entry = tmp_path / "beacon-hook-post-commit"
    fake_entry.write_text("#!/usr/bin/env python\n")
    fake_entry.chmod(0o755)
    monkeypatch.setattr(
        commands.shutil, "which",
        lambda name: str(fake_entry) if name == "beacon-hook-post-commit" else None,
    )

    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    commands._install_claude_hooks(cmd, str(settings_path))

    data = json.loads(settings_path.read_text())
    bash_cmds = [
        h.get("command")
        for entry in data["hooks"].get("PostToolUse", [])
        if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    ]
    # Stale dropped, new added.
    assert "/old/path/beacon-post-commit-hook.sh" not in bash_cmds
    # e-1170: bare entry-point name lands in settings.json (not the
    # absolute path), so on next upgrade we don't have to dedup-by-path.
    assert "beacon-hook-post-commit" in bash_cmds


def test_install_claude_hooks_accepts_python_m_command(tmp_path, monkeypatch):
    """Wheel-on-PATH degenerate case: hook resolves to a
    ``python -m beacon_cli.hooks.post_commit`` string (multi-token, no
    file). The settings writer must NOT reject it via os.path.exists."""
    import commands  # type: ignore
    _force_wheel_only(monkeypatch, commands)

    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    assert "beacon_cli.hooks.post_commit" in cmd

    settings_path = tmp_path / "settings.json"
    commands._install_claude_hooks(cmd, str(settings_path))
    data = json.loads(settings_path.read_text())
    bash_cmds = [
        h.get("command")
        for entry in data["hooks"].get("PostToolUse", [])
        if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    ]
    assert any("beacon_cli.hooks.post_commit" in c for c in bash_cmds)


# ---------------------------------------------------------------------------
# Regression guard: macOS source-layout cmd_skill_install still works
# ---------------------------------------------------------------------------


def test_cmd_skill_install_source_layout_unchanged(tmp_path, monkeypatch):
    """On a fresh source / brew install with a HOME pointing at a tmp dir,
    cmd_skill_install must:
      * copy every skill .md to ``~/.claude/skills/<name>/skill.md``
      * write hook entries to ``~/.claude/settings.json``
    The bash entry-point isn't actually invoked here — we go straight
    through Python — but the end state must match what users see today."""
    import commands  # type: ignore

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    # ``cmd_skill_install`` calls ``_append_claude_md`` which writes to
    # the project's CLAUDE.md (cwd-relative). Use a tmp cwd so we don't
    # touch the real repo.
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    # The project's CLAUDE.md doesn't need to exist; _append_claude_md
    # creates it if needed.

    # Skip the dev pre-commit installer (it pokes at .git/).
    monkeypatch.setattr(commands, "_install_dev_precommit_hook", lambda root: None)

    # Force the hook resolver to return the real bash .sh path (source
    # layout). shutil.which("beacon-hook-post-commit") → None.
    monkeypatch.setattr(commands.shutil, "which", lambda name: None)

    commands.cmd_skill_install()

    # Skills landed.
    claude_skills = fake_home / ".claude" / "skills"
    installed = sorted(p.name for p in claude_skills.iterdir() if p.is_dir())
    assert "beacon-log" in installed, installed
    assert "beacon-session-start" in installed, installed

    # settings.json written with a beacon commit hook. Off Windows this is the
    # bash .sh (source layout); on Windows the .sh is skipped in favor of the
    # ``python -m`` form (ms-44 e-853) — accept either.
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    bash_cmds = [
        h.get("command")
        for entry in settings["hooks"].get("PostToolUse", [])
        if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    ]
    assert any(
        "beacon-post-commit-hook" in c or "beacon_cli.hooks.post_commit" in c
        for c in bash_cmds
    ), bash_cmds


# ---------------------------------------------------------------------------
# dispatch.py — skill install subparser
# ---------------------------------------------------------------------------


def test_dispatch_skill_install_routes_to_commands_py(monkeypatch):
    """``beacon skill install`` (PowerShell path) must build the right
    subprocess invocation: ``python <commands.py> skill_install`` with no
    BEACON_* required env (parity with the bash entry-point)."""
    sys.path.insert(0, str(_REPO_ROOT))
    import beacon_cli.dispatch as dispatch  # type: ignore

    captured = {}

    def fake_call(cmd, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        return 0

    monkeypatch.setattr(dispatch.subprocess, "call", fake_call)
    rc = dispatch.dispatch(_REPO_ROOT, ["skill", "install"])
    assert rc == 0
    assert captured["cmd"][1].endswith("commands.py"), captured["cmd"]
    assert captured["cmd"][-1] == "skill_install"
    # --force not passed → no BEACON_FORCE
    assert "BEACON_FORCE" not in captured["env"]


def test_dispatch_skill_install_forwards_force_and_settings(monkeypatch):
    """``--force`` and ``--settings-path`` must surface as BEACON_* env
    vars so commands.py can branch on them when we ship those flags."""
    sys.path.insert(0, str(_REPO_ROOT))
    import beacon_cli.dispatch as dispatch  # type: ignore

    captured = {}

    def fake_call(cmd, env=None):
        captured["env"] = env
        return 0

    monkeypatch.setattr(dispatch.subprocess, "call", fake_call)
    rc = dispatch.dispatch(
        _REPO_ROOT,
        ["skill", "install", "--force", "--settings-path", "/tmp/x.json"],
    )
    assert rc == 0
    assert captured["env"]["BEACON_FORCE"] == "1"
    assert captured["env"]["BEACON_SETTINGS_PATH"] == "/tmp/x.json"


def test_dispatch_skill_install_respects_settings_path_env(tmp_path, monkeypatch):
    """When ``--settings-path`` is passed via the dispatch CLI, the
    forwarded ``BEACON_SETTINGS_PATH`` must steer ``cmd_skill_install``
    to write hooks into that path instead of ``~/.claude/settings.json``."""
    import commands  # type: ignore

    custom_settings = tmp_path / "custom-settings.json"
    monkeypatch.setenv("BEACON_SETTINGS_PATH", str(custom_settings))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    monkeypatch.setattr(commands, "_install_dev_precommit_hook", lambda root: None)
    monkeypatch.setattr(commands.shutil, "which", lambda name: None)

    commands.cmd_skill_install()

    assert custom_settings.exists(), "BEACON_SETTINGS_PATH must redirect output"
    # Default location must NOT have been written.
    default = fake_home / ".claude" / "settings.json"
    assert not default.exists(), (
        "When BEACON_SETTINGS_PATH is set, the default ~/.claude/settings.json "
        "must be left untouched."
    )
