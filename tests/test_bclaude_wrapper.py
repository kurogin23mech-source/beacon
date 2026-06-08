"""Tests for bin/bclaude (ms-54 e-1167).

These exercise the bash wrapper end-to-end with a fake `claude` on PATH,
so we lock in the contract:

  - default invocation prepends --dangerously-load-development-channels
  - env BEACON_NO_BUS=1 falls back to plain claude + audit msg
  - project .beacon/project.json bus.disabled falls back to plain claude
  - global ~/.beacon/config.json bus.disabled falls back to plain claude
  - extra args after the flag are forwarded unchanged

Each test runs the script in a temp dir with a stubbed `claude` shim so
we never depend on the real Claude Code binary being installed in CI.

We bypass bash quoting subtleties by recording argv as JSON inside the
fake claude — that's easier to assert on than parsing shell output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BCLAUDE = REPO_ROOT / "bin" / "bclaude"


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Build a sandbox: fake claude on PATH, isolated HOME, project dir.

    The fake `claude` is a one-line bash script that dumps its argv to
    a known file as JSON; the test then reads that file and inspects
    what the wrapper actually invoked.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    argv_dump = tmp_path / "argv.json"

    # Stub claude: dumps argv to argv.json and exits 0.
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(textwrap.dedent(f"""
        #!/usr/bin/env bash
        # Dump argv as JSON for the test to inspect.
        python3 -c 'import json, sys; json.dump(sys.argv[1:], open("{argv_dump}", "w"))' "$@"
    """).strip(), encoding="utf-8")
    fake_claude.chmod(0o755)

    # Isolated HOME so the test's --global opt-out doesn't pollute the
    # developer's real ~/.beacon/config.json.
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    # Project dir (CWD when running bclaude). Make it look like a beacon
    # project so the wrapper finds .beacon/project.json.
    proj_dir = tmp_path / "project"
    (proj_dir / ".beacon").mkdir(parents=True)
    (proj_dir / ".beacon" / "project.json").write_text(
        '{"name": "test", "milestones": []}', encoding="utf-8"
    )

    return {
        "tmp_path": tmp_path,
        "bin_dir": bin_dir,
        "argv_dump": argv_dump,
        "home_dir": home_dir,
        "proj_dir": proj_dir,
    }


def _run(fake_env, extra_env=None, args=None):
    """Invoke bclaude in the sandbox, return (returncode, stdout, stderr)."""
    env = os.environ.copy()
    # Prepend the fake bin dir so `command -v claude` finds the stub.
    env["PATH"] = f"{fake_env['bin_dir']}:{env.get('PATH', '')}"
    env["HOME"] = str(fake_env["home_dir"])
    env["USERPROFILE"] = str(fake_env["home_dir"])  # Windows fallback path
    # Defensively unset BEACON_NO_BUS in case the test environment has it.
    env.pop("BEACON_NO_BUS", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(BCLAUDE)] + (args or []),
        cwd=str(fake_env["proj_dir"]),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _argv_from(fake_env):
    """Read the argv the fake claude was called with."""
    if not fake_env["argv_dump"].exists():
        return None
    return json.loads(fake_env["argv_dump"].read_text())


class TestDefaultPath:
    """No opt-out → channel flag is prepended."""

    def test_no_args_adds_channel_flag(self, fake_env):
        code, out, err = _run(fake_env)
        assert code == 0
        argv = _argv_from(fake_env)
        assert argv is not None
        assert argv[:2] == ["--dangerously-load-development-channels",
                            "server:beacon-bus"]

    def test_user_args_are_forwarded_after_flag(self, fake_env):
        code, out, err = _run(fake_env, args=["--model", "opus-4-7"])
        assert code == 0
        argv = _argv_from(fake_env)
        assert argv == [
            "--dangerously-load-development-channels",
            "server:beacon-bus",
            "--model",
            "opus-4-7",
        ]

    def test_no_audit_msg_on_default_path(self, fake_env):
        code, out, err = _run(fake_env)
        assert "opt-out" not in err.lower()


class TestEnvOptOut:
    """BEACON_NO_BUS=1 → fall back to plain claude + audit msg."""

    def test_env_var_skips_channel_flag(self, fake_env):
        code, out, err = _run(fake_env, extra_env={"BEACON_NO_BUS": "1"})
        assert code == 0
        argv = _argv_from(fake_env)
        assert argv == []

    def test_env_var_prints_audit_to_stderr(self, fake_env):
        code, out, err = _run(fake_env, extra_env={"BEACON_NO_BUS": "1"})
        assert "[bclaude]" in err
        assert "opt-out" in err.lower()
        assert "env BEACON_NO_BUS" in err

    def test_env_var_falsy_values_do_not_opt_out(self, fake_env):
        """BEACON_NO_BUS=0 / false / no etc. must NOT trigger opt-out.

        Catches a regression where the wrapper would treat any value as
        truthy. The shell rule "non-empty = true" is the wrong default
        for a config flag.
        """
        for falsy in ("0", "false", "no", "off", ""):
            code, out, err = _run(fake_env,
                                  extra_env={"BEACON_NO_BUS": falsy})
            argv = _argv_from(fake_env)
            assert argv is not None and argv[:1] == [
                "--dangerously-load-development-channels"
            ], f"BEACON_NO_BUS={falsy!r} should not opt out (got argv={argv})"

    def test_user_args_still_forwarded_when_opted_out(self, fake_env):
        code, out, err = _run(fake_env, extra_env={"BEACON_NO_BUS": "1"},
                              args=["--model", "opus-4-7", "extra"])
        assert code == 0
        argv = _argv_from(fake_env)
        assert argv == ["--model", "opus-4-7", "extra"]


class TestProjectOptOut:
    """.beacon/project.json bus.disabled=true → fall back."""

    def test_project_flag_skips_channel_flag(self, fake_env):
        proj_file = fake_env["proj_dir"] / ".beacon" / "project.json"
        proj_file.write_text(
            '{"name": "test", "milestones": [], "bus": {"disabled": true}}',
            encoding="utf-8",
        )
        code, out, err = _run(fake_env)
        assert code == 0
        argv = _argv_from(fake_env)
        assert argv == []
        assert "project" in err.lower()


class TestGlobalOptOut:
    """~/.beacon/config.json bus.disabled=true → fall back."""

    def test_global_flag_skips_channel_flag(self, fake_env):
        global_cfg = fake_env["home_dir"] / ".beacon" / "config.json"
        global_cfg.parent.mkdir(parents=True, exist_ok=True)
        global_cfg.write_text(
            '{"bus": {"disabled": true}}', encoding="utf-8"
        )
        code, out, err = _run(fake_env)
        assert code == 0
        argv = _argv_from(fake_env)
        assert argv == []
        assert "global" in err.lower()


class TestNoClaudeOnPath:
    """If `claude` itself is missing, fail with a clear error."""

    def test_missing_claude_returns_127(self, fake_env, tmp_path):
        # We need bash itself on PATH for subprocess to launch, but we
        # want a PATH that has no `claude`. Build a minimal PATH from
        # the real shell dirs but exclude any directory that contains
        # claude (rare in dev envs, possible in CI).
        env = os.environ.copy()
        env["HOME"] = str(fake_env["home_dir"])
        env.pop("BEACON_NO_BUS", None)
        # Keep only /usr/bin and /bin so `bash` is findable. Filter out
        # `claude` if it happens to live there (it shouldn't — claude
        # ships in ~/.claude/local/ or via npm globals, not /usr/bin).
        # If a dev machine has claude at /usr/local/bin/claude, this
        # test still won't see it because /usr/local/bin is excluded.
        env["PATH"] = "/usr/bin:/bin"
        # Sanity: skip the test entirely if claude IS at /usr/bin (would
        # invalidate the negative case but isn't a real-world setup).
        for p in env["PATH"].split(":"):
            if Path(p, "claude").exists():
                pytest.skip(
                    f"claude unexpectedly present at {p}/claude — "
                    "this test relies on its absence")
        proc = subprocess.run(
            ["bash", str(BCLAUDE)],
            cwd=str(fake_env["proj_dir"]),
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 127
        assert "claude" in proc.stderr.lower()
        assert "not on PATH" in proc.stderr
