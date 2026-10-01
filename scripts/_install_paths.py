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

    **中身の検証はしない** (= ディレクトリの存在だけを見る)。これは e-6686 で
    検討した上での**確定した判断**であり、未決の先送りではない。PR #764 の独立 AX
    レビューは「``commands.py`` の実在まで確かめる ``beacon_cli/main.py`` 側に
    倣え」と指摘した (severity low / confidence medium) が、採らない。理由は 4 つ:

      * **検証を入れても「どのディレクトリを選ぶか」は変わらない**。``lib`` と
        ``_bundled_lib`` は実配置で共存しない (source/editable の root は ``lib``
        を持ち ``_bundled_lib`` を持たない。wheel の install_root は
        ``beacon_cli`` パッケージ自身で ``_bundled_lib`` を持ち ``lib`` を
        持たない)。つまりここでの検証は ``import_lib`` のような**候補を読み飛ばす
        fallback 機構にはならず、エラー文言の差し替えにしかならない**。
      * **daemon 側 (``scripts/codex-receive-loop.py:_import_modules``) は、既に
        読めるエラーで落ちる**。try/except 無しで ``import codex_session`` 等を
        行うので、空/壊れた lib なら
        ``ModuleNotFoundError: No module named 'codex_session'`` が sys.path 付きで
        出る。欠けているモジュール名が既に名指しされており、事前検証が足す情報は
        無い。daemon は人が起動して出力を見るものなので、loud に落ちるのが正しい。
      * **Codex hook 2 本 (``codex-inbox-hook.py`` / ``codex-halt-check-hook.py``)
        は、契約として silent no-op**。``bin/hook_bootstrap.py`` が全 hook に課す
        原則 ("fails SAFE: a hook that cannot import its lib must degrade to a
        silent no-op, never raise into the harness") に従い、空 lib でも
        ``{}`` を出して exit 0 する。ここで事前検証しても沈黙が 1 層早まるだけで、
        沈黙そのものは消えない。hook の no-op が見えない問題は配置解決ではなく
        可観測性の課題であり、別に扱うべき関心事。

        この 2 本目の性質は **e-6686 で揃えたもの**で、元から成立していたのでは
        ない。``codex-inbox-hook.py`` は先頭の ``import codex_receive_loop`` だけが
        無防備で、空 lib では生の traceback が Codex へ抜けていた (PR #773 の独立
        レビュー 2 体が合意で指摘し、一方は実際に空 lib で走らせて exit 1 を実測
        した)。当初この docstring は姉妹 hook の文言を 2 本に一般化して書いており、
        **事実と食い違っていた**。import を包み、入口に二層目の網を置いて主張を真に
        し、両 hook が空 lib で exit 0 することを
        ``tests/test_scripts_lib_resolution_e6686.py`` で固定した。
      * ``commands.py`` という目印そのものがこの呼び出し元にとって誤り。実際に
        import されるのは ``codex_session`` / ``api_client`` / ``bus_protocol`` /
        ``stop_signal`` で、``commands`` は実 install で偶然一致する代理に
        すぎない。正しく検証するなら ``bin/hook_bootstrap.py:import_lib`` のように
        **呼び出し元が必要とするモジュール名を受け取る**形になり、それは配置解決と
        は別レイヤーの設計になる (そして上記 1 点目のとおり、この経路では選択を
        変えないので得が無い)。

    将来この判断を覆す条件は 1 つだけはっきりしている: ``_LIB_SUBDIRS`` の候補が
    **実配置で共存しうる**ようになったら、検証は fallback 機構として意味を持ち始め
    るので再検討する。その場合も目印は ``commands.py`` ではなく呼び出し元が要る
    モジュール名を受け取る形にする。
    """
    root = Path(install_root)
    for sub in _LIB_SUBDIRS:
        candidate = root / sub
        if candidate.is_dir():
            return candidate
    return root / _LIB_SUBDIRS[0]
