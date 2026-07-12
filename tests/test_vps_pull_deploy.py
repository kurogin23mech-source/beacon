"""Integration tests for scripts/vps-pull-deploy.sh (ms-102 / e-3222).

The pull-deploy script used to decide "restart or not" purely from whether
``origin/main`` advanced past local HEAD. If a ``systemctl restart`` ever
failed after ``git reset --hard`` had already advanced HEAD, git looked
up-to-date forever while uvicorn kept serving stale code — and every
subsequent timer tick skipped the restart ("no git change"), so the box was
permanently stuck. ``/api/version`` reads the git rev live, so it reported
the newest version and hid the stuck state.

The fix records the rev the process is *confirmed serving* (written only after
a successful health check) in ``.venv/.beacon-deployed-rev`` and restarts when
that served-rev != target OR the service is down — so a failed restart
self-heals on the next tick.

These tests drive the real script with real ``git`` but PATH shims for
``systemctl`` / ``sudo`` / ``curl`` / ``logger`` so we can assert the restart
decision without a live VPS.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "vps-pull-deploy.sh"


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _write_shims(bindir: Path, state: Path):
    """Fake systemctl / sudo / curl / logger.

    - systemctl is-active  → exit 0 iff <state>/active exists
    - systemctl restart    → append 'restart' to <state>/calls, create active
    - sudo <cmd...>        → exec the rest (drops sudo)
    - curl                 → exit 0 iff <state>/health_ok exists, OR the current
                             git HEAD (curl runs with cwd=REPO) is listed in
                             <state>/healthy_revs. The rev-aware branch lets a
                             rollback test make a NEW rev unhealthy while the
                             prior good rev stays healthy within one script run.
    - logger               → no-op
    """
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        f'ST="{state}"\n'
        'case "$1" in\n'
        '  is-active) [ -f "$ST/active" ] && exit 0 || exit 3 ;;\n'
        '  restart)  echo restart >> "$ST/calls"; : > "$ST/active"; exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    )
    (bindir / "sudo").write_text('#!/usr/bin/env bash\nexec "$@"\n')
    (bindir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        f'ST="{state}"\n'
        '[ -f "$ST/health_ok" ] && exit 0\n'
        'if [ -f "$ST/healthy_revs" ]; then\n'
        '  HEAD="$(git rev-parse HEAD 2>/dev/null)"\n'
        '  grep -qx "$HEAD" "$ST/healthy_revs" 2>/dev/null && exit 0\n'
        'fi\n'
        'exit 22\n'
    )
    (bindir / "logger").write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in ("systemctl", "sudo", "curl", "logger"):
        (bindir / f).chmod(0o755)


def _setup_repo(tmp_path: Path):
    """Bare origin + working clone on main, plus a noop pip shim."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    (seed / "requirements.txt").write_text("")
    (seed / "pyproject.toml").write_text("")
    (seed / "server").mkdir()
    (seed / "server" / "requirements.txt").write_text("")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "v1")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")

    repo = tmp_path / "repo"
    _git(tmp_path, "clone", str(origin), str(repo))
    # noop pip so a "deps changed" branch never blows up
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "pip").write_text("#!/usr/bin/env bash\nexit 0\n")
    (venv_bin / "pip").chmod(0o755)
    return origin, seed, repo


def _run(repo, bindir, state, extra_env=None):
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BEACON_REPO_DIR": str(repo),
        "BEACON_SERVICE": "fake.service",
        "BEACON_HEALTH_URL": "http://fake/health",
        "BEACON_DEPLOY_BRANCH": "main",
        # keep the suite fast: no real settle/poll waits
        "BEACON_RESTART_SETTLE": "0",
        "BEACON_HEALTH_RETRIES": "3",
        "BEACON_HEALTH_INTERVAL": "0",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True, env=env,
    )


def _calls(state):
    f = state / "calls"
    return f.read_text().split() if f.exists() else []


def _advance_origin(seed, origin):
    (seed / "requirements.txt").write_text("# bump\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "v2")
    _git(seed, "push", "origin", "main")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=seed,
        capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path):
    origin, seed, repo = _setup_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    bindir = tmp_path / "bin"
    _write_shims(bindir, state)
    return origin, seed, repo, state, bindir


def test_steady_state_skips_restart(env):
    """served-rev == target and service active → no restart."""
    origin, seed, repo, state, bindir = env
    stamp = repo / ".venv" / ".beacon-deployed-rev"
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    stamp.write_text(head)
    (state / "active").touch()

    r = _run(repo, bindir, state)
    assert r.returncode == 0, r.stderr
    assert _calls(state) == [], "steady state must not restart"


def test_self_heal_restarts_when_served_rev_stale_even_though_git_unchanged(env):
    """THE e-3222 bug: git == origin/main but the running process never got
    the last restart (served-rev stamp is stale). Must restart, not skip."""
    origin, seed, repo, state, bindir = env
    # Advance origin AND fast-forward repo HEAD to it (git looks up-to-date)...
    new_rev = _advance_origin(seed, origin)
    _git(repo, "fetch", "origin", "main")
    _git(repo, "reset", "--hard", "origin/main")
    # ...but the served-rev stamp is still the OLD rev (prior restart failed).
    (repo / ".venv" / ".beacon-deployed-rev").write_text("0" * 40)
    (state / "active").touch()
    (state / "health_ok").touch()

    r = _run(repo, bindir, state)
    assert r.returncode == 0, r.stderr
    assert _calls(state) == ["restart"], (
        "stale served-rev must force a restart even when git is unchanged "
        "(self-heal); got calls=%r\n%s" % (_calls(state), r.stdout)
    )
    assert (repo / ".venv" / ".beacon-deployed-rev").read_text().strip() == new_rev


def test_failed_health_does_not_stamp_so_next_tick_retries(env):
    """A restart whose health check fails must NOT record the served-rev, so
    the next tick sees a stale stamp and retries (the anti-stuck invariant)."""
    origin, seed, repo, state, bindir = env
    _advance_origin(seed, origin)
    (state / "active").touch()
    # health_ok intentionally absent → curl fails

    r = _run(repo, bindir, state)
    assert r.returncode == 1, "health failure must exit non-zero"
    assert _calls(state) == ["restart"]
    assert not (repo / ".venv" / ".beacon-deployed-rev").exists(), (
        "served-rev must NOT be stamped when health check fails"
    )


def test_crashed_service_restarts_even_if_served_rev_current(env):
    """served-rev == target but service is down → restart (liveness)."""
    origin, seed, repo, state, bindir = env
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    (repo / ".venv" / ".beacon-deployed-rev").write_text(head)
    # service NOT active; health will pass after restart
    (state / "health_ok").touch()

    r = _run(repo, bindir, state)
    assert r.returncode == 0, r.stderr
    assert _calls(state) == ["restart"], "down service must be restarted"


# ---------------------------------------------------------------------------
# Auto-rollback + quarantine (ms-105 e-3231)
#
# When a new rev repeatedly fails its health check, the deploy must revert to
# the last-good rev (recorded in .beacon-deployed-rev) and *quarantine* the bad
# rev so the pull loop (origin/main is the source of truth) doesn't re-deploy it
# every tick. The quarantine clears once main advances past the bad rev.
# ---------------------------------------------------------------------------

def _head(path):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                          capture_output=True, text=True, check=True).stdout.strip()


def _advance_with(seed, origin, marker):
    """Advance origin/main with a unique commit (unique content each call)."""
    (seed / "requirements.txt").write_text(f"# {marker}\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", marker)
    _git(seed, "push", "origin", "main")
    return _head(seed)


def test_rollback_to_last_good_and_quarantine_after_threshold(env):
    """A new rev that fails health ROLLBACK_AFTER times reverts to the last-good
    rev and quarantines the bad one."""
    origin, seed, repo, state, bindir = env
    good = _head(repo)  # v1: currently deployed and healthy
    (repo / ".venv" / ".beacon-deployed-rev").write_text(good)
    (state / "healthy_revs").write_text(good + "\n")
    (state / "active").touch()
    bad = _advance_with(seed, origin, "v2-bad")  # not in healthy_revs

    r = _run(repo, bindir, state, {"BEACON_ROLLBACK_AFTER": "1"})

    assert r.returncode == 1, r.stdout  # the target deploy failed (box recovered)
    # one restart for the bad rev, one for the rollback to last-good
    assert _calls(state) == ["restart", "restart"], r.stdout
    quar = repo / ".venv" / ".beacon-quarantine-rev"
    assert quar.read_text().strip() == bad, "bad rev must be quarantined"
    assert (repo / ".venv" / ".beacon-deployed-rev").read_text().strip() == good, (
        "box must be serving the last-good rev after rollback"
    )
    assert _head(repo) == good, "working tree rolled back to last-good"


def test_below_threshold_does_not_rollback(env):
    """A single health failure (below the threshold) retries next tick instead
    of rolling back — flaky one-offs must not churn."""
    origin, seed, repo, state, bindir = env
    good = _head(repo)
    (repo / ".venv" / ".beacon-deployed-rev").write_text(good)
    (state / "healthy_revs").write_text(good + "\n")
    (state / "active").touch()
    _advance_with(seed, origin, "v2-bad")

    r = _run(repo, bindir, state, {"BEACON_ROLLBACK_AFTER": "3"})

    assert r.returncode == 1
    assert _calls(state) == ["restart"], "one failed attempt, no rollback yet"
    assert not (repo / ".venv" / ".beacon-quarantine-rev").exists(), (
        "nothing quarantined below the threshold"
    )
    assert (repo / ".venv" / ".beacon-deployed-rev").read_text().strip() == good, (
        "served-rev unchanged (bad rev health-failed, no stamp)"
    )


def test_quarantine_holds_last_good_without_redeploying_bad_rev(env):
    """Once a bad rev is quarantined and main still points at it, subsequent
    ticks hold the last-good rev and never re-deploy the bad one."""
    origin, seed, repo, state, bindir = env
    good = _head(repo)
    bad = _advance_with(seed, origin, "v2-bad")  # origin/main now at bad
    # post-rollback held state: serving good, bad quarantined, HEAD still good
    (repo / ".venv" / ".beacon-deployed-rev").write_text(good)
    (repo / ".venv" / ".beacon-quarantine-rev").write_text(bad)
    (state / "healthy_revs").write_text(good + "\n")
    (state / "active").touch()

    r = _run(repo, bindir, state, {"BEACON_ROLLBACK_AFTER": "1"})

    assert r.returncode == 0, r.stdout
    assert _calls(state) == [], "must NOT redeploy the quarantined bad rev"
    assert (repo / ".venv" / ".beacon-quarantine-rev").read_text().strip() == bad
    assert _head(repo) == good


def test_quarantine_clears_when_main_advances_past_bad_rev(env):
    """When main advances past the quarantined bad rev (someone fixed it), the
    quarantine clears and the new fixed rev deploys normally."""
    origin, seed, repo, state, bindir = env
    good = _head(repo)
    bad = _advance_with(seed, origin, "v2-bad")
    (repo / ".venv" / ".beacon-deployed-rev").write_text(good)
    (repo / ".venv" / ".beacon-quarantine-rev").write_text(bad)
    (state / "active").touch()
    fixed = _advance_with(seed, origin, "v3-fixed")  # main moves past the bad rev
    (state / "healthy_revs").write_text("\n".join([good, fixed]) + "\n")

    r = _run(repo, bindir, state, {"BEACON_ROLLBACK_AFTER": "1"})

    assert r.returncode == 0, r.stdout
    assert _calls(state) == ["restart"], "should deploy the new fixed rev"
    quar = repo / ".venv" / ".beacon-quarantine-rev"
    assert (not quar.exists()) or quar.read_text().strip() == "", "quarantine cleared"
    assert (repo / ".venv" / ".beacon-deployed-rev").read_text().strip() == fixed
