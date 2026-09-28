"""terminal 到達経路の AST 導出被覆 (ms-166 e-6601)。

閉じた穴: 完遂 terminal の台帳 ``COMPLETION_TERMINAL_HANDLERS`` は **手で並べた表** で、
その *列挙漏れ自体* は誰も検査していなかった。既存の完遂 seam 被覆
(``find_completion_seam_gaps``) は「台帳に載っている handler が producer を呼ぶか」を問うので、
**台帳に載っていない経路は問いの外に居る** — 決着に到達しながら完遂 (生み出した価値の記録 +
目的達成 decision) を出さずに素通りできた。実際 ``cmd_opportunity_phase``
(`beacon opportunity phase <opp> 失注` = 手動フェーズ宣言) がそれで、ms-174 の jump-bypass と
同じ「1 経路だけ塞いで構造で閉じたと誤称する」形だった。

この検査は逆から問う: **terminal 状態を書くデータ層 primitive** を呼ぶ経路をコードから導出し、
台帳に無いものを赤くする。宣言するのが handler ではなく primitive なのが要点 — handler は verb を
足すたびに増えるが terminal を書く primitive は増えないので、台帳の手入れを忘れた側が赤くなる。

Load-bearing (AC — 合成 drift で本当に赤くなるか):
  - ``test_clean_on_real_code``: 今日は 0 件 (green = clean であって checker が寝ているのではない)
  - ``test_detects_the_historical_hole``: 台帳から cmd_opportunity_phase の行を **外すと**
    その経路が new_violation として挙がる = 実際に起きたバグをこの checker が捕まえられる
  - ``test_detects_a_synthetic_unregistered_entry_point``: 架空の primitive を架空の未登録関数が
    呼ぶ形を注入すると検出する (実コードが壊れていることに依存しない決定論的な検出証明)
  - ``test_registered_handler_and_its_helpers_are_not_flagged``: 登録済 handler 本体・その中の
    closure・委譲先 helper を誤検出しない (過検出だと allowlist が増えて台帳が腐る)
"""
from __future__ import annotations

import ast
import glob
import importlib.util
import os

import pytest

# sys.path (lib / scripts / tests) は tests/conftest.py が集約 (ms-142 e-5144)。
import capability_ledger as cl  # noqa: E402

_REPO = os.path.join(os.path.dirname(__file__), "..")
_CHK_PATH = os.path.join(_REPO, "scripts", "check-capability-scope.py")
_spec = importlib.util.spec_from_file_location("check_capability_scope", _CHK_PATH)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


def _all_defined_functions() -> set:
    names: set = set()
    for pat in ("lib/*.py", "server/*.py"):
        for path in glob.glob(os.path.join(_REPO, pat)):
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(node.name)
    return names


# --- 台帳の正直さ (rename で偽 green にならない) -----------------------------------

def test_terminal_primitives_resolve_to_real_functions():
    defined = _all_defined_functions()
    declared: set = set()
    for prims in cl.COMPLETION_TERMINAL_PRIMITIVES.values():
        declared |= set(prims)
    declared |= set(cl.SHARED_SPINE_TERMINAL_PRIMITIVES)
    missing = sorted(p for p in declared if p not in defined)
    assert not missing, (
        "COMPLETION_TERMINAL_PRIMITIVES が実在しない関数を指している "
        f"(rename で台帳が腐った): {missing}")


def test_every_terminable_class_declares_terminal_primitives():
    """台帳に handler 行があるクラスは primitive 行も持つ。

    片方だけ足すと「handler は登録されているが、その周りに他の入口があっても検出できない」
    半端な状態になる (= 検査したつもりの穴)。
    """
    missing = sorted(set(cl.COMPLETION_TERMINAL_HANDLERS)
                     - set(cl.COMPLETION_TERMINAL_PRIMITIVES))
    assert not missing, (
        "COMPLETION_TERMINAL_HANDLERS にあって COMPLETION_TERMINAL_PRIMITIVES に無いクラス "
        f"(terminal を書く primitive を宣言すること): {missing}")
    assert cl.DESCRIPTOR_TERMINAL_SENTINEL in cl.COMPLETION_TERMINAL_PRIMITIVES


def test_judgement_funnel_is_not_a_completion_primitive():
    """判断族を完遂族に混ぜない (e-6599 独立 judge の語彙混同 finding)。

    商談の settle_current_gate / settle_gate は advance / retry も通る **判断** の漏斗。
    完遂 primitive に混ぜると advance (前進) が決着扱いになって壊れる。
    """
    all_prims: set = set(cl.SHARED_SPINE_TERMINAL_PRIMITIVES)
    for prims in cl.COMPLETION_TERMINAL_PRIMITIVES.values():
        all_prims |= set(prims)
    for judgement_only in ("settle_gate", "settle_current_gate",
                           "stage_gate_judgement_decision",
                           "advance_transition", "retry_transition"):
        assert judgement_only not in all_prims, (
            f"'{judgement_only}' は判断 (または前進) の seam であって完遂の terminal では"
            f"ない。完遂 primitive に混ぜると前進が決着扱いになる")


# --- 今日は clean ------------------------------------------------------------------

def test_clean_on_real_code():
    assert chk.find_unregistered_terminal_gaps() == []
    assert chk.find_unregistered_spine_terminal_gaps() == []
    result = chk.run()
    assert result["new_unregistered_terminal"] == []
    assert result["pending_unregistered_terminal"] == []


def test_no_stale_unregistered_terminal_allowlist():
    """許可リストの一方通行ラチェット: 登録したら行を消す (嘘に腐らせない)。"""
    stale = []
    for cls, fn in cl.KNOWN_UNREGISTERED_TERMINAL:
        registered = set(cl.terminal_handlers_for(cls)) | set(
            cl.SHARED_SPINE_TERMINAL_HANDLERS)
        if fn in registered:
            stale.append((cls, fn))
    assert not stale, (
        "KNOWN_UNREGISTERED_TERMINAL に、既に台帳へ登録済みの経路が残っている "
        f"(行を削除すること): {stale}")


# --- 検出証明 (合成 drift で赤くなるか) ----------------------------------------------

def test_detects_the_historical_hole(monkeypatch):
    """実際に起きたバグ: cmd_opportunity_phase が台帳に無かった状態を再現して検出を確認。

    これが赤くならないなら、この checker は当時の穴を見つけられなかったということ。
    """
    rolled_back = dict(cl.COMPLETION_TERMINAL_HANDLERS)
    rolled_back["opportunity"] = ("cmd_opportunity_judge",)
    monkeypatch.setattr(cl, "COMPLETION_TERMINAL_HANDLERS", rolled_back)

    gaps = chk.find_unregistered_terminal_gaps()
    assert [(g["class"], g["function"]) for g in gaps] == \
        [("opportunity", "cmd_opportunity_phase")]
    assert gaps[0]["status"] == "new_violation"
    assert "jump_transition" in gaps[0]["primitives"]
    assert chk.run()["ok"] is False      # CI を止める


def test_detects_a_synthetic_unregistered_entry_point(monkeypatch):
    """架空の primitive を架空のクラスに宣言 — 実コードの壊れ方に依存しない決定論的な証明。

    ``load_project`` は多数の handler が呼ぶ実在トークンなので、これを「terminal primitive」
    と偽って宣言すれば、登録済 handler 以外の呼び出し元が violation として必ず挙がる。
    """
    monkeypatch.setattr(cl, "COMPLETION_TERMINAL_PRIMITIVES",
                        {**cl.COMPLETION_TERMINAL_PRIMITIVES,
                         "phantom": frozenset({"load_project"})})
    monkeypatch.setattr(cl, "COMPLETION_TERMINAL_HANDLERS",
                        {**cl.COMPLETION_TERMINAL_HANDLERS, "phantom": ()})
    gaps = [g for g in chk.find_unregistered_terminal_gaps() if g["class"] == "phantom"]
    assert gaps, "未登録の terminal 到達経路を検出できていない (checker が寝ている)"
    assert all(g["status"] == "new_violation" for g in gaps)
    assert chk.run()["ok"] is False


def test_pending_debt_is_reported_but_does_not_fail(monkeypatch):
    """許可リストに載っている穴は債務として報告するだけで CI は止めない (ラチェット運用)。"""
    rolled_back = dict(cl.COMPLETION_TERMINAL_HANDLERS)
    rolled_back["opportunity"] = ("cmd_opportunity_judge",)
    monkeypatch.setattr(cl, "COMPLETION_TERMINAL_HANDLERS", rolled_back)
    monkeypatch.setattr(cl, "KNOWN_UNREGISTERED_TERMINAL",
                        frozenset({("opportunity", "cmd_opportunity_phase")}))
    gaps = chk.find_unregistered_terminal_gaps()
    assert [g["status"] for g in gaps] == ["pending_debt"]
    result = chk.run()
    assert result["new_unregistered_terminal"] == []
    assert len(result["pending_unregistered_terminal"]) == 1


# --- 過検出しない (allowlist を増やして台帳を腐らせない) -------------------------------

def test_detects_an_entry_point_that_reaches_a_terminal_only_transitively():
    """直接呼びでなく **helper 経由** で terminal に届く新しい入口も捕まえる。

    これが最も逃げやすい形: ``cmd_target_approve`` は ``_apply_transition`` を介して
    milestone の terminal を書く。「登録済 handler が呼ぶ helper は免除」という素朴な規則に
    すると、その helper を呼ぶ **新しい入口** が免除を相続して素通りする — 1 経路だけ塞いで
    構造で閉じたと誤称する、この MS が消している形そのもの。推移的到達で判定しているので
    捕まることを、合成サイトを注入して固定する。
    """
    index = chk._completion_scan_index()
    sites = list(index["sites"])
    # 新しい CLI 入口が helper 経由で milestone terminal に届く形
    sites.append((["cmd_phantom_entry"], {"_apply_transition"}))
    rows = chk._unregistered_terminal_rows(
        sites, index["routes"], "milestone",
        set(cl.COMPLETION_TERMINAL_PRIMITIVES["milestone"]),
        set(cl.terminal_handlers_for("milestone", gate_is_spine=True)))
    assert [r["function"] for r in rows] == ["cmd_phantom_entry"]
    assert rows[0]["status"] == "new_violation"


def test_non_entry_point_helpers_are_not_flagged_on_their_own():
    """helper 単体は violation にしない (台帳が並べるのは入口であって helper ではない)。

    helper を並べさせると台帳が実装の内部構造に追随する羽目になり、リファクタのたびに嘘に
    なる。見るべきは「その helper を通って terminal に届く入口が全部登録されているか」。
    """
    index = chk._completion_scan_index()
    sites = list(index["sites"])
    sites.append((["_phantom_helper"], {"_apply_transition"}))   # 入口ではない
    rows = chk._unregistered_terminal_rows(
        sites, index["routes"], "milestone",
        set(cl.COMPLETION_TERMINAL_PRIMITIVES["milestone"]),
        set(cl.terminal_handlers_for("milestone", gate_is_spine=True)))
    assert [r["function"] for r in rows] == []


def test_registry_only_names_recognisable_entry_points():
    """台帳が並べる名前は必ず「入口の 2 形」(cmd_* か HTTP route) に収まる。

    この検査の前提そのもの。台帳にその 2 形でない名前 (内部 helper 等) が入ると、検出側の
    入口フィルタがその周辺経路を一切見なくなり、**検査したつもりで何も見ていない** 状態に
    なる。前提が崩れた瞬間にここが赤くなるので、静かな false-green にならない。
    """
    routes = chk._completion_scan_index()["routes"]
    registered: set = set(cl.DESCRIPTOR_TERMINAL_HANDLERS) | set(
        cl.SHARED_SPINE_TERMINAL_HANDLERS)
    for names in cl.COMPLETION_TERMINAL_HANDLERS.values():
        registered |= set(names)
    odd = sorted(n for n in registered
                 if not n.startswith("cmd_") and n not in routes)
    assert not odd, (
        "完遂台帳が入口 (cmd_* / HTTP route) でない名前を並べている。検出側の入口フィルタが "
        f"その経路群を見なくなるので、フィルタ側も一緒に広げること: {odd}")


def test_route_entry_points_are_derived_not_hand_listed():
    """入口の 2 形 (cmd_* と HTTP route) はコードから導出する。"""
    routes = chk._completion_scan_index()["routes"]
    assert "done_milestone" in routes, \
        "@router.post で定義された route を入口として認識できていない"
    assert len(routes) > 50, "route 抽出が壊れている (ほぼ拾えていない)"


def test_registered_handler_and_its_helpers_are_not_flagged():
    """登録済 handler 本体 / その中の closure / 委譲先 helper を violation にしない。

    ``server`` 側の route は ``make_router`` 内のネスト関数で、さらにその中の ``def op``
    closure が実際の書き込みを行う。素朴に「最内側の関数名」で属性付けすると、何十もの route が
    共有する ``op`` という無意味な名前に集約されて幽霊の入口が挙がる。逆に「最外側の関数」へ
    畳むと ``server/routers_projects.py`` 全体が ``make_router`` 1 個に潰れる。定義サイトごとに
    入れ子の親チェーンを保持することで、どちらの誤りも避けている。
    """
    flagged = {g["function"] for g in chk.find_unregistered_terminal_gaps()}
    flagged |= {g["function"] for g in chk.find_unregistered_spine_terminal_gaps()}
    for benign in ("op", "make_router", "_apply_transition",
                   "cmd_milestone_done", "done_milestone", "cmd_target_approve"):
        assert benign not in flagged, (
            f"'{benign}' は登録済 handler 本体 / その closure / 委譲先 helper なので "
            f"violation にしてはならない (過検出は allowlist を増やして台帳を腐らせる)")


def test_definition_sites_keeps_the_enclosing_chain():
    """属性付けの土台: ネストした関数がその親チェーンを保持していること。"""
    sites = chk._completion_scan_index()["sites"]
    chains = [chain for chain, _tokens in sites]
    nested_ops = [c for c in chains if c and c[0] == "op" and len(c) > 1]
    assert nested_ops, "ネストした 'op' closure が 1 つも見つからない (走査の前提が崩れた)"
    # 親チェーンに実際の route 名が載っている (= 入口を名指しできる)
    assert any("done_milestone" in c for c in nested_ops), \
        "closure の親チェーンに route 名が含まれていない"


# --- 完遂発火の実配線 (checker が緑でも実物が動かないと意味がない) -----------------------

def test_manual_terminal_phase_fires_completion():
    """`beacon opportunity phase <opp> <決着>` が完遂 seam を DIRECT に呼ぶ。

    台帳に登録しただけでは穴は塞がらない (登録は検査対象に入れただけ)。判断 (judge) 経由の
    決着と同じ生成物が出ることを、handler 本体からの直接呼び出しとして固定する。
    """
    src = open(os.path.join(_REPO, "lib", "commands.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_opportunity_phase")
    direct = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            tok = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if tok:
                direct.add(tok)
    assert "on_target_completion" in direct, (
        "cmd_opportunity_phase が完遂 seam を直接呼んでいない — 手動宣言での決着が "
        "生み出した価値 / 目的達成 decision を出さないまま素通りする")
    assert "opportunity_phase_is_terminal" in direct, (
        "決着かどうかの判定なしに完遂を撃っている (前進の手動宣言でも発火してしまう)")


# --- 実挙動: 手動宣言での決着が判断経由と同じ生成物を出す ------------------------------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import commands  # noqa: E402
import sales_entities as se  # noqa: E402


def _project(tmp_path, monkeypatch, data: dict) -> Path:
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


def _fired(monkeypatch) -> list:
    """``on_target_completion`` の発火を捕まえる (実書き込みはせず引数だけ記録)。"""
    calls = []
    import target_completion
    monkeypatch.setattr(target_completion, "on_target_completion",
                        lambda data, target, **kw: calls.append(
                            ((target or {}).get("id"), kw.get("verdict"), kw.get("reason"))))
    return calls


def test_manual_jump_to_terminal_fires_completion_at_runtime(tmp_path, monkeypatch, capsys):
    """`beacon opportunity phase <opp> 失注` が実際に完遂 seam を撃つ。

    AST の配線確認 (test_manual_terminal_phase_fires_completion) と対で、実行しても本当に
    発火することを押さえる — 「機構を書いた」と「動く」は別なので両方要る。
    """
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", phase="提案準備", created_at="T0")
    _project(tmp_path, monkeypatch, data)
    calls = _fired(monkeypatch)

    monkeypatch.setenv("BEACON_OPP_ID", opp)
    monkeypatch.setenv("BEACON_PHASE", "失注")
    monkeypatch.setenv("BEACON_PHASE_NOTE", "予算見送り")
    commands.cmd_opportunity_phase()
    capsys.readouterr()

    assert calls == [(opp, "失注", "予算見送り")], \
        "手動宣言での決着が完遂 seam を撃っていない (= 生み出した価値も目的達成 decision も残らない)"


def test_manual_jump_to_non_terminal_does_not_fire_completion(tmp_path, monkeypatch, capsys):
    """前進や訂正の手動宣言では完遂を撃たない (決着でないものを決着扱いにしない)。"""
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", phase="提案準備", created_at="T0")
    _project(tmp_path, monkeypatch, data)
    calls = _fired(monkeypatch)

    monkeypatch.setenv("BEACON_OPP_ID", opp)
    monkeypatch.setenv("BEACON_PHASE", "先方検討中")
    monkeypatch.setenv("BEACON_PHASE_NOTE", "訂正")
    commands.cmd_opportunity_phase()
    capsys.readouterr()

    assert calls == [], "決着でない手動宣言で完遂を撃っている"


def test_terminal_check_uses_the_phase_actually_set(tmp_path, monkeypatch, capsys):
    """前後に空白のあるフェーズ名でも決着なら完遂を撃つ。

    判定を env 由来の生文字列で行うと、``jump_transition`` が内部で strip した結果とズレて
    「" 失注" で決着したのに完遂が飛ばない」静かな取りこぼしになる。実際に設定された値
    (``rec["phase"]``) を真値源にしていることを固定する。
    """
    data = se.build_sales_project("S", "obj")
    opp = se.opportunity_add(data, "Deal", phase="提案準備", created_at="T0")
    _project(tmp_path, monkeypatch, data)
    calls = _fired(monkeypatch)

    monkeypatch.setenv("BEACON_OPP_ID", opp)
    monkeypatch.setenv("BEACON_PHASE", "  失注  ")
    monkeypatch.setenv("BEACON_PHASE_NOTE", "予算見送り")
    commands.cmd_opportunity_phase()
    capsys.readouterr()

    assert calls == [(opp, "失注", "予算見送り")], \
        "空白込みのフェーズ名で決着したときに完遂が飛んでいない"
