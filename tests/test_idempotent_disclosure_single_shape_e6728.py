"""冪等 no-op の開示が 1 つの形に寄っていることの試験 (ms-166 e-6728)。

何が問題だったか
----------------
同じ意味の開示が **3 通りの名前** で割れていた。どれも「冪等な書き込みが no-op に
なった」ことを伝えるフラグで、違うのは **何を鍵に重複と見なすか** だけ:

    already_set         受信記録のスタンプ   鍵 = (event_id, stage)
    idempotent_replay   bus の送信           鍵 = client_event_id
    deduplicated        判断記録             鍵 = 内容 (what / 対象 / 時刻窓)

呼び出し側が知りたいことは 3 つとも同じ「私の書き込みは新しい行を作ったのか、既存の
行を返しただけなのか」。にもかかわらず名前が割れているので、呼び出し側は 3 つ覚えねば
ならず、4 つ目のエンドポイントを足す人がまた別の名前を自作する余地があった。

この file が留めるもの
----------------------
1. 開示が共有の整形口 (``idempotency.disclose``) を通ること
2. **登録されていない新しい名前を使えない** こと (= 4 つ目の自作を機械で止める)
3. 従来名が残ること (後方互換 — 外部の消費者を壊さない)
4. 呼び出し側が 1 つの読み口 (``was_no_op``) で済むこと
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import idempotency as idem  # noqa: E402

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_the_formatter_carries_both_the_canonical_and_the_legacy_name():
    """後方互換: 正規名を足しても従来の読み手が壊れないこと。"""
    out = idem.disclose({"x": 1}, no_op=True, legacy="deduplicated")
    assert out["idempotent_no_op"] is True
    assert out["deduplicated"] is True, "従来名が落ちています (既存の読み手が壊れる)"
    assert out["x"] == 1, "元の payload を保たない"


def test_the_flag_is_always_present_not_only_when_true():
    """true のときだけ生やさないこと。

    生やす形だと「キーが無い = false」と「この版の API にそのフィールドが無い」を
    呼び出し側が区別できず、``"flag" in result`` で判定するコードが常に false に倒れる
    (判断記録の独立 AX レビュー AX-3 が指摘した形。原則をここに一般化した)。
    """
    out = idem.disclose({}, no_op=False, legacy="already_set")
    assert out["idempotent_no_op"] is False
    assert out["already_set"] is False
    assert "idempotent_no_op" in out and "already_set" in out


def test_an_unregistered_flag_name_is_refused():
    """**4 つ目の名前を自作できないこと。** これがこの課題の本体。"""
    import pytest as _pytest
    with _pytest.raises(ValueError) as ei:
        idem.disclose({}, no_op=True, legacy="already_recorded")
    assert "未登録" in str(ei.value)
    assert "LEGACY_FLAGS" in str(ei.value), "どこに足せばよいかを言っていない"


def test_one_read_port_covers_every_name():
    """呼び出し側が 3 つの名前を覚えなくてよいこと。"""
    assert idem.was_no_op({"idempotent_no_op": True})
    for legacy in idem.LEGACY_FLAGS:
        assert idem.was_no_op({legacy: True}), legacy
    assert not idem.was_no_op({"idempotent_no_op": False})
    assert not idem.was_no_op({}) and not idem.was_no_op(None)


def test_the_canonical_flag_wins_over_a_stale_legacy_value():
    """正規名が在ればそれを読むこと (両者が食い違ったときの優先順位を固定する)。"""
    assert not idem.was_no_op({"idempotent_no_op": False, "deduplicated": True})
    assert idem.was_no_op({"idempotent_no_op": True, "deduplicated": False})


def test_every_disclosure_site_goes_through_the_formatter():
    """3 機構が整形口を通っていること。

    通っていない site が 1 つ残ると、そこだけ正規名が載らず、``was_no_op`` を使う
    呼び出し側が「no-op ではない」と読む (= この課題が消したはずの食い違いが復活する)。
    """
    sites = {
        "server/firestore_client.py": "already_set",
        "server/app.py": "idempotent_replay",
        "server/routers_projects.py": "deduplicated",
    }
    for rel, legacy in sites.items():
        src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
        assert f'legacy="{legacy}"' in src, (
            f"{rel} が整形口を通っていません (従来名 {legacy} を直書きしたまま)")
        assert "_idem.disclose(" in src, rel + " で disclose を呼んでいません"


def test_no_site_writes_a_disclosure_flag_as_a_bare_dict_key():
    """従来名を **辞書リテラルに直書きしていない** こと。

    直書きが残っていると、そこだけ正規名が載らない (= 整形口を通っていないのと同じ)。
    disclose の引数として渡す形 (``legacy="..."``) だけを許す。
    """
    offenders = []
    for rel in ("server/firestore_client.py", "server/app.py",
                "server/routers_projects.py"):
        src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
        for legacy in idem.LEGACY_FLAGS:
            # `"already_set": True` のような辞書キーとしての直書き
            if re.search(rf'"{legacy}"\s*:', src):
                offenders.append(f"{rel}:{legacy}")
    assert not offenders, (
        "従来名を辞書キーに直書きしている箇所があります (整形口を通してください): "
        + repr(offenders))


def test_the_readers_use_the_shared_read_port():
    """読み手が個別の名前を直接見ていないこと。"""
    for rel in ("lib/cmd_decision.py", "lib/commands_shared.py"):
        src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
        assert "_idem.was_no_op(" in src, rel + " が共有の読み口を通っていません"
        assert 'result.get("deduplicated")' not in src, (
            rel + " が従来名を直接読んでいます (3 つ覚える状態に戻ります)")


def test_the_three_mechanisms_are_documented_in_one_place():
    """どれを読めばよいかを引ける場所が 1 つあること (受入条件 2)。

    3 つの名前・出す場所・重複の鍵が揃っていること。鍵まで書くのが要点 —
    名前が割れた原因は「鍵の違いを名前に織り込んだ」ことだったので、鍵が
    書かれていないと次の人が同じ間違いをする。
    """
    doc = open(os.path.join(_ROOT, "lib", "idempotency.py"),
               encoding="utf-8").read()
    for name in idem.LEGACY_FLAGS:
        assert name in doc, name + " が表に無い"
    for key in ("event_id, stage", "client_event_id", "内容"):
        assert key in doc, "重複の鍵 " + key + " が書かれていない"
    assert len(idem.LEGACY_FLAGS) == 3, (
        "機構が増減しています。表とこの試験を同時に更新してください: "
        + repr(sorted(idem.LEGACY_FLAGS)))


def test_the_shared_module_is_imported_as_a_real_statement_everywhere():
    """``import`` が **本物の文** として在ること (docstring の中ではなく)。

    配線時に実際に踏んだ: モジュールの docstring 本文に "import" の語が含まれる
    ファイルで、文字列検索で挿入位置を決めたため **docstring の中に import を
    書き込んでいた**。構文的には通る (ただの文字列) が実行時に NameError になる。
    テストが捕まえたが、構文木で見れば最初から分かる形だった。

    「字面が在る」と「文として在る」は別 — このリポジトリが繰り返し踏んでいる
    「表層で確かめて意味を確かめていない」型の一例。
    """
    import ast
    for rel in ("server/routers_projects.py", "server/app.py",
                "server/firestore_client.py",
                "lib/cmd_decision.py", "lib/commands_shared.py"):
        tree = ast.parse(open(os.path.join(_ROOT, rel), encoding="utf-8").read())
        real = any(isinstance(n, ast.Import)
                   and any(a.name == "idempotency" for a in n.names)
                   for n in tree.body)
        assert real, (
            f"{rel} の idempotency の import がトップレベルの文になっていません "
            f"(docstring の中に書かれている可能性があります — 実行時に NameError)")
