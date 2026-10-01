"""A success line must not precede the write that makes it true (ms-160 e-6688).

Observed 2026-09-29 while working on e-5981: ``beacon task done e-5981``
printed ``Done: [e-5981] ...`` on stdout, printed the lost-update abort on
stderr, and exited 1 — yet the task stayed ``todo``. ``save_project`` aborts
correctly; the bug was ordering. In a terminal the two streams interleave, so
the completion line is what the reader keeps, and the next step proceeds on a
write that never happened.

Pinned here:
  1. behaviour — when the write aborts, nothing that claims success reaches
     stdout, and the exit code is non-zero;
  2. the checker is clean on the tree as it stands;
  3. test-the-test — restoring the original (print-then-save) order makes the
     checker report it. A checker nobody has seen fail proves nothing;
  4. precision — the shapes that must NOT be reported, so the checker does not
     get muted for crying wolf.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "beacon"
sys.path.insert(0, str(ROOT / "lib"))


def _checker():
    spec = importlib.util.spec_from_file_location(
        "print_before_save", ROOT / "scripts" / "check-print-before-save.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- 1. behaviour: an aborted write prints no success line ------------------

@pytest.fixture
def project(tmp_path):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "t",
        "milestones": [{
            "id": "ms-1", "title": "m", "status": "in_progress", "progress": 0,
            "entries": [{"id": "e-1", "type": "task", "status": "todo",
                         "description": "a task"}],
        }],
    }), encoding="utf-8")
    return tmp_path


def test_aborted_write_prints_no_success_line_and_exits_nonzero(project, monkeypatch, capsys):
    """Drive cmd_task_done with a store whose save raises the lost-update
    ConflictError, exactly as the cloud guard does, and check BOTH halves of
    the acceptance condition: nothing claiming success on stdout, non-zero exit.

    stdout and stderr are asserted separately — a terminal interleaves them
    (which is how the success line got believed), a captured stream does not.
    """
    monkeypatch.chdir(project)
    import commands_shared
    import cmd_task
    from store_api import ConflictError

    class _Conflicting:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def save_project(self, data):
            raise ConflictError(
                "Cloud project changed since it was read — aborting to avoid "
                "overwriting another writer's changes. Re-run the command.")

    real_get_store = commands_shared.get_store
    monkeypatch.setattr(commands_shared, "get_store",
                        lambda *a, **k: _Conflicting(real_get_store(*a, **k)))
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "done for the test")

    with pytest.raises(SystemExit) as exc:
        cmd_task.cmd_task_done()
    out = capsys.readouterr()
    assert exc.value.code != 0, "an aborted write must exit non-zero"
    assert "Done:" not in out.out, (
        "stdout claimed completion for a write that was refused: " + repr(out.out))
    assert out.out.strip() == "", "nothing at all should reach stdout: " + repr(out.out)
    assert "aborting" in out.err, out.err


def test_successful_write_still_reports_completion(project, monkeypatch, capsys):
    """The fix must not silence the normal path: when the write lands, the
    completion line is still printed (a guard that breaks the feature is worse
    than the bug)."""
    monkeypatch.chdir(project)
    import cmd_task
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "done for the test")
    cmd_task.cmd_task_done()
    out = capsys.readouterr()
    assert "Done: [e-1]" in out.out, out.out
    data = json.loads((project / ".beacon" / "project.json").read_text(encoding="utf-8"))
    assert data["milestones"][0]["entries"][0]["status"] == "done"


# --- 2 & 3. the checker: clean now, and red on the original ordering --------

def test_checker_is_clean_on_the_tree():
    mod = _checker()
    assert mod.collect() == [], mod.collect()


def test_checker_reports_the_original_task_done_ordering(tmp_path):
    """test-the-test: reconstruct the shipped bug and confirm it is reported."""
    mod = _checker()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "cmd_x.py").write_text(textwrap.dedent('''
        def cmd_task_done():
            data = load_project()
            ms, entry = core.task_done(data, entry_id)
            print(f"Done: [{entry_id}] {entry['description']}")
            save_project(data)
    '''), encoding="utf-8")
    hits = mod.collect(lib)
    assert len(hits) == 1, hits
    assert hits[0][1] == "cmd_task_done"
    assert hits[0][3].startswith("Done:")


def test_checker_is_satisfied_once_the_print_moves_below_the_save(tmp_path):
    mod = _checker()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "cmd_x.py").write_text(textwrap.dedent('''
        def cmd_task_done():
            data = load_project()
            ms, entry = core.task_done(data, entry_id)
            save_project(data)
            print(f"Done: [{entry_id}] {entry['description']}")
    '''), encoding="utf-8")
    assert mod.collect(lib) == []


# --- 4. precision: the shapes that must NOT be reported ---------------------

@pytest.mark.parametrize("name,src", [
    ("validation error returns before the save", '''
        def cmd_x():
            if not entry_id:
                print("Error: entry id required")
                sys.exit(1)
            save_project(data)
    '''),
    ("no-op notice returns before the save", '''
        def cmd_x():
            if already_adopted:
                print("already adopted")
                return
            save_project(data)
    '''),
    ("write, report, then a follow-on write", '''
        def cmd_x():
            save_project(data)
            print(f"Renamed {a} to {b}")
            if changed:
                save_project(data)
    '''),
    ("stderr warning is not a success claim", '''
        def cmd_x():
            print("Warning: odd state", file=sys.stderr)
            save_project(data)
    '''),
    ("print nested in a conditional is a maybe, not a claim", '''
        def cmd_x():
            if verbose:
                print("extra detail")
            save_project(data)
    '''),
])
def test_checker_does_not_cry_wolf(tmp_path, name, src):
    mod = _checker()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "cmd_x.py").write_text(textwrap.dedent(src), encoding="utf-8")
    assert mod.collect(lib) == [], name


def test_unparseable_file_is_reported_not_treated_as_clean(tmp_path):
    """A file the checker cannot read must not read as 'no problems' — the
    failure mode a guard is least able to notice about itself (ms-160 e-6349)."""
    mod = _checker()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "broken.py").write_text("def cmd_x(:\n", encoding="utf-8")
    hits = mod.collect(lib)
    assert hits and "parsed" in hits[0][3], hits


# --- the real call sites, read from the shipped source ----------------------

def test_task_done_prints_completion_after_the_save_in_both_paths():
    """Both arms of cmd_task_done (plain done, and the PR-merge arm) must save
    before they report. Read structurally, not by eyeballing line numbers."""
    tree = ast.parse((ROOT / "lib" / "cmd_task.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_task_done")
    saves, claims = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None)
            if name == "save_project":
                saves.append(node.lineno)
            elif name == "print" and not any(k.arg == "file" for k in node.keywords):
                a = node.args[0] if node.args else None
                text = ""
                if isinstance(a, ast.JoinedStr):
                    text = "".join(v.value for v in a.values
                                   if isinstance(v, ast.Constant) and isinstance(v.value, str))
                elif isinstance(a, ast.Constant):
                    text = str(a.value)
                if text.startswith(("Done:", "Merged PR")):
                    claims.append((text, node.lineno))
    assert len(saves) == 2, saves
    assert len(claims) == 2, claims
    for text, lineno in claims:
        assert any(s < lineno for s in saves), (
            f"{text!r} at line {lineno} is printed before any save: {saves}")
