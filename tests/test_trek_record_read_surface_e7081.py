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

判断記録の種別についての固定は、一度 **偽の緑** だった (e-7320): ソース文字列の
包含で測っていたため、集合から外して「外した」とコメントに書くだけで緑になった。
いまは実体 (引退辞書の membership) を見る。壊したら本当に赤くなるか、そして
**コメントだけ消しても緑のままか** は
``scripts/invert-decision-vocab-retirement-e7320.py`` が実測する。
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


def test_the_local_read_path_is_gone_and_stays_gone():
    """ローカル記録を読む経路は**意図して無い** (ms-185 e-7322 / e-7327)。

    この file の他のテストは「残す」側を測っているので、ここだけ向きが逆になる。
    足した理由: 上の 2 本を読んだ人が「方針5 = 記録を後から読めなくしない」から
    ローカル側も残すべきだと推測して復元しうるため。実際に起きたのは逆で、
    ``lib/store*.py`` の ``list_treks`` 5 箇所には**生きた読み手が居ないのに**
    「``lib/cmd_bus.py`` の送信予算の迂回が来る」という**誤った根拠のコメント**が
    付いていた (独立思想レビューが実測で否定。cmd_bus には ``store.`` の使用が
    1 つも無い)。

    残す面はクラウド側 (上の 2 本が固定している HTTP 3 ルート + 3 backend の
    読み取り 3 関数)。ローカルの ``~/.beacon/treks/*.json`` は**消していない**ので
    素の JSON として手で開けば読める。製品面として読む口は無い、が正しい状態。
    判断記録は e-7324。

    戻すときの条件: 実在する読み手と一緒に戻すこと。読み手の無い passthrough を
    「将来使うから」で置くと、誤った根拠のコメントが 5 ファイルに増殖した前回と
    同じ形になる。
    """
    import importlib
    import sys

    sys.path.insert(0, str(REPO / "lib"))

    # Store 層の 4 実装 + 抽象に ``list_treks`` が無い。
    for mod_name, cls_name in (("store", "Store"), ("store_local", "LocalStore"),
                               ("store_api", "StoreApi"), ("store_sqlite", "SqliteStore")):
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, cls_name, None)
        assert cls is not None, f"{mod_name}.{cls_name} が無い (この検査の前提が崩れている)"
        assert not hasattr(cls, "list_treks"), (
            f"{cls_name}.list_treks が戻っている — 読み手が在るか確かめること。"
            f"前回は読み手ゼロのまま『cmd_bus が来る』と書いて 5 ファイルに残した")

    # HTTP クライアントの便利メソッド 2 つも無い。
    api_client = importlib.import_module("api_client")
    for name in ("list_treks", "get_trek"):
        assert not hasattr(api_client.ApiClient, name), (
            f"api_client.ApiClient.{name} が戻っている — 32 メソッドに適用した"
            f"『呼び出し元ゼロなら落とす』と一貫しない")

    # ローカル記録を読むモジュールごと無い。
    assert not (REPO / "lib" / "trek_store.py").exists(), (
        "lib/trek_store.py が戻っている — 読み手と一緒に戻したのか確認すること")


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


# 引退した種別。産出する継ぎ目は ms-185 で消えたが、過去の記録を引いた読み手が
# 種別を解決できなければならない (= e-7067 の作法)。
RETIRED_KINDS = ("trek-review", "halt", "resume")


def test_the_decision_vocabulary_keeps_the_trek_kinds():
    """引退した種別を読み手が解決できる (ms-185 e-7081、測り方は e-7320 で修正)。

    **初版はコメントで素通りしていた** (e-7320): ``kind in src`` でソース文字列の
    包含を見ていたので、集合から外して「外した」とコメントに書くだけで緑になった。
    実際 e-7071 で 3 種別は集合から落ちており、緑はコメントが支えていた。
    緑は信頼されるので、偽の緑は無検査より悪い。ここは **実体 (membership)** を見る。

    判断の分界 (e-7320):
    * ``KNOWN_DECISION_KINDS`` = いま継ぎ目が産出する種別。捕獲被覆の検査の母集団
      なので、産出者の無い種別を入れてはいけない (永久に赤くなる)。
    * ``RETIRED_DECISION_KINDS`` = かつて産出していた種別。読み手だけのための辞書で、
      産出者を要求しない。
    だから引退種別は「KNOWN に在る」のではなく「RETIRED に在り KNOWN に無い」が正。
    この 2 つを同時に固定するので、``test_decision_event_schema`` の
    「trek 3 種は KNOWN から外れていること」と矛盾しない (初版は矛盾していて、
    弱い測り方のせいで両方緑だった)。
    """
    import sys
    sys.path.insert(0, str(REPO / "lib"))
    import decision_vocab

    for kind in RETIRED_KINDS:
        assert kind in decision_vocab.RETIRED_DECISION_KINDS, (
            f"引退した種別 {kind!r} が読み手用の辞書から消えています。過去の判断記録を"
            f"この種別で引いた人に『綴り違いの可能性』と偽の警告が出ます (e-7320)")
        assert decision_vocab.explain_retired_decision_kind(kind), (
            f"{kind!r} の説明文が空です (読み手が種別の意味を解決できません)")
        assert kind not in decision_vocab.KNOWN_DECISION_KINDS, (
            f"{kind!r} が産出者台帳の母集団 (KNOWN_DECISION_KINDS) に戻っています。"
            f"産出する継ぎ目が無いので捕獲被覆の検査が永久に赤くなります。"
            f"本当に戻すなら producer を一緒に戻すこと")

    # 生きている種別は KNOWN 側に在る (= 引退辞書へ誤って移していない)。
    assert "scope-approval" in decision_vocab.KNOWN_DECISION_KINDS
    assert "scope-approval" not in decision_vocab.RETIRED_DECISION_KINDS


def test_retired_kinds_do_not_trigger_the_typo_warning():
    """引退種別を ``--kind`` で引いても「綴り違い」と言われない (e-7320)。

    これが e-7081 の判断が守りたかった実害そのもの。語彙から外した結果、
    ``beacon decision list --kind trek-review`` が 0 件に添えて「綴り違いの可能性」と
    警告していた —— 綴りは正しいので、開示が嘘になっていた。
    """
    import sys
    sys.path.insert(0, str(REPO / "lib"))
    import cmd_decision

    recognized = cmd_decision._recognized_decision_kinds()
    for kind in RETIRED_KINDS:
        assert kind in recognized, (
            f"{kind!r} が『見覚えのある種別』に入っていません。0 件のときに綴り違いを"
            f"疑わせる偽の警告が出ます (e-7320)")
