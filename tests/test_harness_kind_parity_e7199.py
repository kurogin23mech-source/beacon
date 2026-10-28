"""端末の種類の語彙が、名乗り側 (JS) と受け付け側 (Go) で一致していること (e-7199)。

運用室の「端末へ飛ぶ」は、bridge が名乗る ``runtime.harness.kind`` を viewer が
表引きして前面化スクリプトを選ぶ。語彙を決める場所が 2 つある:

* 名乗り側: ``channel/bus-harness-kind.mjs`` の ``TERM_PROGRAM_KINDS``
  (TERM_PROGRAM → 正典名)
* 受け付け側: ``viewer/jump.go`` の ``terminalDrivers`` (正典名の別名集合)

JS は Go を import できず、Go は JS を読まない。どちらの言語の試験も自分の表だけを
見て緑になるので、片方にだけ iTerm2 が在る状態 (= 宣言は対応済み、実機では一度も
成立しない) が 1 か月以上気付かれなかった。ここが両方の表を **構造的に** 読んで
突き合わせる唯一の歯止め。

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


def _brace_block(source: str, head_pattern: str, what: str) -> str:
    """``head_pattern`` の直後に始まる ``{ ... }`` を波括弧の対応で切り出す。"""
    head = re.search(head_pattern, source)
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


def go_driver_kinds() -> set:
    """``var terminalDrivers = []terminalDriver{ ... kinds: []string{...} ... }`` を読む。"""
    src = GO_SOURCE.read_text(encoding="utf-8")
    body = _brace_block(
        src, r"var\s+terminalDrivers\s*=\s*\[\]terminalDriver\s*\{",
        "Go の受け付け表 terminalDrivers")
    kinds: set = set()
    for m in re.finditer(r"kinds:\s*\[\]string\s*\{([^}]*)\}", body):
        kinds.update(re.findall(r'"([^"]*)"', m.group(1)))
    if not kinds:
        raise AssertionError("Go の受け付け表から kinds を 1 つも読めませんでした")
    return kinds


def go_raw_prefix() -> str:
    src = GO_SOURCE.read_text(encoding="utf-8")
    m = re.search(r'const\s+rawTerminalPrefix\s*=\s*"([^"]*)"', src)
    assert m, "jump.go に rawTerminalPrefix が無い"
    return m.group(1)


# --- 構造 (静的) の突き合わせ --------------------------------------------------

def test_js_canonical_kinds_are_all_accepted_by_go():
    """名乗り側が送りうる正典名は、受け付け側の別名集合に全部含まれる。"""
    js = js_term_program_kinds()
    go = go_driver_kinds()
    missing = {k: v for k, v in js.items() if v not in go}
    assert not missing, (
        "名乗り側 (bus-harness-kind.mjs) が送る値を viewer/jump.go が受け付けません: "
        f"{missing}。両方の表を同じ変更で直してください。")


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
def test_node_output_for_each_known_terminal_lands_in_go_table():
    """表の読み取りではなく、関数を実際に走らせた出力が Go の表に入ることを見る。"""
    js = js_term_program_kinds()
    go = go_driver_kinds()
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
        assert out[term_program] in go, (
            f"TERM_PROGRAM={term_program!r} の名乗り {out[term_program]!r} を Go が受け付けない")
    # 知らない端末は接頭辞付きの生の形で、Go が剥がした残りは表に無い (= 未対応に落ちる)。
    assert out["WezTerm"] == js_raw_prefix() + "WezTerm"
    assert out["WezTerm"][len(js_raw_prefix()):].lower() not in go
    assert out[""] == "terminal" and "terminal" in go
