"""Go ビューワーの「要対応」判定が、正典の状態集合を取りこぼしていないこと。

運用室の「要対応のみ」は、サーバが導出した正典状態 (lib/bus_liveness.ALL_STATES、
7 値の凍結集合) を Go 側で順位付けして決める。Go はその語彙を **文字列リテラルで
再実装している** ので、Python 側に状態が足されたとき Go の表が置いて行かれても
どちらの言語の試験も落ちない — ms-177 の interrupted (= 事故で止まった) が roster の
順位表を素通りしたのと同じ型の穴。

#804 の独立レビュー (保守性) の指摘で足した突き合わせ。先例は
tests/test_attention_roster_e6293.py の網羅性ガード (Python 内部の同種の縛り)。

**substring 一致で済ませない**: help 文や別のコメントに状態名が出てくるだけで素通り
するので、順位表のブロックを構造的に切り出してキー集合として比べる (緩い一致は
偽の緑を作り、緑のガードは信頼されるぶん無ガードより悪い)。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lib"))

import bus_liveness  # noqa: E402

GO_SOURCE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "viewer", "sessions_view.go")

TABLE_NAME = "serverStateWorking"


def extract_ranking_table(source: str) -> dict:
    """Go の ``var serverStateWorking = map[string]bool{...}`` を構造的に読む。

    ブロックを波括弧の対応で切り出してから ``"key": value`` だけを拾う。表が
    見つからない / 空で返ることを呼び出し側が「一致」と誤読しないよう、
    どちらも例外にする (無言の 0 件一致は偽の緑そのもの)。
    """
    head = re.search(
        r"var\s+%s\s*=\s*map\[string\]bool\s*\{" % re.escape(TABLE_NAME),
        source)
    if head is None:
        raise AssertionError(
            "Go 側の順位表 %s が見つかりません。改名したならこのガードも直して"
            "ください (ガードが黙って無効になるのを防ぐため例外にしています)。"
            % TABLE_NAME)
    i = head.end() - 1  # 開き波括弧
    depth = 0
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                body = source[i + 1:j]
                break
    else:
        raise AssertionError("順位表の波括弧が閉じていません")

    table = {}
    for key, value in re.findall(
            r'"([^"]+)"\s*:\s*(true|false)\s*,', body):
        table[key] = (value == "true")
    if not table:
        raise AssertionError("順位表から 1 件も読めませんでした (書式が変わった?)")
    return table


@pytest.fixture(scope="module")
def ranking():
    with open(GO_SOURCE, encoding="utf-8") as fh:
        return extract_ranking_table(fh.read())


def test_go_ranking_covers_every_canonical_state(ranking):
    canonical = set(bus_liveness.ALL_STATES)
    missing = canonical - set(ranking)
    assert not missing, (
        "Go の運用室が順位付けしていない正典状態: %s。"
        "順位付けが無い状態はローカルの busy/idle 申告に流れず止まる側に倒れるため、"
        "「要対応」の集合が Python 側の意図と無言でずれます。"
        "viewer/sessions_view.go の %s に追記してください。"
        % (sorted(missing), TABLE_NAME))


def test_go_ranking_has_no_states_outside_the_canon(ranking):
    canonical = set(bus_liveness.ALL_STATES)
    extra = set(ranking) - canonical
    assert not extra, (
        "Go が正典集合に無い状態を順位付けしています: %s。"
        "状態集合は凍結集合なので、増やすなら Python 側 (ALL_STATES) が先です。"
        % sorted(extra))


def test_only_running_counts_as_working(ranking):
    """稼働と判定するのは running だけ (ここが増えると要対応から静かに行が落ちる)。"""
    working = sorted(k for k, v in ranking.items() if v)
    assert working == [bus_liveness.STATE_RUNNING], (
        "稼働と判定する状態が %s。running だけのはずです。" % working)


def test_guard_fails_when_a_state_is_dropped():
    """このガード自身が drift で赤くなることを確かめる (試験の試験)。

    網羅性ガードは「緑であること」を信頼されるので、取りこぼしを本当に検出できる
    かを実際に欠けた表で測る。測らないと、書式変更でパターンが当たらなくなった
    ときに 0 件一致で素通りする。
    """
    crippled = (
        'var %s = map[string]bool{\n'
        '\t"running":        true,\n'
        '\t"idle":           false,\n'
        '}\n' % TABLE_NAME)
    table = extract_ranking_table(crippled)
    missing = set(bus_liveness.ALL_STATES) - set(table)
    assert missing, "欠けた表でも取りこぼしを検出できていません (ガードが無効)"
    assert "interrupted" in missing


def test_guard_fails_when_table_is_renamed_away():
    """表が消えた/改名された場合に、黙って一致扱いにならないこと。"""
    with pytest.raises(AssertionError):
        extract_ranking_table("var somethingElse = map[string]bool{}\n")
