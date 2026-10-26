#!/usr/bin/env python3
"""定期発火のガード 2 本 (構文木 + HTTP 配線) の各主張が「守る対象を壊したとき実際に赤くなるか」
を 1 件ずつ測る反転ハーネス (ms-185 e-7065)。

緑のガードは信頼されるので、**偽の安全は無ガードより悪い**。このスクリプトは
``server/app.py`` に 1 箇所ずつ壊し方を当てて pytest を走らせ、意図したテストが
赤くなることを確認する。全ケースで「壊したのに緑」が 0 件であることが、ガードが
本物である根拠になる。

ms-185 e-7066 で 2 つ足した: (a) source の登録簿 (Operation の発火 / 締切リマインド)
を壊すケース群、(b) ``scripts/invert_harness.py`` の共有部品への載せ替え
(= バイトコードキャッシュの隔離と、実行後にベースラインが緑へ戻ることの実測)。
このハーネスは元々その 2 つを持っておらず、**ハーネス自身が偽の結果を作りうる**
形だった (経緯は invert_harness.py の docstring)。

使い方: python3 scripts/invert-tick-guard.py
終了コード: 全ケースで期待どおり赤くなれば 0、1 件でも「壊したのに緑」なら 1。
``server/app.py`` は毎ケース必ず元に戻す (finally)。
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "server" / "app.py"
# e-7066 follow-up: 壊す先は server/app.py だけではない。「配線は在るが毎回
# 失敗している」を捕まえるのは箱の外の watchdog 側 (判定器と、それに内訳を
# 渡す monitor) なので、その 2 ファイルも壊して測る。守る対象が 2 ファイルに
# またがっているのに 1 ファイルだけ壊していると、もう一方は無防備なまま
# 「全件 OK」と報告される (= ハーネスが偽の安全を作る)。
WATCHDOG = ROOT / "lib" / "tick_health.py"
MONITOR = ROOT / "scripts" / "tick-health-monitor.py"
AST_TEST = "tests/test_tick_now_iso_binding.py"
WIRE_TEST = "tests/test_system_tick_entrypoint_e7065.py"
SOURCE_TEST = "tests/test_scheduled_source_registry_e7066.py"
BASELINE = [AST_TEST, WIRE_TEST, SOURCE_TEST]


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


# --- ms-185 e-7066: 相乗りしている source の観測 -----------------------------

def mutate_drop_deadline_source(src: str) -> str:
    """締切リマインドを登録簿から外す (= Operation だけ回って緑に見える形)。

    **これが e-7066 の本丸。** 相乗りは 2 つあるので、片方を落としても tick は
    毎分走り続け、liveness だけ見ていると緑のまま。"""
    anchor = '    ("deadline", _fire_due_deadlines),\n'
    assert anchor in src, "anchor not found (mutate_drop_deadline_source)"
    return src.replace(anchor, "", 1)


# ``list(SCHEDULED_SOURCE_NAMES)`` を出す場所は 3 つある (直近 tick の記録 /
# tick の応答 / tick-health の endpoint)。**3 つとも別々に壊して測る** —
# 1 箇所だけ壊して緑なら「他の 2 箇所は誰も見ていない」ということなので、
# まとめて 1 ケースにすると、どの面が無防備かが隠れる。
# (実際このハーネスが、最初に書いた 1 ケース版は応答ではなく記録の側を壊して
#  いて、テストは素通りしていた = 偽の緑を作りかけていた。)
_LITERAL = '["operation", "deadline"],  # MUTANT'


def _literalize(src: str, preceding: str, what: str) -> str:
    """``preceding`` の直後に来る ``scheduled_sources`` だけを literal に変える。"""
    i = src.index(preceding) if preceding in src else -1
    assert i >= 0, f"anchor not found ({what}: preceding)"
    target = '"scheduled_sources": list(SCHEDULED_SOURCE_NAMES),'
    j = src.find(target, i)
    assert j >= 0, f"anchor not found ({what}: target)"
    return src[:j] + f'"scheduled_sources": {_LITERAL}' + src[j + len(target):]


def mutate_literal_in_last_tick_report(src: str) -> str:
    """直近 tick の記録の source 一覧を手書き literal にする。"""
    return _literalize(src, '"scheduled_fires": len(scheduled_fires)',
                       "mutate_literal_in_last_tick_report")


def mutate_literal_in_tick_response(src: str) -> str:
    """tick の応答の source 一覧を手書き literal にする。

    実体 (回す対象) と報告が別の出所になると、source を落としても報告だけ
    「両方回っています」と言い続けられる = 嘘がつける形。"""
    return _literalize(src, "# ms-185 e-7066 — どの source を回したか",
                       "mutate_literal_in_tick_response")


def mutate_literal_in_tick_health(src: str) -> str:
    """tick-health endpoint の source 一覧を手書き literal にする。"""
    return _literalize(src, '"last_tick_report": _last_tick_report,',
                       "mutate_literal_in_tick_health")


def mutate_drop_source_stamp(src: str) -> str:
    """report entry への出所の刻印をやめる (= 合計に潰れて内訳が読めない)。"""
    anchor = '                _entry.setdefault("source", _source_name)'
    assert anchor in src, "anchor not found (mutate_drop_source_stamp)"
    return src.replace(anchor, "                pass  # MUTANT", 1)


def mutate_tick_health_hides_sources(src: str) -> str:
    """/api/system/tick-health から scheduled_sources を落とす。

    箱の外の watchdog はこの endpoint しか読めないので、ここが消えると
    source の欠落は本番で誰にも気付かれない。"""
    anchor = '        "scheduled_sources": list(SCHEDULED_SOURCE_NAMES),\n    }'
    assert anchor in src, "anchor not found (mutate_tick_health_hides_sources)"
    return src.replace(anchor, "    }", 1)


def mutate_call_deadline_outside_registry(src: str) -> str:
    """登録簿を回しつつ、締切リマインドだけ直呼びも残す (= 二重の出所)。

    「登録簿から消しても動き続ける」形。報告と実体が静かにズレる。"""
    anchor = "        if changed:\n            try:\n                db.save_project(pid, project)"
    assert anchor in src, "anchor not found (mutate_call_deadline_outside_registry)"
    return src.replace(
        anchor,
        "        _fire_due_deadlines(pid, project, now_iso, report)  # MUTANT\n"
        + anchor, 1)


# --- ms-185 e-7066 follow-up: 「配線は在るが毎回失敗」の検知 ----------------
#
# 独立 AX レビューの high。登録簿の名前だけを見る判定は、source が毎 tick
# 呼ばれて毎回例外を投げていても ok を返す。壊す先が watchdog 側なので、
# 下の 4 ケースは APP ではなく WATCHDOG / MONITOR に当てる。


def mutate_verdict_ignores_the_breakdown(src: str) -> str:
    """判定器が内訳を見るのをやめる (= 名前の所属判定だけに戻す)。"""
    anchor = "    failing = failing_scheduled_sources(fire_summary, required)\n"
    assert anchor in src, "anchor not found (mutate_verdict_ignores_the_breakdown)"
    return src.replace(anchor, "    failing = []  # MUTANT\n", 1)


def mutate_alert_ignores_failing(src: str) -> str:
    """鳴る条件から failing を外す (= 判定はできても alert が出ない形)。"""
    anchor = 'return bool(sources) and sources.get("status") in ("missing", "failing")'
    assert anchor in src, "anchor not found (mutate_alert_ignores_failing)"
    return src.replace(
        anchor, 'return bool(sources) and sources.get("status") == "missing"  # MUTANT', 1)


def mutate_failing_predicate_ignores_fired(src: str) -> str:
    """全失敗の判定から「1 件も成功していない」を落とす (= 一部失敗でも鳴る)。

    締めすぎる方向の変異。鳴きっぱなしの watchdog は mute され、結果として
    何も守らなくなるので、緩める方向と同じく壊れとして扱う。"""
    anchor = "        if errors > 0 and fired == 0:"
    assert anchor in src, "anchor not found (mutate_failing_predicate_ignores_fired)"
    return src.replace(anchor, "        if errors > 0:  # MUTANT", 1)


def mutate_monitor_drops_the_breakdown(src: str) -> str:
    """monitor が内訳を判定器に渡すのをやめる (= 判定器は直ったが本番で効かない)。

    「足した」≠「全域に効く」。判定器だけ直して monitor の配線を忘れる形は
    実際に今回の欠陥そのものだったので、独立のケースとして測る。"""
    anchor = ("        fire_summary=(_report or {}).get(\"scheduled_by_source\")\n"
              "        if isinstance(_report, dict) else None,\n")
    assert anchor in src, "anchor not found (mutate_monitor_drops_the_breakdown)"
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
    # --- source の観測 (tests/test_scheduled_source_registry_e7066.py) ---
    ("締切リマインドを登録簿から外す", mutate_drop_deadline_source,
     SOURCE_TEST + "::test_one_tick_fires_both_sources_and_reports_them_separately"),
    ("締切リマインドを外す (watchdog の期待値との突合)", mutate_drop_deadline_source,
     SOURCE_TEST + "::test_watchdog_required_sources_are_all_actually_driven"),
    ("source 一覧を literal 化 (tick の応答)", mutate_literal_in_tick_response,
     SOURCE_TEST + "::test_dropping_one_source_is_visible_in_the_report"),
    ("source 一覧を literal 化 (直近 tick の記録)", mutate_literal_in_last_tick_report,
     SOURCE_TEST + "::test_dropping_one_source_is_visible_in_the_report"),
    ("source 一覧を literal 化 (tick-health endpoint)", mutate_literal_in_tick_health,
     SOURCE_TEST + "::test_dropping_one_source_is_visible_in_the_report"),
    ("report entry の出所の刻印をやめる", mutate_drop_source_stamp,
     SOURCE_TEST + "::test_one_tick_fires_both_sources_and_reports_them_separately"),
    ("tick-health から source 一覧を隠す", mutate_tick_health_hides_sources,
     SOURCE_TEST + "::test_tick_health_exposes_the_driven_sources"),
    ("登録簿の外から締切リマインドを直呼びする", mutate_call_deadline_outside_registry,
     SOURCE_TEST + "::test_scheduled_sources_are_driven_only_through_the_registry"),
    # --- e-7066 follow-up: 「配線は在るが毎回失敗」(壊す先は watchdog 側) ---
    ("判定器が内訳を見るのをやめる", mutate_verdict_ignores_the_breakdown,
     SOURCE_TEST + "::test_a_source_that_errors_every_attempt_is_not_reported_ok",
     WATCHDOG),
    ("鳴る条件から全失敗を外す", mutate_alert_ignores_failing,
     SOURCE_TEST + "::test_a_source_that_errors_every_attempt_is_not_reported_ok",
     WATCHDOG),
    ("全失敗の判定から「1 件も成功なし」を落とす (鳴きすぎ)",
     mutate_failing_predicate_ignores_fired,
     SOURCE_TEST + "::test_partial_failure_does_not_cry_wolf", WATCHDOG),
    ("monitor が内訳を渡すのをやめる", mutate_monitor_drops_the_breakdown,
     SOURCE_TEST + "::test_monitor_passes_the_breakdown_through_to_the_verdict",
     MONITOR),
]


# pytest が「テストを集められなかった」ときの印。変異が構文を壊すと**どの**
# テストも赤くなるので、赤いこと自体はガードが効いた証拠にならない。これを
# 成功として数えると、壊し方が下手なだけのケースが「守られている」に化ける
# (= 偽の安全をハーネスが作る)。実際この 3 ケースで一度踏んだ: literal 化の
# 置換が末尾のカンマをコメントに飲ませて SyntaxError を作り、2 件が OK に
# 見えていた。
_COLLECTION_FAILURE_MARKS = ("found no collectors", "SyntaxError",
                             "errors in", "error in")


def run_one(test_name: str) -> tuple:
    """(ガードが捕まえたか, 出力の末尾)。収集エラーは「捕まえた」に数えない。"""
    target = test_name if "::" in test_name and test_name.startswith("tests/") \
        else f"{AST_TEST}::{test_name}"
    went_red, tail, r = ih.run_pytest(target, ROOT)
    combined = r.stdout + r.stderr
    if any(mark in combined for mark in _COLLECTION_FAILURE_MARKS):
        return (False, "INVALID (テストを収集できていない — 変異が構文を壊した"
                       f"か、対象名が違う): {tail}")
    return (went_red, tail)


def main() -> int:
    originals = {path: path.read_text(encoding="utf-8")
                 for path in (APP, WATCHDOG, MONITOR)}
    # まず壊していない状態で全件緑を確認 (= ベースライン)
    if not ih.check_baseline(BASELINE, ROOT):
        return 1

    bad = 0
    for case in CASES:
        # 4 要素目は壊す先のファイル (省略時は server/app.py)。守る対象が
        # 複数ファイルにまたがるので、ケースごとにどこを壊すかを持たせる。
        label, mutate, test_name = case[0], case[1], case[2]
        target_file = case[3] if len(case) > 3 else APP
        try:
            target_file.write_text(mutate(originals[target_file]), encoding="utf-8")
            ih.invalidate_bytecode_cache(target_file, ROOT)
            went_red, tail = run_one(test_name)
        except AssertionError as exc:
            ih.restore(originals, ROOT)
            print(f"  SKIP  {label}: 当てる場所が見つからない ({exc}) "
                  f"— SKIP は緑と同じ害なので、壊し方を実装に合わせて直すこと")
            bad += 1
            continue
        finally:
            ih.restore(originals, ROOT)
        mark = "  OK  " if went_red else " MISS "
        if not went_red:
            bad += 1
        print(f"{mark}{label}\n          → {test_name}: {tail}")

    print()
    if bad:
        print(f"NG: {bad} 件が「壊したのに赤くならない」か SKIP。"
              f"偽の安全なので直すこと。")
        return 1
    # ハーネスが環境を汚したまま終わらないことを最後に実測する。
    if not ih.check_not_polluted(BASELINE, ROOT):
        return 1
    print()
    print(f"OK: {len(CASES)} 件すべて、守る対象を壊すと赤くなった。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
