#!/usr/bin/env python3
"""メモ (session note) まわりの安全装置が、壊したとき実際に赤くなるかを測る
反転ハーネス (ms-178 e-7182 / e-7185)。

`note clear` / `note reap` の安全性は **順序** で買っている: 両ストアの退避を
取ってから削除する。取れなければ何も削除しない。退避の脚は 2 本 (local の
`.jsonl.bak` / cloud の `.cloud.bak`) あり、**測られていない脚は守られていない**。

実際そうなっていた。目的達成レビューの独立 judge (2026-10-27) が「local の退避が
OSError を上げた後も削除を続ける」変異を入れたところ、note 系 7 ファイルすべてが
緑のまま通った。挙動は正しく、ガードだけが片脚だったという形で、ms-178 で
繰り返し出ている型そのもの (安全装置を宣言しておきながら、ある経路でだけ
効いていない)。

ここでは 2 本の脚を**対**として測る。片脚だけ測ると、もう片脚が落ちたときに
気付けない — それが e-7182 の起点だった。

壊し方は 2 方向を網羅する:
  * **順序を飛ばす** (退避の失敗を飲んで削除を続ける) → データ消失側
  * **告知を落とす** (素の例外で落ちる / 退避を片脚だけ書く) → 「何が起きたか
    操作者に伝わらない」側。止まってはいるが、復元の手掛かりが残らない

バイトコードキャッシュの隔離と、実行後にベースラインが緑へ戻ることの実測は
``scripts/invert_harness.py`` が持つ (なぜその形かの経緯もそこに在る。サイズが
同一の変異 + 同一秒内の復元で stale な `.pyc` が偽の結果を出す穴を、この
プロジェクトは実際に踏んでいる)。

e-7185 で測る対象を 4 つ足した。いずれも **挙動は正しいがガードが固定されて
いなかった** 箇所で、目的達成レビューの独立 judge の変異が既存テスト全件を
素通りした実績がある:
  * M2b: cloud が読めない (例外) のに error を空で返す = 読めないと空の混同
  * M1:  bin/beacon が help の素通り時に BEACON_HELP_ONLY を立てない
  * M8:  未分類 verb / 台帳が import できないときに読み取り専用ゲートが通す
  * M14: bash の `note list` が余分な位置引数を再び --json として飲む

**変異はファイルサイズを変えることを assert する** (AC)。サイズ中立な変異 +
同一秒内の復元で stale な `.pyc` が偽の結果を出す実例を 2026-10-27 に踏んだ。
サイズが変わらない変異を書いてしまったら SKIP として失敗させ、書き直させる。

使い方: python3 scripts/invert-note-backup-ordering.py
終了コード: 全ケースで期待どおり赤くなれば 0、1 件でも「壊したのに緑」か
SKIP (= 当てる場所が無い) なら 1。SKIP は緑と同じ害なので失敗として数える。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTE = ROOT / "lib" / "cmd_note.py"
BIN = ROOT / "bin" / "beacon"
GATE = ROOT / "lib" / "readonly_gate.py"
TARGET_FILES = (NOTE, BIN, GATE)

BACKUP_TEST = "tests/test_note_clear_backup_e6656.py"
#: 刈り取り側も同じ `_selective_delete` を通るので、順序を壊したときに
#: こちらが気付くかも一緒に測る (1 機構 2 方針)。純粋関数を固めた
#: `test_note_reap_e7150.py` ではなく、**本番の配線を端から端まで動かす**
#: 継ぎ目の module を見る (そちらは入力が正しいときの判定しか見ていない)。
REAP_TEST = "tests/test_note_reap_io_seam_e7150.py"
#: e-7185 の 4 件が住む場所。
TRUTH_TEST = "tests/test_note_list_truth_source_e6655.py"
HELP_TEST = "tests/test_note_help_destructive_guard_e6654.py"
FLAG_TEST = "tests/test_note_flag_value_guard_e7150.py"
BASELINE = [BACKUP_TEST, REAP_TEST, TRUTH_TEST, HELP_TEST, FLAG_TEST]

LOCAL_LEG = "test_unwritable_local_backup_aborts_without_deleting_anything"
CLOUD_LEG = "test_unreadable_cloud_aborts_without_deleting_anything"


# --- 順序を飛ばす方向 (データ消失側) ---------------------------------------

def swallow_the_backup_failure(src: str) -> str:
    """退避が書けなくても削除を続ける (= judge の M3b、e-7182 の起点そのもの)。

    `backup_desc` に代入してから続けるのが肝: 未代入のまま進めると NameError で
    赤くなり、「ガードが効いた」と「別の理由で落ちた」を見分けられない。
    """
    anchor = ('    except OSError as exc:\n'
              '        print("Aborted: 退避を書けません (%s)。何も削除していません。" % exc,\n'
              '              file=sys.stderr)\n'
              '        sys.exit(1)\n')
    assert anchor in src, "anchor not found (swallow_the_backup_failure)"
    return src.replace(
        anchor,
        '    except OSError as exc:\n'
        '        backup_desc = "(退避なし: %s)" % exc  # MUTANT: 飲んで続ける\n',
        1)


def continue_after_an_unreadable_cloud(src: str) -> str:
    """cloud が読めなくても削除を続ける (= local 脚の鏡像)。"""
    anchor = ('            print("  何も削除していません (退避の取れない削除は行いません)。",\n'
              '                  file=sys.stderr)\n'
              '            sys.exit(1)\n')
    assert anchor in src, "anchor not found (continue_after_an_unreadable_cloud)"
    return src.replace(
        anchor,
        '            print("  何も削除していません (退避の取れない削除は行いません)。",\n'
        '                  file=sys.stderr)\n'
        '            cloud_error = ""  # MUTANT: 読めなかったことを忘れて続ける\n',
        1)


# --- 告知を落とす方向 ------------------------------------------------------

def let_the_backup_failure_escape_raw(src: str) -> str:
    """退避の失敗を素の OSError で落とす (中止の告知も rc の約束も無い)。

    止まってはいるので「消えてはいない」が、操作者には traceback しか残らない。
    「何も削除していません」と言えることまでが保証の一部。
    """
    anchor = ('    try:\n'
              '        backup_desc = write_backups(local, cloud, project_id)\n'
              '    except OSError as exc:\n'
              '        print("Aborted: 退避を書けません (%s)。何も削除していません。" % exc,\n'
              '              file=sys.stderr)\n'
              '        sys.exit(1)\n')
    assert anchor in src, "anchor not found (let_the_backup_failure_escape_raw)"
    return src.replace(
        anchor,
        '    backup_desc = write_backups(local, cloud, project_id)  # MUTANT: 素で落ちる\n',
        1)


def skip_the_cloud_snapshot(src: str) -> str:
    """local の退避だけ書いて cloud の退避を飛ばす (= e-6656 の元の非対称)。"""
    anchor = ('    if project_id:\n'
              '        cb = _cloud_backup_path()\n')
    assert anchor in src, "anchor not found (skip_the_cloud_snapshot)"
    return src.replace(
        anchor,
        '    if False:  # MUTANT: cloud の退避を飛ばす\n'
        '        cb = _cloud_backup_path()\n',
        1)


# --- e-7185: 読めないと空の混同 (M2b) --------------------------------------

def swallow_the_list_notes_exception(src: str) -> str:
    """cloud の読み取りが例外を上げても error を空で返す (= judge の M2b)。

    この 1 行が `_fetch_cloud_notes` の docstring が「defect そのもの」と呼ぶ
    混同を復活させる。全呼び出し側 (一覧 / 削除ゲートの見積り / 刈り取りの拒否)
    がこの値で分岐するので、影響範囲は関数 1 つに収まらない。
    """
    anchor = ('    except Exception as exc:  # network / server error\n'
              '        return [], f"{type(exc).__name__}: {exc}"\n')
    assert anchor in src, "anchor not found (swallow_the_list_notes_exception)"
    return src.replace(
        anchor,
        '    except Exception:  # MUTANT: 読めなかったことを空として返す\n'
        '        return [], ""\n',
        1)


# --- e-7185: help の素通りでゲートを立てない (M1) ---------------------------

def never_arm_the_help_gate(src: str) -> str:
    """bin/beacon が help の素通り時に BEACON_HELP_ONLY を立てない (= M1)。

    env var が在るときの chokepoint は効く (judge が実行確認済) が、それを
    **立てる側**が誰にも固定されていなかった。立てなければ、help 登録の無い
    破壊的 verb が `--help` で実行される = e-6654 の原状復帰。
    """
    anchor = "    export BEACON_HELP_ONLY=1\n"
    assert anchor in src, "anchor not found (never_arm_the_help_gate)"
    return src.replace(
        anchor, "    : # MUTANT: ゲートを立てない\n", 1)


# --- e-7185: 読み取り専用ゲートの fail-open (M8) ----------------------------

def open_the_gate_when_the_ledger_is_unavailable(src: str) -> str:
    """台帳が import できないとき True を返す (= M8 の片側)。"""
    anchor = ('    except Exception:\n'
              '        return False\n'
              '    return bool(entry) and entry.get("cls") == "Q"\n')
    assert anchor in src, "anchor not found (open_the_gate_when_the_ledger_is_unavailable)"
    return src.replace(
        anchor,
        '    except Exception:\n'
        '        return True  # MUTANT: 台帳が無いとき全 verb を通す\n'
        '    return bool(entry) and entry.get("cls") == "Q"\n',
        1)


def open_the_gate_for_an_unclassified_verb(src: str) -> str:
    """未分類の verb を通す (= M8 のもう片側)。"""
    anchor = '    return bool(entry) and entry.get("cls") == "Q"\n'
    assert anchor in src, "anchor not found (open_the_gate_for_an_unclassified_verb)"
    return src.replace(
        anchor,
        '    # MUTANT: 未分類 (= 分類忘れ) を安全側として通す\n'
        '    return (not entry) or entry.get("cls") == "Q"\n',
        1)


# --- e-7185: 余分な位置引数を飲む (M14) -------------------------------------

def swallow_a_stray_positional_as_json(src: str) -> str:
    """bash の `note list` が余分な語を再び --json として飲む (= M14)。

    `${3:+1}` 時代の defect の形。誤った呼び出しが成功に見え、`note list --help`
    がメモ本文を吐いた経路そのもの。
    """
    anchor = '                        *)      _guard_no_positional "note list" "$1" ;;\n'
    assert anchor in src, "anchor not found (swallow_a_stray_positional_as_json)"
    return src.replace(
        anchor,
        '                        *)      note_json="1"; shift ;;  # MUTANT: 飲む\n',
        1)


# (label, 対象ファイル, mutate, 赤くなるべき対象)
CASES = [
    # e-7182: 順序を飛ばす
    ("local の退避が書けなくても削除を続ける (judge の M3b)",
     NOTE, swallow_the_backup_failure, f"{BACKUP_TEST}::{LOCAL_LEG}"),
    ("cloud が読めなくても削除を続ける (local 脚の鏡像)",
     NOTE, continue_after_an_unreadable_cloud, f"{BACKUP_TEST}::{CLOUD_LEG}"),
    ("local の退避が書けなくても刈り取りを続ける",
     NOTE, swallow_the_backup_failure,
     f"{REAP_TEST}::test_unwritable_backup_aborts_the_reap_end_to_end"),
    # e-7182: 告知を落とす
    ("退避の失敗を素の例外で落とす (中止の告知が無い)",
     NOTE, let_the_backup_failure_escape_raw, f"{BACKUP_TEST}::{LOCAL_LEG}"),
    ("cloud の退避を飛ばして local だけ書く",
     NOTE, skip_the_cloud_snapshot, BACKUP_TEST),
    # e-7185
    ("M2b: cloud の読み取り例外を空の error として返す",
     NOTE, swallow_the_list_notes_exception,
     f"{TRUTH_TEST}::test_a_raising_client_becomes_a_non_empty_error"),
    ("M1: help の素通りでゲートを立てない",
     BIN, never_arm_the_help_gate,
     f"{HELP_TEST}::test_bin_beacon_sets_help_only_on_the_fall_through"),
    ("M8: 台帳が import できないときゲートを通す",
     GATE, open_the_gate_when_the_ledger_is_unavailable,
     f"{HELP_TEST}::test_the_gate_refuses_when_the_ledger_cannot_be_imported"),
    ("M8: 未分類の verb をゲートが通す",
     GATE, open_the_gate_for_an_unclassified_verb,
     f"{HELP_TEST}::test_the_gate_refuses_an_unclassified_verb"),
    ("M14: 余分な位置引数を --json として飲む",
     BIN, swallow_a_stray_positional_as_json, FLAG_TEST),
]

# 「赤くなった」が「ガードが効いた」とは限らない。変異が構文を壊したり対象を
# 収集できなかったりしても赤くなるので、**赤の理由**まで見ないと偽の OK を出す。
#
# ここは出力の自由文を substring で探さない。以前の書き方 (`"error in" in 出力`)
# は、テスト側の docstring に「a syntax error in the ledger」という文があるだけで
# INVALID と誤判定した (2026-10-27 実測)。緩い一致はこのリポジトリが 3 回踏んだ
# 形なので、pytest の **終了コード** という構造を見る:
#   0 = 全部緑 (= MISS)、1 = テストが落ちた (= 本来狙っていた赤)、
#   2 interrupted / 3 internal error / 4 usage error / 5 no tests collected。
# 加えて、収集時 error も終了コード 1 に落ちるので **要約行** (= 機械が出す
# 1 行) に error が数えられていないことだけを見る。本文は見ない。
_PYTEST_TESTS_FAILED = 1
_PYTEST_INVALID_RCS = (2, 3, 4, 5)


def run_one(target: str):
    went_red, tail, r = ih.run_pytest(target, ROOT)
    if r.returncode in _PYTEST_INVALID_RCS:
        return (False, f"INVALID (pytest rc={r.returncode} — 収集/起動の失敗): {tail}")
    if r.returncode == _PYTEST_TESTS_FAILED and "error" in tail.lower():
        return (False, f"INVALID (要約行が error を数えている): {tail}")
    return (went_red, tail)


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in TARGET_FILES}
    if not ih.check_baseline(BASELINE, ROOT):
        return 1

    bad = 0
    for label, path, mutate, target in CASES:
        try:
            mutated = mutate(originals[path])
            # サイズが変わらない変異は、同一秒内の復元と組み合わさると Python の
            # 無効化判定 (mtime 秒 + サイズ) を両方すり抜け、変異版の .pyc が
            # 再利用される。2026-10-27 に実際に踏んだので、ここで止める。
            assert len(mutated) != len(originals[path]), \
                "変異がファイルサイズを変えていない (stale .pyc で偽の結果になる)"
            path.write_text(mutated, encoding="utf-8")
            ih.invalidate_bytecode_cache(path, ROOT)
            went_red, tail = run_one(target)
        except AssertionError as exc:
            ih.restore(originals, ROOT)
            print(f"  SKIP  {label}: 当てる場所が無い ({exc}) — SKIP は緑と同じ害")
            bad += 1
            continue
        finally:
            ih.restore(originals, ROOT)
        print(("  OK  " if went_red else " MISS ") + label)
        print(f"          → {target}: {tail}")
        if not went_red:
            bad += 1

    print()
    if bad:
        print(f"NG: {bad} 件が「壊したのに赤くならない」か SKIP。偽の安全なので直すこと。")
        return 1
    if not ih.check_not_polluted(BASELINE, ROOT):
        return 1
    print()
    print(f"OK: {len(CASES)} 件すべて、守る対象を壊すと赤くなった。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
