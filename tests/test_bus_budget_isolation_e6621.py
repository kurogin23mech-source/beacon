"""Running the suite must not change how the next run behaves (ms-166 e-6621).

The bus send gate reads ``.beacon/bus-budget.json`` to decide whether this
session is acting autonomously. A grant left behind by a test therefore does not
just linger — it makes the NEXT run refuse operation-bearing sends, so bus tests
fail by the dozen on a developer's machine while CI stays green (CI starts from a
clean checkout). The failure looks like the developer's own regression.

``cmd_trek_join`` reaches ``_arm_for_trek``, which writes an unconditional
20-turn grant, and tests/test_trek_cli_cloud.py drove it with no isolation. It
was hit 6 times on 2026-10-01 across four sessions; one of them nearly shipped a
real regression behind "probably that bus thing again".

Pinned here:
  * the budget path honours an override, so isolation is a mechanism rather than
    something each new test has to remember (the sibling of
    ``BEACON_BUS_SENT_LOG_PATH``, ms-141 e-4965);
  * the autouse fixture points it at tmp for every test;
  * running a budget-granting path twice gives the same answer both times;
  * the leak guard actually fires, and names the file — a guard nobody has seen
    fail is a guard nobody knows works.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import commands_shared  # noqa: E402


# --- the path is overridable, and the fixture uses it ------------------------

def test_budget_path_honours_the_override(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "bus-budget.json"
    monkeypatch.setenv("BEACON_BUS_BUDGET_PATH", str(target))
    assert commands_shared._get_bus_budget_path() == str(target)


def test_budget_path_falls_back_to_the_project_dir(tmp_path, monkeypatch):
    """No override ⇒ beside the project file, which is the production shape."""
    monkeypatch.delenv("BEACON_BUS_BUDGET_PATH", raising=False)
    proj = tmp_path / ".beacon" / "project.json"
    proj.parent.mkdir(parents=True)
    proj.write_text(json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(proj))
    assert commands_shared._get_bus_budget_path() == str(
        tmp_path / ".beacon" / "bus-budget.json")


def test_isolating_the_project_root_moves_the_budget_with_it(tmp_path, monkeypatch):
    """One redirect moves every bus side-file, which is why per-module isolation
    is cheap enough to be the rule.

    An earlier attempt made a suite-wide autouse fixture force
    ``BEACON_BUS_BUDGET_PATH`` for every test. That broke 14 tests whose SUBJECT
    is the budget file (tests/test_bus_budget.py isolates its own project root
    and then asserts the file appears beside it) — the override hijacked the path
    they had correctly chosen. Isolation belongs to the module that writes;
    DETECTION is what belongs suite-wide (``_fail_on_repo_beacon_write``).
    """
    monkeypatch.delenv("BEACON_BUS_BUDGET_PATH", raising=False)
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    commands_shared._write_bus_budget({"total": 1, "used": 0, "channels": []})
    assert (beacon_dir / "bus-budget.json").exists()
    assert not (ROOT / ".beacon" / "bus-budget.json").exists()


# --- writing a budget stays inside the test ---------------------------------

def test_granting_budget_does_not_touch_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_BUS_BUDGET_PATH", str(tmp_path / "bus-budget.json"))
    repo_budget = ROOT / ".beacon" / "bus-budget.json"
    existed = repo_budget.exists()
    commands_shared._write_bus_budget(
        {"total": 20, "used": 0, "channels": [], "trek_id": "tk-fake01"})
    assert commands_shared._read_bus_budget()["trek_id"] == "tk-fake01"
    assert repo_budget.exists() is existed, (
        "writing a budget reached the repository's own .beacon/")


def test_two_consecutive_runs_see_the_same_budget():
    """AC 1: the second run must not inherit the first run's grant.

    Driven in a subprocess pair so each gets its own fixture-assigned path, the
    way two suite runs would. Before the fix the second run saw the first run's
    20-turn grant and the send gate switched to autonomous mode.
    """
    code = (
        "import sys; sys.path.insert(0, %r)\n" % str(ROOT / "lib")
        + "import commands_shared as cs\n"
        "print('BEFORE' if cs._read_bus_budget() is None else 'INHERITED')\n"
        "cs._write_bus_budget({'total': 20, 'used': 0, 'channels': []})\n"
    )
    env = {**os.environ, "BEACON_TEST_MODE": "1"}
    seen = []
    for _ in range(2):
        # a fresh budget path per run, as the autouse fixture gives each test
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env["BEACON_BUS_BUDGET_PATH"] = os.path.join(d, "bus-budget.json")
            r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, env=env, cwd=str(ROOT))
            seen.append(r.stdout.strip())
    assert seen == ["BEFORE", "BEFORE"], (
        "the second run inherited the first run's budget: " + repr(seen))


# --- the guard itself must be seen failing (AC 4) ---------------------------

class _ProbeIn_tests:
    """A throwaway test file placed INSIDE tests/, then removed.

    It has to live there: pytest auto-loads conftest.py only from the rootdir
    down to the test file's own directory, so a probe written under ``tmp_path``
    runs with no conftest and therefore no guard — it would pass for the wrong
    reason and report the guard as broken (observed while writing this test).
    """

    def __init__(self, body: str, name: str):
        self.path = ROOT / "tests" / name
        self.body = body

    def __enter__(self) -> Path:
        self.path.write_text(self.body, encoding="utf-8")
        return self.path

    def __exit__(self, *exc):
        if self.path.exists():
            self.path.unlink()
        return False


def _run_one(body: str, name: str, watch_dir: Path) -> subprocess.CompletedProcess:
    """Run the probe with the guard watching ``watch_dir`` instead of the repo.

    The probe must not write the real ``.beacon/``: under ``-n auto`` that file
    lands inside other workers' before/after windows and blames them (4 innocent
    tests per run, measured before this indirection existed).
    """
    with _ProbeIn_tests(body, name) as t:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(t), "-q", "-p", "no:cacheprovider"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ,
                 "BEACON_TEST_REPO_BEACON_DIR": str(watch_dir),
                 "PYTHONPATH": os.pathsep.join(
                     [str(ROOT / "lib"), str(ROOT / "tests")])})


def test_the_leak_guard_fires_and_names_the_file(tmp_path):
    """Write into the directory the guard watches and expect to be caught."""
    watch = tmp_path / "watched"
    watch.mkdir()
    body = (
        "import os\n"
        f"WATCH = {str(watch)!r}\n"
        "def test_leaks():\n"
        "    open(os.path.join(WATCH, 'e6621-probe.json'), 'w').write('{}')\n"
    )
    r = _run_one(body, "test_e6621_probe_leak.py", watch)
    assert r.returncode != 0, (
        "the leak guard did not fail the test that wrote the watched dir:\n"
        + r.stdout + r.stderr)
    assert "e6621-probe.json" in (r.stdout + r.stderr), (
        "the guard fired but did not name the file:\n" + r.stdout + r.stderr)
    assert not (ROOT / ".beacon" / "e6621-probe.json").exists(), (
        "verifying the guard must not write the real repository")


def test_the_leak_guard_does_not_cry_wolf(tmp_path):
    """A test that writes only under tmp_path must pass.

    A guard that fails honest tests gets disabled, and then it protects nothing.
    """
    body = (
        "def test_clean(tmp_path):\n"
        "    (tmp_path / 'x.json').write_text('{}')\n"
    )
    watch = tmp_path / "watched"
    watch.mkdir()
    r = _run_one(body, "test_e6621_probe_clean.py", watch)
    assert r.returncode == 0, (
        "the guard failed a test that stayed inside tmp_path:\n"
        + r.stdout + r.stderr)


def test_the_opt_out_marker_is_registered():
    """An unregistered marker only warns, so a typo would silently not opt out."""
    r = subprocess.run([sys.executable, "-m", "pytest", "--markers"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert "allow_repo_beacon_write" in r.stdout, r.stdout[-600:]


def test_the_exclusion_set_is_exact_names_not_prefixes():
    """A prefix match would re-admit the leak class the guard exists to catch.

    ``bus-budget.json`` must not be excusable because some excluded name happens
    to be a prefix of it.
    """
    import conftest
    for name in conftest._NOT_THE_TESTS_FAULT:
        assert not "bus-budget.json".startswith(name), name
        assert not name.endswith("*"), (
            "the exclusion set must hold exact entry names, not patterns: " + name)


# --- independent review of PR #780 (AX-1 / AX-2 / AX-3 / M-1) ----------------
# The first version of the guard had two blind spots that made it pass in the one
# environment it mattered most in, plus an error message pointing at a symbol
# that had been deleted. Each is pinned here so it cannot come back.


def test_the_guard_works_when_the_directory_does_not_exist_yet(tmp_path):
    """AX-1: an absent ``.beacon/`` must be an empty snapshot, not a skipped check.

    The first version returned None from ``os.listdir`` on OSError and then
    returned early — so in a bare checkout, where ``.beacon/`` does not exist at
    all (it is gitignored), a test could create the directory and write into it
    and the guard said nothing. That is CI's exact starting condition, and the
    commit that added the guard cited "CI starts from a clean checkout" as the
    reason the original leak stayed hidden.
    """
    watch = tmp_path / "not-created-yet"          # deliberately NOT mkdir'd
    body = (
        "import os\n"
        f"WATCH = {str(watch)!r}\n"
        "def test_creates_the_dir_then_leaks():\n"
        "    os.makedirs(WATCH, exist_ok=True)\n"
        "    open(os.path.join(WATCH, 'from-scratch.json'), 'w').write('{}')\n"
    )
    r = _run_one(body, "test_e6621_probe_absent_dir.py", watch)
    assert r.returncode != 0, (
        "the guard skipped the check because the directory did not exist yet — "
        "that is CI's condition:\n" + r.stdout + r.stderr)
    assert "from-scratch.json" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_the_guard_sees_writes_into_existing_subdirectories(tmp_path):
    """M-1: the snapshot must recurse.

    ``os.listdir`` saw only the top level. The repo's ``.beacon/`` already holds
    seven subdirectories, whose NAMES appear in both snapshots, so a new file
    inside one of them produced no difference — while the docstring claimed the
    guard caught "the NEXT one".
    """
    watch = tmp_path / "watched"
    (watch / "documents").mkdir(parents=True)
    (watch / "documents" / "pre-existing.json").write_text("{}", encoding="utf-8")
    body = (
        "import os\n"
        f"WATCH = {str(watch)!r}\n"
        "def test_leaks_into_a_subdir():\n"
        "    open(os.path.join(WATCH, 'documents', 'nested-leak.json'), 'w').write('{}')\n"
    )
    r = _run_one(body, "test_e6621_probe_nested.py", watch)
    assert r.returncode != 0, (
        "a write into an existing subdirectory went unreported:\n"
        + r.stdout + r.stderr)
    assert "nested-leak.json" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_pruned_directories_are_not_walked_but_still_excluded(tmp_path):
    """Pruning must not turn into a hole: excluded dirs stay excluded, and the
    guard must not pay to walk them.

    ``triggers/`` is bridge-owned, so a file appearing there is not the test's
    doing — but the reason it is skipped must be the exclusion list, not an
    accident of how the walk is written.
    """
    import conftest
    watch = tmp_path / "watched"
    (watch / "triggers").mkdir(parents=True)
    before = conftest._snapshot_beacon_dir(str(watch))
    (watch / "triggers" / "fired.json").write_text("{}", encoding="utf-8")
    assert conftest._snapshot_beacon_dir(str(watch)) == before, (
        "a file under an excluded directory changed the snapshot")


def test_the_refusal_names_only_things_that_exist():
    """AX-2: the error must not send the reader after a deleted symbol.

    The first version said "see _isolate_bus_budget / _isolate_bus_sent_log".
    ``_isolate_bus_budget`` had been removed during the redesign, so the one
    person following the instructions searched for something that did not exist.
    Every identifier the message offers has to be findable.
    """
    src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    fn = src.split("def _fail_on_repo_beacon_write", 1)[1].split("\n@pytest.fixture", 1)[0]
    # Only what the MESSAGE offers — the fixture body legitimately names the
    # test-only directory override, which the reader is never told to go find.
    message = fn.split("raise AssertionError(", 1)[1]
    offered = set(re.findall(r"\b(BEACON_[A-Z_]+|_isolate_[a-z_]+)\b", message))
    assert offered, "the refusal offers no concrete handle at all"
    # Search where a reader would: the whole tests/ and lib/ trees, not just this
    # file. The message legitimately points at a fixture in another test module.
    haystack = []
    for sub in ("tests", "lib"):
        for path in sorted((ROOT / sub).rglob("*.py")):
            if path.name == "conftest.py" or path.name == Path(__file__).name:
                continue        # the message's own home, and this test
            haystack.append(path.read_text(encoding="utf-8", errors="ignore"))
    repo_text = "\n".join(haystack)
    missing = sorted(n for n in offered if n not in repo_text)
    assert not missing, (
        "the refusal points at {0}, which a reader cannot find anywhere in "
        "tests/ or lib/ — following the instructions leads nowhere. (This is what "
        "happened with _isolate_bus_budget, a fixture deleted during the "
        "redesign.)".format(", ".join(missing)))


def test_a_misspelled_opt_out_marker_cannot_be_silent():
    """AX-3: --strict-markers turns a typo into a collection error.

    Registering the marker makes the right spelling discoverable; it does not stop
    the wrong one. Without strict mode ``allow_repo_beacon_writes`` only warns,
    and warnings print far from the assertion the reader is looking at.
    """
    body = (
        "import pytest\n"
        "@pytest.mark.allow_repo_beacon_writes\n"   # deliberate typo
        "def test_typo():\n"
        "    pass\n"
    )
    r = _run_one(body, "test_e6621_probe_typo.py", ROOT / ".beacon")
    assert r.returncode != 0, (
        "a misspelled marker was accepted — the typo would read as an opt-out "
        "while doing nothing:\n" + r.stdout + r.stderr)
    assert "allow_repo_beacon_writes" in (r.stdout + r.stderr), r.stdout + r.stderr


# --- recorded debt, not a silenced guard (ms-166 e-6621 / e-6833) ------------


def test_known_leaks_are_recorded_not_hidden():
    """The debt must be readable as debt, with each entry explained.

    A bare set of filenames is indistinguishable from "we decided these are
    fine". What makes it debt is that it is named, reasoned, and shrinking.
    """
    import conftest
    assert conftest.KNOWN_LEAKS, "an empty list means the gate is fully closed"
    src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    block = src.split("KNOWN_LEAKS = frozenset({", 1)[1].split("})", 1)[0]
    for name in conftest.KNOWN_LEAKS:
        assert name in block, name
    assert "e-6833" in src, "the debt must point at the task that burns it down"


def test_a_new_kind_of_leak_still_fails(tmp_path):
    """The gate that remains: a file name NOT in KNOWN_LEAKS fails the test.

    This is the whole justification for recording the two known names instead of
    disabling the guard — new code cannot introduce a new kind of leak.
    """
    watch = tmp_path / "watched"
    watch.mkdir()
    body = (
        "import os\n"
        f"WATCH = {str(watch)!r}\n"
        "def test_new_kind():\n"
        "    open(os.path.join(WATCH, 'brand-new-leak.json'), 'w').write('{}')\n"
    )
    r = _run_one(body, "test_e6621_probe_newkind.py", watch)
    assert r.returncode != 0, (
        "a leak of an unlisted kind was not reported:\n" + r.stdout + r.stderr)
    assert "brand-new-leak.json" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_a_known_leak_does_not_fail(tmp_path):
    """And the recorded ones do not, which is what lets CI go green today."""
    watch = tmp_path / "watched"
    watch.mkdir()
    body = (
        "import os\n"
        f"WATCH = {str(watch)!r}\n"
        "def test_known_kind():\n"
        "    open(os.path.join(WATCH, 'session.json'), 'w').write('{}')\n"
    )
    r = _run_one(body, "test_e6621_probe_knownkind.py", watch)
    assert r.returncode == 0, (
        "a recorded leak failed the suite — CI would stay red:\n"
        + r.stdout + r.stderr)


# --- 覆域の軸は「残ったか」であって「名前」ではない (e-6816 / 2026-10-05) ------
#
# 以前は「アトミック書き込みの途中半分」を _NOT_THE_TESTS_FAULT に名前で 1 件ずつ
# 載せて免除していた (project.json.tmp)。軸がファイル名だったので、同じ機構の別名
# (session-state.json.tmp) がそのまま素通りし、.beacon/ を一切触らないテストを
# 名指しした。以下はその軸の入れ替えが、免除すべきものだけを免除し、捕まえるべき
# ものは捕まえることを両側から留める。

def test_a_file_gone_by_report_time_is_not_reported(tmp_path):
    """スナップショット時には在り、報告時には消えているファイルを報告しない。

    これが実際に起きた形: after スナップショットと報告の間に、**スイート外の
    書き手** (開発者自身のセッションのフックがアトミックに session-state.json を
    書き換える) の .tmp が窓に入る。2026-10-05 に -n 4 で実測、.beacon/ を一切
    触らない test_plugin_skill_matches_canonical_source が名指しされた。

    判定関数を直接測る (fixture 越しの振る舞いでは測れない)。テスト本体から
    os.replace しても、after スナップショットは teardown 時なので既に消えており
    スナップショットがそれを見ない = 空振りのプローブになる。実際にこれを最初に
    書いて反転テストで空振りだと分かった。報告時に消えているという条件は、
    「スナップショット後に第三者が消す」ことでしか作れず、時間に依らせずに
    再現できないため、決定を下す関数の側を留める。
    """
    import conftest
    watch = tmp_path / "watched"
    watch.mkdir()
    (watch / "stays.json").write_text("{}", encoding="utf-8")
    kept = conftest._report_persisting(
        str(watch), {"stays.json", "session-state.json.tmp"})
    assert kept == {"stays.json"}, (
        "消えたファイルを報告対象に残している (名前で免除していた時代の形): "
        + repr(kept))


def test_a_persisting_tmp_file_is_still_reported(tmp_path):
    """残った .tmp は報告する — 名前での免除が構造的に見逃していた側。

    project.json.tmp は名前で免除されていたので、**本当に置き去りにされた**
    場合も報告されなかった。軸を永続性に変えると、免除は「消えたもの」だけに
    かかり、残ったものは名前に関わらず捕まる。つまりこの入れ替えは前より緩く
    なく、厳しくなっている。
    """
    watch = tmp_path / "watched"
    watch.mkdir()
    body = (
        "import os\n"
        f"WATCH = {str(watch)!r}\n"
        "def test_leaves_a_tmp():\n"
        "    open(os.path.join(WATCH, 'project.json.tmp'), 'w').write('{}')\n"
    )
    r = _run_one(body, "test_e6621_probe_persisting_tmp.py", watch)
    assert r.returncode != 0, (
        "置き去りにされた .tmp を見逃している:\n" + r.stdout + r.stderr)
    assert "project.json.tmp" in (r.stdout + r.stderr), (
        "発火したがファイル名を言っていない:\n" + r.stdout + r.stderr)


def test_no_transient_is_excused_by_name_any_more():
    """免除一覧に .tmp 名を戻さない。

    戻すと、その名前だけは「残っていても」免除される状態に逆戻りする
    (免除の意味が『消えたから無害』ではなく『この名前だから無害』に化ける)。
    """
    import conftest
    offenders = [n for n in conftest._NOT_THE_TESTS_FAULT if n.endswith(".tmp")]
    assert offenders == [], (
        "途中半分を名前で免除している: " + repr(offenders) +
        " — 免除は _report_persisting (残ったかどうか) が所管する")


def test_the_guard_actually_routes_through_the_persistence_filter():
    """判定関数が在るだけでなく、ガードがそれを**通っている**ことを留める。

    上の unit test は _report_persisting の論理を測るが、fixture がそれを呼ぶのを
    やめても緑のままになる (関数が定義されたまま未使用になるだけ)。実際に反転
    テストで確認した: 呼び出しを外しても unit test は通った。「足した」と
    「全域に効く」は別なので、消費側の配線も構造で留める。

    文字列の部分一致ではなく構文木から呼び出し名を抽出する (共有プリミティブ
    tests/_ast_structural.py)。help 文や docstring に名前が出ているだけで
    素通りするのが緩い一致の失敗形。
    """
    import ast

    from _ast_structural import called_names_in_function

    src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    called = called_names_in_function(ast.parse(src), "_fail_on_repo_beacon_write")
    assert "_report_persisting" in called, (
        "ガードが永続性フィルタを通っていない — 報告は after スナップショットの "
        "差分そのままになり、窓に入った途中半分で無関係なテストを名指しする。"
        "実際に呼ばれている名前: " + repr(sorted(called)))


# --- 掃討のための計器: 全作成者を数える (ms-166 e-6833 ステップ1) --------------
#
# ガード本体は「置き去りにされたか」を軸にしているので、ある名前を **最初に作った**
# テストしか報告しない (2 人目以降は before スナップショットに既にその名前が在る)。
# 一覧 (KNOWN_LEAKS) を空にするには「その名前を作る全員」が要るが、ガードからは
# 1 人しか見えない。だから 1 件直すたびに別の 1 件が名指しされ、収束しない。
#
# conftest の注記が定めた順序: ①全作成者を報告する形に変える ②フル実行で一覧を得る
# ③潰してから一覧を空にする。ここは①を留める。

def test_the_census_is_off_by_default():
    """既定では無作用であること。

    更新時刻という軸はガードが意図的に見ていない (開発者自身のセッションが常時
    書き換えるのでノイズになる)。測定したいときだけ立てる。
    """
    import conftest
    import os as _os
    assert conftest._CENSUS_ON == (_os.environ.get("BEACON_LEAK_CENSUS") == "1")
    # 既定の実行で成果物を残さないこと
    if not conftest._CENSUS_ON:
        assert not _os.path.exists(
            _os.path.join(_os.path.dirname(__file__), "_leak_census.json")), (
            "既定の実行で国勢調査の成果物が残っています")


def test_the_census_counts_every_writer_not_just_the_first(tmp_path):
    """**2 人目以降の作成者も数えること。** これが計器の存在理由。

    ガード本体は 1 人目しか見えない。同じ名前を 2 つのテストが書いたとき、両方が
    一覧に出ること、かつ書いていないテストが出ないことを実測する。
    """
    watch = tmp_path / "watched"
    watch.mkdir()
    body = (
        "import os, time\n"
        f"WATCH = {str(watch)!r}\n"
        "def test_writer_one():\n"
        "    time.sleep(0.01)\n"
        "    open(os.path.join(WATCH, 'session.json'), 'w').write('{}')\n"
        "def test_writer_two():\n"
        "    time.sleep(0.01)\n"
        "    open(os.path.join(WATCH, 'session.json'), 'w').write('{}')\n"
        "def test_not_a_writer():\n"
        "    pass\n"
    )
    with _ProbeIn_tests(body, "test_e6833_census_probe.py") as t:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", str(t), "-q", "-p", "no:cacheprovider"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ,
                 "BEACON_LEAK_CENSUS": "1",
                 "BEACON_TEST_REPO_BEACON_DIR": str(watch),
                 "PYTHONPATH": os.pathsep.join(
                     [str(ROOT / "lib"), str(ROOT / "tests")])})
    out = r.stdout + r.stderr
    assert "[leak-census]" in out, "国勢調査が走っていません:\n" + out[-800:]
    census_file = ROOT / "tests" / "_leak_census.json"
    try:
        data = json.loads(census_file.read_text(encoding="utf-8"))
    finally:
        if census_file.exists():
            census_file.unlink()
    writers = data.get("session.json", [])
    assert len(writers) == 2, (
        "全作成者を数えていません (1 人目だけだと収束しない): " + repr(data))
    assert all("writer" in w for w in writers), repr(writers)
    assert not any("not_a_writer" in w for w in writers), (
        "書いていないテストを作成者に数えています: " + repr(writers))


def test_the_census_does_not_define_a_second_sessionfinish_hook():
    """同名の hook を 2 つ定義しないこと。

    最初そう書いたが、この conftest には既に ``pytest_sessionfinish`` が在り
    (後に定義されている方が勝つ)、私の hook は **一度も呼ばれなかった**。
    pytest は落ちも警告もしないので、書き出されないことに自分で気づくまで
    分からなかった。同じファイルに同名の hook を足すのは、黙って無効になる形。
    """
    import ast
    src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    names = [n.name for n in ast.parse(src).body
             if isinstance(n, ast.FunctionDef) and n.name.startswith("pytest_")]
    dups = {n for n in names if names.count(n) > 1}
    assert not dups, (
        "同名の pytest hook が複数定義されています (後のものが前を黙って無効化します): "
        + repr(sorted(dups)))
