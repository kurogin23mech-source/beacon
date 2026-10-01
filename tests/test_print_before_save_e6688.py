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


# --- 4. the full catch/quiet matrix -----------------------------------------
#
# Both independent reviews of PR #777 reproduced the same blind spot: the first
# version treated "a save exists somewhere inside this compound statement" as
# "the write happened", so a save in one arm of an `if`, inside a `try` whose
# handler swallows the abort, or inside a loop that may not iterate, all made
# the checker wave through an unconditional success print. The try/except shape
# is structurally the e-6688 bug itself. Every shape below is pinned so neither
# half of the guard's accuracy — what it catches, and what it leaves alone —
# can regress silently.

MUST_REPORT = {
    "the historical bug: print then save":
        "def cmd_x():\n"
        "    print(f'Done: [{eid}]')\n"
        "    save_project(data)\n",
    "save in only one arm, print unconditional":
        "def cmd_x():\n"
        "    if needs_write:\n"
        "        save_project(data)\n"
        "    else:\n"
        "        pass\n"
        "    print(f'Done: [{eid}]')\n",
    "save in an if with no else":
        "def cmd_x():\n"
        "    if needs_write:\n"
        "        save_project(data)\n"
        "    print(f'Done: [{eid}]')\n",
    "try/except swallows the abort":
        "def cmd_x():\n"
        "    try:\n"
        "        save_project(data)\n"
        "    except ConflictError:\n"
        "        pass\n"
        "    print(f'Done: [{eid}]')\n",
    "save inside a loop (zero iterations is a real path)":
        "def cmd_x():\n"
        "    for item in items:\n"
        "        save_project(data)\n"
        "    print(f'Done: [{eid}]')\n",
    "the success line moved into a helper":
        "def _report_done(entry):\n"
        "    print(f'Done: {entry}')\n"
        "def cmd_x():\n"
        "    _report_done(entry)\n"
        "    save_project(data)\n",
    "defining a helper is not performing the write":
        "def cmd_x():\n"
        "    def _write():\n"
        "        save_project(data)\n"
        "    print(f'Done: [{eid}]')\n"
        "    _write()\n",
    "file=sys.stdout is stdout, not stderr":
        "def cmd_x():\n"
        "    print(f'Done: [{eid}]', file=sys.stdout)\n"
        "    save_project(data)\n",
    "file= an expression we cannot resolve stays in scope":
        "def cmd_x():\n"
        "    print(f'Done: [{eid}]', file=pick_stream())\n"
        "    save_project(data)\n",
}

MUST_STAY_QUIET = {
    "validation error returns before the save":
        "def cmd_x():\n"
        "    if not entry_id:\n"
        "        print('Error: entry id required')\n"
        "        sys.exit(1)\n"
        "    save_project(data)\n",
    "no-op notice returns before the save":
        "def cmd_x():\n"
        "    if already:\n"
        "        print('already adopted')\n"
        "        return\n"
        "    save_project(data)\n",
    "write, report, then a follow-on write":
        "def cmd_x():\n"
        "    save_project(data)\n"
        "    print(f'Renamed {a} to {b}')\n"
        "    if changed:\n"
        "        save_project(data)\n",
    "stderr warning is not a success claim":
        "def cmd_x():\n"
        "    print('Warning: odd state', file=sys.stderr)\n"
        "    save_project(data)\n",
    "print nested in a conditional is a maybe":
        "def cmd_x():\n"
        "    if verbose:\n"
        "        print('extra detail')\n"
        "    save_project(data)\n",
    "try saves and every handler saves too":
        "def cmd_x():\n"
        "    try:\n"
        "        save_project(data)\n"
        "    except ConflictError:\n"
        "        save_project(data)\n"
        "    print(f'Done: [{eid}]')\n",
    "finally always runs":
        "def cmd_x():\n"
        "    try:\n"
        "        work()\n"
        "    finally:\n"
        "        save_project(data)\n"
        "    print(f'Done: [{eid}]')\n",
    "both arms of the if save":
        "def cmd_x():\n"
        "    if cond:\n"
        "        save_project(data)\n"
        "    else:\n"
        "        save_project(data)\n"
        "    print(f'Done: [{eid}]')\n",
    "a with-block body always runs":
        "def cmd_x():\n"
        "    with lock():\n"
        "        save_project(data)\n"
        "    print(f'Done: [{eid}]')\n",
    "a verb that never writes makes no write claim":
        "def cmd_x():\n"
        "    print('listing')\n"
        "    print('more')\n",
    "print after a plain save":
        "def cmd_x():\n"
        "    save_project(data)\n"
        "    print(f'Done: [{eid}]')\n",
}


def _collect_src(tmp_path, src):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "cmd_x.py").write_text(src, encoding="utf-8")
    return _checker().collect(lib)


@pytest.mark.parametrize("name", sorted(MUST_REPORT))
def test_checker_reports_every_unbacked_success_line(tmp_path, name):
    hits = _collect_src(tmp_path, MUST_REPORT[name])
    assert hits, "MISSED: " + name + " — the guard would wave this through"


@pytest.mark.parametrize("name", sorted(MUST_STAY_QUIET))
def test_checker_does_not_cry_wolf(tmp_path, name):
    hits = _collect_src(tmp_path, MUST_STAY_QUIET[name])
    assert hits == [], "FALSE POSITIVE: " + name + " -> " + repr(hits)


def test_scope_is_stated_in_the_success_message():
    """The guard checks lib/ and the save_project primitive only. An unscoped
    'OK' would read as a repo-wide guarantee it does not provide — the gap both
    reviews of PR #777 raised."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-print-before-save.py")],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "lib/*.py" in r.stdout and "save_project()" in r.stdout, r.stdout
    assert "NOT checked" in r.stdout, r.stdout


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

# --- 5. ALLOW の stale 検出 (親レビュー PR #777) -----------------------------
#
# 広めに allowlist を取る doctrine (「false positive は 1 行で済むが false
# negative は壊れるまで気づかれない」) が成り立つ前提は、**その行が今も必要だと
# 誰かが保証していること**。stale 検出はその保証を人間の記憶から機械へ移す側で、
# doctrine と対になっている。姉妹ガード
# check-cli-help-drift.collect_help_flag_drift が同じ理由で同じ検査を持つ。
#
# 実際、この検査を入れた瞬間に ALLOW 7 行のうち 5 行が既に死んでいた (経路解析を
# 厳密化した時点で、それらの print はもう検出されなくなっていた)。死んだ行は
# 同じ (ファイル, 関数, 文頭) に対する次の退行を黙って免除する。

def test_no_stale_allow_entries_today():
    hits, stale = _checker().collect(with_stale=True)
    assert stale == [], (
        "ALLOW にもう何も免除していない行が残っています: " + repr(stale))
    assert hits == [], hits


def test_stale_check_reports_an_entry_that_matches_nothing(monkeypatch, tmp_path):
    """親レビューの明示的な要求: stale 検出自身が stale な行で赤くなることを
    実測する。これを確かめないと『stale 検出はあるが何も検出しない』形になりうる
    (このガードが防ごうとしている失敗そのもの)。"""
    mod = _checker()
    dummy = ("cmd_nonexistent.py", "cmd_never_defined", "Done: ")
    monkeypatch.setitem(mod.ALLOW, dummy, "a deliberately stale row")
    hits, stale = mod.collect(with_stale=True)
    assert any("cmd_never_defined" in row for row in stale), (
        "stale 行が報告されていません: " + repr(stale))


def test_stale_check_does_not_flag_an_entry_that_is_still_exempting(tmp_path):
    """逆向きの確認: 実際に print を免除している行は stale と報告されない。
    そうでないと、生きている行を消させる方向に誤誘導する。"""
    mod = _checker()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "cmd_x.py").write_text(
        "def cmd_x():\n"
        "    print('Keepme: informational')\n"
        "    save_project(data)\n", encoding="utf-8")
    saved = dict(mod.ALLOW)
    mod.ALLOW.clear()
    mod.ALLOW[("cmd_x.py", "cmd_x", "Keepme:")] = "still exempting a real print"
    try:
        hits, stale = mod.collect(lib, with_stale=True)
    finally:
        mod.ALLOW.clear()
        mod.ALLOW.update(saved)
    assert hits == [], hits
    assert stale == [], "生きている行を stale と誤報告しました: " + repr(stale)


def test_main_exits_nonzero_on_a_stale_entry_alone(monkeypatch):
    """hits が 0 でも stale があれば CI は赤であること。stale だけ警告で通すと、
    結局誰も消さない。"""
    mod = _checker()
    monkeypatch.setitem(mod.ALLOW,
                        ("cmd_nonexistent.py", "cmd_never_defined", "Done: "),
                        "a deliberately stale row")
    assert mod.main() == 1
