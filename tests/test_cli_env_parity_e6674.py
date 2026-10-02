"""旗 → env の写像が両フロントで揃っていることの機械検査 (ms-160 e-6674).

旗の **名前** を比べる check-cli-help-drift.py はこの級を原理的に見られない。
片方のフロントに無い旗は「不一致」ではなく「不在」で、両方に無ければ揃って
いると数えられるため。実際に落ちていたのは旗そのものではなく、argv を環境変数へ
写す対応表の方で、それは 2 言語で手書きされている。

実測された代償: beacon doc add --force / search --source / deploy record
--version / cloud list --json ほか約 45 件が bash では通り python では
`unrecognized arguments` で落ちる。PR #778 の bus send --recipient-confirmed は
Windows から cross-user DM を **送れない** 状態を作っていた。

この試験が固定するのは 3 つ:
  1. 現状が緑であること (= KNOWN_GAPS で既存の負債を許容した状態)
  2. 新しい drift を入れると赤くなること (= 検出力の実測)
  3. 直した負債の行を残すと赤くなること (= stale 検出。残った行は同じ場所の
     次の退行を黙って免除する)
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _checker():
    spec = importlib.util.spec_from_file_location(
        "cli_env_parity", ROOT / "scripts" / "check-cli-env-parity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_new_env_parity_drift_today():
    mod = _checker()
    rows = mod.collect()
    assert rows == [], (
        "新しい 旗→env の drift です。該当フロントに写像を足すか、"
        "フロントが渡すべき値でないなら AMBIENT_ENV に入れてください: " + repr(rows))


def test_the_backlog_is_recorded_not_hidden():
    """KNOWN_GAPS は『判定済みで許した』ではなく『記録された負債』。空にすり替えて
    検査を無意味にしていないことを確認する。"""
    mod = _checker()
    assert len(mod.KNOWN_GAPS) > 0
    for row, why in mod.KNOWN_GAPS.items():
        assert len(row) == 3 and row[2] in ("bash", "python"), row
        # Every row states its verdict in place. A bare coordinate would leave
        # whoever burns the list down unable to tell a real bug from a
        # deliberate non-gap without re-investigating all 68 (PR #781 M-2).
        assert isinstance(why, str) and len(why) > 10, (row, why)


def _shadow(tmp_path):
    dst = tmp_path / "repo"
    (dst).mkdir()
    for part in ("lib", "bin", "beacon_cli"):
        shutil.copytree(ROOT / part, dst / part)
    return dst


def test_a_new_unpassed_env_is_reported(tmp_path):
    """test-the-test: 新しい動詞が env を読むのにどちらのフロントも渡さない状態を
    作ると報告されること。検出力を実測しないガードは、何も検出しないガードと
    見分けがつかない。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    # a verb that reads a brand-new env name, dispatched by both frontends
    (repo / "lib" / "cmd_probe_e6674.py").write_text(
        "import os\n"
        "def cmd_probe_e6674_demo():\n"
        "    return os.environ.get('BEACON_NEWLY_UNWIRED', '')\n",
        encoding="utf-8")
    binf = repo / "bin" / "beacon"
    binf.write_text(binf.read_text(encoding="utf-8")
                    + '\n# shadow: python3 "$COMMANDS_PY" probe_e6674_demo\n',
                    encoding="utf-8")
    disp = repo / "beacon_cli" / "dispatch.py"
    disp.write_text(disp.read_text(encoding="utf-8")
                    + '\ndef _handle_probe_e6674(root, args):\n'
                      '    return _run_commands_py(root, "probe_e6674_demo", {})\n',
                    encoding="utf-8")
    rows = mod.collect(repo)
    names = {(v, e) for v, e, _ in rows}
    assert ("probe_e6674_demo", "BEACON_NEWLY_UNWIRED") in names, rows


def test_an_env_the_frontend_does_pass_is_not_reported(tmp_path):
    """逆向き: フロントが実際に渡している env は報告しない。誤検出するガードは
    無視されるようになり、無視されたガードは何も守らない。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    (repo / "lib" / "cmd_probe_e6674.py").write_text(
        "import os\n"
        "def cmd_probe_e6674_demo():\n"
        "    return os.environ.get('BEACON_WIRED_OK', '')\n",
        encoding="utf-8")
    binf = repo / "bin" / "beacon"
    binf.write_text(binf.read_text(encoding="utf-8")
                    + '\n# shadow\nBEACON_WIRED_OK="$x" python3 "$COMMANDS_PY" probe_e6674_demo\n',
                    encoding="utf-8")
    disp = repo / "beacon_cli" / "dispatch.py"
    disp.write_text(disp.read_text(encoding="utf-8")
                    + '\ndef _handle_probe_e6674(root, args):\n'
                      '    env = {"BEACON_WIRED_OK": args.x}\n'
                      '    return _run_commands_py(root, "probe_e6674_demo", env)\n',
                    encoding="utf-8")
    names = {(v, e) for v, e, _ in mod.collect(repo)}
    assert ("probe_e6674_demo", "BEACON_WIRED_OK") not in names, mod.collect(repo)


def test_ambient_env_is_not_reported(tmp_path):
    """運用者 / 試験用の env (doctor の skip 等) は、そもそもフロントが渡す値では
    ないので報告しない。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    (repo / "lib" / "cmd_probe_e6674.py").write_text(
        "import os\n"
        "def cmd_probe_e6674_demo():\n"
        "    return os.environ.get('BEACON_DEBUG', '')\n",
        encoding="utf-8")
    binf = repo / "bin" / "beacon"
    binf.write_text(binf.read_text(encoding="utf-8")
                    + '\n# shadow: python3 "$COMMANDS_PY" probe_e6674_demo\n',
                    encoding="utf-8")
    names = {(v, e) for v, e, _ in mod.collect(repo)}
    assert ("probe_e6674_demo", "BEACON_DEBUG") not in names


def test_stale_known_gap_is_reported(monkeypatch):
    """直った負債の行を残すと報告されること。残った行は同じ場所の次の退行を
    黙って免除するので、stale 検出は負債リストと対になっている
    (姉妹ガード check-cli-help-drift.py の stale_allowlist と同じ規律)。"""
    mod = _checker()
    mod.KNOWN_GAPS[("verb_that_does_not_exist", "BEACON_NOPE", "bash")] = "deliberately stale"
    assert mod.main() == 1

# --- 独立レビュー (PR #781) の指摘に対する回帰 ------------------------------
#
# AX レビューが実測の囮で 3 つの false negative を示した。いずれも
# 「名前がその辺にあれば配線済みと数える」= 緩い一致で、過去にも同じ型を
# 踏んでいる (ms-173 #747)。検出できないガードは、緑のときに嘘をつく。

def test_a_todo_string_is_not_wiring(tmp_path):
    """python 側: 関数内に env 名の文字列があるだけ (TODO / help 文 / ログ) で
    『渡している』と数えてはならない。意図を先にコメントで書いてから実装する
    という普通の順序で、ガードが黙る穴だった (AX-1)。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    (repo / "lib" / "cmd_probe_a.py").write_text(
        "import os\n"
        "def cmd_probe_a_demo():\n"
        "    return os.environ.get('BEACON_PROBE_A', '')\n", encoding="utf-8")
    d = repo / "beacon_cli" / "dispatch.py"
    d.write_text(d.read_text(encoding="utf-8")
                 + '\ndef _handle_probe_a(root, args):\n'
                   '    _todo = ["BEACON_PROBE_A"]   # named, not wired\n'
                   '    env = {}\n'
                   '    return _run_commands_py(root, "probe_a_demo", env)\n',
                 encoding="utf-8")
    names = {(v, e) for v, e, f in mod.collect(repo) if f == "python"}
    assert ("probe_a_demo", "BEACON_PROBE_A") in names, mod.collect(repo)


def test_an_env_built_in_a_local_dict_is_recognised(tmp_path):
    """逆向き: 実際の配線の書き方 (注釈付き代入 + dict() のコピー + 添字書き) を
    見落とさないこと。見落とすと本物の配線を『穴』と誤報告する。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    (repo / "lib" / "cmd_probe_a.py").write_text(
        "import os\n"
        "def cmd_probe_a_demo():\n"
        "    return (os.environ.get('BEACON_BASE',''), os.environ.get('BEACON_LATE',''))\n",
        encoding="utf-8")
    d = repo / "beacon_cli" / "dispatch.py"
    d.write_text(d.read_text(encoding="utf-8")
                 + '\ndef _handle_probe_a(root, args):\n'
                   '    base: dict = {"BEACON_BASE": args.a}\n'
                   '    env = dict(base)\n'
                   '    env["BEACON_LATE"] = args.b\n'
                   '    return _run_commands_py(root, "probe_a_demo", env)\n',
                 encoding="utf-8")
    names = {(v, e) for v, e, f in mod.collect(repo) if f == "python"}
    assert ("probe_a_demo", "BEACON_BASE") not in names, mod.collect(repo)
    assert ("probe_a_demo", "BEACON_LATE") not in names, mod.collect(repo)


@pytest.mark.parametrize("decoy,label", [
    ('# example: BEACON_PROBE_B=1 is how you would set it', "コメント内の代入"),
    ('echo "usage: BEACON_PROBE_B=1 ..."', "echo される usage 文字列"),
])
def test_bash_text_that_is_not_an_assignment(tmp_path, decoy, label):
    """bash 側: コメントや引用符の中の `VAR=` は配線ではない。コメントは
    コードより遅れて腐るので、囮を仕込まなくても普通の劣化で再現する (AX-2)。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    (repo / "lib" / "cmd_probe_b.py").write_text(
        "import os\n"
        "def cmd_probe_b_demo():\n"
        "    return os.environ.get('BEACON_PROBE_B', '')\n", encoding="utf-8")
    b = repo / "bin" / "beacon"
    b.write_text(b.read_text(encoding="utf-8")
                 + "\n%s\npython3 \"$COMMANDS_PY\" probe_b_demo\n" % decoy,
                 encoding="utf-8")
    names = {(v, e) for v, e, f in mod.collect(repo) if f == "bash"}
    assert ("probe_b_demo", "BEACON_PROBE_B") in names, label


def test_the_bash_side_still_sees_the_real_surface():
    """コメント除去を入れたとき、`"$COMMANDS_PY"` ごと消して **bash 側の動詞が
    0 件** になった。測っていなければ『何も見ないガード』が緑のまま出荷されて
    いた。実表面を見ていることを数で固定する。"""
    mod = _checker()
    sets, verbs = mod._bash_sets(mod.ROOT / "bin" / "beacon", mod.ROOT / "bin" / "lib")
    assert len(verbs) > 100, len(verbs)
    assert "BEACON_NOTE_TEXT" in sets.get("note_add", set()), sorted(sets.get("note_add", ()))


def test_lookback_does_not_cross_file_boundaries(tmp_path):
    """短い bin/lib/*.sh の先頭付近の dispatch 行が、直前の別ファイル末尾の
    代入を自分の覆域として数えないこと。該当する短いファイルが実在する (AX-2)。"""
    mod = _checker()
    repo = _shadow(tmp_path)
    (repo / "lib" / "cmd_probe_c.py").write_text(
        "import os\n"
        "def cmd_probe_c_demo():\n"
        "    return os.environ.get('BEACON_PROBE_C', '')\n", encoding="utf-8")
    libsh = repo / "bin" / "lib"
    (libsh / "cmd_zz_donor.sh").write_text(
        "#!/bin/bash\n" + "\n".join('BEACON_PROBE_C="x"' for _ in range(3)) + "\n",
        encoding="utf-8")
    (libsh / "cmd_zz_user.sh").write_text(
        '#!/bin/bash\npython3 "$COMMANDS_PY" probe_c_demo\n', encoding="utf-8")
    names = {(v, e) for v, e, f in mod.collect(repo) if f == "bash"}
    assert ("probe_c_demo", "BEACON_PROBE_C") in names, (
        "別ファイルの代入を自分の覆域に数えています")


def test_an_exemption_is_scoped_to_its_verb(tmp_path):
    """AMBIENT_ENV は (動詞, env) で効くこと。1 つの動詞を黙らせるつもりの
    除外が、同じ名前を読む無関係な動詞まで黙らせてはならない (AX-4)。"""
    mod = _checker()
    assert mod._is_ambient("doctor", "BEACON_DOCTOR_SKIP_MS81")
    assert not mod._is_ambient("task_add", "BEACON_DOCTOR_SKIP_MS81")
    # "*" は全域を意図したときだけの明示的な選択
    assert mod._is_ambient("anything", "BEACON_DEBUG")


def test_the_green_message_states_its_method_not_a_guarantee(capsys):
    """緑の文が手法と限界を述べること。『every env ... is passed』は、この
    検査が与えない保証を主張していた (AX-5)。読み手はラベルを信じて再導出を
    やめるので、言い過ぎたラベルは検出漏れより性質が悪い。"""
    mod = _checker()
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "no NEW" in out and "not 'every mapping is proven'" in out, out


def test_the_declined_fingerprint_argument_records_its_expiry():
    """AX-3 (high) を却下した根拠は「座標の意味が 1 つ」だが、それはこの検査が
    キーの存在だけを見ている間しか成り立たない。値の照合や分岐の網羅を足した
    瞬間に座標は 2 つの意味を持ち、fingerprint が要る。

    却下は判断の記録として残るので、**その期限も一緒に残っていること**を
    固定する。次に厳密化する人が、却下の理由ごと引き継げるように
    (親レビュー PR #781 の条件付き支持)。
    """
    src = (ROOT / "scripts" / "check-cli-env-parity.py").read_text(encoding="utf-8")
    assert "AX-3" in src, "却下した指摘の出所が残っていません"
    assert "expiry" in src, "根拠の期限が書かれていません"
    for clue in ("KEY PRESENCE", "wrong flag", "ONE branch", "fingerprint"):
        assert clue in src, clue
