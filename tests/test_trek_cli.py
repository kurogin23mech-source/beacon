"""CLI smoke tests for `beacon trek` (ms-69 / e-1653).

Spawns bin/beacon as a subprocess, points trek storage at a tmp dir via
BEACON_TREKS_DIR, exercises create → list → show → start → archive →
listing semantics + transition rejection.

These tests confirm:
- bash wiring → commands.py dispatcher → trek_store round trip
- creator identity gating (email + session_id required)
- list visibility filter (actor-scoped vs --all-actors)
- archived hides by default, --all surfaces it
- terminal archived state rejects start
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BEACON = REPO_ROOT / "bin" / "beacon"


def _run(env_extra: dict, *args: str) -> subprocess.CompletedProcess:
    """Run beacon CLI in an isolated cwd (= the tmp project dir from the
    fixture) so ``bin/beacon`` doesn't walk up to a parent repo's cloud
    config and flip ``_is_cloud_mode()`` to True (e-1681 regression).

    The fixture stamps ``BEACON_CWD`` into env_extra; we pop it out and
    hand it to ``subprocess.run(cwd=)``.
    """
    env = os.environ.copy()
    env.update(env_extra)
    cwd = env.pop("BEACON_CWD", None)
    return subprocess.run(
        [str(BEACON), "trek", *args],
        env=env, capture_output=True, text=True, cwd=cwd,
    )


@pytest.fixture
def trek_env(tmp_path):
    """Base env with BEACON_TREKS_DIR pointing at a per-test tmp dir.

    The fixture also creates a minimal .beacon/project.json INSIDE
    ``tmp_path`` and asks ``_run`` to launch the subprocess with cwd at
    that tmp_path. This way ``bin/beacon``'s ``find_beacon_root`` walk
    settles on the tmp project (which has no cloud config) instead of
    the parent repo's worktree, which can have cloud mode set globally.

    BEACON_PROJECT_FILE is intentionally NOT passed in env_extra because
    ``bin/beacon`` line 7 unconditionally re-exports it to the relative
    ``.beacon/project.json``; the cwd-based isolation is the only path
    that actually shields the test from parent cloud config.
    """
    treks_dir = tmp_path / "treks"
    project_file = tmp_path / ".beacon" / "project.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text('{"name":"test","milestones":[],"operations":[]}\n')
    # ms-61 / e-2132 — isolate ~/.beacon/ via BEACON_HOME so the credentials
    # auto-read fallback (_resolve_creator_identity) doesn't pick up the host
    # developer's real login. Tests that assert env-removal-hard-errors stay
    # valid; tests that pass env continue to work (= env wins regardless).
    fake_beacon_home = tmp_path / "fake-beacon-home"
    fake_beacon_home.mkdir()
    return {
        "BEACON_TREKS_DIR": str(treks_dir),
        "BEACON_USER_ID": "u-test",
        "BEACON_USER_EMAIL": "test@example.com",
        "BEACON_SESSION_ID": "sv-test-1",
        "BEACON_CWD": str(tmp_path),
        "BEACON_HOME": str(fake_beacon_home),
        # ms-88 / e-2090 — bypass typed-ack gate for subprocess fixtures.
        # 個別の gate behavior は test_trek_join_consent_gate_* で別途検証する。
        "BEACON_TREK_CONSENT_ACK": "1",
    }


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_trek_create_basic(trek_env):
    r = _run(trek_env, "create", "My Trek", "--type", "temporary", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["title"] == "My Trek"
    assert doc["type"] == "temporary"
    assert doc["status"] == "planning"
    assert doc["leader_session_id"] == "sv-test-1"
    assert doc["creator_actor"] == {"user_id": "u-test", "email": "test@example.com"}
    assert doc["halt"] is None


def test_trek_create_requires_email(trek_env, tmp_path):
    """ms-61 / e-2132: env も credentials.json も無い時のみ email error。

    旧テストは env 削除だけで hard error を assert していたが、 ms-61 fix で
    credentials.json auto-read fallback が入ったので、 真の error 条件は
    「env + credentials の両方とも欠落」 になった。 HOME を tmp に向けて
    credentials の fallback も無効化する。
    """
    env = dict(trek_env)
    env.pop("BEACON_USER_EMAIL")
    # Isolate HOME so the auto-read fallback can't pick up the host's
    # ~/.beacon/credentials.json (= ms-61 / e-2132 のテスト前提)。
    fake_home = tmp_path / "no-credentials-home"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)
    r = _run(env, "create", "x", "--json")
    assert r.returncode != 0
    assert "EMAIL" in r.stderr


def test_trek_create_requires_session_id(trek_env):
    env = dict(trek_env)
    env.pop("BEACON_SESSION_ID")
    r = _run(env, "create", "x", "--json")
    assert r.returncode != 0
    assert "SESSION_ID" in r.stderr or "session" in r.stderr.lower()


def test_trek_create_default_type_is_persistent(trek_env):
    r = _run(trek_env, "create", "x", "--json")
    assert r.returncode == 0
    assert json.loads(r.stdout)["type"] == "persistent"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_trek_list_empty(trek_env):
    r = _run(trek_env, "list", "--json")
    assert r.returncode == 0
    assert json.loads(r.stdout) == []


def test_trek_list_returns_created(trek_env):
    _run(trek_env, "create", "T1", "--json")
    _run(trek_env, "create", "T2", "--json")
    r = _run(trek_env, "list", "--json")
    assert r.returncode == 0
    titles = {t["title"] for t in json.loads(r.stdout)}
    assert titles == {"T1", "T2"}


def test_trek_list_actor_scoped_default(trek_env):
    """Default list filters by current actor."""
    _run(trek_env, "create", "Mine", "--json")
    env_other = dict(trek_env)
    env_other.update({
        "BEACON_USER_ID": "u-other",
        "BEACON_USER_EMAIL": "other@example.com",
    })
    _run(env_other, "create", "Theirs", "--json")
    # current user only sees their own
    r = _run(trek_env, "list", "--json")
    visible = {t["title"] for t in json.loads(r.stdout)}
    assert visible == {"Mine"}
    # --all-actors disables filter
    r_all = _run(trek_env, "list", "--all-actors", "--json")
    visible_all = {t["title"] for t in json.loads(r_all.stdout)}
    assert visible_all == {"Mine", "Theirs"}


def test_trek_list_hides_archived_by_default(trek_env):
    r = _run(trek_env, "create", "soon-archived", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    _run(trek_env, "start", tid)
    _run(trek_env, "archive", tid)
    r_default = _run(trek_env, "list", "--json")
    assert json.loads(r_default.stdout) == []
    r_all = _run(trek_env, "list", "--all", "--json")
    assert len(json.loads(r_all.stdout)) == 1


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def test_trek_show_existing(trek_env):
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    r2 = _run(trek_env, "show", tid, "--json")
    assert r2.returncode == 0
    assert json.loads(r2.stdout)["trek_id"] == tid


def test_trek_show_missing(trek_env):
    r = _run(trek_env, "show", "tk-nope", "--json")
    assert r.returncode != 0
    assert "not found" in r.stderr


# ---------------------------------------------------------------------------
# state transitions: start / archive
# ---------------------------------------------------------------------------

def test_trek_start_transitions_planning_to_active(trek_env):
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    r_start = _run(trek_env, "start", tid, "--json")
    assert r_start.returncode == 0
    assert json.loads(r_start.stdout)["status"] == "active"


def test_trek_archive_from_active(trek_env):
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    _run(trek_env, "start", tid)
    r_a = _run(trek_env, "archive", tid, "--json")
    assert r_a.returncode == 0
    doc = json.loads(r_a.stdout)
    assert doc["status"] == "archived"
    assert doc["archived_at"]


def test_trek_archive_from_planning(trek_env):
    """planning → archived should work (= cancel before start)."""
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    r_a = _run(trek_env, "archive", tid, "--json")
    assert r_a.returncode == 0
    assert json.loads(r_a.stdout)["status"] == "archived"


def test_trek_archived_is_terminal(trek_env):
    """archived → start must reject (= terminal state, SPEC 方針 2)."""
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    _run(trek_env, "start", tid)
    _run(trek_env, "archive", tid)
    r_bad = _run(trek_env, "start", tid)
    assert r_bad.returncode != 0
    assert "invalid trek transition" in r_bad.stderr.lower() or "archived" in r_bad.stderr


def test_trek_start_from_archived_planning_rejected(trek_env):
    """planning → archived → start should also reject."""
    r = _run(trek_env, "create", "T", "--json")
    tid = json.loads(r.stdout)["trek_id"]
    _run(trek_env, "archive", tid)
    r_bad = _run(trek_env, "start", tid)
    assert r_bad.returncode != 0


def test_trek_start_missing_trek(trek_env):
    r = _run(trek_env, "start", "tk-missing")
    assert r.returncode != 0
    assert "not found" in r.stderr


# ---------------------------------------------------------------------------
# invite / join / leave (e-1654)
# ---------------------------------------------------------------------------

def _make_trek_and_return_id(trek_env, title="T") -> str:
    r = _run(trek_env, "create", title, "--json")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["trek_id"]


def test_trek_invite_basic(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "invite", tid, "--actor", "b@x.com", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    emails = {m["email"] for m in doc["members"]}
    assert "b@x.com" in emails
    invitee = next(m for m in doc["members"] if m["email"] == "b@x.com")
    assert invitee["role"] == "member"
    assert invitee["joined_at"] == ""  # invited but not yet joined


def test_trek_invite_requires_actor(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "invite", tid)
    assert r.returncode != 0
    assert "actor" in r.stderr.lower() or "email" in r.stderr.lower()


def test_trek_invite_rejects_duplicate(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    r = _run(trek_env, "invite", tid, "--actor", "b@x.com")
    assert r.returncode != 0
    assert "already" in r.stderr.lower()


def test_trek_invite_notify_acknowledged_but_noop(trek_env):
    """--notify should not fail and should mention deferred implementation."""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "invite", tid, "--actor", "b@x.com", "--notify")
    assert r.returncode == 0
    assert "notify" in r.stdout.lower()  # acknowledged in human output


def test_trek_join_after_invite(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    # B joins from their own session
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r = _run(env_b, "join", tid, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    b_member = next(m for m in doc["members"] if m["email"] == "b@x.com")
    assert b_member["joined_at"]  # now joined


# ---------------------------------------------------------------------------
# Auto-arm (ms-75 / e-2047): default arms session, --no-arm opts out
# ---------------------------------------------------------------------------

def test_trek_join_auto_arms_session_by_default(trek_env):
    """beacon trek join (= without --no-arm) should add 3 trek channels to
    bus_auto_execute_channels, write .beacon/bus-budget.json with 20 turns,
    and report the arm summary in JSON output."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r = _run(env_b, "join", tid, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    arm = doc.get("_arm")
    assert arm is not None
    assert arm["trek_id"] == tid
    assert arm["budget_turns"] == 20
    # All 4 trek channels must be in the post-arm allowlist (= ms-92 / e-2164
    # added trek-leader-digest as the 4th alongside the 3 from ms-75 / e-2047).
    expected = {
        "trek-progress-check", "trek-trigger", "trek-task-review",
        "trek-leader-digest",
    }
    assert expected.issubset(set(arm["channels"]))
    # Four brand-new channels for the first-ever join.
    assert set(arm["channels_added"]) == expected

    # project.json reflects the channel allowlist write.
    cwd = trek_env["BEACON_CWD"]
    with open(Path(cwd) / ".beacon" / "project.json") as f:
        proj = json.load(f)
    assert expected.issubset(set(proj.get("bus_auto_execute_channels") or []))

    # bus-budget.json holds the 20-turn grant + trek_id audit marker.
    with open(Path(cwd) / ".beacon" / "bus-budget.json") as f:
        budget = json.load(f)
    assert budget["total"] == 20
    assert budget["used"] == 0
    assert budget["trek_id"] == tid


def test_trek_join_no_arm_flag_skips_auto_arm(trek_env):
    """--no-arm: trek joined, but no bus_auto_execute_channels mutation and
    no bus-budget.json write. JSON output carries _arm.skipped marker."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r = _run(env_b, "join", tid, "--no-arm", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["_arm"] == {"skipped": True, "reason": "--no-arm"}

    cwd = trek_env["BEACON_CWD"]
    with open(Path(cwd) / ".beacon" / "project.json") as f:
        proj = json.load(f)
    # No trek channels added with opt-out.
    auto_chans = set(proj.get("bus_auto_execute_channels") or [])
    assert "trek-progress-check" not in auto_chans
    assert "trek-trigger" not in auto_chans
    # No budget file (= bus-budget.json not created).
    assert not (Path(cwd) / ".beacon" / "bus-budget.json").exists()


def test_trek_join_auto_arm_is_idempotent_on_channels(trek_env):
    """Re-joining a trek (= after leave + rejoin, or a second member joining
    the same project's trek) must not duplicate channel entries. Budget
    is unconditionally refreshed though, since rejoining signals intent
    to be armed again."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r1 = _run(env_b, "join", tid, "--json")
    assert r1.returncode == 0
    # leave + re-invite + re-join to exercise the idempotent path.
    _run(env_b, "leave", tid)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    r2 = _run(env_b, "join", tid, "--json")
    assert r2.returncode == 0
    doc = json.loads(r2.stdout)
    arm = doc["_arm"]
    # Second join: channels already in allowlist → channels_added is empty.
    assert arm["channels_added"] == []
    # But the channels list still reflects the trek channels.
    expected = {"trek-progress-check", "trek-trigger", "trek-task-review"}
    assert expected.issubset(set(arm["channels"]))


def test_trek_join_auto_arm_human_output_mentions_skill_hint(trek_env):
    """Non-JSON output should tell the user to start /beacon-bus-armed
    Skill — the 3rd auto-arm action per AC 1."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    r = _run(env_b, "join", tid)
    assert r.returncode == 0, r.stderr
    assert "/beacon-bus-armed" in r.stdout
    assert "budget granted" in r.stdout
    assert "--no-arm" in r.stdout  # opt-out reminder


def test_trek_join_rejects_uninvited(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    env_stranger = dict(trek_env)
    env_stranger.update({
        "BEACON_USER_ID": "u-stranger",
        "BEACON_USER_EMAIL": "stranger@x",
    })
    r = _run(env_stranger, "join", tid)
    assert r.returncode != 0
    assert "no invitation" in r.stderr.lower() or "not invited" in r.stderr.lower()


# ---------------------------------------------------------------------------
# Consent gate (ms-88 / e-2090) — Trek 参加は autonomous loop 入場の権限委譲なので
# CLI 直叩きで無音成立しないことを構造的に担保する。
# ---------------------------------------------------------------------------

def test_trek_join_consent_gate_blocks_non_tty_without_flag(trek_env):
    """Non-TTY (= subprocess pipe) + consent_ack 未指定 → 拒否。

    pytest subprocess は非 TTY なので、 BEACON_TREK_CONSENT_ACK env を
    敢えて除外して typed-ack も flag bypass も無い状態を作る。
    """
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    # consent_ack bypass を fixture から外す
    env_b.pop("BEACON_TREK_CONSENT_ACK", None)
    r = _run(env_b, "join", tid)
    assert r.returncode != 0, (
        "non-TTY 経路は typed-ack を取れないので join を block すべき"
    )
    # explanation には participation 意味を書いてある
    assert "blanket" in r.stderr.lower() or "implications" in r.stderr.lower()


def test_trek_join_consent_gate_bypass_with_flag(trek_env):
    """--i-understand-the-implications flag が CLI から渡されたら gate を通過する。"""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    env_b.pop("BEACON_TREK_CONSENT_ACK", None)
    # flag 経路: dispatch.py が BEACON_TREK_CONSENT_ACK=1 に展開する。
    r = _run(env_b, "join", tid, "--i-understand-the-implications", "--no-arm", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    b_member = next(m for m in doc["members"] if m["email"] == "b@x.com")
    assert b_member["joined_at"]


def test_trek_join_consent_explanation_has_four_sections(trek_env):
    """ms-92 / e-2182 AC #1: consent gate explanation must carry 4 sections.

    The 4 sections (= mirror the SPEC of e-2182 + CORE doc
    trek-positioning `b1XOKXQeC0JXaKkO0CRt`):
      (a) Trek とは何か
      (b) 委譲する権限 (= concrete unlocks)
      (c) user 確認境界 (= explicit no-go area)
      (d) 撤回方法 (= beacon trek leave)

    The non-TTY path writes the full explanation to stderr and exits 1
    (= forcing function), so this test drives a subprocess pipe (=
    non-TTY) without consent_ack and asserts the 4 section headers all
    appear in stderr.
    """
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    env_b.pop("BEACON_TREK_CONSENT_ACK", None)
    r = _run(env_b, "join", tid)
    assert r.returncode != 0, "consent gate must block non-TTY without flag"
    stderr = r.stderr
    # AC #1 — 4 section headers all present
    assert "(a) Trek とは何か" in stderr, (
        "section (a) Trek とは何か must be in the explanation"
    )
    assert "(b) 参加で委譲する権限" in stderr, (
        "section (b) 委譲する権限 must be in the explanation"
    )
    assert "(c) user 確認境界" in stderr, (
        "section (c) user 確認境界 must be in the explanation"
    )
    assert "(d) 撤回方法" in stderr, (
        "section (d) 撤回方法 must be in the explanation"
    )
    # AC #2 — each section carries concrete action examples (= 1-3 行)
    # (b): concrete autonomous actions
    assert "blanket" in stderr.lower() or "自動承認" in stderr
    assert "executor" in stderr.lower() or "working" in stderr
    # (c): concrete user-confirm boundaries
    assert "deploy" in stderr.lower()
    assert "release" in stderr.lower()
    assert "merge" in stderr.lower()
    # (d): concrete leave action
    assert f"beacon trek leave {tid}" in stderr


def test_trek_join_consent_explanation_references_core_doc(trek_env):
    """ms-92 / e-2182 AC #4: vocabulary must align with CORE doc trek-positioning."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    env_b.pop("BEACON_TREK_CONSENT_ACK", None)
    r = _run(env_b, "join", tid)
    # CORE doc id (b1XOKXQeC0JXaKkO0CRt) and the 缶詰 metaphor
    assert "b1XOKXQeC0JXaKkO0CRt" in r.stderr, (
        "CORE doc trek-positioning must be referenced for vocabulary alignment"
    )
    assert "缶詰" in r.stderr, "CORE doc 缶詰 metaphor must be carried forward"
    # 3 段境界 (= e-2169) reference makes (c) section concrete
    assert "e-2169" in r.stderr


def test_trek_join_consent_flag_enforcement_unchanged(trek_env):
    """ms-92 / e-2182 AC #3: --i-understand-the-implications flag enforcement
    is preserved — only the explanation text changed, not the gate semantics.
    """
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    env_b.pop("BEACON_TREK_CONSENT_ACK", None)
    # Without the flag → blocked
    r_blocked = _run(env_b, "join", tid)
    assert r_blocked.returncode != 0
    # With the flag → passes
    r_pass = _run(env_b, "join", tid, "--i-understand-the-implications",
                  "--no-arm", "--json")
    assert r_pass.returncode == 0, r_pass.stderr


def test_trek_join_consent_gate_bypass_with_env_var(trek_env):
    """BEACON_TREK_CONSENT_ACK=1 env var 経路でも gate を通過する (= fixture default の挙動を明示テスト)。"""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
        "BEACON_TREK_CONSENT_ACK": "1",
    })
    r = _run(env_b, "join", tid, "--no-arm", "--json")
    assert r.returncode == 0, r.stderr


def test_trek_leave_removes_member(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({"BEACON_USER_ID": "u-b", "BEACON_USER_EMAIL": "b@x.com"})
    _run(env_b, "join", tid)
    r = _run(env_b, "leave", tid, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    emails = {m["email"] for m in doc["members"]}
    assert "b@x.com" not in emails


def test_trek_leave_blocks_leader(trek_env):
    """Leader cannot leave (must transfer-leader first)."""
    tid = _make_trek_and_return_id(trek_env)
    # creator is the leader; trying to leave should fail
    r = _run(trek_env, "leave", tid)
    assert r.returncode != 0
    assert "leader" in r.stderr.lower()


def test_trek_leave_non_member(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    env_stranger = dict(trek_env)
    env_stranger.update({
        "BEACON_USER_ID": "u-stranger",
        "BEACON_USER_EMAIL": "stranger@x",
    })
    r = _run(env_stranger, "leave", tid)
    assert r.returncode != 0
    assert "not a member" in r.stderr.lower()


# ---------------------------------------------------------------------------
# plan (= scope editing) (e-1655)
# ---------------------------------------------------------------------------

def test_trek_plan_add_scope_milestone(trek_env):
    """ms-97 / e-2626 AC23 — scope-add now stages a pending op.

    ``beacon trek plan --add-scope`` stages a pending record; the user
    must run ``beacon trek scope-approve <pending_id>`` to actually
    grow ``scope[]``. The json mode response shows the pending op so
    callers can chain (= mirror of the e-2611 scope-remove flip).
    """
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "beacon-1:ms-64", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    # AC23 — scope unchanged; pending op staged.
    assert {"project": "beacon-1", "milestone": "ms-64"} not in doc["scope"]
    pending = doc.get("pending_scope_ops") or []
    assert len(pending) == 1
    assert pending[0]["action"] == "scope_add"
    assert pending[0]["entry"] == {"project": "beacon-1", "milestone": "ms-64"}
    pid = pending[0]["pending_id"]
    # Approve flushes the pending into scope[].
    r2 = _run(trek_env, "scope-approve", tid, pid, "--json")
    assert r2.returncode == 0, r2.stderr
    doc2 = json.loads(r2.stdout)
    assert {"project": "beacon-1", "milestone": "ms-64"} in doc2["scope"]
    assert doc2.get("pending_scope_ops") == []


def test_trek_plan_add_scope_operation(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "pe-1:op-12", "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    # AC23 staging — not yet in scope.
    assert {"project": "pe-1", "operation": "op-12"} not in doc["scope"]
    pid = doc["pending_scope_ops"][0]["pending_id"]
    r2 = _run(trek_env, "scope-approve", tid, pid, "--json")
    doc2 = json.loads(r2.stdout)
    assert {"project": "pe-1", "operation": "op-12"} in doc2["scope"]


def test_trek_plan_add_scope_task(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "lps-1:e-1234", "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert {"project": "lps-1", "task": "e-1234"} not in doc["scope"]
    pid = doc["pending_scope_ops"][0]["pending_id"]
    r2 = _run(trek_env, "scope-approve", tid, pid, "--json")
    doc2 = json.loads(r2.stdout)
    assert {"project": "lps-1", "task": "e-1234"} in doc2["scope"]


def test_trek_plan_add_scope_project_wide_rejected(trek_env):
    """ms-97 / e-2659 (AC7 CLI layer): project-wide add is now rejected.

    Pre-AC7 the CLI accepted ``--add-scope lps-1`` (= no `:<ref>`) and
    staged a pending op for a project-wide entry. With AC7 the CLI's
    ``parse_scope_arg`` runs in strict mode by default, so this case
    bails out before any cloud call with a clear ``narrowing key`` hint.
    """
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "lps-1", "--json")
    assert r.returncode != 0, r.stdout
    assert "narrowing key" in r.stderr, r.stderr


def _inject_legacy_project_wide_scope(trek_env, tid: str, project: str) -> None:
    """Splice a legacy project-wide row into a trek doc on disk.

    ms-97 / e-2659 (AC8): the strict write path forbids project-wide
    scope adds, but grandfathered rows still exist on disk for older
    treks. Tests for the CLI warning surface must seed such a row
    without going through the strict CLI / server gates — we patch the
    JSON file in ``BEACON_TREKS_DIR`` directly.
    """
    import json as _json
    import os as _os
    path = _os.path.join(trek_env["BEACON_TREKS_DIR"], f"{tid}.json")
    with open(path, "r", encoding="utf-8") as f:
        doc = _json.load(f)
    doc.setdefault("scope", []).append({"project": project})
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(doc, f, ensure_ascii=False, indent=2)


def test_trek_show_warns_on_legacy_project_wide_scope(trek_env):
    """ms-97 / e-2659 (AC8 + AC31): trek show flags grandfathered rows."""
    tid = _make_trek_and_return_id(trek_env)
    _inject_legacy_project_wide_scope(trek_env, tid, "lps-1")
    r = _run(trek_env, "show", tid)
    assert r.returncode == 0, r.stderr
    assert "Project-wide scope entries detected" in r.stdout
    assert "lps-1" in r.stdout
    # The remediation hint must point at the actual CLI verb.
    assert "--add-scope" in r.stdout


def test_trek_show_no_warning_when_all_narrowed(trek_env):
    """No warning bar when scope[] is fully narrowed."""
    tid = _make_trek_and_return_id(trek_env)
    _stage_and_approve_add(trek_env, tid, "beacon-1:ms-64")
    r = _run(trek_env, "show", tid)
    assert r.returncode == 0, r.stderr
    assert "Project-wide scope entries detected" not in r.stdout


def test_trek_list_warns_on_legacy_project_wide_scope(trek_env):
    """ms-97 / e-2659 (AC8 + AC31): trek list summarises grandfathered rows."""
    tid = _make_trek_and_return_id(trek_env)
    _inject_legacy_project_wide_scope(trek_env, tid, "lps-1")
    r = _run(trek_env, "list")
    assert r.returncode == 0, r.stderr
    assert "[⚠ 1 project-wide]" in r.stdout
    assert "Project-wide scope entries detected" in r.stdout
    assert f"{tid}: lps-1" in r.stdout


def _stage_and_approve_add(trek_env, tid, ref):
    """Helper: run plan --add-scope <ref> + scope-approve to commit.

    ms-97 / e-2626 — scope-add now stages a pending op (AC23). Tests
    that historically did setup with a single ``--add-scope`` call must
    chain through scope-approve to actually grow ``scope[]``. This
    helper hides that two-step dance for setup-only call sites.
    """
    r = _run(trek_env, "plan", tid, "--add-scope", ref, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    pid = doc["pending_scope_ops"][-1]["pending_id"]
    r2 = _run(trek_env, "scope-approve", tid, pid, "--json")
    assert r2.returncode == 0, r2.stderr


# ---------------------------------------------------------------------------
# ms-97 Phase 5 (AC23 / AC25) — canonical scope-add / scope-approve /
# scope-reject verb subparser wirings. The verbs land alongside the existing
# `plan --add-scope` flow; the staging machinery is shared (cmd_trek_plan).
# ---------------------------------------------------------------------------

def test_trek_scope_add_canonical_verb_milestone(trek_env):
    """ms-97 / AC23 — `beacon trek scope-add --project P --milestone M`
    is the canonical verb. It stages a pending op equivalent to
    ``plan --add-scope P:M``.
    """
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "scope-add", tid,
             "--project", "beacon-1", "--milestone", "ms-64", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    # Staged, not committed.
    assert {"project": "beacon-1", "milestone": "ms-64"} not in doc["scope"]
    pending = doc.get("pending_scope_ops") or []
    assert len(pending) == 1
    assert pending[0]["action"] == "scope_add"
    assert pending[0]["entry"] == {"project": "beacon-1", "milestone": "ms-64"}


def test_trek_scope_add_canonical_verb_task(trek_env):
    """ms-97 / AC23 — --task / --e narrowing key variant works."""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "scope-add", tid,
             "--project", "lps-1", "--task", "e-1234", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    pending = doc.get("pending_scope_ops") or []
    assert len(pending) == 1
    assert pending[0]["entry"] == {"project": "lps-1", "task": "e-1234"}


def test_trek_scope_add_requires_narrowing_key(trek_env):
    """ms-97 / AC7+AC23 — bare project-wide scope-add must be rejected
    by the CLI flag-validation layer (= no narrowing key passed).
    """
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "scope-add", tid, "--project", "lps-1", "--json")
    assert r.returncode != 0, r.stdout
    # Either the CLI flag-layer or the downstream parse_scope_arg surfaces
    # the rejection — accept either wording for forward compatibility.
    msg = r.stderr or r.stdout
    assert "milestone" in msg or "narrowing key" in msg or "operation" in msg


def test_trek_scope_add_requires_project(trek_env):
    """ms-97 / AC23 — --project is required."""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "scope-add", tid, "--milestone", "ms-64", "--json")
    assert r.returncode != 0, r.stdout
    assert "--project" in (r.stderr or r.stdout)


def test_trek_scope_approve_canonical_verb_commits_pending(trek_env):
    """ms-97 / AC25 — `beacon trek scope-approve <trek-id> <pending-id>`
    flushes a staged scope op into ``scope[]``.
    """
    tid = _make_trek_and_return_id(trek_env)
    # Stage via the canonical verb.
    r = _run(trek_env, "scope-add", tid,
             "--project", "beacon-1", "--milestone", "ms-64", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    pid = doc["pending_scope_ops"][0]["pending_id"]
    # Approve.
    r2 = _run(trek_env, "scope-approve", tid, pid, "--json")
    assert r2.returncode == 0, r2.stderr
    doc2 = json.loads(r2.stdout)
    assert {"project": "beacon-1", "milestone": "ms-64"} in doc2["scope"]
    assert doc2.get("pending_scope_ops") == []


def test_trek_scope_reject_canonical_verb_drops_pending(trek_env):
    """ms-97 / AC25 — `beacon trek scope-reject <trek-id> <pending-id>`
    drops a staged op without applying it.
    """
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "scope-add", tid,
             "--project", "beacon-1", "--milestone", "ms-64", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    pid = doc["pending_scope_ops"][0]["pending_id"]
    # Reject.
    r2 = _run(trek_env, "scope-reject", tid, pid, "--json")
    assert r2.returncode == 0, r2.stderr
    doc2 = json.loads(r2.stdout)
    # Scope unchanged, pending op gone.
    assert {"project": "beacon-1", "milestone": "ms-64"} not in doc2["scope"]
    assert doc2.get("pending_scope_ops") == []


def test_trek_plan_remove_scope(trek_env):
    """ms-97 / e-2611 AC25 — scope-remove now stages a pending op.

    The CLI ``beacon trek plan ... --remove-scope`` stages a pending
    record; the user must run ``beacon trek scope-approve <pending_id>``
    to actually shrink ``scope[]``. The json mode response includes
    the pending op so callers can chain.

    AC23 (e-2626) made scope-add also stage; setup steps below chain
    through ``_stage_and_approve_add`` so the trek's ``scope[]`` is
    actually populated before the remove-side flow under test.
    """
    # ms-97 / e-2659 (AC7): both setup adds must carry narrowing keys
    # so they survive the strict-mode parse layer. The remove path is
    # what's actually under test here.
    tid = _make_trek_and_return_id(trek_env)
    _stage_and_approve_add(trek_env, tid, "beacon-1:ms-64")
    _stage_and_approve_add(trek_env, tid, "pe-1:ms-5")
    r = _run(trek_env, "plan", tid, "--remove-scope", "beacon-1:ms-64",
             "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    # AC25 — scope still has both entries; the pending op was staged.
    assert {"project": "beacon-1", "milestone": "ms-64"} in doc["scope"]
    assert {"project": "pe-1", "milestone": "ms-5"} in doc["scope"]
    pending = doc.get("pending_scope_ops") or []
    assert len(pending) == 1
    assert pending[0]["action"] == "scope_remove"
    assert pending[0]["entry"] == {"project": "beacon-1", "milestone": "ms-64"}
    pid = pending[0]["pending_id"]
    # Approve flushes the pending into scope[].
    r2 = _run(trek_env, "scope-approve", tid, pid, "--json")
    assert r2.returncode == 0, r2.stderr
    doc2 = json.loads(r2.stdout)
    assert doc2["scope"] == [{"project": "pe-1", "milestone": "ms-5"}]
    assert doc2.get("pending_scope_ops") == []


def test_trek_plan_requires_add_or_remove(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid)
    assert r.returncode != 0
    assert "add-scope" in r.stderr or "remove-scope" in r.stderr


def test_trek_plan_rejects_both_add_and_remove(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid,
             "--add-scope", "a:ms-1",
             "--remove-scope", "a:ms-1")
    assert r.returncode != 0
    assert "not both" in r.stderr.lower() or "one" in r.stderr.lower()


def test_trek_plan_add_scope_rejects_duplicate(trek_env):
    """ms-97 / e-2626 — duplicate-add still rejected, now at stage-time.

    Stage + approve the first add, then a second --add-scope of the
    same ref must fail with an "already present" error (= 409 on the
    HTTP side, exit code != 0 on the CLI side).
    """
    tid = _make_trek_and_return_id(trek_env)
    _stage_and_approve_add(trek_env, tid, "beacon-1:ms-64")
    r = _run(trek_env, "plan", tid, "--add-scope", "beacon-1:ms-64")
    assert r.returncode != 0
    assert "already" in r.stderr.lower()


def test_trek_plan_remove_scope_missing(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--remove-scope", "beacon-1:ms-64")
    assert r.returncode != 0
    assert "not found" in r.stderr.lower()


def test_trek_plan_unknown_ref_prefix(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "plan", tid, "--add-scope", "beacon-1:foo-99")
    assert r.returncode != 0
    assert "unknown" in r.stderr.lower() or "ref" in r.stderr.lower()


# ---------------------------------------------------------------------------
# stop / resume / transfer-leader (e-1662)
# ---------------------------------------------------------------------------

def test_trek_stop_sets_halt(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "start", tid)
    r = _run(trek_env, "stop", tid, "--reason", "deploy in progress", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["status"] == "active"  # status unchanged
    assert doc["halt"]
    assert doc["halt"]["issued_by_session_id"] == "sv-test-1"
    assert doc["halt"]["reason"] == "deploy in progress"


def test_trek_stop_rejects_planning(trek_env):
    """Cannot stop a trek that hasn't started."""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "stop", tid)
    assert r.returncode != 0
    assert "active" in r.stderr.lower()


def test_trek_stop_requires_session_id(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "start", tid)
    env = dict(trek_env)
    env.pop("BEACON_SESSION_ID")
    r = _run(env, "stop", tid)
    assert r.returncode != 0
    assert "session" in r.stderr.lower()


def test_trek_resume_clears_halt(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "start", tid)
    _run(trek_env, "stop", tid)
    r = _run(trek_env, "resume", tid, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["halt"] is None
    assert doc["status"] == "active"


def test_trek_resume_idempotent(trek_env):
    """Resuming an unhalted trek is a no-op (= no error)."""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "start", tid)
    r = _run(trek_env, "resume", tid)
    assert r.returncode == 0
    assert "not halted" in r.stdout.lower() or "no-op" in r.stdout.lower()


def test_trek_transfer_leader_basic(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "transfer-leader", tid, "--to", "sv-new-leader", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["leader_session_id"] == "sv-new-leader"


def test_trek_transfer_leader_requires_to(trek_env):
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "transfer-leader", tid)
    assert r.returncode != 0
    assert "to" in r.stderr.lower() or "session" in r.stderr.lower()


# ---------------------------------------------------------------------------
# take-over (ms-88 / e-2089) — fresh session leader recovery path
# ---------------------------------------------------------------------------

def test_trek_take_over_rebinds_leader_session_to_fresh_session(trek_env):
    """同 user / 別 session で take-over すると leader_session_id が新 session に bind し直る。"""
    tid = _make_trek_and_return_id(trek_env)

    # Fresh session of the same user (= dogfood の Mac restart 後シナリオ)
    fresh_env = dict(trek_env)
    fresh_env["BEACON_SESSION_ID"] = "sv-fresh-leader"
    r = _run(fresh_env, "take-over", tid, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["leader_session_id"] == "sv-fresh-leader"
    assert doc.get("_take_over", {}).get("new_leader_session_id") == "sv-fresh-leader"


def test_trek_take_over_rejects_non_leader_member(trek_env):
    """role=member の参加者は take-over できない (leader 専用)。"""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "invite", tid, "--actor", "b@x.com")
    env_b = dict(trek_env)
    env_b.update({
        "BEACON_USER_ID": "u-b",
        "BEACON_USER_EMAIL": "b@x.com",
        "BEACON_SESSION_ID": "sv-b-1",
    })
    _run(env_b, "join", tid)  # b joins as role="member"
    r = _run(env_b, "take-over", tid)
    assert r.returncode != 0
    assert "leader" in r.stderr.lower()


def test_trek_take_over_rejects_non_member(trek_env):
    """member ですらない user は take-over 不可。"""
    tid = _make_trek_and_return_id(trek_env)
    stranger_env = dict(trek_env)
    stranger_env.update({
        "BEACON_USER_ID": "u-stranger",
        "BEACON_USER_EMAIL": "stranger@x.com",
        "BEACON_SESSION_ID": "sv-stranger",
    })
    r = _run(stranger_env, "take-over", tid)
    assert r.returncode != 0
    assert "member" in r.stderr.lower()


def test_trek_take_over_is_idempotent_when_already_bound(trek_env):
    """同 session で再 take-over → exit 0、 既 bind を保持。"""
    tid = _make_trek_and_return_id(trek_env)
    r1 = _run(trek_env, "take-over", tid, "--json")
    assert r1.returncode == 0, r1.stderr
    # trek_env の BEACON_SESSION_ID は sv-test-1 → 既に leader として bind 済
    r2 = _run(trek_env, "take-over", tid, "--json")
    assert r2.returncode == 0, r2.stderr
    assert "sv-test-1" in r2.stdout or "no-op" in r2.stdout.lower()


def test_trek_take_over_requires_session_id(trek_env):
    """BEACON_SESSION_ID 未設定で take-over → エラー。"""
    tid = _make_trek_and_return_id(trek_env)
    env_no_sid = dict(trek_env)
    env_no_sid.pop("BEACON_SESSION_ID", None)
    r = _run(env_no_sid, "take-over", tid)
    assert r.returncode != 0
    assert "session" in r.stderr.lower()


# ---------------------------------------------------------------------------
# kickoff (ms-88 / e-2138 + e-2139 #1 CLI wrapper) — Kickoff Ritual stamp
#
# PR #177 で server endpoint + schema は land 済、 ここでは CLI wrapper の
# round-trip を pin する (= /beacon-trek-pulse Step 0.4 が呼ぶ経路の前提)。
# ---------------------------------------------------------------------------

def test_trek_kickoff_stamps_session_as_completed(trek_env):
    """kickoff CLI で local mode で stamp すると kickoff_status に pending=false が乗る。"""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "kickoff", tid,
             "--kickoff-dm-event-id", "evt-test-kickoff", "--json")
    assert r.returncode == 0, r.stderr
    entry = json.loads(r.stdout)
    # creator session (= sv-test-1 in trek_env fixture) が stamp 対象になる
    assert entry["session_id"] == "sv-test-1"
    assert entry["pending"] is False
    assert entry["sent_at"]  # non-empty ISO
    assert entry["kickoff_dm_event_id"] == "evt-test-kickoff"


def test_trek_kickoff_session_id_override_via_flag(trek_env):
    """`--session-id` flag が env を上書きして別 session の stamp ができる。"""
    tid = _make_trek_and_return_id(trek_env)
    # creator は trek_env BEACON_SESSION_ID = sv-test-1。 別 session を override
    # で渡す (= dogfood で executor が leader 経路で別 session を stamp する想定)。
    r = _run(trek_env, "kickoff", tid,
             "--session-id", "sv-other-session",
             "--kickoff-dm-event-id", "evt-other", "--json")
    assert r.returncode == 0, r.stderr
    entry = json.loads(r.stdout)
    assert entry["session_id"] == "sv-other-session"
    assert entry["pending"] is False


def test_trek_kickoff_is_idempotent(trek_env):
    """同 session で 2 度 kickoff → 2 度目も exit 0、 既存 sent_at 保持。"""
    tid = _make_trek_and_return_id(trek_env)
    r1 = _run(trek_env, "kickoff", tid, "--json")
    assert r1.returncode == 0
    first_sent_at = json.loads(r1.stdout)["sent_at"]
    r2 = _run(trek_env, "kickoff", tid, "--json")
    assert r2.returncode == 0, r2.stderr
    # idempotent — first stamp is preserved (= mark_kickoff_completed の契約)
    assert json.loads(r2.stdout)["sent_at"] == first_sent_at


def test_trek_kickoff_rejects_non_member(trek_env):
    """member ですらない user が kickoff → エラー (member 限定)。"""
    tid = _make_trek_and_return_id(trek_env)
    stranger_env = dict(trek_env)
    stranger_env.update({
        "BEACON_USER_ID": "u-stranger",
        "BEACON_USER_EMAIL": "stranger@x.com",
        "BEACON_SESSION_ID": "sv-stranger",
    })
    r = _run(stranger_env, "kickoff", tid)
    assert r.returncode != 0
    assert "member" in r.stderr.lower()


def test_trek_kickoff_requires_session_id(trek_env):
    """BEACON_SESSION_ID 未設定 + --session-id 未指定で kickoff → エラー。"""
    tid = _make_trek_and_return_id(trek_env)
    env_no_sid = dict(trek_env)
    env_no_sid.pop("BEACON_SESSION_ID", None)
    r = _run(env_no_sid, "kickoff", tid)
    assert r.returncode != 0
    assert "session" in r.stderr.lower()


# ---------------------------------------------------------------------------
# pulse-ack (ms-88 / e-2106) — Layer 2 observability self-report
# ---------------------------------------------------------------------------

def test_trek_pulse_ack_records_invocation(trek_env):
    """pulse-ack を打つと total_acks が増え、 last_picked_choice が記録される。"""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "pulse-ack", tid, "--picked-choice", "continue",
             "--note", "self-test", "--json")
    assert r.returncode == 0, r.stderr
    entry = json.loads(r.stdout)
    assert entry["session_id"] == "sv-test-1"
    assert entry["total_acks"] == 1
    assert entry["last_picked_choice"] == "continue"
    assert len(entry["history"]) == 1
    assert entry["history"][0]["picked_choice"] == "continue"
    assert entry["history"][0]["note"] == "self-test"


def test_trek_pulse_ack_increments_total_on_repeated_call(trek_env):
    """同 session で 2 回 ack → total_acks=2 + history 2 件。"""
    tid = _make_trek_and_return_id(trek_env)
    _run(trek_env, "pulse-ack", tid, "--picked-choice", "continue")
    r = _run(trek_env, "pulse-ack", tid, "--picked-choice", "no-op", "--json")
    assert r.returncode == 0, r.stderr
    entry = json.loads(r.stdout)
    assert entry["total_acks"] == 2
    assert entry["last_picked_choice"] == "no-op"
    assert len(entry["history"]) == 2


def test_trek_pulse_ack_rejects_invalid_picked_choice(trek_env):
    """validate_pulse_picked_choice が known token のみ受け入れる。"""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "pulse-ack", tid, "--picked-choice", "bogus")
    assert r.returncode != 0
    assert "picked_choice" in r.stderr.lower() or "expected one of" in r.stderr.lower()


def test_trek_pulse_ack_allows_empty_picked_choice(trek_env):
    """picked_choice 省略 (= 空文字) は許可 (= 最小情報 ack)。"""
    tid = _make_trek_and_return_id(trek_env)
    r = _run(trek_env, "pulse-ack", tid, "--json")
    assert r.returncode == 0, r.stderr
    entry = json.loads(r.stdout)
    assert entry["total_acks"] == 1
    assert entry["last_picked_choice"] == ""


def test_trek_pulse_ack_caps_note_at_200_chars(trek_env):
    """200 文字 cap で長文を切り詰める (= history 肥大防止)。"""
    tid = _make_trek_and_return_id(trek_env)
    long_note = "x" * 500
    r = _run(trek_env, "pulse-ack", tid, "--picked-choice", "continue",
             "--note", long_note, "--json")
    assert r.returncode == 0, r.stderr
    entry = json.loads(r.stdout)
    assert len(entry["history"][0]["note"]) == 200
