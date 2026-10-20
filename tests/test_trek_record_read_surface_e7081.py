"""Trek の過去の記録を「読める」状態として残す判断を固定する (ms-185 e-7081)。

**判断**: 読み出しは残し、書き込みだけを落とす。

**なぜ**: この MS は「記録は当時そう判断した事実」という前提で進めている
(却下理由・失われる機能・判断軌跡をすべて判断記録に残している)。その同じ MS が、
過去 14 件の Trek の記録を**製品のどの面からも読めない状態**にするのは筋が通らない。
データベースには残るが誰も引けない、は「消した」と実質同じで、監査可能性が落ちる。

同じ作法を e-7067 で先に 1 箇所やっている:
``lib/dm_consent.RETIRED_CONSENT_REASON_EXPLANATIONS`` — 規則は消したが、過去の
監査記録に残る判定理由の**説明だけ**は読めるまま残した (定数は置かない = 誰も
セットしない判定は作らない)。読む側だけ残す、という同じ形をここでも採る。

**このファイルの役目**: 削除を進める段 (e-7070 / e-7079) が読み出し面を一緒に
落とさないための強制力。消すときは「落とした」とここで必ず赤くなる。
削除後に残っていなければならない面を 1 つずつ名指しで固定する。

書き込み面 (作成 / 参加 / スコープ承認 / halt / 発火) はこのファイルの対象外 —
落ちるのが正しいので、ここで固定してはならない。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]

# 削除後も読めなければならない面。左が「何を読む面か」、右が実装上の目印。
READ_SURFACES = {
    "一覧 (HTTP)": (pathlib.Path("server/routers_treks.py"), '@router.get("/api/treks")'),
    "個別表示 (HTTP)": (pathlib.Path("server/routers_treks.py"),
                        '@router.get("/api/treks/{trek_id}")'),
    "記録の読み出し (HTTP)": (pathlib.Path("server/routers_treks.py"),
                              '@router.get("/api/treks/{trek_id}/logs.jsonl")'),
}

# 保存層の読み取り関数 (backend 両方に在ること)。書き込み関数は対象外。
STORE_READS = ("get_trek", "list_treks", "list_trek_logs")
STORE_FILES = (pathlib.Path("server/firestore_client.py"),
               pathlib.Path("server/dynamodb_client.py"))


def _defines(path: pathlib.Path, name: str) -> bool:
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
               for n in ast.walk(tree))


def test_http_read_surfaces_survive():
    missing = []
    for label, (path, marker) in READ_SURFACES.items():
        if marker not in (REPO / path).read_text(encoding="utf-8"):
            missing.append(f"{label} ({marker} in {path})")
    assert not missing, (
        "過去の Trek の記録を読む面が落ちています: " + "; ".join(missing) +
        "。ms-185 e-7081 の判断は『読み出しは残し、書き込みだけ落とす』です。"
        "本当に読めなくするなら、判断記録を書き換えてからこのテストを畳むこと")


def test_store_read_functions_survive_on_both_backends():
    missing = [f"{path}:{name}" for path in STORE_FILES for name in STORE_READS
               if not _defines(path, name)]
    assert not missing, (
        f"保存層の読み取り関数が落ちています: {missing}。"
        f"片方の backend だけ残すと、backend を切り替えた瞬間に読めなくなる")


def test_the_search_path_can_still_include_trek_records():
    """過去の記録を横断検索する経路 (--include-trek) が生きている。"""
    import inspect
    import sys
    sys.path.insert(0, str(REPO / "lib"))
    import retro_query
    params = inspect.signature(retro_query.retro_query).parameters
    assert "include_trek" in params, (
        "--include-trek の受け口が落ちています。記録は残っているのに "
        "横断検索から引けない状態になります")
    # CLI 側の旗も生きていること (= 受け口だけ在って誰も渡さない形を作らない)
    cli = (REPO / "lib" / "commands.py").read_text(encoding="utf-8")
    assert "BEACON_INCLUDE_TREK" in cli, (
        "CLI 側が --include-trek を渡さなくなっています (受け口だけ残っても "
        "人は引けない)")


def test_the_decision_vocabulary_keeps_the_trek_kinds():
    """判断記録の種別 (trek-review / scope-approval / halt / resume) が残っている。

    過去の判断記録はこの語彙で保存されている。語彙から消すと、記録を読んだ側が
    種別を解決できない (= e-7067 で引退語彙を別辞書に残したのと同じ理由)。
    """
    import sys
    sys.path.insert(0, str(REPO / "lib"))
    import decision_vocab
    src = (REPO / "lib" / "decision_vocab.py").read_text(encoding="utf-8")
    for kind in ("trek-review", "scope-approval", "halt", "resume"):
        assert kind in src, f"判断記録の種別 {kind!r} が語彙から消えています"
    assert hasattr(decision_vocab, "__file__")
