"""値を取るフラグに値が無いとき、黙って落ちないこと (ms-178 / e-7150).

独立した AX レビュー (2026-10-26) が実測で突いた穴。`bin/beacon` の値取りフラグは
`_need_flag_value "$#" "$1"` を通してから `shift 2` する作法になっており (当時 9 箇所)、
値が無ければ `Error: フラグ '<flag>' に値がありません` を出して exit 1 する。ところが
この差分で足した 3 つのフラグはその作法を通していなかった。

実害は「エラーが出ない」ことではなく **何も出ない** ことだった: `set -euo pipefail`
下で `shift 2` が失敗し、beacon 全体が **exit 1 / 出力ゼロ** で終わる。呼び出した側は
どのフラグが悪かったのかも、そもそも引数の問題なのかも分からない。もう一つの経路は
もっと悪く、後ろに別のフラグがあるとそれを値として食う: `note reap --since-min --yes`
は `--yes` を `--since-min` の値として飲み込み、明示した確認フラグが消える。

このモジュールは **実際に `bin/beacon` を起動して**確かめる。bash の引数解析は Python
から import できないので、ここだけは subprocess で測るのが唯一の手段になる。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BEACON = ROOT / "bin" / "beacon"

# この差分が足した値取りフラグ。増えたらここに足す (= 作法から外れた新フラグが
# 無言で入るのを止める見張り)。
VALUE_FLAGS = [
    ("note", "list", "--since-days"),
    ("note", "list", "--limit"),
    ("note", "reap", "--since-min"),
]


@pytest.fixture
def project(tmp_path):
    """本物の repo を触らない一時プロジェクト + その env。"""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    (beacon / "session_notes.jsonl").write_text(
        json.dumps({"ts": "2026-10-25T00:00:00+00:00", "text": "x"}) + "\n",
        encoding="utf-8")
    import os
    env = dict(os.environ)
    env["BEACON_PROJECT_FILE"] = str(beacon / "project.json")
    return tmp_path, env


def _run(project, args):
    cwd, env = project
    return subprocess.run([sys.executable and str(BEACON)] + args,
                          cwd=str(cwd), env=env, capture_output=True,
                          text=True, timeout=120)


@pytest.mark.skipif(not BEACON.exists(), reason="bin/beacon がない")
@pytest.mark.parametrize("noun,sub,flag", VALUE_FLAGS)
def test_missing_value_is_diagnosed_not_silent(project, noun, sub, flag):
    """値を省いたら、どのフラグが悪いかを名指しして止まる。

    直す前はここが exit 1 / 出力ゼロだった。「落ちた」ことしか分からない形は、
    エラーメッセージが無いのと同じではなく **それより悪い** (呼び出した側は
    別の原因を疑って探し回る)。
    """
    r = _run(project, [noun, sub, flag])
    assert r.returncode != 0, "値なしのフラグが受理された"
    combined = r.stdout + r.stderr
    assert combined.strip(), "exit だけして何も言っていない (旧実装の挙動)"
    assert flag in combined, combined
    assert "値がありません" in combined, combined


@pytest.mark.skipif(not BEACON.exists(), reason="bin/beacon がない")
def test_following_flag_is_not_swallowed_as_a_value(project):
    """後ろのフラグを値として食わない。

    `note reap --since-min --yes` で `--yes` が食われると、利用者が明示した確認が
    消える。削除されない方向 (= 拒否) に倒れるので直接のデータ損失ではないが、
    「書いた通りに動かず、そうと分からない」形そのものである。
    """
    r = _run(project, ["note", "reap", "--since-min", "--yes"])
    combined = r.stdout + r.stderr
    assert r.returncode != 0, "フラグを値として食ったまま続行した"
    assert "--yes" in combined and "--since-min" in combined, combined
    # 「値が無い」とは別の診断であることまで見る。残り引数が 2 本あるので
    # 引数の個数を数えるだけのガードはここを通してしまう (実際に通していた)。
    assert "別のフラグを値として読み込んでいます" in combined, combined


@pytest.mark.skipif(not BEACON.exists(), reason="bin/beacon がない")
def test_guarded_flags_still_accept_a_real_value(project):
    """ガードを足したことで正常系を壊していない。"""
    r = _run(project, ["note", "list", "--limit", "5", "--json"])
    assert r.returncode == 0, r.stdout + r.stderr
    json.loads(r.stdout.strip() or "[]")


@pytest.mark.skipif(not BEACON.exists(), reason="bin/beacon がない")
def test_every_new_value_flag_goes_through_the_guard():
    """差分が足した値取りフラグが、全部ガードを通っていること (静的な見張り)。

    上の 3 本は起動して確かめているが、それは「このフラグ」の話でしかない。
    4 本目を足した人がガードを忘れても気付けるように、`note` のブロック内の
    `shift 2` が全て `_need_flag_value` と同じ行に在ることを構造として固定する。
    """
    lines = BEACON.read_text(encoding="utf-8").splitlines()
    # `note)` ブロックの範囲を取る (次の同インデントのケースラベルまで)。
    start = next(i for i, l in enumerate(lines) if l.strip() == "note)")
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].startswith("    ") and lines[i].strip().endswith(")")
               and not lines[i].startswith("     ")
               and lines[i].strip() not in ("note)",))
    block = lines[start + 1:end]

    # 見るのは **フラグの case 行** だけ。各サブコマンドの先頭にある裸の
    # `shift 2` は「動詞とサブコマンドを捨てる」ための固定長の消費で、値を
    # 取るフラグとは別物 (ここを一緒に数えると、守る対象でない行で赤くなり、
    # 除外リストを足して回る運用になる = 見張りが鈍る)。
    flag_cases = [
        (start + 1 + n, l.strip()) for n, l in enumerate(block)
        if l.strip().startswith("--") and "shift 2" in l
    ]
    assert flag_cases, "note ブロックに値取りフラグの case が見つからない (検査が空振り)"

    unguarded = [(ln, text) for ln, text in flag_cases
                 if "_need_flag_value" not in text]
    assert unguarded == [], (
        "note ブロックに値取りフラグのガード漏れ: %s" % unguarded)
