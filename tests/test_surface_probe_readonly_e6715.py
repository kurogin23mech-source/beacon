"""The AX surface probe must not write to the project (ms-160 e-6715).

``commands._collect_surface_snapshot`` probes each command group with a bogus
subcommand to sample its usage / error surface. The list was curated on the
assumption that a *group* command cannot execute real logic when handed an
unknown subcommand. ``beacon note`` broke that assumption: its positional IS
free text, so the bogus token was not rejected as bogus — it was saved as a
real session note. 17 of the 48 notes in the live project store were this probe
string, polluting every parallel session's ``note list`` and session-end.

The fix is not "drop note from the list" (the next free-text verb would repeat
it) but a read-only gate at the python dispatch chokepoint, so the property
holds for whatever the list contains later.

Layers pinned here:
  1. end to end — running the real snapshot leaves the project byte-identical;
  2. test-the-test — with the gate disabled the SAME assertion goes red, so a
     green run is evidence of the gate, not of the probe never reaching python;
  3. the refusal is reported as a refusal, never as a silent no-op;
  4. the sentinel has one definition, shared with the cleanup verb.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import cli_surface  # noqa: E402
import commands  # noqa: E402
import readonly_gate  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A local-mode project (no cloud.json) with one pre-existing note."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    (beacon / "session_notes.jsonl").write_text(
        json.dumps({"ts": "2026-10-01T00:00:00+0900", "text": "handoff"}) + "\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _fingerprint(root: Path) -> dict:
    """path -> sha256 for every file under .beacon, so a probe that creates,
    appends to, or rewrites ANY of them shows up (not just the notes file)."""
    out = {}
    for p in sorted((root / ".beacon").rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _notes(root: Path) -> list:
    f = root / ".beacon" / "session_notes.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


# --- layer 1: the real snapshot changes nothing -----------------------------

def test_snapshot_leaves_the_project_unchanged(project):
    before = _fingerprint(project)
    commands._collect_surface_snapshot()
    assert _fingerprint(project) == before, (
        "the AX surface probe wrote to the project")


def test_snapshot_writes_no_probe_note(project):
    commands._collect_surface_snapshot(commands_list=["note"])
    texts = [n.get("text") for n in _notes(project)]
    assert cli_surface.SURFACE_PROBE_SENTINEL not in texts, texts
    assert texts == ["handoff"], texts


# --- layer 2: test-the-test — the assertion really can go red ---------------

def test_without_the_gate_the_probe_does_write(project, monkeypatch):
    """Drive the collector with the gate neutralised. If this does NOT write a
    note, the tests above are passing for some other reason (e.g. the probe
    never reaching python) and would not notice the gate being removed."""
    monkeypatch.setattr(readonly_gate, "active_reason", lambda env=None: None)
    real_run = subprocess.run

    def _ungated(argv, **kw):
        env = dict(kw.pop("env", None) or os.environ)
        env.pop("BEACON_SURFACE_PROBE", None)
        return real_run(argv, env=env, **kw)

    monkeypatch.setattr(commands.subprocess, "run", _ungated)
    commands._collect_surface_snapshot(commands_list=["note"])
    texts = [n.get("text") for n in _notes(project)]
    assert cli_surface.SURFACE_PROBE_SENTINEL in texts, (
        "ungated probe did not write — this test no longer proves the gate "
        "is what keeps the project clean: " + repr(texts))


# --- layer 3: a refusal is reported as a refusal ----------------------------

def test_blocked_probe_is_not_reported_as_a_silent_no_op(project):
    probe = commands._collect_surface_snapshot(commands_list=["note"])[0]
    assert probe["blocked_by_readonly_gate"] is True, probe
    assert probe["silent_no_op"] is False, probe
    assert probe["exit_code"] == readonly_gate.PROBE_REFUSAL_EXIT, probe
    assert "refused" in probe["stderr"], probe


def test_read_only_group_probe_still_shows_its_real_surface(project):
    """Fail-closed must not mean fail-always: a group whose bogus subcommand is
    rejected by its own parser must still be sampled, not gate-blocked."""
    probe = commands._collect_surface_snapshot(commands_list=["milestone"])[0]
    assert probe["blocked_by_readonly_gate"] is False, probe
    assert (probe["stdout"] + probe["stderr"]).strip(), probe


# --- layer 4: no second spelling of the sentinel ----------------------------

def _docstring_nodes(tree):
    """The ast.Constant nodes that are docstrings, so the scan below can skip
    them: prose quoting the sentinel documents it, it does not re-define it."""
    import ast
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _functional_sentinel_copies(literal: str, files) -> list:
    """Files that carry ``literal`` as a string the CODE uses — a second
    definition. Structural, not a substring scan over the whole file: a
    docstring example quoting the sentinel is prose and must not trip the
    guard, or the guard gets muted for being noisy."""
    import ast
    offenders = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if literal not in text:
            continue
        if path.suffix != ".py":
            offenders.append(path)       # shell/other: no AST, flag the match
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            offenders.append(path)       # unparseable: do not call it clean
            continue
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and literal in node.value and id(node) not in skip):
                offenders.append(path)
                break
    return offenders


def _scanned_files():
    return [p for p in sorted((ROOT / "lib").glob("*.py")) + sorted((ROOT / "bin").rglob("*"))
            if p.is_file() and p.name != "cli_surface.py"]


def test_sentinel_has_a_single_definition():
    """The cleanup verb finds old garbage by matching this exact string. A
    literal copy in code would let the two drift and the cleanup silently skip
    what it exists to remove."""
    offenders = _functional_sentinel_copies(
        cli_surface.SURFACE_PROBE_SENTINEL, _scanned_files())
    assert offenders == [], (
        "hardcoded copy of the probe sentinel; import "
        "cli_surface.SURFACE_PROBE_SENTINEL instead: "
        + ", ".join(str(p.relative_to(ROOT)) for p in offenders))


def test_the_sentinel_guard_catches_a_real_copy(tmp_path):
    """A guard that cannot fail is useless. A docstring mention must stay
    clean; the same string assigned in code must be caught."""
    literal = cli_surface.SURFACE_PROBE_SENTINEL
    prose = tmp_path / "prose.py"
    prose.write_text('"""example: milestone %s"""\n' % literal, encoding="utf-8")
    code = tmp_path / "code.py"
    code.write_text('bogus = "%s"\n' % literal, encoding="utf-8")
    assert _functional_sentinel_copies(literal, [prose]) == []
    assert _functional_sentinel_copies(literal, [code]) == [code]
