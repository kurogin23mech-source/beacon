"""T1-system の層を残す判断の前提を、実コードで測り続ける (ms-185 e-7078)。

**判断**: T1-system (= サーバ自身が発行する最上位の署名) の層は **残す**。
Trek を畳むと発行者は 0 になるが、層を消すのは**緩める方向**だと実測で分かった。

**実測した事実** (2026-10-20):

  * 本番で発火する Operation が運ぶのは、人間が ``beacon operation approve`` で
    発行した **T2** の署名 (server/routers_projects.py ``operation_approve`` →
    ``TIER_T2``)。T1-system ではない。
  * ``operation-trigger`` は ``SYSTEM_PROVENANCE_CHANNELS`` に在るので、T2 では
    足りず、発火イベントは ``propose-to-ai`` に降格される。
  * **ゲートを外すと同じイベントが署名なしで自動実行として通る。**

つまりこの層は「誰も満たせない死んだ判定」ではなく、**本番で唯一発火している
channel を自動実行から止めている現役の判定**である。発行者が居ないこととゲートが
働くことは別の話で、ここを混同すると「使われていないから消す」で危険側に倒れる。

このファイルが固定するのは 4 点:
  1. 本番の形 (T2) が降格されること — ゲートが現に効いている。
  2. ``operation-trigger`` を署名必須の集合から外すと、同じイベントが通ること
     — 止めているのはゲートだという因果の証明 (消したときに何が起きるかを
     テストの中で先に見せる)。
  3. T1-system が付いていれば通ること — 層そのものは機能している。
  4. 発行者の棚卸し: ``issue_t1_system_envelope`` の呼び出し元がすべて Trek の
     ファイルに在ること。e-7070 で Trek を消したあと発行者が 0 になるのは
     **意図した状態**で、Trek 以外に発行者が生えていたらここで気付く。
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import bus_delivery as bd  # noqa: E402

ALLOWLIST = ["operation-trigger"]

# 本番の ``_fire_operation`` が組み立てる形 (= 人間が approve した T2 を再利用する)。
FIRED_OPERATION_EVENT = {
    "channel": "operation-trigger",
    "delivery": "auto-execute",
    "envelope": {"tier": "T2", "issuer": "user@example.com",
                 "actions_authorized": ["operation.check"]},
}


def test_a_fired_operation_is_downgraded_today():
    """本番の形 (T2) は自動実行として通らない。"""
    delivery, downgraded_from, reason = bd.classify_auto_execute(
        FIRED_OPERATION_EVENT, allowlist=ALLOWLIST)
    assert delivery == bd.PROPOSE_TO_AI
    assert downgraded_from == bd.AUTO_EXECUTE
    assert reason == bd.DOWNGRADE_NON_SYSTEM_ENVELOPE


def test_removing_the_gate_would_honor_it_unsigned():
    """**止めているのはゲートである**ことを、外した世界と比べて証明する。

    「使われていないから消す」で危険側に倒れるのを防ぐための対照実験。
    ゲートの対象から ``operation-trigger`` を外すと、同じイベントが署名なしの
    まま自動実行として通る。
    """
    delivery, _from, _reason = bd.classify_auto_execute(
        FIRED_OPERATION_EVENT, allowlist=ALLOWLIST,
        provenance_channels=frozenset())
    assert delivery == bd.AUTO_EXECUTE, (
        "ゲートを外しても降格された — この対照実験が意味を失っている。"
        "allowlist / retired の判定順が変わった可能性がある")


def test_operation_trigger_is_in_the_provenance_set():
    """ゲートの対象から ``operation-trigger`` が落ちていない。

    落ちると上の対照実験の「外した世界」が既定になる (= 署名なしの自動実行)。
    """
    assert "operation-trigger" in bd.SYSTEM_PROVENANCE_CHANNELS


def test_a_system_minted_envelope_is_honored():
    """層そのものは機能している (= 署名が在れば通る)。"""
    event = dict(FIRED_OPERATION_EVENT,
                 envelope={"tier": bd.T1_SYSTEM_TIER,
                           "issuer": bd.T1_SYSTEM_ISSUER})
    delivery, _from, _reason = bd.classify_auto_execute(
        event, allowlist=ALLOWLIST)
    assert delivery == bd.AUTO_EXECUTE


# --- 発行者の棚卸し --------------------------------------------------------

# Trek を所管するファイル。ここ以外に発行者が生えたら下のテストが赤くなる。
TREK_OWNED = {
    pathlib.Path("server/routers_treks.py"),
    pathlib.Path("server/app.py"),            # _run_trek_tick / _post_trek_notify_*
    pathlib.Path("server/routers_projects.py"),  # Trek が active でなければ 400 の発行口
}
ISSUER_FN = "issue_t1_system_envelope"


def _call_sites(path: pathlib.Path):
    """``path`` 内で ``ISSUER_FN`` を呼んでいる行番号と、属する関数名。"""
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name != ISSUER_FN:
            continue
        owner = max((f for f in funcs
                     if f.lineno <= node.lineno <= (f.end_lineno or 0)),
                    key=lambda f: f.lineno, default=None)
        out.append((node.lineno, owner.name if owner else "(module)"))
    return out


def test_every_issuer_lives_in_a_trek_path():
    """発行者はすべて Trek 経路にある (= Trek を消せば発行者が 0 になる)。

    これは「発行者が居なくなること」を**意図として固定する**テストである。
    Trek 以外の発行者が生えたなら、それは本番の自律の姿勢を変える変更なので、
    判断記録を伴うべきで、ここが赤くなって気付く形にしておく。
    """
    stray = []
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if rel.parts[0] in ("tests", ".trash", ".archive", ".worktrees"):
            continue
        if not any(part == "server" or part == "lib" for part in rel.parts):
            continue
        try:
            sites = _call_sites(rel)
        except (SyntaxError, FileNotFoundError):
            continue
        if sites and rel not in TREK_OWNED:
            stray.append((str(rel), sites))
    assert not stray, (
        f"Trek 経路の外に T1-system の発行者がある: {stray}. "
        f"Operation 等が発行者になるのは本番の自律の姿勢を変える判断なので、"
        f"判断記録を添えてこの一覧を更新すること (ms-185 e-7078)")


# --- 案内文が真因を指すか ------------------------------------------------

def _hook_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_inbox_hook", REPO / "bin" / "beacon-bus-inbox-hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_advice_does_not_call_a_designed_downgrade_a_spoofing_risk():
    """Operation の発火に「偽装の恐れ」と案内しない (ms-185 e-7078)。

    ``operation-trigger`` は**設計上ずっと T1-system を持たない**ので、Operation の
    発火は毎回この降格に落ちる。それを「偽装の恐れ」「opt-in し直しても直らない」
    と案内すると、operator は自分の Operation に対してセキュリティ事故を探し始める
    — 真因と逆の調査に入る (#809 で実際に踏んだ型)。
    """
    hook = _hook_module()
    msg = hook._downgrade_advice(bd.DOWNGRADE_NON_SYSTEM_ENVELOPE,
                                 bd.OPERATION_TRIGGER_CHANNEL)
    assert "偽装" not in msg, "設計どおりの降格を偽装の恐れと案内している"
    assert "設計上" in msg
    assert "beacon-operation-execute" in msg


def test_a_real_missing_signature_still_warns_about_spoofing():
    """本当に署名が欠けている降格では、警告を弱めない (= 逆側に壊していない)。"""
    hook = _hook_module()
    msg = hook._downgrade_advice(bd.DOWNGRADE_NON_SYSTEM_ENVELOPE, "trek-trigger")
    assert "偽装" in msg


@pytest.mark.parametrize("path", sorted(TREK_OWNED), ids=lambda p: p.name)
def test_the_issuer_census_is_not_stale(path):
    """一覧に挙げたファイルが実際に発行者を持っている。

    e-7070 で Trek を消したあとは呼び出し元が消えるので、このテストは
    **そのとき赤くなる** — それが「発行者 0 になった」の合図であり、
    上のドキュメント (bus_delivery / envelope の注記) と一覧をそこで畳む。
    """
    assert _call_sites(path), (
        f"{path} に T1-system の発行者が無い。Trek を消し終えたなら、"
        f"TREK_OWNED とこのテストを畳み、『発行者 0 で層だけ残す』状態を "
        f"ドキュメントどおりに固定し直すこと")
