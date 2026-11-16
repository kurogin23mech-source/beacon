"""URL 軸とヘッダの構造的な固定 (ms-185 e-7325 で移設)。

**この file が引き継いだもの**: ``tests/test_trek_url_axis_and_header_independence.py``
(ms-86 / e-2250) の 13 件のうち、**Trek に依らない 2 件**。元 file は Trek detail の
URL 軸 (``?trek=<id>`` 単独表示 / 閉じた時の復元 / Trek が開いている間は project URL
同期を抑止) を固定していたが、e-7325 で画面本体ごと落としたので、11 件は守る対象が
消えた。元 file は ``.trash/ms-185-e7325/`` に退避してある (消さずに残す)。

残した 2 件は Trek と無関係に生きている面を測っている:

  1. ヘッダが project 名 + tag を出すこと。元は「Trek を開いていない時の分岐」
     として測っていたが、分岐そのものが無くなったので**常に**そうなる。
  2. ``_setUrlForProjectView`` が ``?project=`` を書き戻すこと。この helper は
     Trek と一緒に生まれたが、やっていることは project 軸の URL 同期で、
     e-7325 の削除対象には入っていない (= 呼び出し元が残っている)。

**なぜ空にせず移設したのか**: 対象を消して古い pin をそのまま緑で通すのは e-7320 で
踏んだ型 (= 自分のコメントで pin が素通りする偽の緑)。守る対象が在る 2 件は、守る
場所を変えて生かす。
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = REPO / "server" / "static" / "index.html"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def test_header_shows_project_name_and_tag():
    """ヘッダは project 名と tag を出す。

    ms-185 e-7325: 元は ``state.openTrekId`` が無い時の分岐として測っていた。
    その分岐 (= Trek を開いている間は brand "Beacon" に差し替える) ごと落ちたので、
    いまは条件無しでこの 3 つが在ることを測る。
    """
    src = _read(WEB_INDEX)
    assert 'class="project-name"' in src
    assert "esc(p.name)" in src
    assert "PLATFORM.headerTag" in src


def test_set_url_for_project_view_restores_project():
    """``_setUrlForProjectView`` が ``?project=`` を書き戻す。"""
    src = _read(WEB_INDEX)
    assert "function _setUrlForProjectView(projectId)" in src
    body_start = src.index("function _setUrlForProjectView")
    body = src[body_start:body_start + 800]
    assert "history.replaceState" in body
    assert "params.set('project'" in body
