"""定期発火の ``now_iso`` が無条件に束縛され、Trek 側の再束縛が漏れないことを固定する。

**守っている実障害 (ms-66、2026-07-25 に文脈ゼロの調査で発見)**: ``now_iso`` は
以前 trek-quiesce の条件分岐の中だけで代入されていた。Python はこれで関数全体の
ローカル変数にするので、その分岐が走らない tick (= 普段) では
``_fire_due_scheduled(now_iso)`` が ``UnboundLocalError`` を上げ、それが bare
``except`` に飲まれて、**ms-107 (804dfa16) 以降サーバ定期発火の Operation 発火経路が
毎 tick 無音で死んでいた**。Trek の集約は影響を受けなかった (``trek_mod.utcnow_iso()``
を直接呼ぶため)。症状: due で envelope 承認済みの server_tick Operation が一度も
発火しない (``meta.last_fired_at`` が null のまま) のに、tick 自体は生きている。

**ms-185 e-7065 で足した 2 つ目の固定**: 上の穴は「Trek の集約」と「ターゲットに
依存しない定期発火」が 1 つの関数本体に同居していたことに由来する。同居している限り、
Trek 側が ``now_iso`` を再束縛すると (``server/app.py`` の quiesce 分岐が実際に
``now_iso = trek_mod.utcnow_iso()`` をやっている) その値が ``_fire_due_scheduled`` に
漏れる — つまり quiesce が走った tick だけ、注入された時計ではなく実時計が渡る
(ms-128 AC12 の意図が破れる)。e-7065 で ``_run_trek_tick`` を別関数に出して構造的に
閉じたので、**その構造そのもの**を固定する: ``_fire_due_scheduled`` は
``_run_system_tick`` から呼ばれ、``_run_trek_tick`` からは呼ばれない。

実行時テストでなく AST (構文木) テストなのは、この欠陥が DB の種類に依らない
変数スコープの問題だから — 束縛が無条件で呼び出しより前にあることを見るのが、
一番正確で一番安い。
"""

from __future__ import annotations

import ast
import os

APP = os.path.join(os.path.dirname(__file__), "..", "server", "app.py")

# 定期発火の入口の本体 (= Trek 非依存の正本)。``_fire_due_scheduled`` を呼ぶ側。
SYSTEM_TICK_FN = "_run_system_tick"
# Trek の集約。``now_iso`` を再束縛するが、それがここから漏れてはいけない側。
TREK_TICK_FN = "_run_trek_tick"


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(open(APP, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(
        f"{name} not found in server/app.py — 定期発火の本体の名前が変わったなら、"
        f"このガードも一緒に移すこと (置いて行くと、守りたい場所で赤くなれない)"
    )


def _toplevel_now_iso_lineno(fn: ast.AST):
    """``now_iso = ...`` の代入のうち、**関数本体の直下** (= if/for/try に入れ子に
    なっていない) のものの行番号。"""
    for stmt in fn.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "now_iso":
                    return stmt.lineno
    return None


def _fire_due_scheduled_call_lineno(fn: ast.AST):
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_fire_due_scheduled":
            return node.lineno
    return None


def test_now_iso_bound_unconditionally_before_fire_due_scheduled():
    fn = _fn(SYSTEM_TICK_FN)
    bind_line = _toplevel_now_iso_lineno(fn)
    assert bind_line is not None, (
        f"now_iso must be assigned at the TOP LEVEL of {SYSTEM_TICK_FN} "
        "(not only inside a conditional branch). Otherwise _fire_due_scheduled("
        "now_iso) raises UnboundLocalError on every tick without that branch, "
        "and the Operation-firing path silently dies (ms-66 regression)."
    )
    fire_line = _fire_due_scheduled_call_lineno(fn)
    assert fire_line is not None, (
        f"_fire_due_scheduled(now_iso) call not found in {SYSTEM_TICK_FN} — "
        "ターゲット非依存の定期発火 (Operation / 締切リマインド) が入口から"
        "呼ばれていない = 本番の自律が駆動されない"
    )
    assert bind_line < fire_line, (
        "the top-level now_iso binding must PRECEDE the _fire_due_scheduled call "
        f"(bind at line {bind_line}, call at line {fire_line})."
    )


def test_trek_tick_cannot_leak_its_now_iso_into_scheduled_firing():
    """ms-185 e-7065 — Trek の集約から ``_fire_due_scheduled`` を呼んではいけない。

    呼べる位置にあると、Trek が quiesce 分岐で ``now_iso`` を実時計に再束縛した値が
    ターゲット非依存の発火に渡る (= 分離前に実際そうなっていた)。構造で閉じたことを
    固定する。
    """
    trek_fn = _fn(TREK_TICK_FN)
    leak_line = _fire_due_scheduled_call_lineno(trek_fn)
    assert leak_line is None, (
        f"_fire_due_scheduled must NOT be called from {TREK_TICK_FN} "
        f"(found at line {leak_line}). Trek の集約はローカルの now_iso を quiesce "
        "分岐で実時計に再束縛するので、同じ関数からターゲット非依存の発火を呼ぶと "
        "その実時計が漏れる (ms-128 AC12 が quiesce が走った tick だけ破れる)。"
        f"発火は {SYSTEM_TICK_FN} から呼ぶこと。"
    )


def test_trek_tick_binds_its_own_now_iso_unconditionally():
    """ms-185 e-7065 — Trek 側も自分の ``now_iso`` を関数本体の直下で束縛する。

    分離前は呼び出し元の同名変数がこの役を担っていた。分離で呼び出し元の変数は
    見えなくなるので、quiesce 分岐を通らない経路から ``now_iso`` を読む箇所
    (quiesce 通知の後段) が UnboundLocalError になりうる。無条件束縛で閉じる。
    """
    trek_fn = _fn(TREK_TICK_FN)
    assert _toplevel_now_iso_lineno(trek_fn) is not None, (
        f"now_iso must be assigned at the TOP LEVEL of {TREK_TICK_FN}. "
        "分離前は呼び出し元の変数に依存していたので、ここで束縛しないと quiesce "
        "分岐を通らない経路で UnboundLocalError になる (ms-66 と同じ型の穴)。"
    )


def test_scheduled_fire_error_is_not_silently_swallowed():
    """The bare except around _fire_due_scheduled must log the error (to stderr /
    server logger), so this class of silent-death never hides again (ms-66)."""
    src = open(APP, encoding="utf-8").read()
    # locate the except block guarding _fire_due_scheduled and assert it logs.
    assert "_fire_due_scheduled(now_iso)" in src
    # the handler references the server logger with the op-exc, right after the
    # call. Use rindex so a doc-comment mention of the call doesn't shadow the
    # real call site (the actual `try: scheduled_fires = _fire_due_scheduled...`).
    idx = src.rindex("_fire_due_scheduled(now_iso)")
    window = src[idx: idx + 600]
    assert "_server_logger.error" in window, (
        "the except handler around _fire_due_scheduled must log the exception "
        "(a bare swallow is exactly what hid the ms-66 UnboundLocalError)."
    )


def test_scheduled_firing_entrypoint_is_trek_neutral():
    """ms-185 e-7065 — 定期発火の正本の入口が Trek 由来の名前でない。

    Trek を畳む (ms-185) ときに、本番の自律 (Operation の発火 / 締切リマインド) を
    一緒に落とさないための住所の固定。旧入口は転送として残るので、両方在ることを見る。
    """
    src = open(APP, encoding="utf-8").read()
    assert '@app.post("/api/system/tick")' in src, (
        "Trek 非依存の定期発火の入口 POST /api/system/tick が無い。Trek を消すと "
        "本番の Operation 発火と締切リマインドが一緒に止まる (ms-145 / e-4104 で"
        "一度踏んだ型)。"
    )
    assert '@app.post("/api/system/trek-scheduler/tick")' in src, (
        "旧入口 POST /api/system/trek-scheduler/tick が無い。外部の駆動役 "
        "(本番 VPS の systemd timer) の設定を更新し終えるまでは転送として残すこと "
        "(撤去は新入口経由の発火を本番で実測したあと)。"
    )
