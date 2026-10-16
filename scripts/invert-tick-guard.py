#!/usr/bin/env python3
"""定期発火のガード 2 本 (構文木 + HTTP 配線) の各主張が「守る対象を壊したとき実際に赤くなるか」
を 1 件ずつ測る反転ハーネス (ms-185 e-7065)。

緑のガードは信頼されるので、**偽の安全は無ガードより悪い**。このスクリプトは
``server/app.py`` に 1 箇所ずつ壊し方を当てて pytest を走らせ、意図したテストが
赤くなることを確認する。全ケースで「壊したのに緑」が 0 件であることが、ガードが
本物である根拠になる。

使い方: python3 scripts/invert-tick-guard.py
終了コード: 全ケースで期待どおり赤くなれば 0、1 件でも「壊したのに緑」なら 1。
``server/app.py`` は毎ケース必ず元に戻す (finally)。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "server" / "app.py"
AST_TEST = "tests/test_tick_now_iso_binding.py"
WIRE_TEST = "tests/test_system_tick_entrypoint_e7065.py"


def mutate_now_iso_into_branch(src: str) -> str:
    """_run_system_tick の無条件 now_iso 束縛を条件分岐の中へ押し込む (= ms-66 の穴)。"""
    anchor = ("    now_iso = now.strftime(\"%Y-%m-%dT%H:%M:%S.%fZ\")\n"
              "    trek = _run_trek_tick(body, now)")
    assert anchor in src, "anchor not found (mutate_now_iso_into_branch)"
    return src.replace(anchor,
                       "    if body.trek_ids:\n"
                       "        now_iso = now.strftime(\"%Y-%m-%dT%H:%M:%S.%fZ\")\n"
                       "    trek = _run_trek_tick(body, now)")


def mutate_leak_fire_into_trek(src: str) -> str:
    """_run_trek_tick の中から _fire_due_scheduled を呼ぶ (= 実時計が漏れる形に戻す)。"""
    anchor = "    import copy\n"
    i = src.index("def _run_trek_tick(")
    j = src.index(anchor, i) + len(anchor)
    return src[:j] + "    _fire_due_scheduled(now_iso)  # MUTANT\n" + src[j:]


def mutate_drop_trek_now_iso(src: str) -> str:
    """_run_trek_tick の無条件 now_iso 束縛を削る。"""
    i = src.index("def _run_trek_tick(")
    j = src.index("def _run_system_tick(")
    body = src[i:j]
    target = "    now_iso = now.strftime(\"%Y-%m-%dT%H:%M:%S.%fZ\")\n"
    assert target in body, "anchor not found (mutate_drop_trek_now_iso)"
    return src[:i] + body.replace(target, "", 1) + src[j:]


def mutate_drop_logger(src: str) -> str:
    """_fire_due_scheduled を囲む except からログ出力を外す (= 無音で飲む形に戻す)。"""
    idx = src.rindex("_fire_due_scheduled(now_iso)")
    window = src[idx: idx + 600]
    assert "_server_logger.error" in window, "anchor not found (mutate_drop_logger)"
    return src[:idx] + window.replace("_server_logger.error", "_noop_logger_X", 1) \
        + src[idx + 600:]


def mutate_drop_new_entrypoint(src: str) -> str:
    """Trek 非依存の新入口を消す (= Trek を消すと本番の自律が止まる形に戻る)。"""
    return src.replace('@app.post("/api/system/tick")',
                       '@app.post("/api/system/tick-MUTANT")', 1)


def mutate_drop_old_entrypoint(src: str) -> str:
    """旧入口 (転送) を消す (= 外部の駆動役の設定が古いまま 404 になる)。"""
    return src.replace('@app.post("/api/system/trek-scheduler/tick")',
                       '@app.post("/api/system/trek-scheduler/tick-MUTANT")', 1)


def mutate_rename_system_tick(src: str) -> str:
    """定期発火の本体の名前を変える (= ガードを置いて行った状態)。"""
    return src.replace("def _run_system_tick(", "def _run_system_tick_RENAMED(", 1)


def mutate_old_entrypoint_stops_forwarding(src: str) -> str:
    """旧入口が転送をやめる (= 駆動役の設定が古いまま空振りし、本番の自律が無音で止まる)。"""
    anchor = "    return system_tick_endpoint(body, request)"
    assert anchor in src, "anchor not found (mutate_old_entrypoint_stops_forwarding)"
    return src.replace(anchor, "    return {}  # MUTANT: 転送をやめた", 1)


def mutate_new_entrypoint_skips_auth(src: str) -> str:
    """新入口が内部鍵の検査を飛ばす (= 無認証で誰でも本番の発火を叩ける)。"""
    anchor = "    _require_scheduler_key(request)\n    return _run_system_tick(body)"
    assert anchor in src, "anchor not found (mutate_new_entrypoint_skips_auth)"
    return src.replace(anchor, "    return _run_system_tick(body)  # MUTANT", 1)


def mutate_drop_tick_health_stamp(src: str) -> str:
    """tick 完了の記録をやめる (= 外部の見張りが「駆動役が死んだ」を検知できなくなる)。"""
    anchor = '    _last_tick_at = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")'
    assert anchor in src, "anchor not found (mutate_drop_tick_health_stamp)"
    return src.replace(anchor, "    _last_tick_at = None  # MUTANT", 1)


def mutate_drop_scheduled_fires_from_response(src: str) -> str:
    """応答から scheduled_fires を落とす (= 自律が駆動されたかを外から読めなくなる)。"""
    anchor = '        # ms-107 e-3434 chunk 3b — scheduled things fired on this tick.\n        "scheduled_fires": scheduled_fires,\n'
    assert anchor in src, "anchor not found (mutate_drop_scheduled_fires_from_response)"
    return src.replace(anchor, "", 1)


CASES = [
    ("now_iso を条件分岐の中へ (ms-66 の穴)", mutate_now_iso_into_branch,
     "test_now_iso_bound_unconditionally_before_fire_due_scheduled"),
    ("Trek の集約から発火を呼ぶ (実時計の漏れ)", mutate_leak_fire_into_trek,
     "test_trek_tick_cannot_leak_its_now_iso_into_scheduled_firing"),
    ("Trek 側の now_iso 束縛を削る", mutate_drop_trek_now_iso,
     "test_trek_tick_binds_its_own_now_iso_unconditionally"),
    ("except のログ出力を外す (無音で飲む)", mutate_drop_logger,
     "test_scheduled_fire_error_is_not_silently_swallowed"),
    ("Trek 非依存の新入口を消す", mutate_drop_new_entrypoint,
     "test_scheduled_firing_entrypoint_is_trek_neutral"),
    ("旧入口 (転送) を消す", mutate_drop_old_entrypoint,
     "test_scheduled_firing_entrypoint_is_trek_neutral"),
    ("本体の名前を変える (ガードの置き去り)", mutate_rename_system_tick,
     "test_now_iso_bound_unconditionally_before_fire_due_scheduled"),
    # --- HTTP の配線 (tests/test_system_tick_entrypoint_e7065.py) ---
    ("旧入口が転送をやめる", mutate_old_entrypoint_stops_forwarding,
     WIRE_TEST + "::test_both_entrypoints_return_the_same_shape"),
    ("新入口が内部鍵の検査を飛ばす", mutate_new_entrypoint_skips_auth,
     WIRE_TEST + "::test_new_entrypoint_requires_the_internal_key"),
    ("tick 完了の記録をやめる", mutate_drop_tick_health_stamp,
     WIRE_TEST + "::test_tick_health_sees_a_tick_from_the_new_entrypoint"),
    ("応答から scheduled_fires を落とす", mutate_drop_scheduled_fires_from_response,
     WIRE_TEST + "::test_new_trek_neutral_entrypoint_runs_a_tick"),
]


def run_one(test_name: str) -> tuple[bool, str]:
    """(赤くなったか, 出力の末尾)"""
    target = test_name if "::" in test_name and test_name.startswith("tests/") \
        else f"{AST_TEST}::{test_name}"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip().split("\n")[-1]
    return (r.returncode != 0, out)


def main() -> int:
    original = APP.read_text(encoding="utf-8")
    # まず壊していない状態で全件緑を確認 (= ベースライン)
    base = subprocess.run(
        [sys.executable, "-m", "pytest", AST_TEST, WIRE_TEST, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    if base.returncode != 0:
        print("BASELINE RED — 壊す前からガードが落ちている。先にそれを直すこと。")
        print((base.stdout + base.stderr)[-2000:])
        return 1
    print(f"baseline: {(base.stdout).strip().splitlines()[-1]}\n")

    bad = 0
    for label, mutate, test_name in CASES:
        try:
            APP.write_text(mutate(original), encoding="utf-8")
            went_red, tail = run_one(test_name)
        except AssertionError as exc:
            print(f"  SKIP  {label}: 当てる場所が見つからない ({exc}) "
                  f"— SKIP は緑と同じ害なので、壊し方を実装に合わせて直すこと")
            bad += 1
            continue
        finally:
            APP.write_text(original, encoding="utf-8")
        mark = "  OK  " if went_red else " MISS "
        if not went_red:
            bad += 1
        print(f"{mark}{label}\n          → {test_name}: {tail}")

    print()
    if bad:
        print(f"NG: {bad} 件が「壊したのに赤くならない」か SKIP。"
              f"偽の安全なので直すこと。")
        return 1
    print(f"OK: {len(CASES)} 件すべて、守る対象を壊すと赤くなった。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
