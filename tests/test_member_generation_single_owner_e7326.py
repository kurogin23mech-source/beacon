"""members の世代判定を持つ場所が 1 つであることを固定する (ms-185 e-7326)。

**この検査が置き換えたもの**: e-7069 には ``test_trek_member_rule_parity_e7069.py``
が在り、正本 (``lib/trek.py``) と写しの一致を突き合わせていた。e-7070 が本体を
落とすとき、その突き合わせテストも本体と一緒に ``.trash/`` へ移した。一方で世代判定
そのもの (``migration_phase`` の既定 / 有効値 / session_id keyed か) は
``lib/cmd_bus.py`` と ``server/routers_treks.py`` の **2 ファイルにベタ書きで写され**、
一致を測るものがゼロになった —— 独立保守性レビューが「e-7069 が『二重実装は黙って
ズレる』として足した突き合わせと同じ穴を、削除の過程で作り直した」と指摘した状態。

**e-7326 の起票時の前提は、着手時にはもう古かった。** 実測すると ``lib/cmd_bus.py``
側の写しは e-7080 / e-7321 で既に落ちている (送信予算の迂回判定ごと。同ファイルの
``# ms-185 e-7080 / e-7321`` の注記を参照)。つまり「2 箇所がズレる」欠陥は消えており、
写しは ``server/routers_treks.py`` の 1 つだけになっていた。

なので固定するものを**一致 (parity) から単一所有 (single owner) へ読み替えた**。
2 つ在るものの一致を測るのではなく、**2 つ目が現れたら赤くなる**側を測る。残る risk は
「ズレる」ではなく「また写される」ことなので、そこを閉じるのが筋である。

検査しないこと: 値そのものの正しさ (phase に 'D' を足すべきか等) は見ていない。
見ているのは「世代の語彙を宣言している場所が本番コードに 1 つだけか」だけ。
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent

# 本番コードの探索範囲。tests/ は対象外 (テストが期待値を書くのは写しではない)。
PROD_DIRS = ("lib", "server", "bin", "scripts", "beacon_cli", "channel")

# 世代の語彙を「宣言している」形。コメントや注記での言及は数えない。
DECLARATIONS = (
    # 定数の宣言 (= 値集合 / 既定値 / meta キー名を持つ側)
    re.compile(r'^\s*_?(?:DEFAULT_MIGRATION_PHASE|VALID_MIGRATION_PHASES|'
               r'MIGRATION_PHASE_META_KEY)\s*[:=]'),
    # 判定関数の定義
    re.compile(r'^\s*(?:async\s+)?def\s+_?(?:get_migration_phase|'
               r'is_session_id_keyed)\s*\('),
)

# 単一の所有者。ここが唯一宣言してよい場所。
OWNER = pathlib.Path("server/routers_treks.py")


def _declaring_files() -> dict:
    found: dict = {}
    for d in PROD_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix not in (".py", ".mjs", ".js"):
                continue
            if ".trash" in p.parts or ".archive" in p.parts:
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            hits = [i for i, l in enumerate(lines, 1)
                    if any(pat.match(l) for pat in DECLARATIONS)]
            if hits:
                found[p.relative_to(REPO)] = hits
    return found


def test_the_generation_vocabulary_has_exactly_one_owner():
    found = _declaring_files()
    assert set(found) == {OWNER}, (
        f"members の世代判定を宣言している場所: {sorted(map(str, found))}。"
        f"期待は {OWNER} の 1 つだけ。2 つ目が現れた場合、phase を増やすときに"
        f"片方だけ直しても CI は緑のままになり、読み取り経路と他の読み手が"
        f"黙って食い違う (e-7069 が突き合わせを足した理由そのもの)。"
        f"共有する必要があるなら、写すのではなく一方が他方を import すること。"
    )


def test_the_owner_actually_declares_all_three_pieces():
    """所有者が 3 つ (既定値 / 値集合 / 判定関数) を揃って持っている。

    上の検査だけだと、所有者から宣言が消えても「0 件 = 1 件でない」で赤くなる
    ものの、理由が「増えた」なのか「消えた」なのか読めない。両方を分けて測る。
    """
    src = (REPO / OWNER).read_text(encoding="utf-8")
    for marker in ('_DEFAULT_MIGRATION_PHASE = "pre-A"',
                   '_VALID_MIGRATION_PHASES = ("pre-A", "A", "B", "C")',
                   '_MIGRATION_PHASE_META_KEY = "migration_phase"',
                   "def _get_migration_phase(",
                   "def _is_session_id_keyed("):
        assert marker in src, f"{OWNER} が {marker!r} を持っていません"
