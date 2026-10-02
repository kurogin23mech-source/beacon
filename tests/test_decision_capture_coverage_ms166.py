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
# 射程 — 何を真値源とし、何を見ていないか (曖昧にすると偽の安全になる):
#
# 判断記録の payload を **形** で見分ける。``kind`` と ``decision`` を両方持ち、かつ
# ``rationale`` / ``decided_by`` / ``related`` のいずれかを持つ辞書リテラルが判断記録の
# payload。これは命名規約ではなく構造の判別軸で、無関係な辞書 (bus event / target の
# arm マッピング / claim の応答) は後者の目印を持たないので構造的に外れる。
#
# この形にしたのは、独立 AX レビュー (PR #783) が **書き込み関数の呼び出し地点だけを
# 見る** 初版の穴を突いたため。初版は ``record_decision`` 等の呼び出し引数と、
# ファイル名が ``decision_`` で始まるモジュールの ``*_KIND`` 定数しか見ていなかった。
# ところが本番で使われている gate-judgement の実際の書き込み地点は
# ``lib/sales_entities.py`` が ``decision_outbox.stage(payload, ...)`` に積む形で、
# 3 つの書き込み関数を一つも呼ばない。拾えていたのは定数が偶然
# ``decision_outbox.py`` (= ``decision_`` 接頭辞) に在ったからで、**ガードの覆域が
# 実際の書き込み経路ではなくファイル名の慣習に依っていた**。新しい target class が
# 同じ形で新種別を積めば、ガードは緑のまま未宣言の種別が出荷される —
# e-6756 が構造的に不可能にしたはずの退行そのもの。
#
# kind の値はリテラルだけでなく **定数参照も解く** (同一モジュールの ``NAME`` と
# ``<module>.NAME`` の両形)。これで gate-judgement は実際の積み地点で、pr-intent は
# 書き込みを呼ばない payload 生成関数で、それぞれ名前の慣習に頼らず見つかる。
#
# 見ていないもの: kind が **実行時に決まる** 地点 (環境変数 / HTTP body / 条件式)。
# リテラルにも定数にも解けないので列挙できない。これを黙って飛ばすと射程の嘘に
# なるので、``test_unresolved_kind_sites_are_all_declared`` が「解けない地点は宣言済の
# ものだけ」を機械で固定する (新しい実行時命名経路が無言で増えない)。

# 判断記録の payload を見分ける形 (命名でなく構造)。
_PAYLOAD_REQUIRED_KEYS = frozenset({"kind", "decision"})
_PAYLOAD_MARKER_KEYS = frozenset({"rationale", "decided_by", "related"})
# kind を kwarg で受ける builder (payload を組み立てて返す唯一の収束口)。
_KIND_KWARG_CALLEES = frozenset({"build_decision_event"})

# kind が実行時に決まるため解けない書き込み地点。(ファイル名 → なぜ解けないか) で宣言する。
# 新しい実行時命名経路が増えたらここに足す — 足さないと下の guard が落ちる。
_RUNTIME_NAMED_KIND_SITES = {
    "cmd_decision.py": "BEACON_DECISION_KIND が任意の文字列を kind にする汎用の記録口",
    "decision_event.py": "halt / resume は resumed フラグで kind を切り替える + builder 内部",
    "routers_projects.py": "HTTP の body が運ぶ kind をそのまま通す汎用 route",
    "root_target.py": "arm マッピングの kind (判断記録ではない) が添字で決まる",
}


def _module_level_str_const(path: str, name: str):
    """``path`` の module-level ``name = "..."`` を文字列で返す (無ければ None)。"""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (isinstance(t, ast.Name) and t.id == name
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                return node.value.value
    return None


def _resolve_kind_value(value: ast.AST, path: str):
    """kind の値を ``(文字列 or None, どう解いたか)`` で返す。

    リテラル / 同一モジュールの定数 / ``<module>.定数`` の 3 形を解く。解けない形
    (= 実行時に決まる) は ``None`` を返し、呼び出し側が宣言済かを照合する。
    """
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value, "literal"
    if isinstance(value, ast.Name):
        got = _module_level_str_const(path, value.id)
        return (got, "const " + value.id) if got else (None, "name " + value.id)
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        for root in (_LIB_DIR, _SERVER_DIR, os.path.dirname(path)):
            candidate = os.path.join(root, value.value.id + ".py")
            if os.path.exists(candidate):
                got = _module_level_str_const(candidate, value.attr)
                if got:
                    return got, "const " + value.value.id + "." + value.attr
        return None, "attribute " + value.value.id + "." + value.attr
    return None, type(value).__name__.lower()


def _literal_keys(d: ast.Dict) -> set:
    return {k.value for k in d.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _is_decision_payload(d: ast.Dict) -> bool:
    keys = _literal_keys(d)
    return bool(_PAYLOAD_REQUIRED_KEYS <= keys and (_PAYLOAD_MARKER_KEYS & keys))


def kind_sites_in(path: str):
    """1 ファイルの判断記録 kind を ``({kind: {site}}, [解けなかった site])`` で返す。"""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return {}, []
    resolved: dict = {}
    unresolved: list = []
    base = os.path.basename(path)
    for d in [n for n in ast.walk(tree) if isinstance(n, ast.Dict)]:
        if not _is_decision_payload(d):
            continue
        for k, v in zip(d.keys, d.values):
            if not (isinstance(k, ast.Constant) and k.value == "kind"):
                continue
            kind, how = _resolve_kind_value(v, path)
            if kind:
                resolved.setdefault(kind, set()).add(
                    base + ":" + str(d.lineno) + " (" + how + ")")
            else:
                unresolved.append((base, d.lineno, how))
    for call in [n for n in ast.walk(tree) if isinstance(n, ast.Call)]:
        f = call.func
        callee = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        if callee not in _KIND_KWARG_CALLEES:
            continue
        for kw in call.keywords:
            if kw.arg != "kind":
                continue
            kind, how = _resolve_kind_value(kw.value, path)
            if kind:
                resolved.setdefault(kind, set()).add(
                    base + ":" + str(call.lineno) + " (kwarg " + how + ")")
            else:
                unresolved.append((base, call.lineno, "kwarg " + how))
    return resolved, unresolved


def _declared_kinds() -> set:
    return (set(de.KNOWN_DECISION_KINDS)
            | set(cl.DECISION_CAPTURE_PRODUCERS)
            | set(cl.DECISION_CAPTURE_BOUNDARY)
            | set(cl.DECISION_CAPTURE_DERIVED_KINDS)
            | set(cl.DECISION_CAPTURE_ADHOC_KINDS))


def _scan_all():
    resolved: dict = {}
    unresolved: list = []
    for root in (_LIB_DIR, _SERVER_DIR):
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            r, u = kind_sites_in(os.path.join(root, name))
            for k, sites in r.items():
                resolved.setdefault(k, set()).update(sites)
            unresolved += u
    return resolved, unresolved


def kinds_written_in_code() -> dict:
    """lib/ + server/ の判断記録 payload が運ぶ kind を ``{kind: {site}}`` で全列挙する。"""
    return _scan_all()[0]


def test_every_kind_written_in_code_is_declared_in_the_ledger():
    declared = _declared_kinds()
    written = kinds_written_in_code()
    undeclared = {k: sorted(v) for k, v in written.items() if k not in declared}
    assert not undeclared, (
        "判断記録を書いているのに語彙にも台帳にも載っていない kind があります "
        "(ms-166 e-6756 — 被覆検査が素通りし、配線が外れても気づけない): "
        + repr(undeclared) + "。"
        "専用 seam があるなら KNOWN_DECISION_KINDS に文書化して "
        "DECISION_CAPTURE_PRODUCERS に producer を足す。seam に届かない会話判断なら "
        "DECISION_CAPTURE_BOUNDARY、既存成果物からの導出なら "
        "DECISION_CAPTURE_DERIVED_KINDS、実行時に名付けられるなら "
        "DECISION_CAPTURE_ADHOC_KINDS に理由付きで宣言してください。")


def test_unresolved_kind_sites_are_all_declared():
    """解けなかった書き込み地点は、宣言済の実行時命名経路だけであること。

    これが無いと「解けないものは黙って飛ばす」= 射程の嘘になる。新しい実行時命名
    経路が増えたら、ここで落ちて宣言を促す。
    """
    _, unresolved = _scan_all()
    undeclared = sorted({(f, line, how) for f, line, how in unresolved
                         if f not in _RUNTIME_NAMED_KIND_SITES})
    assert not undeclared, (
        "kind を解けない判断記録の書き込み地点が宣言されていません "
        "(実行時に kind が決まるなら _RUNTIME_NAMED_KIND_SITES に理由付きで "
        "足してください): " + repr(undeclared))


# --- test-the-test: 捕まえるべき形ごとに赤くなるか / 正しい形は緑か -----------

def test_extractor_finds_the_outbox_staging_site_not_a_filename_convention():
    """独立 AX レビュー (PR #783) が突いた穴の回帰ピン。

    gate-judgement の実際の書き込み地点は ``lib/sales_entities.py`` が
    ``decision_outbox.stage`` に積む形で、書き込み関数を一つも呼ばない。初版の
    抽出器はこの地点を見ておらず、定数が偶然 ``decision_`` 接頭辞のファイルに在った
    から拾えていた。実際の積み地点で見つかることを固定する。
    """
    sites = kinds_written_in_code().get("gate-judgement") or set()
    assert any("sales_entities.py" in s for s in sites), (
        "gate-judgement を実際の書き込み地点で拾えていません: " + repr(sorted(sites)))
    assert any("decision_outbox.GATE_JUDGEMENT_KIND" in s for s in sites), (
        "定数参照を解けていません: " + repr(sorted(sites)))


def test_extractor_finds_a_kind_that_only_a_builder_carries():
    # pr-intent は書き込みを呼ばない payload 生成関数が運ぶ。呼び出し地点だけを見る
    # 抽出器では拾えない形。
    sites = kinds_written_in_code().get("pr-intent") or set()
    assert any("decision_derive.py" in s for s in sites), repr(sorted(sites))


def test_guard_fails_on_a_new_kind_staged_through_the_outbox(tmp_path):
    # test-the-test: 書き込み関数を一つも呼ばず outbox に積むだけの新しい経路
    # (= このレビューが指摘した拡張点そのもの) を合成して、拾うことを確かめる。
    src = (
        "import decision_outbox\n"
        "\n"
        "def stage_new_class_judgement(target, outcome, reason, actor):\n"
        "    payload = {\n"
        '        "kind": "brand-new-judgement",\n'
        '        "decision": outcome,\n'
        '        "rationale": reason or None,\n'
        '        "decided_by": actor,\n'
        '        "related": {"target_id": target},\n'
        "    }\n"
        "    return decision_outbox.stage(payload, verify=None)\n"
    )
    p = tmp_path / "new_class_entities.py"
    p.write_text(src, encoding="utf-8")
    resolved, _ = kind_sites_in(str(p))
    assert "brand-new-judgement" in resolved, (
        "新しい積み経路を見逃しました: " + repr(resolved))
    assert "brand-new-judgement" not in _declared_kinds()


def test_extractor_resolves_a_constant_reference(tmp_path):
    # 定数参照の 2 形 (同一モジュール / <module>.定数) が解けること。
    (tmp_path / "some_kinds.py").write_text(
        'OTHER_KIND = "cross-module"\n', encoding="utf-8")
    src = (
        "import some_kinds\n"
        '\nLOCAL_KIND = "same-module"\n'
        "\n"
        "def write_local(client, pid, outcome):\n"
        "    client.record_decision(pid, {\n"
        '        "kind": LOCAL_KIND,\n'
        '        "decision": outcome,\n'
        '        "decided_by": "autonomous-AI",\n'
        "    })\n"
        "\n"
        "def write_cross(client, pid, outcome):\n"
        "    client.record_decision(pid, {\n"
        '        "kind": some_kinds.OTHER_KIND,\n'
        '        "decision": outcome,\n'
        '        "decided_by": "autonomous-AI",\n'
        "    })\n"
    )
    p = tmp_path / "cmd_y.py"
    p.write_text(src, encoding="utf-8")
    resolved, unresolved = kind_sites_in(str(p))
    assert "same-module" in resolved, repr(resolved)
    assert "cross-module" in resolved, (
        "<module>.定数 を解けていません: " + repr((resolved, unresolved)))


def test_extractor_ignores_dicts_that_are_not_decision_payloads(tmp_path):
    """偽陽性側を測る。``kind`` と ``decision`` を持つだけの辞書は判断記録ではない。

    実在の 2 例を縮めたもの: target の arm マッピング (``decision`` が辞書) と
    claim の応答 (``reason`` / ``from_session_id`` を使う)。厳しすぎるガードは
    無視されるので、ここが赤くならないことも検査の一部。
    """
    src = (
        "def arms():\n"
        "    return {\n"
        '        "kind": "root",\n'
        '        "phase_ball": None,\n'
        '        "evidence_arms": [],\n'
        '        "decision": {"kind": "completion_approval"},\n'
        '        "deliverable": {"kind": "achievement"},\n'
        "    }\n"
        "\n"
        "def claim_response(claim_id, decision, sid, reason):\n"
        "    return {\n"
        '        "kind": "claim_response",\n'
        '        "claim_id": claim_id,\n'
        '        "decision": decision,\n'
        '        "from_session_id": sid,\n'
        '        "reason": reason,\n'
        "    }\n"
        "\n"
        "def bus_event():\n"
        '    return {"kind": "trek-progress-check", "to": "x"}\n'
    )
    p = tmp_path / "unrelated.py"
    p.write_text(src, encoding="utf-8")
    resolved, unresolved = kind_sites_in(str(p))
    assert resolved == {}, "判断記録でない辞書を拾っています: " + repr(resolved)
    assert unresolved == [], (
        "判断記録でない辞書を未解決として報告しています: " + repr(unresolved))


def test_extractor_sees_the_real_tree_and_is_not_vacuous():
    # 走査対象の解決に失敗して空集合を返し、上の guard が無条件に緑になる形を防ぐ。
    written = kinds_written_in_code()
    assert len(written) >= 8, "実コードからの抽出が少なすぎます: " + repr(sorted(written))
    for expected in ("task-done", "disposition", "review-adjudication", "pr-intent",
                     "gate-judgement", "completion-verdict"):
        assert expected in written, (
            expected + " を抽出できていません: " + repr(sorted(written)))


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
