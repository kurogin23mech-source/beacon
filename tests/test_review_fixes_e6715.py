"""Independent-review findings on PR #776, and the guards that keep them fixed
(ms-160 e-6715).

Three findings were confirmed empirically before being accepted:

  M-1 (high, maintainability) — `beacon note purge-probes` had no branch in
      beacon_cli/dispatch.py, the second CLI frontend (Windows/pipx). `note`
      dispatches sub-verbs by hand, so the token did not raise `invalid choice`:
      it fell through to the free-text arm and was SAVED AS A NOTE named
      "purge-probes", exit 0. That is this task's own defect, reproduced on the
      one frontend the fix did not reach. Neither CI nor the cli-drift guard saw
      it, because the parity checker only compares argparse-subparser-backed
      nouns and `note` is not one.

  A-1 (low, AX) — bash accepted `--confirm|-y|--yes` while the usage line and
      the help registry named only `--confirm`. The sibling `note clear`
      documents the full alias set, so the two descriptions disagreed about the
      same concept.

  A-2 (low, AX) — the probe inherited the parent's environment wholesale, so an
      ambient BEACON_HELP_ONLY would win the reason priority in active_reason()
      and the probe would be refused as *help* (exit 0, stdout). The collector
      then recorded silent_no_op=True for a command it had actually refused —
      the audit fabricating the very defect it exists to find.

Each guard below is paired with a test that breaks the fix and asserts the
guard notices; a guard nobody has seen fail is not evidence of anything.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

import readonly_gate  # noqa: E402

SENTINEL_NOTE = json.dumps({"ts": "2026-10-01T10:00:00+0900",
                            "text": "__ax_surface_probe__"}) + "\n"
REAL_NOTE = json.dumps({"ts": "2026-10-01T10:01:00+0900", "text": "real"}) + "\n"


def _drift_module():
    spec = importlib.util.spec_from_file_location(
        "drift_check", ROOT / "scripts" / "check-cli-help-drift.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def project(tmp_path, monkeypatch):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text('{"name":"t","milestones":[]}', encoding="utf-8")
    notes = beacon / "session_notes.jsonl"
    notes.write_text(SENTINEL_NOTE + REAL_NOTE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path, notes


# --- M-1: the second CLI frontend must route the sub-verb ------------------

def test_python_frontend_runs_purge_probes_instead_of_saving_a_note(project):
    """The original defect: the token became a note. Drive the Python frontend
    (not bin/beacon) so a bash-only fix cannot make this pass."""
    cwd, notes = project
    from beacon_cli import dispatch
    rc = dispatch.dispatch(ROOT, ["note", "purge-probes", "--confirm"])
    assert rc == 0
    texts = [json.loads(l)["text"]
             for l in notes.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert "purge-probes" not in texts, (
        "the sub-verb was saved as a note instead of being executed: " + repr(texts))
    assert texts == ["real"], texts


def test_python_frontend_purge_probes_is_dry_run_without_confirm(project):
    """The confirmation contract must hold on BOTH frontends, not just bash."""
    cwd, notes = project
    before = notes.read_text(encoding="utf-8")
    from beacon_cli import dispatch
    assert dispatch.dispatch(ROOT, ["note", "purge-probes"]) == 0
    assert notes.read_text(encoding="utf-8") == before


# --- M-1 recurrence guard: the parity checker's blind spot is now covered ---

def test_hand_dispatch_parity_is_clean_today():
    report = _drift_module().collect_hand_dispatch_subverb_drift()
    assert report["ok"], report["missing_from_python_hand_dispatch"]


def test_hand_dispatch_guard_goes_red_when_a_branch_is_removed(tmp_path):
    """test-the-test: delete the purge-probes branch from a COPY of dispatch.py
    and confirm the comparator reports it. Without this, a guard that silently
    matched nothing would look identical to a guard that found nothing wrong."""
    mod = _drift_module()
    src = mod.PYTHON_DISPATCH.read_text(encoding="utf-8")
    broken = src.replace('    if sub == "purge-probes":', '    if sub == "__never__":', 1)
    assert broken != src, "the branch this test breaks has been renamed"
    copy = tmp_path / "dispatch.py"
    copy.write_text(broken, encoding="utf-8")
    seen = mod.python_hand_dispatched_sub_verbs(copy, nouns={"note"})["note"]
    assert "purge-probes" not in seen
    intact = mod.python_hand_dispatched_sub_verbs(mod.PYTHON_DISPATCH, nouns={"note"})["note"]
    assert "purge-probes" in intact


def test_hand_dispatch_scan_is_structural_not_substring(tmp_path):
    """A sub-verb named only in prose (docstring / usage text) must NOT count as
    routed, or the guard goes green on documentation."""
    mod = _drift_module()
    fake = tmp_path / "dispatch.py"
    fake.write_text(
        'def _handle_note(root, args):\n'
        '    """Usage: beacon note ghostverb — documented but not routed."""\n'
        '    print("try: beacon note ghostverb")\n'
        '    if sub == "real":\n'
        '        return 0\n',
        encoding="utf-8")
    seen = mod.python_hand_dispatched_sub_verbs(fake, nouns={"note"})["note"]
    assert seen == {"real"}, seen


# --- A-1: every accepted confirmation alias is disclosed -------------------

def _bash_purge_aliases() -> set:
    """The tokens bin/beacon's purge-probes arm actually accepts."""
    text = (ROOT / "bin" / "beacon").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines()
                if 'note_purge_confirm="1"' in l and ")" in l)
    label = line.strip().split(")")[0]
    return {t.strip() for t in label.split("|")}


def test_help_registry_names_every_accepted_confirm_alias():
    """Accepting a flag the description never mentions makes a careful agent
    report it as unsupported. Derive the expected set from the parser itself."""
    sys.path.insert(0, str(ROOT / "lib"))
    import commands
    entry = next(e for e in commands._help_registry()
                 if e["command"] == "beacon note purge-probes")
    advertised = set(entry["flags"])
    accepted = _bash_purge_aliases()
    assert accepted <= advertised, (
        "bin/beacon accepts confirmation aliases the help registry hides: "
        + repr(sorted(accepted - advertised)))


def test_python_frontend_accepts_the_same_confirm_aliases(project):
    """The alias set must be real on both frontends, not just documented."""
    cwd, notes = project
    from beacon_cli import dispatch
    for alias in sorted(_bash_purge_aliases() - {"--confirm"}):
        notes.write_text(SENTINEL_NOTE + REAL_NOTE, encoding="utf-8")
        assert dispatch.dispatch(ROOT, ["note", "purge-probes", alias]) == 0
        texts = [json.loads(l)["text"]
                 for l in notes.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert texts == ["real"], (alias, texts)


# --- A-2: the probe cannot inherit another reason from its parent ----------

def test_probe_is_not_misreported_when_help_only_is_ambient(project, monkeypatch):
    """With BEACON_HELP_ONLY in the environment the gate used to refuse under
    the HELP reason (exit 0, stdout), and the collector recorded the refusal as
    silent_no_op=True — the audit inventing the defect it hunts."""
    cwd, notes = project
    monkeypatch.setenv("BEACON_HELP_ONLY", "1")
    import commands
    probe = commands._collect_surface_snapshot(commands_list=["note"])[0]
    assert probe["blocked_by_readonly_gate"] is True, probe
    assert probe["silent_no_op"] is False, probe
    assert probe["exit_code"] == readonly_gate.PROBE_REFUSAL_EXIT, probe
    texts = [json.loads(l)["text"]
             for l in notes.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert "__ax_surface_probe__" in texts and len(texts) == 2, texts


def test_all_reason_env_vars_is_derived_not_hand_listed():
    """A new reason added to REASON_ENV must be stripped by the probe without
    anyone remembering to update a second list."""
    assert readonly_gate.ALL_REASON_ENV_VARS == frozenset(readonly_gate.REASON_ENV.values())
    assert len(readonly_gate.ALL_REASON_ENV_VARS) == len(readonly_gate.REASON_ENV)
