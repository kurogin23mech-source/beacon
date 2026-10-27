#!/usr/bin/env python3
"""「退避なし ⇒ 削除なし」の順序保証が、壊したとき実際に赤くなるかを測る
反転ハーネス (ms-178 e-7182)。

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

BACKUP_TEST = "tests/test_note_clear_backup_e6656.py"
#: 刈り取り側も同じ `_selective_delete` を通るので、順序を壊したときに
#: こちらが気付くかも一緒に測る (1 機構 2 方針)。純粋関数を固めた
#: `test_note_reap_e7150.py` ではなく、**本番の配線を端から端まで動かす**
#: 継ぎ目の module を見る (そちらは入力が正しいときの判定しか見ていない)。
REAP_TEST = "tests/test_note_reap_io_seam_e7150.py"
BASELINE = [BACKUP_TEST, REAP_TEST]

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


# (label, mutate, 赤くなるべき対象)
CASES = [
    # 順序を飛ばす
    ("local の退避が書けなくても削除を続ける (judge の M3b)",
     swallow_the_backup_failure, f"{BACKUP_TEST}::{LOCAL_LEG}"),
    ("cloud が読めなくても削除を続ける (local 脚の鏡像)",
     continue_after_an_unreadable_cloud, f"{BACKUP_TEST}::{CLOUD_LEG}"),
    ("local の退避が書けなくても刈り取りを続ける",
     swallow_the_backup_failure,
     f"{REAP_TEST}::test_unwritable_backup_aborts_the_reap_end_to_end"),
    # 告知を落とす
    ("退避の失敗を素の例外で落とす (中止の告知が無い)",
     let_the_backup_failure_escape_raw, f"{BACKUP_TEST}::{LOCAL_LEG}"),
    ("cloud の退避を飛ばして local だけ書く",
     skip_the_cloud_snapshot, BACKUP_TEST),
]

# pytest が「テストを集められなかった」ときの印。変異が構文を壊すとどのテストも
# 赤くなるので、赤いこと自体はガードが効いた証拠にならない。
_COLLECTION_FAILURE_MARKS = ("found no collectors", "SyntaxError",
                             "errors in", "error in")


def run_one(target: str):
    went_red, tail, r = ih.run_pytest(target, ROOT)
    combined = r.stdout + r.stderr
    if any(mark in combined for mark in _COLLECTION_FAILURE_MARKS):
        return (False, f"INVALID (テストを収集できていない): {tail}")
    return (went_red, tail)


def main() -> int:
    originals = {NOTE: NOTE.read_text(encoding="utf-8")}
    if not ih.check_baseline(BASELINE, ROOT):
        return 1

    bad = 0
    for label, mutate, target in CASES:
        try:
            mutated = mutate(originals[NOTE])
            assert mutated != originals[NOTE], "変異がソースを変えていない"
            NOTE.write_text(mutated, encoding="utf-8")
            ih.invalidate_bytecode_cache(NOTE, ROOT)
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
