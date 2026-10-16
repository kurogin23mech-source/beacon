"""ms-184 — `beacon recap` の旗 → env 写像が両フロントで一致することの試験.

なぜ要るか
==========
`scripts/check-cli-help-drift.py` の旗パリティ (`REQUIRED_FLAG_PARITY`) は
bash 側の `cmd_<verb>()` **関数**から旗を読む形なので、case arm として書かれた
動詞 (`attention` / `session working` / `bus send` / そして `recap`) を
**構造的に見られない**。そのスクリプト自身のコメントが、その形の動詞は専用の
パリティ試験で留めると索引している。これがその `recap` 版。

実測した穴 (独立 maintainability レビュー PR#803):
`tests/test_recap_entrypath_ms184.py` の旗検査は、**Skill の例文に出てくる旗**
しか突き合わせていなかった (= `--project` / `--since` / `--json` の 3 つ)。
`--evidence` / `--limit` / `--from` / `--to` の 4 つは両フロントに正しく配線
されてはいるが、**bash の `--evidence)` を消しても全部緑のまま通る**。
覆われていたのは偶然 (Skill の散文が偶々その旗を使っていた) で、契約では
なかった。この repo の動機づけ事故 (PR #74 で Windows の `session` /
`channel install` が落ちた件) と同じ型。

やり方は既存の作法に倣う: 両フロントを**同じ argv で実際に起動**し、
`commands.py` へ渡す env を突き合わせる。旗の名前の有無ではなく写像を見るので、
「宣言はあるが値が流れない」も捕まる。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: commands.py を env 印字スタブに差し替える。recap は BEACON_RECAP_PROJECT /
#: BEACON_SINCE / BEACON_FROM / BEACON_TO / BEACON_EVIDENCE / BEACON_LIMIT /
#: BEACON_JSON を使うので、その前置きを拾う。
_STUB = '''\
import json, os, sys
want = ("BEACON_RECAP_", "BEACON_SINCE", "BEACON_FROM", "BEACON_TO",
        "BEACON_EVIDENCE", "BEACON_LIMIT", "BEACON_JSON")
keys = {k: v for k, v in os.environ.items() if k.startswith(want)}
print(json.dumps({"subcmd": sys.argv[1], "env": keys}, ensure_ascii=False))
'''


@pytest.fixture
def stub_tree(tmp_path):
    """commands.py を env 印字スタブに差し替えた影の木 (実データには触れない)."""
    shutil.copytree(ROOT / "bin", tmp_path / "bin")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "commands.py").write_text(_STUB, encoding="utf-8")
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({"name": "probe", "milestones": []}), encoding="utf-8")
    return tmp_path


def _run_bash(stub_tree, argv):
    env = dict(os.environ)
    env.pop("BEACON_PROJECT_FILE", None)
    proc = subprocess.run(
        ["bash", str(stub_tree / "bin" / "beacon"), *argv],
        capture_output=True, text=True, cwd=str(stub_tree), env=env)
    return proc


def _bash_payload(proc):
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError("stub produced no JSON:\n" + proc.stdout + proc.stderr)


def _run_python(stub_tree, argv):
    sys.path.insert(0, str(ROOT))
    import beacon_cli.dispatch as d
    captured = {}

    def _fake(root, subcmd, env_overrides, *, extra_args=None):
        captured["subcmd"] = subcmd
        captured["env"] = dict(env_overrides)
        return 0

    real, real_ensure = d._run_commands_py, d._ensure_project
    d._run_commands_py = _fake
    d._ensure_project = lambda: None
    buf = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = buf
    try:
        rc = d.dispatch(stub_tree, list(argv))
    finally:
        sys.stderr = real_stderr
        d._run_commands_py = real
        d._ensure_project = real_ensure
    captured["stderr"] = buf.getvalue()
    captured["rc"] = rc
    return captured


#: (argv, 期待される commands.py key, 期待される env の部分集合).
#: **recap の全旗を 1 件以上の case で通す** — Skill の散文が偶々使う旗だけ、
#: という偶然の覆いにしない。
CASES = [
    (["recap", "targets", "--since", "yesterday"],
     "recap_targets", {"BEACON_SINCE": "yesterday"}),
    (["recap", "targets", "--project", "p-9", "--since", "3d"],
     "recap_targets", {"BEACON_RECAP_PROJECT": "p-9", "BEACON_SINCE": "3d"}),
    (["recap", "targets", "--from", "2026-10-01", "--to", "2026-10-05"],
     "recap_targets", {"BEACON_FROM": "2026-10-01", "BEACON_TO": "2026-10-05"}),
    (["recap", "targets", "--since", "7d", "--evidence", "3", "--limit", "2"],
     "recap_targets", {"BEACON_EVIDENCE": "3", "BEACON_LIMIT": "2"}),
    (["recap", "targets", "--since", "today", "--json"],
     "recap_targets", {"BEACON_JSON": "1"}),
    (["recap", "projects", "--since", "last-week"],
     "recap_projects", {"BEACON_SINCE": "last-week"}),
    (["recap", "projects", "--from", "2026-09-01", "--to", "2026-09-30",
      "--limit", "5", "--json"],
     "recap_projects", {"BEACON_FROM": "2026-09-01", "BEACON_TO": "2026-09-30",
                        "BEACON_LIMIT": "5", "BEACON_JSON": "1"}),
]


@pytest.mark.parametrize("argv,subcmd,expected", CASES,
                         ids=[" ".join(c[0]) for c in CASES])
def test_both_frontends_hand_commands_py_the_same_env(stub_tree, argv, subcmd,
                                                      expected):
    """同じ argv を両フロントに与え、commands.py へ渡る key と env を比べる."""
    bash = _bash_payload(_run_bash(stub_tree, argv))
    py = _run_python(stub_tree, argv)

    assert bash["subcmd"] == subcmd, \
        f"bash が呼ぶ backend が {bash['subcmd']!r} (期待 {subcmd!r})"
    assert py["subcmd"] == subcmd, \
        f"python が呼ぶ backend が {py['subcmd']!r} (期待 {subcmd!r})"

    for key, value in expected.items():
        assert bash["env"].get(key) == value, (
            f"bash: {key} が {bash['env'].get(key)!r} (期待 {value!r}) — "
            "旗が宣言されていても値が流れていない可能性")
        assert py["env"].get(key) == value, (
            f"python: {key} が {py['env'].get(key)!r} (期待 {value!r}) — "
            "Windows / pipx 利用者はこの旗が効かない")


#: 旗のうち **env に流れない** もの = 拒否されるのが正しい挙動。被覆表から
#: 黙って除外すると「覆われているつもり」が再発するので、どの試験が代わりに
#: 留めているかを名前で索引する (スクリプト側コメントが case arm 動詞について
#: やっているのと同じ作法)。
REFUSED_FLAGS = {
    ("projects", "--project"): "test_both_frontends_refuse_project_on_projects",
}


def test_both_frontends_refuse_project_on_projects(stub_tree):
    """`recap projects --project` は両フロントで拒否され、同じ誘導を出す.

    `projects` は既に所有プロジェクト全体を見るので、`--project` はこの旗を
    付けた人が「深さ」を欲しくて「幅」の動詞を打ったことを意味する。黙って
    無視すると、名指ししたはずの 1 プロジェクトの答えに見える 12 件の掃引が
    返る。Python 側はこの旗を**宣言**している (宣言しないと argparse の汎用
    エラーになり理由も誘導も消える) ので、宣言と拒否が両方在ることを実駆動で
    確かめる。
    """
    argv = ["recap", "projects", "--project", "p-9"]

    bash = _run_bash(stub_tree, argv)
    assert bash.returncode == 1, f"bash rc={bash.returncode} (期待 1)"
    assert "--project is not accepted" in (bash.stdout + bash.stderr)
    assert "recap targets --project p-9" in (bash.stdout + bash.stderr), \
        "拒否するだけでなく、どう打ち直すかを示すこと"
    assert "{" not in bash.stdout, "拒否なのに commands.py へ流れている"

    py = _run_python(stub_tree, argv)
    assert py["rc"] == 1, f"python rc={py['rc']} (期待 1)"
    assert "--project is not accepted" in py["stderr"]
    assert "recap targets --project p-9" in py["stderr"]
    assert "subcmd" not in py, "拒否なのに commands.py へ流れている"


def test_every_declared_flag_is_exercised_by_a_case():
    """CASES が recap の**全旗**を覆っていることを機械で確かめる.

    これが無いと、旗を 1 本足した人が CASES に足し忘れても緑のままになり、
    「覆われているつもりの偶然」が再発する (それがこの試験の起点)。
    母集団は Python のパーサ宣言から導出する (手書きの一覧にしない)。
    """
    src = (ROOT / "beacon_cli" / "dispatch.py").read_text(encoding="utf-8")
    declared: dict = {}
    for sub in ("targets", "projects"):
        var = f"p_recap_{sub}"
        flags = set()
        for line in src.splitlines():
            if line.strip().startswith(f"{var}.add_argument("):
                for tok in line.split('"'):
                    if tok.startswith("--") and tok not in ("--help",):
                        flags.add(tok)
        assert flags, f"recap {sub} のパーサ宣言が読めなかった"
        declared[sub] = flags

    exercised: dict = {"targets": set(), "projects": set()}
    for argv, _subcmd, _expected in CASES:
        sub = argv[1]
        for tok in argv:
            if tok.startswith("--"):
                exercised[sub].add(tok)

    for sub, flags in declared.items():
        refused = {f for (s_, f) in REFUSED_FLAGS if s_ == sub}
        missing = sorted(flags - exercised[sub] - refused)
        assert not missing, (
            f"`recap {sub}` の旗 {missing} を CASES が 1 度も通していない — "
            "両フロント一致の契約から漏れる (この試験が存在する理由そのもの)。"
            "env に流れない旗なら REFUSED_FLAGS に、どの試験が留めるかを"
            "名前付きで索引すること")

    # 索引した試験が実在することも確かめる (索引が腐ると黙った免除になる)
    here = set(globals())
    for (sub, flag), test_name in REFUSED_FLAGS.items():
        assert test_name in here, (
            f"REFUSED_FLAGS が `recap {sub} {flag}` の留め先として "
            f"{test_name} を指しているが、その試験が存在しない")
