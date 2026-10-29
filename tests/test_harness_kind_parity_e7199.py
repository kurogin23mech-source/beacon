"""端末の種類の語彙が、名乗り側 (JS) と受け付け側 (Go) で一致していること (e-7199)。

運用室の「端末へ飛ぶ」は、bridge が名乗る ``runtime.harness.kind`` を viewer が
表引きして前面化スクリプトを選ぶ。語彙を決める場所が 2 つある:

* 名乗り側: ``channel/bus-harness-kind.mjs`` の ``TERM_PROGRAM_KINDS``
  (TERM_PROGRAM → 正典名)
* 受け付け側: ``viewer/jump.go`` の ``terminalDrivers`` (driver ごとの
  kinds = 正典名と旧別名 / rawNames = 生の形で来る小文字の TERM_PROGRAM)

JS は Go を import できず、Go は JS を読まない。どちらの言語の試験も自分の表だけを
見て緑になるので、片方にだけ iTerm2 が在る状態 (= 宣言は対応済み、実機では一度も
成立しない) が 1 か月以上気付かれなかった。ここが両方の表を **構造的に** 読んで
突き合わせる唯一の歯止め。

突き合わせは **driver 単位** で行う (PR#820 独立保守性レビュー): JS の 1 行
``TERM_PROGRAM → 正典名`` は、Go の **同じ 1 つの driver** の rawNames
(= lower(TERM_PROGRAM)) と kinds (= 正典名) に対応していなければならない。全 driver を
平らにした集合で「含まれるか」だけ見ると、正典名を別の driver (= 別の端末アプリ) に
置き間違えても緑のまま通る。

**substring 一致で済ませない**: コメントや help 文に ``iterm2`` が出てくるだけで
素通りするので、表のブロックを波括弧で切り出してからリテラルを拾う。表が見つから
ない / 空は「一致」ではなく例外 (無言の 0 件一致は偽の緑そのもの)。先例は
tests/test_go_attention_state_parity.py。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
JS_MODULE = REPO / "channel" / "bus-harness-kind.mjs"
GO_SOURCE = REPO / "viewer" / "jump.go"
BUS_MJS = REPO / "channel" / "bus.mjs"


def _brace_block(source: str, head_pattern: str, what: str, start: int = 0) -> str:
    """``head_pattern`` の直後に始まる ``{ ... }`` を波括弧の対応で切り出す。"""
    head = re.compile(head_pattern).search(source, start)
    if head is None:
        raise AssertionError(
            f"{what} が見つかりません。改名したならこのガードも直してください "
            "(ガードが黙って無効になるのを防ぐため例外にしています)。")
    i = source.index("{", head.end() - 1)
    depth = 0
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[i + 1:j]
    raise AssertionError(f"{what} の波括弧が閉じていません")


def _top_level_blocks(body: str) -> list:
    """``{...}, {...}`` と並ぶ要素を、深さ 1 の波括弧対応で 1 つずつ切り出す。"""
    out, depth, start = [], 0, None
    for j, ch in enumerate(body):
        if ch == "{":
            if depth == 0:
                start = j
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(body[start + 1:j])
                start = None
    return out


def js_term_program_kinds() -> dict:
    """``export const TERM_PROGRAM_KINDS = Object.freeze({ 'k': 'v', ... })`` を読む。"""
    src = JS_MODULE.read_text(encoding="utf-8")
    body = _brace_block(
        src, r"export\s+const\s+TERM_PROGRAM_KINDS\s*=\s*Object\.freeze\s*\(\s*\{",
        "JS の正規化表 TERM_PROGRAM_KINDS")
    pairs = re.findall(r"""['"]([^'"]+)['"]\s*:\s*['"]([^'"]+)['"]""", body)
    if not pairs:
        raise AssertionError("JS の正規化表が空です (空の表は何も守っていない)")
    return dict(pairs)


def js_raw_prefix() -> str:
    src = JS_MODULE.read_text(encoding="utf-8")
    m = re.search(r"export\s+const\s+RAW_TERMINAL_PREFIX\s*=\s*'([^']*)'", src)
    assert m, "bus-harness-kind.mjs に RAW_TERMINAL_PREFIX が無い"
    return m.group(1)


def _go_string_list(block: str, field: str) -> list:
    m = re.search(r"\b%s:\s*\[\]string\s*\{([^}]*)\}" % re.escape(field), block)
    if m is None:
        return []
    return re.findall(r'"([^"]*)"', m.group(1))


def go_drivers() -> list:
    """``var terminalDrivers = []terminalDriver{ {...}, {...} }`` を driver ごとに読む。

    返り値は ``[{"appName", "kinds", "rawNames"}]``。driver が 1 つも取れない /
    appName が無い driver があるのは表の形が変わった証拠なので例外にする。
    """
    src = GO_SOURCE.read_text(encoding="utf-8")
    body = _brace_block(
        src, r"var\s+terminalDrivers\s*=\s*\[\]terminalDriver\s*\{",
        "Go の受け付け表 terminalDrivers")
    drivers = []
    for blk in _top_level_blocks(body):
        name = re.search(r'\bappName:\s*"([^"]*)"', blk)
        if name is None:
            raise AssertionError(f"appName の無い driver がある: {blk.strip()[:80]}")
        drivers.append({
            "appName": name.group(1),
            "kinds": _go_string_list(blk, "kinds"),
            "rawNames": _go_string_list(blk, "rawNames"),
        })
    if not drivers:
        raise AssertionError("Go の受け付け表から driver を 1 つも読めませんでした")
    return drivers


def go_raw_prefix() -> str:
    src = GO_SOURCE.read_text(encoding="utf-8")
    m = re.search(r'const\s+rawTerminalPrefix\s*=\s*"([^"]*)"', src)
    assert m, "jump.go に rawTerminalPrefix が無い"
    return m.group(1)


def _drivers_with(drivers: list, field: str, value: str) -> list:
    return [d["appName"] for d in drivers if value in d[field]]


# --- 構造 (静的) の突き合わせ --------------------------------------------------

def test_each_js_row_lands_in_exactly_one_go_driver():
    """JS の 1 行 (TERM_PROGRAM → 正典名) は、Go の同じ 1 つの driver の rawNames と
    kinds に対応する。平らな集合の「含まれるか」ではなく driver 単位で見る。"""
    js = js_term_program_kinds()
    drivers = go_drivers()
    problems = []
    for term_program, kind in js.items():
        by_kind = _drivers_with(drivers, "kinds", kind)
        by_raw = _drivers_with(drivers, "rawNames", term_program.lower())
        if len(by_kind) != 1:
            problems.append(f"{term_program!r} の正典名 {kind!r} が kinds に {by_kind} ({len(by_kind)} driver)")
        if len(by_raw) != 1:
            problems.append(f"{term_program!r} の生の名前 {term_program.lower()!r} が rawNames に {by_raw} ({len(by_raw)} driver)")
        if len(by_kind) == 1 and len(by_raw) == 1 and by_kind != by_raw:
            problems.append(f"{term_program!r}: 正典名は {by_kind[0]}、生の名前は {by_raw[0]} と別の driver に着く")
    assert not problems, (
        "名乗り側 (bus-harness-kind.mjs) と受け付け側 (viewer/jump.go) の対応が崩れています:\n  "
        + "\n  ".join(problems) + "\n両方の表を同じ変更で直してください。")


def test_go_kinds_and_raw_names_are_unique_across_drivers():
    """同じ別名 / 生の名前が 2 つの driver に居ると、先に並んだ方が黙って勝つ。"""
    drivers = go_drivers()
    for field in ("kinds", "rawNames"):
        seen = {}
        for d in drivers:
            for v in d[field]:
                seen.setdefault(v, []).append(d["appName"])
        dups = {v: apps for v, apps in seen.items() if len(apps) > 1}
        assert not dups, f"{field} が複数 driver で重複: {dups}"


def test_js_table_names_the_two_terminals_we_run():
    """表が空に近い形に痩せても緑にならないよう、実運用の 2 端末を名指しで要求する。"""
    js = js_term_program_kinds()
    assert "iTerm.app" in js, "iTerm2 (TERM_PROGRAM=iTerm.app) が正規化表から消えている"
    assert "Apple_Terminal" in js, "Terminal.app (TERM_PROGRAM=Apple_Terminal) が正規化表から消えている"


def test_raw_prefix_is_identical_across_languages():
    """生の形の接頭辞は、付ける側 (JS) と剥がす側 (Go) で byte 一致。"""
    assert js_raw_prefix() == go_raw_prefix(), (
        "RAW_TERMINAL_PREFIX (JS) と rawTerminalPrefix (Go) がずれています")
    assert js_raw_prefix(), "接頭辞が空だと生の形と正典名の区別が付かない"


def test_bus_mjs_delegates_and_has_no_inline_term_program_branch():
    """bus.mjs は共有モジュールへ委ね、自前の TERM_PROGRAM 分岐を持たない
    (それが iTerm2 を表から落とした経路)。"""
    src = BUS_MJS.read_text(encoding="utf-8")
    import_line = next(
        (ln for ln in src.splitlines() if "from './bus-harness-kind.mjs'" in ln), "")
    assert "harnessKindFromEnv" in import_line, (
        "bus.mjs が bus-harness-kind.mjs から harnessKindFromEnv を import していない")
    fn = _brace_block(src, r"function\s+detectHarnessKind\s*\(\s*\)\s*\{", "bus.mjs の detectHarnessKind")
    assert "harnessKindFromEnv(process.env)" in fn
    code_lines = [ln for ln in fn.splitlines() if not ln.strip().startswith("//")]
    assert not any("TERM_PROGRAM" in ln for ln in code_lines), (
        "detectHarnessKind が TERM_PROGRAM を自前で見ている (共有表を迂回)")


# --- 挙動 (node 実走) の突き合わせ ---------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node が無い環境では名乗り側を実走できない (静的突合は上で走る)")
def test_node_output_for_each_known_terminal_lands_in_its_go_driver():
    """表の読み取りではなく、関数を実際に走らせた出力が Go の正しい driver に入ることを見る。"""
    js = js_term_program_kinds()
    drivers = go_drivers()
    probes = list(js.keys()) + ["WezTerm", ""]
    script = (
        f"import {{ harnessKindFromEnv }} from {json.dumps(str(JS_MODULE))}\n"
        f"const probes = {json.dumps(probes)}\n"
        "const out = {}\n"
        "for (const p of probes) out[p] = harnessKindFromEnv(p ? { TERM_PROGRAM: p } : {})\n"
        "process.stdout.write(JSON.stringify(out))\n"
    )
    proc = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node probe failed:\n{proc.stderr}"
    out = json.loads(proc.stdout)
    for term_program in js:
        by_kind = _drivers_with(drivers, "kinds", out[term_program])
        by_raw = _drivers_with(drivers, "rawNames", term_program.lower())
        assert by_kind == by_raw and len(by_kind) == 1, (
            f"TERM_PROGRAM={term_program!r} の名乗り {out[term_program]!r} は driver {by_kind}、"
            f"生の名前は driver {by_raw} — 同じ 1 つの driver に着いていない")
    # 知らない端末は接頭辞付きの生の形で、Go が剥がした残りは rawNames に無い (= 未対応に落ちる)。
    assert out["WezTerm"] == js_raw_prefix() + "WezTerm"
    assert not _drivers_with(drivers, "rawNames", "wezterm")
    assert out[""] == "terminal" and _drivers_with(drivers, "kinds", "terminal")
