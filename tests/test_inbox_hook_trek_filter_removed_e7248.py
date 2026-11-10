"""ms-185: 受信フックから「産出者ゼロの判定」を外したことを固定する。

独立した思想レビューが、`bin/beacon-bus-inbox-hook.py` が archived-trek フィルタを
main loop に配線したまま残していると指摘した。このフィルタは trek-* channel の event
ごとに `GET /api/treks/{id}` を引き、archived な trek の event を AI に見せず落とす層
だった。それを書いていた trek scheduler は e-7070 で撤去済みなので、**判定する相手が
居ない** —— SPEC 方針 4 が名指しで避けた「動かない配線 / 誰もセットしない判定」そのもの。

退役 channel が万一届いた場合の扱いは、落とすのではなく **開示する** (e-7079 が決めた
受信側の振る舞い)。したがってこの試験が固定するのは 2 つで、**両方そろって初めて意味が
ある**: 撤去した機構が戻っていないこと / 開示が生きていること。片方だけを測ると
「フィルタを消したが開示も一緒に落とした」= 受信が黙って消える退行を緑のまま通す。

## 測り方について — ソースの文字列では測らない (PR#840 / 4 周のレビュー)

この試験は当初、フックのソースを文字列で検索していた。4 周の独立レビューがその形を
3 段階で壊した:

1. 全文から `/api/treks/` を探す → **撤去を説明する自分の退役注記で赤くなった**
2. `#` で始まる行を除く → docstring 形式の注記では赤くなる (約束より実装が狭い)
3. `tokenize` で COMMENT と STRING を除く → **URL は定義上 string literal の中にしか
   無いので、探している対象そのものを捨てた**。違反を入れても緑。judge が削除された
   フィルタを別名で丸ごと再実装し、全件 pass することを実測した

偽陽性を直すたびに逆側へ振れ、3 手で「絶対に発火しない検査」に着地した。散文の正しさは
機械で閉じられないので、**ここでは約束しない**。測るのは次の 2 つだけにする:

- **モジュール属性** — 撤去した関数が本当に無いか (コメントに名前が出ても反応しない)
- **挙動** — 退役 channel の event が実際に降格され、降格理由が付くか

散文の drift は独立レビューが見る。実際に PR#840 ではレビューが 2 回それを捕まえた。

## この試験が捕まえられないこと (約束しない範囲を明記する)

**別名での再実装は捕まらない。** 誰かが `_check_trek_archived` のような別の名前で同じ
per-event の API 往復を書き直せば、属性の検査は素通りする (実測済み)。文字列で測る形なら
捕まえられる、とも言えない —— 4 周のレビューがその道は偽の赤と偽の緑を交互に作ると実証
した (上記 3 段階)。

つまりこの退行を機械で閉じる手段は、いまのところ無い。**無いものを在るように書かない**
ために、ここに明記しておく。実際にこの型を捕まえたのは独立レビューで、PR#840 では
judge が削除されたフィルタを別名で再実装して「検査が通ってしまう」ことを示した。節目の
独立レビューがこの穴の受け皿である。
"""
import importlib.util
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "bin", "beacon-bus-inbox-hook.py")
sys.path.insert(0, os.path.join(ROOT, "lib"))


@pytest.fixture(scope="module")
def hook_module():
    """フックをモジュールとして読む (test_beacon_bus_inbox_hook.py と同じ流儀)。"""
    spec = importlib.util.spec_from_file_location("bus_inbox_hook_e7248", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bus_inbox_hook_e7248"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", [
    "_is_trek_scheduler_event",
    "_lookup_trek_status",
    "_filter_archived_trek_events",
    "_format_trek_retired_block",
    "_TREK_SCHEDULER_CHANNELS",
    "TREK_RETIRED_CHANNELS",
])
def test_removed_symbols_are_really_gone(hook_module, name):
    """撤去した関数と定数がモジュールに存在しないこと。

    ソース文字列ではなく **属性** で測るので、退役注記がその名前を挙げていても
    反応しない (= 「何が在ったか」の記録を書いても赤くならない)。
    `TREK_RETIRED_CHANNELS` は `lib/bus_delivery.RETIRED_PROVENANCE_CHANNELS` の
    写しだったので、正本 1 箇所に寄せて消した。
    """
    assert not hasattr(hook_module, name), (
        f"{name} がフックに戻っています。産出者 (trek scheduler) は e-7070 で撤去済み "
        f"なのでこの判定は相手が居ません。退役 channel の集合の正本は "
        f"lib/bus_delivery.RETIRED_PROVENANCE_CHANNELS です"
    )


def test_decision_layer_downgrades_a_retired_channel_with_a_reason_tag():
    """届いた退役 channel が黙って消えないこと —— **実際に通る経路で**測る。

    初版はソースに `elif channel in TREK_RETIRED_CHANNELS:` の文字列が在ることで
    「開示は生きている」と主張していた。独立 AX レビューがその分岐が **到達不能** で
    あることを実測した: `classify_auto_execute` は退役 channel を `auto-execute` 判定の
    前に `propose-to-ai` へ降格するので、`elif delivery == "auto-execute":` の内側に
    在るあの分岐には一度も入らない。つまりその試験は偽の緑で、消えた開示を「生きて
    いる」と報告しうる形だった。

    なので測るのは挙動にする: 退役 channel の event が降格され、降格理由が付くこと。

    **この試験が測る層 (名前を実態に合わせた、ms-185 e-7396 項目 4)**: ここで測るのは
    判定層 `lib/bus_delivery.classify_auto_execute` の戻り値 **だけ** である。旧名
    (`test_retired_channel_is_still_disclosed_via_the_downgrade_path`) は「開示される」
    = 受信フックの保証を名乗っていたが、フックは一度も走らせていない。実測: フック側で
    理由をイベントに載せる配線 (`bin/beacon-bus-inbox-hook.py` の
    `ev["_downgrade_reason"] = downgrade_reason`) を無効化してもこのファイルは全件緑に
    なる。緑のガードは信頼されるので、名前が保証を過大に名乗る状態そのものが害だった。

    フック側の配線は
    `test_hook_surfaces_the_retired_channel_advice_end_to_end` が測る (そちらは配線を
    壊すと赤くなる)。
    """
    import bus_delivery

    ev = {
        "channel": "trek-trigger",
        "payload": {"trek_id": "tk-1"},
        "delivery": "auto-execute",
        "envelope": {"tier": "T2", "scope": "trek:tk-1", "signature": "x"},
    }
    # allowlist に入れても (= 人間が opt-in しても) 降格されるのが「退役」の意味。
    delivery, downgraded_from, reason = bus_delivery.classify_auto_execute(
        ev, allowlist=frozenset({"trek-trigger"}))
    assert delivery == "propose-to-ai", delivery
    assert downgraded_from == "auto-execute", downgraded_from
    assert reason == "retired-provenance-channel", reason


def test_hook_surfaces_the_retired_channel_advice_end_to_end(
        hook_module, tmp_path, monkeypatch, capsys):
    """受信フックを **実際に流して**、退役 channel の案内が AI 向けの出力に出ること。

    ms-185 e-7396 項目 4: 兄弟の
    `test_decision_layer_downgrades_a_retired_channel_with_a_reason_tag` は判定層の
    戻り値だけを測る。「開示が生きている」という**フック側の保証**は、フックを流さない
    限り測れていない —— 実測で、理由をイベントに載せる配線を無効化しても兄弟の試験は
    緑のままだった。ここがその配線を測る受け皿で、壊すと赤くなる。

    測るのは挙動 (出力に案内が出るか) で、ソースの文字列ではない (このファイル冒頭の
    「測り方について」を参照)。
    """
    import json as _json

    root = tmp_path / "proj-retired"
    (root / ".beacon").mkdir(parents=True, exist_ok=True)
    # 退役 channel を **許可リストに入れた** 状態 (= 人間が昔 opt-in した残り) で
    # 降格されるのが「退役」の意味。
    (root / ".beacon" / "project.json").write_text(
        _json.dumps({"bus_auto_execute_channels": ["trek-trigger"]}))
    (root / ".beacon" / "session.json").write_text(
        _json.dumps({"session_id": "sess-X"}))
    (root / ".beacon" / "cloud.json").write_text(
        _json.dumps({"api_url": "https://api.test", "project_id": "proj-1"}))

    ev = {
        "event_id": "ev-retired-1",
        "channel": "trek-trigger",
        "sender_session_id": "sv-other",
        "created_at": "2026-11-10T00:00:00Z",
        "payload": {"trek_id": "tk-1"},
        "delivery": "auto-execute",
    }

    monkeypatch.setattr(hook_module, "_load_id_token", lambda: "fake-token")
    monkeypatch.setattr(hook_module, "_list_unread", lambda *a, **kw: [ev])
    monkeypatch.setattr(hook_module, "_ack_cursor", lambda *a, **kw: None)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(_json.dumps({"cwd": str(root), "session_id": "sess-X",
                                 "hook_event_name": "UserPromptSubmit"})))
    hook_module.main()
    out = capsys.readouterr().out

    assert "ev-retired-1" in out, (
        "退役 channel の event が出力から消えています。扱いは「落とす」ではなく "
        "「開示する」(e-7079)"
    )
    assert "downgraded from" in out, (
        "降格の印が出ていません (フック側の `if downgraded_from:` の配線)"
    )
    # 回復手段が案内に出ること。trek-* は署名を要求する集合には居らず退役扱いなので、
    # 直し方は「許可リストから外す」である (e-7396 項目 3)。
    assert "auto-execute remove" in out, (
        "回復手段の案内 (許可リストから外す) が出ていません。フック側で "
        "`_downgrade_reason` をイベントに載せる配線が外れると、理由タグが落ちて "
        "案内が別物になります"
    )


def test_hook_still_parses():
    """構文が壊れていない (= 大きく削ったので形を実際に確かめる)。"""
    import ast
    with open(HOOK, encoding="utf-8") as f:
        ast.parse(f.read())

def test_the_per_event_loop_has_no_direct_network_call():
    """event ごとのループが、その場でネットワークを叩かないこと。

    撤去した層の実害は「event 1 件ごとに `GET /api/treks/{id}` を往復する」ことだった。
    上の属性検査は **別名での再実装を捕まえられない** (そう明記した) が、独立レビューの
    5 周目が「*形* なら安く測れる」と指摘した: event を回すループの中に I/O 呼び出しが
    在るかどうかは AST で見える。現状の件数は 0 なので誤検知の土台が無い。

    ## この検査が約束しないこと

    **呼び出し先までは追わない。** ループが `_helper(ev)` を呼び、その helper が中で
    往復するなら、この検査は素通りする。到達可能性の解析はまさに 4 周のレビューで
    私が繰り返し失敗した「賢い検査」の側で、同じ轍は踏まない。ここが約束するのは
    「ループ本体に I/O の呼び出しが直接書かれていない」という 1 つの形だけ。
    """
    import ast

    with open(HOOK, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    loops = [n for n in ast.walk(main_fn)
             if isinstance(n, ast.For) and "unread" in ast.unparse(n.iter)]
    assert loops, (
        "main に `for ... in unread:` のループが見つかりません。"
        "この検査はそのループを対象にしているので、形が変わったら検査も見直してください"
    )
    io_names = ("_api_get", "urlopen", "urlretrieve", "Request")
    offenders = []
    for loop in loops:
        for call in (n for n in ast.walk(loop) if isinstance(n, ast.Call)):
            name = ast.unparse(call.func)
            if any(tok in name for tok in io_names):
                offenders.append(name)
    assert not offenders, (
        f"event ごとのループの中で直接ネットワークを叩いています: {offenders}。"
        f"撤去した archived-trek フィルタの実害はこの形 (event 1 件ごとの往復) でした"
    )
