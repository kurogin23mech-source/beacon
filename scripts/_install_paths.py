"""この install の ``lib`` ディレクトリを解決する唯一の定義 (scripts/ 用)。

なぜこのモジュールが要るか
--------------------------
「install root から import 可能な lib ディレクトリを探す」という規則
(= source / editable install は ``<root>/lib``、pipx や brew の wheel は
``beacon_cli/_bundled_lib`` に再配置される) は、``scripts/`` 配下の複数の
スタンドアロンスクリプトが必要とする。各スクリプトが自前で書くと、配置規則を
変える人が 1 箇所だけ直して出荷し、残りが黙って古い規則のまま動く。

``bin/hook_bootstrap.py`` が ``bin/`` 配下の hook 群に対して同じ役割を果たして
いる (PR #738 の保守性レビュー指摘で作られた)。ただしあれは自分の位置からの
兄弟解決に特化していて ``_bundled_lib`` (wheel 配置) を扱わないため、
``install_root`` を受け取る ``scripts/`` 側にはこちらが要る。

ファイル名にアンダースコアを使っているのは意図的: ``scripts/`` の他のスクリプトは
``codex-receive-loop.py`` のようにハイフンを含み Python から import できない。
ハイフンの無いこのモジュールは、同じ ``scripts/`` に居る任意のスクリプトから
``from _install_paths import resolve_lib_dir`` で参照できる (Python は実行中の
スクリプトのディレクトリを ``sys.path`` 先頭に置くため)。
"""
from __future__ import annotations

from pathlib import Path

# lib が置かれうる場所を優先順に。先に来たものを採る。
_LIB_SUBDIRS = ("lib", "_bundled_lib")

def resolve_lib_dir(install_root: "str | Path") -> Path:
    """``install_root`` 配下の lib ディレクトリを返す。

    ``<root>/lib`` → ``<root>/_bundled_lib`` の順に、実在するディレクトリを探す。
    どちらも無ければ ``<root>/lib`` を返す (呼び出し側が import 失敗として扱える
    ように、例外は投げない)。

    **中身の検証はしない** (= ディレクトリの存在だけを見る)。PR #764 の独立 AX
    レビューは「``commands.py`` の実在まで確かめる ``beacon_cli/main.py`` 側に
    倣え」と指摘したが、採らなかった。理由は 2 つ:

      * ここの呼び出し元 (Codex daemon / bcodex watcher) が実際に import するのは
        ``codex_session`` / ``api_client`` / ``bus_protocol`` であって
        ``commands`` ではない。``commands.py`` は実 install では偶然一致する
        代理の目印にすぎず、この呼び出し元が要求する条件ではない。
      * 中身を検証するなら ``bin/hook_bootstrap.py:import_lib`` のように
        **呼び出し元が必要とするモジュール名を受け取って**確かめるのが正しい形で、
        それは配置解決とは別の設計判断になる。e-6591 (Windows の生存確認) の
        ついでに daemon 起動経路の契約を変えるのは範囲外。

    目印検証を入れるかどうかは follow-up task に切り出した。
    """
    root = Path(install_root)
    for sub in _LIB_SUBDIRS:
        candidate = root / sub
        if candidate.is_dir():
            return candidate
    return root / _LIB_SUBDIRS[0]
