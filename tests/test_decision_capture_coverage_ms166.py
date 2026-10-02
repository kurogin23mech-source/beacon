"""Decision-capture coverage checker (ms-166 e-5974).

An axis ORTHOGONAL to the ms-163 completion-seam checks: those police whether every
terminable target-CLASS reaches the 完遂 decision producer; THIS one polices whether every
judgment-SEAM decision KIND (task-done / review-adjudication / completion-verdict / halt /
dm-send / pr-intent 導出 …) actually has a producer that is WIRED. A kind declared in the
SSOT vocabulary but produced by nothing = the "配線はあるが silent に produce しない"
non-function ms-166 targets (the旗艦 decision arm's read-window mismatch e-5970 was the same
family; this is the mechanical detector that stops it regressing).

Load-bearing (AC7 — the checker DETECTS an unwired kind before a fix and is clean after):
  - ``test_decision_capture_clean_on_real_code`` proves every judgment-seam kind's producer
    is wired today (green = "clean", not "checker asleep").
  - ``test_decision_capture_detects_unwired_kind_synthetically`` proves a kind whose producer
    token is invoked/registered NOWHERE is surfaced as a ``new_violation`` (deterministic
    synthetic token, so the detection proof does not depend on real code being broken).
  - ``test_decision_capture_covers_known_kinds`` forces every ``KNOWN_DECISION_KIND`` to be
    either produced (in DECISION_CAPTURE_PRODUCERS) or explicitly seam-less (in
    DECISION_CAPTURE_BOUNDARY), so a new kind cannot enter the vocabulary silently.
  - ``test_no_stale_decision_capture_gap`` forces an allowlist row's deletion once wired (the
    ratchet cannot rot into a lie — same discipline as KNOWN_COMPLETION_SEAM_GAP).
"""
from __future__ import annotations

import ast
import glob
import importlib.util
import os
import textwrap

# sys.path (lib / scripts / tests) is centralized in tests/conftest.py (ms-142 e-5144).
import capability_ledger as cl  # noqa: E402

# decision_event lives under server/ — add it so we can pin the SSOT vocabulary agreement.
import sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import decision_event as de  # noqa: E402

_REPO = os.path.join(os.path.dirname(__file__), "..")
_LIB_DIR = os.path.join(_REPO, "lib")
_SERVER_DIR = os.path.join(_REPO, "server")
_CHK_PATH = os.path.join(_REPO, "scripts", "check-capability-scope.py")
_spec = importlib.util.spec_from_file_location("check_capability_scope", _CHK_PATH)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


def _all_defined_functions_broad() -> set:
    """Every function name defined across lib/*.py + server/*.py (broader than the
    completion-scan population, so a producer defined in a leaf module like
    decision_derive.py is still found)."""
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


# --- registry honesty (a rename cannot rot the coverage table into a false pass) ---

def test_decision_capture_producers_resolve_to_real_functions():
    defined = _all_defined_functions_broad()
    declared = set()
    for producers in cl.DECISION_CAPTURE_PRODUCERS.values():
        declared |= set(producers)
    missing = sorted(p for p in declared if p not in defined)
    assert not missing, (
        "DECISION_CAPTURE_PRODUCERS names a function that does not exist "
        f"(rename → registry rot): {missing}")


def test_decision_capture_covers_known_kinds():
    # SSOT agreement (like test_ratchet_family_tables_agree): every KNOWN_DECISION_KIND is
    # either produced (has a DECISION_CAPTURE_PRODUCERS row) or explicitly declared seam-less
    # (DECISION_CAPTURE_BOUNDARY). So a kind added to the vocabulary without wiring a producer
    # — or declaring it conversational-only — fails HERE with a clear message.
    covered = set(cl.DECISION_CAPTURE_PRODUCERS) | set(cl.DECISION_CAPTURE_BOUNDARY)
    uncovered = sorted(k for k in de.KNOWN_DECISION_KINDS if k not in covered)
    assert not uncovered, (
        "KNOWN_DECISION_KIND(s) with neither a wired producer nor a boundary declaration "
        f"(wire a producer in DECISION_CAPTURE_PRODUCERS, or declare seam-less in "
        f"DECISION_CAPTURE_BOUNDARY): {uncovered}")


def test_decision_capture_no_orphan_producers():
    # Inverse SSOT guard (maintainability review PR#724): every DECISION_CAPTURE_PRODUCERS key
    # must be a KNOWN_DECISION_KIND or a declared derived kind (DECISION_CAPTURE_DERIVED_KINDS).
    # So a kind removed/renamed in the vocabulary cannot leave a dead producer row wired behind
    # (the row would keep the checker green while the kind is gone = silent divergence).
    allowed = set(de.KNOWN_DECISION_KINDS) | set(cl.DECISION_CAPTURE_DERIVED_KINDS)
    orphans = sorted(k for k in cl.DECISION_CAPTURE_PRODUCERS if k not in allowed)
    assert not orphans, (
        "DECISION_CAPTURE_PRODUCERS key that is neither a KNOWN_DECISION_KIND nor a declared "
        f"derived kind (a vocabulary kind was removed/renamed — drop or reclassify the row): "
        f"{orphans}")


def test_decision_capture_derived_kinds_are_outside_vocabulary():
    # A derived kind is by definition NOT in the vocabulary; if one gets promoted INTO
    # KNOWN_DECISION_KINDS, it should be dropped from the derived set (else the exception rots).
    leaked = sorted(k for k in cl.DECISION_CAPTURE_DERIVED_KINDS
                    if k in de.KNOWN_DECISION_KINDS)
    assert not leaked, (
        "DECISION_CAPTURE_DERIVED_KINDS lists a kind now IN KNOWN_DECISION_KINDS "
        f"(it is no longer a derived exception — drop the row): {leaked}")


def test_decision_capture_producers_and_boundary_disjoint():
    # A kind is either seam-full (has a producer) or seam-less (boundary) — never both.
    both = sorted(set(cl.DECISION_CAPTURE_PRODUCERS) & set(cl.DECISION_CAPTURE_BOUNDARY))
    assert not both, f"kind declared BOTH produced and seam-less: {both}"


# --- clean today (green = clean, not asleep) ---

def test_decision_capture_clean_on_real_code():
    # every judgment-seam kind's producer is wired (calls for builders, dispatch-registration
    # for the CLI verb handler) → ZERO gaps on real code.
    assert chk.find_decision_capture_gaps() == []
    result = chk.run()
    assert result["new_decision_capture"] == []
    assert result["pending_decision_capture"] == []


# --- detection proof (synthetic, so it does not depend on real code being broken) ---

def test_decision_capture_detects_unwired_kind_synthetically(monkeypatch):
    # Add a phantom kind whose producer token is invoked/registered NOWHERE. The checker MUST
    # surface exactly it as a new_violation (and keep the real kinds clean). Deterministic —
    # a green real run therefore means "wired", not "checker asleep".
    monkeypatch.setattr(cl, "DECISION_CAPTURE_PRODUCERS",
                        {**cl.DECISION_CAPTURE_PRODUCERS,
                         "phantom-seam": frozenset({"produce_phantom_decision_nonexistent"})})
    gaps = chk.find_decision_capture_gaps()
    assert [g["kind"] for g in gaps] == ["phantom-seam"]
    assert gaps[0]["status"] == "new_violation"
    # run() must fail overall on a fresh unwired kind (it gates ok=).
    assert chk.run()["ok"] is False


def test_fresh_decision_capture_gap_is_new_not_pending(monkeypatch):
    # A gap NOT in the ratchet allowlist is a new_violation (fails CI), not silently accepted.
    monkeypatch.setattr(cl, "KNOWN_DECISION_CAPTURE_GAP", frozenset())
    monkeypatch.setattr(cl, "DECISION_CAPTURE_PRODUCERS",
                        {**cl.DECISION_CAPTURE_PRODUCERS,
                         "unlisted-seam": frozenset({"produce_unlisted_nonexistent"})})
    gaps = {g["kind"]: g["status"] for g in chk.find_decision_capture_gaps()}
    assert gaps.get("unlisted-seam") == "new_violation"


def test_allowlisted_gap_is_pending_not_failing(monkeypatch):
    # Symmetric: an unwired kind that IS allowlisted is reported as pending_debt (does not fail
    # ok=), so the ratchet mechanism itself works when a real temporary gap is accepted.
    monkeypatch.setattr(cl, "DECISION_CAPTURE_PRODUCERS",
                        {**cl.DECISION_CAPTURE_PRODUCERS,
                         "temp-seam": frozenset({"produce_temp_nonexistent"})})
    monkeypatch.setattr(cl, "KNOWN_DECISION_CAPTURE_GAP", frozenset({"temp-seam"}))
    gaps = {g["kind"]: g["status"] for g in chk.find_decision_capture_gaps()}
    assert gaps.get("temp-seam") == "pending_debt"
    result = chk.run()
    assert any(g["kind"] == "temp-seam" for g in result["pending_decision_capture"])
    assert all(g["kind"] != "temp-seam" for g in result["new_decision_capture"])


def test_no_stale_decision_capture_gap():
    # The ratchet cannot lie: every allowlisted kind must ACTUALLY be an unwired gap on real
    # code (a kind whose producer got wired must be DROPPED from the allowlist). Empty today,
    # so this holds vacuously — but it fails the moment a stale row outlives its fix.
    real_gap_kinds = {g["kind"] for g in chk.find_decision_capture_gaps()}
    stale = sorted(k for k in cl.KNOWN_DECISION_CAPTURE_GAP if k not in real_gap_kinds)
    assert not stale, (
        "KNOWN_DECISION_CAPTURE_GAP lists a kind that is no longer an unwired gap "
        f"(producer got wired — drop the row): {stale}")


# ---------------------------------------------------------------------------
# 本番に現れる全種別が台帳に載っているか (ms-166 e-6756)
# ---------------------------------------------------------------------------
#
# 上の 2 方向 SSOT guard は「語彙 (KNOWN_DECISION_KINDS) と台帳」の一致を見るが、
# **どちらにも載らずに書かれている kind** は両方を素通りする。実際 disposition は
# e-5651 から達成ゲートが書いていたのに、語彙にも台帳にも無く、本番 1000 件中 86 件
# あるのに「配線が外れても checker が緑」の状態だった。
#
# ここでは真値源をコード側に取り、**decision 書き込み地点の kind リテラルを機械で
# 列挙**して台帳と突き合わせる。本番データのサンプリングではなくコード由来にするのは、
# テストが本番を読みに行かないため (= ms-166 が別途塞いでいる汚染経路を増やさない) と、
# 新しい writer を足した瞬間に手元で赤くなるため。
#
# 射程の限界 (正直に書く — ここを曖昧にすると偽の安全になる):
# kind が実行時に決まる経路はリテラルが無いので列挙できない。該当するのは
#   * ``beacon decision record`` の ``BEACON_DECISION_KIND`` (任意文字列)
#   * ``decision_event_from_halt`` の resumed フラグによる halt / resume の切替
# 前者が triage の出所で、DECISION_CAPTURE_ADHOC_KINDS に宣言して射程外であることを
# 明示する。後者は両 kind が既に台帳に在る。

_DECISION_WRITERS = frozenset({"record_decision", "build_decision_event",
                               "append_decision_event"})


def _callee_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")


def _kind_literal_of_dict(d: ast.Dict):
    for k, v in zip(d.keys, d.values):
        if (isinstance(k, ast.Constant) and k.value == "kind"
                and isinstance(v, ast.Constant) and isinstance(v.value, str)):
            return v.value
    return None


def _dict_assigned_to(scope: ast.AST, name: str):
    """同一スコープで ``name`` に代入された辞書リテラル (``payload = {...}`` の解決)。"""
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Name) and t.id == name
                        and isinstance(node.value, ast.Dict)):
                    return node.value
    return None


def _kinds_written_in(path: str) -> dict:
    """``path`` の decision 書き込み地点で使われている kind リテラルを ``{kind: {site}}`` で返す。

    走査するのは **書き込み呼び出しの引数だけ**。関数まるごとを走査すると、巨大な
    router factory に同居する無関係な ``"kind"`` (bus event / target kind 等) を
    7 件拾って偽陽性になることを実測したため。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    found: dict = {}
    base = os.path.basename(path)
    scopes = [n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module))]
    for scope in scopes:
        label = getattr(scope, "name", "<module>")
        for call in [n for n in ast.walk(scope) if isinstance(n, ast.Call)]:
            if _callee_name(call) not in _DECISION_WRITERS:
                continue
            seen = []
            payloads = list(call.args) + [kw.value for kw in call.keywords
                                          if kw.arg in ("data", "payload", "decision")]
            for a in payloads:
                if isinstance(a, ast.Dict):
                    seen.append(_kind_literal_of_dict(a))
                elif isinstance(a, ast.Name):
                    d = _dict_assigned_to(scope, a.id)
                    if d is not None:
                        seen.append(_kind_literal_of_dict(d))
            for kw in call.keywords:
                if (kw.arg == "kind" and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)):
                    seen.append(kw.value.value)
            for k in seen:
                if k:
                    found.setdefault(k, set()).add(f"{base}:{label}")
    return found


def _kind_constants_in(path: str) -> dict:
    """decision モジュールの module-level ``*_KIND = "..."`` 定数。

    対象を decision モジュールに限るのは、``*_KIND`` を全ファイルで走査すると bus
    channel や target kind の定数 (``ROOT_TARGET_KIND`` / ``WELCOME_TICK_KIND`` 等) を
    拾って偽陽性になるため。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    found: dict = {}
    base = os.path.basename(path)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (isinstance(t, ast.Name) and t.id.endswith("_KIND")
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                found.setdefault(node.value.value, set()).add(f"{base}:{t.id}")
    return found


def _declared_kinds() -> set:
    return (set(de.KNOWN_DECISION_KINDS)
            | set(cl.DECISION_CAPTURE_PRODUCERS)
            | set(cl.DECISION_CAPTURE_BOUNDARY)
            | set(cl.DECISION_CAPTURE_DERIVED_KINDS)
            | set(cl.DECISION_CAPTURE_ADHOC_KINDS))


def kinds_written_in_code() -> dict:
    """lib/ + server/ の decision 書き込みで使われる kind リテラルを全列挙する。"""
    out: dict = {}
    for root in (_LIB_DIR, _SERVER_DIR):
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            src = open(path, encoding="utf-8").read()
            if any(w in src for w in _DECISION_WRITERS):
                for k, sites in _kinds_written_in(path).items():
                    out.setdefault(k, set()).update(sites)
            if name.startswith("decision_") or name == "decision_event.py":
                for k, sites in _kind_constants_in(path).items():
                    out.setdefault(k, set()).update(sites)
    return out


def test_every_kind_written_in_code_is_declared_in_the_ledger():
    declared = _declared_kinds()
    written = kinds_written_in_code()
    undeclared = {k: sorted(v) for k, v in written.items() if k not in declared}
    assert not undeclared, (
        "decision を書いているのに語彙にも台帳にも載っていない kind があります "
        f"(ms-166 e-6756 — 被覆検査が素通りし、配線が外れても気づけない): {undeclared}。"
        "専用 seam があるなら KNOWN_DECISION_KINDS に文書化して "
        "DECISION_CAPTURE_PRODUCERS に producer を足す。seam に届かない会話判断なら "
        "DECISION_CAPTURE_BOUNDARY、既存成果物からの導出なら "
        "DECISION_CAPTURE_DERIVED_KINDS、実行時に名付けられるなら "
        "DECISION_CAPTURE_ADHOC_KINDS に理由付きで宣言してください。")


def test_the_written_kind_extractor_actually_finds_a_new_kind(tmp_path):
    # test-the-test: 台帳に無い kind を書く writer を合成して、抽出器が拾うことを確かめる。
    # 「0 件だから緑」なのか「抽出器が何も見ていないから緑」なのかを区別する。
    src = textwrap.dedent('''
        def _record_something():
            client.record_decision(project_id, {
                "kind": "brand-new-judgement",
                "decision": "x",
            })
    ''')
    p = tmp_path / "cmd_new.py"
    p.write_text(src, encoding="utf-8")
    found = _kinds_written_in(str(p))
    assert "brand-new-judgement" in found, f"抽出器が新しい kind を見逃しました: {found}"
    assert "brand-new-judgement" not in _declared_kinds()


def test_the_written_kind_extractor_resolves_a_payload_variable(tmp_path):
    # payload を変数に組んでから渡す形 (cmd_task.py の実際の形) も拾えること。
    src = textwrap.dedent('''
        def _record_something():
            payload = {"kind": "via-variable", "decision": "x"}
            client.record_decision(project_id, payload)
    ''')
    p = tmp_path / "cmd_var.py"
    p.write_text(src, encoding="utf-8")
    assert "via-variable" in _kinds_written_in(str(p))


def test_the_written_kind_extractor_ignores_unrelated_kind_keys(tmp_path):
    # 偽陽性側も測る: decision 書き込みと無関係な "kind" を同じ関数に置いても拾わない。
    # (巨大な router factory で実際に 7 件拾った病理。厳しすぎる guard は無視される。)
    src = textwrap.dedent('''
        def make_router():
            bus.publish({"kind": "trek-progress-check", "to": "x"})
            arms = {"kind": "root"}
            client.record_decision(project_id, {"kind": "task-done"})
            return arms
    ''')
    p = tmp_path / "routers_x.py"
    p.write_text(src, encoding="utf-8")
    found = _kinds_written_in(str(p))
    assert set(found) == {"task-done"}, f"無関係な kind を拾っています: {sorted(found)}"


def test_extractor_sees_the_real_tree_and_is_not_vacuous():
    # 実コードから複数 kind が実際に取れていること (= 走査対象の解決に失敗して
    # 空集合を返し、上の guard が無条件に緑になる形を防ぐ)。
    written = kinds_written_in_code()
    assert len(written) >= 6, f"実コードからの抽出が少なすぎます: {sorted(written)}"
    for expected in ("task-done", "disposition", "review-adjudication", "pr-intent"):
        assert expected in written, f"{expected} を抽出できていません: {sorted(written)}"


# --- 実行時生成 kind の宣言セットが腐らないようにする -----------------------

def test_decision_capture_adhoc_kinds_have_no_producer():
    # 専用 seam を後から作ったなら ADHOC からは外す (二重登録は射程の嘘になる)。
    both = sorted(set(cl.DECISION_CAPTURE_ADHOC_KINDS) & set(cl.DECISION_CAPTURE_PRODUCERS))
    assert not both, (
        "DECISION_CAPTURE_ADHOC_KINDS と DECISION_CAPTURE_PRODUCERS に同じ kind が "
        f"居ます (seam ができたなら ADHOC から外してください): {both}")


def test_decision_capture_adhoc_kinds_are_outside_vocabulary():
    # 語彙に昇格したなら ADHOC の例外は不要になる (pr-intent の derived guard と同じ規律)。
    leaked = sorted(k for k in cl.DECISION_CAPTURE_ADHOC_KINDS
                    if k in de.KNOWN_DECISION_KINDS)
    assert not leaked, (
        "DECISION_CAPTURE_ADHOC_KINDS の kind が KNOWN_DECISION_KINDS に入りました "
        f"(専用 seam を作ったなら producer 行へ移してください): {leaked}")


def test_adhoc_kinds_declare_their_writer_and_provenance():
    # 「載せない理由が台帳に明記されている」を機械で確かめる (e-6756 の受入条件)。
    # 宣言セットは在るのに、なぜ producer を持てないのかが書かれていなければ、
    # 次の読み手は「書き忘れ」と区別できない。
    src = open(os.path.join(_LIB_DIR, "capability_ledger.py"), encoding="utf-8").read()
    anchor = src.index("DECISION_CAPTURE_ADHOC_KINDS")
    block = src[max(0, anchor - 1800):anchor]
    assert "cmd_decision_record" in block, (
        "ADHOC セットの直前に書き手 (cmd_decision_record) が明記されていません")
    assert "BEACON_DECISION_KIND" in block, (
        "ADHOC セットの直前に、kind が実行時に名付けられる経路が明記されていません")
    for kind in cl.DECISION_CAPTURE_ADHOC_KINDS:
        assert kind in block, f"ADHOC の {kind} に由来の注記がありません"
