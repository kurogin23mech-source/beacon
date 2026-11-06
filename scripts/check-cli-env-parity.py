#!/usr/bin/env python3
"""Every env var a verb READS must be passed by the frontends that reach it
(ms-160 e-6674).

Beacon has two CLI frontends — ``bin/beacon`` (bash) and
``beacon_cli/dispatch.py`` (the Windows / pipx shim). Both translate argv into
environment variables and then run ``lib/commands.py <verb>``. The thing that
actually carries a flag's value is therefore the **flag → env mapping**, and
that mapping is written out by hand, twice.

``scripts/check-cli-help-drift.py`` compares flag NAMES. It cannot see this
class, and the reason is structural: *a flag missing from one frontend is not
"unequal", it is absent* — two frontends that both lack a flag look aligned,
and a frontend that lacks a flag the other has drops out of the comparison
rather than failing it. Its ``REQUIRED_FLAG_PARITY`` closes the gap for a
hand-curated handful of verbs; everything else is unchecked.

What that costs, measured: ``beacon doc add --force``, ``search --source``,
``deploy record --version``, ``cloud list --json`` and ~40 more are accepted by
bash and REJECTED by the Python shim. A Windows operator following a Skill
hits ``unrecognized arguments``. ms-160 e-6674 found the class after
``meeting reschedule`` silently dropped a calendar assignment; PR #778 found
``bus send --recipient-confirmed`` missing, which made cross-user DM
impossible from Windows — not inconvenient, impossible.

So this checker compares the MAPPING, for every verb, not a curated list:

  reads(verb)   — the BEACON_* names ``cmd_<verb>()`` pulls out of os.environ
  bash(verb)    — the BEACON_*= assignments on the path that dispatches <verb>
  python(verb)  — the BEACON_* keys the ``_handle_*`` that dispatches <verb>
                  mentions

A name a verb reads but a frontend reaching it never sets is reported.

KNOWN_GAPS carries the backlog that existed when the checker was written, so it
goes green today and every NEW drift fails. Each row carries its verdict, in
the shape ``scripts/check-pid-liveness.py``'s ALLOWLIST uses (the reason lives
with the entry, not in a ticket) — see that file for the established form. The backlog is data, not a verdict:
each line is classified in the companion audit (see the task's notes), and
burning it down is follow-up work, not a precondition for stopping the bleeding.

Deliberately conservative — both directions of imprecision are stated:
  * the Python side is read per REACHING PATH (ms-185 e-7300): for each dispatch
    call, only the statements that can run before it on its own path are
    resolved. Statements before it in the same block count whole (so a dict
    built up incrementally — ``env = {...}`` then ``if ...: env["X"] = ...`` —
    still counts as passed, erring toward reporting less). Sibling branches and
    anything after the call do not. It still under-reports: an env built by a
    shape this does not model (a call, a merge) falls back to every key the
    path builds.
  * the bash side reads the 60 lines above the dispatch line, which is where
    the ``VAR=... python3 "$COMMANDS_PY" <verb>`` idiom puts its assignments.

How much that first bullet costs, measured (ms-185 e-7079, independent AX
review). Handlers here are sibling ``if cmd == "...":`` branches that each
build a dict literal assigned to the SAME local name ``env``. Read per
function, one branch's keys are credited to every sibling — so a verb that
does not pass a flag looks wired because a neighbour passes it. Measured on
this repo at the time: ``push_record`` was credited with ``BEACON_JSON`` that
only the ``list`` branch passes, and ``doc_add`` / ``doc_update`` /
``doc_show`` / ``doc_delete`` were all credited with ``BEACON_TREK_ID`` that
only the ``list`` branch passes. Scoping resolution to the path that reaches
the dispatch call surfaced 13 real pre-existing gaps (``skill_install`` x8,
``task_cancel``, ``deploy_record``, ``doc_add --target``,
``opportunity_add``, ``push_record``) — verified pre-existing by running the
scoped version against an origin/main tree.

**ms-185 e-7300 fixed the scoping.** The 4 gaps that were still hidden when it
landed (``push_record`` / ``deploy_record`` ``BEACON_JSON``, ``doc_add``
``BEACON_TARGET``, ``opportunity_add`` ``BEACON_OPP_DESCRIPTION``) are recorded
in KNOWN_GAPS with their verdicts; the other 9 of the 13 had been wired or
recorded by then. The earlier count stands as the measurement that justified
the change, not as today's state.

The first attempt at this scoping leaked FORWARD through source order (it
credited a dict assigned *after* the call), which is why it was not bolted onto
an unrelated change; that patch is kept at
``.archive/cli-env-parity-branch-scoping-WIP.patch``. The property is now
pinned by ``tests/test_cli_env_parity_branch_scoping_e7300.py``, which measures
all three directions on a minimal synthetic source: siblings invisible, keys
added along the path counted, assignments after the call invisible.

An OK from this checker still is not proof that every mapping is wired — the
fallback above, and the bash side's line-window, both err toward silence.

Plain script, no pytest import: scripts/ci-strict-drift-guards.sh calls it from
jobs that have no pytest (ms-160 e-6349).
"""

import ast
import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Env names that are never a frontend's job to pass: operator / test knobs read
# straight from the environment by design, and values a frontend resolves for
# itself. Matching is exact on the name.
# Values a frontend is not supposed to pass. Scoped to ``(verb, env)``; the
# verb ``"*"`` means genuinely process-wide. Scoping matters because env names
# are ad-hoc strings: a global exemption added to clear ONE verb also silences
# every other verb that happens to read the same name, now and in future, with
# nothing in the output naming the collateral (AX review, PR #781 A-4). The
# "*" rows are the ones where that breadth is the intent, stated as a choice.
AMBIENT_ENV = {
    # genuinely process-wide: set by the operator's environment or by a layer
    # above argv, never by a flag on any verb.
    ("*", "BEACON_DEBUG"),
    ("*", "BEACON_SESSION_ID"),          # resolved by the session layer
    ("*", "BEACON_SESSION_KIND"),
    # set by the autonomous-execution envelope, not by a human's argv
    ("*", "BEACON_OPERATION_AUTO_EXECUTE"),
    ("*", "BEACON_OPERATION_ENVELOPE_ID"),
    # per-verb operator / test knobs, documented as env-only (no flag intended)
    ("doctor", "BEACON_DOCTOR_MIN_VERSION"),
    ("doctor", "BEACON_DOCTOR_SKIP_CLOUD_STATE"),
    ("doctor", "BEACON_DOCTOR_SKIP_MS81"),
    ("doctor", "BEACON_DOCTOR_SKIP_PRINCIPLE_MARKER"),
    ("doctor", "BEACON_DOCTOR_SKIP_SKILL_DRIFT"),
    ("bus_send", "BEACON_QUALGATE_OFF"),
    ("summary", "BEACON_SUPPRESS_DEPRECATION"),
    # `beacon view` の 2 つの素通し口 (ms-170 e-7249 で個別判定した)。どちらも
    # 「合っていないと承知のうえでその盤を見たい」ときの操作者向けの口で、旗に
    # する予定が無い (旗にすると、間違った盤を開くのが普段の選択肢に見える)。
    # 以前は KNOWN_GAPS に「内部用か未実装かは個別判定が要る」として置かれて
    # いた。判定したのでここへ移す。双子の片方だけ免除すると、同じ判断の 2 行が
    # 別の欄に分かれて、次に読む人がどちらが正しいか分からなくなる。
    ("view", "BEACON_VIEW_SKIP_HANDSHAKE"),
    ("view", "BEACON_VIEW_SKIP_STALENESS_CHECK"),
    # ms-185 e-7079: Trek を新しく書くための値。**どのフロントも渡さないのが
    # 設計**で、未配線ではない。`doc add` / `doc update` の `--trek` は python
    # (argparse) と bash (bin/lib/cmd_doc.sh) の両方から落としたが、
    # lib/cmd_doc.py は BEACON_TREK_ID を読み続ける — argv より上の層
    # (API / MCP) から来た書き込みを _refuse_trek_write で断るため。
    # 読む側だけが残るのは、断る口を残すことの裏返し。
    # 読み取り (`doc list --trek`) は別で、そちらは両フロントが今も渡す。
    ("doc_add", "BEACON_TREK_ID"),
    ("doc_update", "BEACON_TREK_ID"),
}


def _is_ambient(verb: str, env: str) -> bool:
    return ("*", env) in AMBIENT_ENV or (verb, env) in AMBIENT_ENV


def _verb_reads(lib_dir: pathlib.Path) -> dict:
    """verb -> BEACON_* names its cmd_<verb>() reads from os.environ."""
    reads = collections.defaultdict(set)
    for path in sorted(lib_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not fn.name.startswith("cmd_"):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", None) in ("get", "getenv")
                        and node.args):
                    a = node.args[0]
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and a.value.startswith("BEACON_")):
                        reads[fn.name[4:]].add(a.value)
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)
                        and node.slice.value.startswith("BEACON_")):
                    reads[fn.name[4:]].add(node.slice.value)
    return reads


def _env_keys_of(node) -> set:
    """BEACON_* names this expression contributes as dict KEYS."""
    out = set()
    if isinstance(node, ast.Dict):
        for k in node.keys:
            if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and k.value.startswith("BEACON_")):
                out.add(k.value)
    return out


def _copy_source(node):
    """If this expression copies another local dict (``dict(x)`` / ``{**x}``),
    the name it copies from."""
    if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict"
            and len(node.args) == 1 and isinstance(node.args[0], ast.Name)):
        return node.args[0].id
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if k is None and isinstance(v, ast.Name):   # {**x}
                return v.id
    return None


# ある文へ**入る前に評価される**フィールド (= header)。ms-185 e-7330 follow-up
# (PR #839 の独立保守性レビュー, high)。
#
# 以前はここを「list の先頭要素が ast.stmt なら block とみなして飛ばす」という
# 形で判定していた。``ast.Try`` の ``handlers`` は先頭要素が ``ast.excepthandler``
# (= ``ast.stmt`` ではない) なので**その判定を素通りし、except 本体が丸ごと経路に
# 混入していた**。呼び出しが ``try`` 本体の中に在るとき、except が走ったなら
# その呼び出しは起きていない —— つまり except 本体は到達経路上に無い。
#
# 「block でない物を通す」という除外型の判定は、新しい構文 (match / TryStar 等) が
# 増えるたびに静かに穴が開く。入れてよいフィールドを**名前で挙げる**許可型に変えた。
# 挙げていない型の文が祖先に来たら header は空 = 何も足さない (= 報告を少なくする側)。
_HEADER_FIELDS = {
    ast.If: ("test",),
    ast.While: ("test",),
    ast.For: ("target", "iter"),
    ast.AsyncFor: ("target", "iter"),
    ast.With: ("items",),
    ast.AsyncWith: ("items",),
    ast.Try: (),          # body へ直接入る。handlers / orelse / finalbody は経路外
    # except 節の中に呼び出しが在る場合。``type`` / ``name`` (= except 節の見出し) は
    # その節に入る前に評価されるので経路上。
    #
    # **既知の限界** (PR #839 2 周目の完全性検査で露出、dispatch.py に実例 0 件):
    # 呼び出しが except 節の中に在るとき、**try 本体で組んだ鍵は数えない**。例外が
    # 投げられるまで try 本体は部分的に走っているので、本当は数えるのが正しい。
    # 経路の組み立てが「どの枝から上がってきたか」を持っていないため、ここだけ
    # 分岐を意識した造りが要る。実例が 0 件のうちに造りを増やすより、限界を書いて
    # 現挙動を試験で固定する方を採った (``test_a_call_inside_an_except_handler_has_a
    # _known_limit``)。この向きの誤りは**偽のギャップ** (= 配線済みを未配線と言う)
    # なので、CI が赤くなって人が気付く側に転ぶ。黙って通る側ではない。
    ast.ExceptHandler: ("type", "name"),
}
if hasattr(ast, "TryStar"):          # py3.11+
    _HEADER_FIELDS[ast.TryStar] = ()
if hasattr(ast, "match_case"):       # py3.10+ (match 文)
    _HEADER_FIELDS[ast.Match] = ("subject",)
    _HEADER_FIELDS[ast.match_case] = ("guard",)

# **表の打ち間違いを無音の劣化にしない** (PR #839 2 周目の独立保守性レビュー, high)。
#
# 許可型に変えたのは、除外型が新しい構文のたびに静かに穴を開けるからだった。ところが
# 許可型そのものが同じ穴を持っていた: 参照側は ``getattr(stmt, field, None)`` で引くので、
# フィールド名を 1 文字間違えても None が返り ``_walk_executed`` が黙って何も返さない。
# judge が ``ast.With`` の ``"items"`` を ``"itemz"`` に変異させたところ、**8 本の試験と
# 本体実行の両方が緑のまま**通った。過大報告を直した修正が、過少報告の方向に同型の穴を
# 作っていた形 (= 1 経路だけ閉じて「閉じた」と主張する、この MS で 3 度目)。
#
# ここで ``_fields`` と突き合わせるので、打ち間違いは **import した瞬間に落ちる**。
# 参照側を直すのではなく宣言側で閉じるのは、読み手 (次に表を触る AI) が間違えた場所で
# すぐ分かるのが肝だから。
for _node_type, _fields in _HEADER_FIELDS.items():
    _known = set(getattr(_node_type, "_fields", ()))
    _unknown = [f for f in _fields if f not in _known]
    if _unknown:
        raise AssertionError(
            f"_HEADER_FIELDS[{_node_type.__name__}] が持たないフィールドを挙げています: "
            f"{_unknown} (実在するのは {sorted(_known)})。"
            f"綴りを間違えると参照側の getattr が None を返して黙って何も数えなくなり、"
            f"配線されている鍵を『渡していない』と誤判定します")

# 関数 / クラス / lambda の**定義**は、その本体を実行しない。
_NOT_EXECUTED_HERE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _walk_executed(node):
    """``node`` を評価したときに**実際に走る**部分だけを歩く。

    ms-185 e-7330 follow-up (PR #839 の独立保守性レビュー, high): 以前は
    ``ast.walk`` で丸ごと歩いていたので、呼び出しより前に在る**入れ子の
    ``def`` の本体**まで数えていた。関数を定義しても本体は走らないので、
    helper 内部の無関係な代入が「渡している」側に混入していた。

    どちらの漏れも**過大報告** (= 渡していない鍵を渡していると数える) で、
    この検査が存在する理由 (未配線の検出) を静かに外す。モジュール冒頭が
    「報告を少なくする側に倒す、over-report しない」と書いているのに、実際には
    over-report する経路が 2 つ在った。
    """
    yield node
    if isinstance(node, _NOT_EXECUTED_HERE):
        # 定義そのもの (= 名前の束縛) は経路上。本体は走らないので降りない。
        # 定義時に評価されるのは装飾子 / 既定引数 / 基底クラスだけなので、そこは歩く。
        for field in ("decorator_list", "bases", "keywords", "args", "returns"):
            value = getattr(node, field, None)
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, ast.AST):
                    yield from _walk_executed(item)
        return
    for child in ast.iter_child_nodes(node):
        yield from _walk_executed(child)


class _ReachingPath:
    """ある呼び出しへ**到達する経路上**の文を返す (ms-185 e-7300)。

    **直した穴**: 以前は局所名の解決を ``ast.walk(fn)`` で囲っている関数ごと
    行っていた。このリポジトリの処理部は ``if cmd == "..."`` の姉妹分岐が並び、
    どの分岐も同じ局所名 ``env`` に辞書を組み立てるので、**ある分岐が渡す鍵が
    隣の分岐の「渡している」集合にも貸与されていた**。実測 (2026-11-03):
    push record は list 分岐の BEACON_JSON を、doc add / update / show / delete は
    list 分岐の BEACON_TREK_ID を、渡していないのに渡していると判定されていた。
    検査が存在する理由そのものについて、自分の担当範囲の一部で赤くなれない状態。

    経路の作り方: 呼び出しから関数まで親を辿り、各段で **その文より前の文は
    部分木ごと**、含んでいる文は次の段へ降りる。姉妹分岐 (別の arm / 呼び出しより
    後の文) は入らない。

    「前の文は部分木ごと」が偽のギャップを防ぐ: ``env = {...}`` の後に
    ``if ...: env["X"] = ...`` を足して、その後で渡す形は、``if`` が呼び出しより
    前に在るので中身まで数える。実行されるか分からない分岐でも「渡している」側に
    倒すのは、この検査が一貫して報告を少なくする側へ寄せているのと同じ理由。
    """

    def __init__(self, fn):
        self._fn = fn
        # 子 -> 親、および文 -> (その文が属する list, index)
        self._parent = {}
        self._slot = {}
        for node in ast.walk(fn):
            for _field, value in ast.iter_fields(node):
                items = value if isinstance(value, list) else [value]
                for i, item in enumerate(items):
                    if not isinstance(item, ast.AST):
                        continue
                    self._parent[item] = node
                    if isinstance(item, ast.stmt) and isinstance(value, list):
                        self._slot[item] = (value, i)

    def _enclosing_stmt(self, node):
        cur = node
        while cur is not None and not (isinstance(cur, ast.stmt) and cur in self._slot):
            cur = self._parent.get(cur)
        return cur

    def stmts_reaching(self, call):
        """``call`` に到達する経路上の AST ノード列。"""
        st = self._enclosing_stmt(call)
        if st is None:
            return []
        # 内側から外側へ、各段の「その文を含む文」を集める
        chain = []
        cur = st
        while cur is not None:
            chain.append(cur)
            parent = self._parent.get(cur)
            cur = self._enclosing_stmt(parent) if parent is not None else None
            if cur is not None and cur in chain:   # 念のための循環止め
                break
        out = []
        for depth, stmt in enumerate(chain):
            lst, idx = self._slot[stmt]
            for before in lst[:idx]:
                out.extend(_walk_executed(before))
            if depth == 0:
                # 最内の文は丸ごと (呼び出しと同一文での組み立てを拾う)
                out.extend(_walk_executed(stmt))
            else:
                # 外側の文は **header だけ**。block は内側の段が担う。
                for field in _HEADER_FIELDS.get(type(stmt), ()):
                    value = getattr(stmt, field, None)
                    items = value if isinstance(value, list) else [value]
                    for item in items:
                        if isinstance(item, ast.AST):
                            out.extend(_walk_executed(item))
        return out


def _locals_along(nodes):
    """経路上のノード列から「局所名 -> 書き込まれた BEACON_* 鍵」を組む。

    以前は関数全体に対して 1 回だけ組んでいた処理を、経路ごとに呼べる純粋関数へ
    出した (ms-185 e-7300)。中身の規則は変えていない — dict リテラル / 局所への
    代入 / ``d["BEACON_X"] = ...`` の subscript 書き込み / ``d.update({...})`` /
    ``dict(x)`` と ``{**x}`` の写し。
    """
    locals_: dict = collections.defaultdict(set)
    copies: list = []          # (dest, source) for dict(x) / {**x}

    def _record(tgt, value):
        keys = _env_keys_of(value)
        if isinstance(tgt, ast.Name):
            if keys:
                locals_[tgt.id] |= keys
            src = _copy_source(value)
            if src:
                copies.append((tgt.id, src))
        if (isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Name)
                and isinstance(tgt.slice, ast.Constant)
                and isinstance(tgt.slice.value, str)
                and tgt.slice.value.startswith("BEACON_")):
            locals_[tgt.value.id].add(tgt.slice.value)

    for node in nodes:
        # plain and ANNOTATED assignment (``base_env: Dict[str, str] = {...}``
        # is an AnnAssign, and missing it made every key of the biggest env
        # dict in the file invisible)
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                _record(tgt, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _record(node.target, node.value)
        # walrus (``env := {...}``)。PR #839 2 周目の独立 AX レビュー (silent):
        # ``ast.Assign`` / ``AnnAssign`` しか見ていなかったので、walrus で組むと
        # 検出されず**偽のギャップ**になる。dispatch.py に現在 0 件だが、足された
        # 瞬間に CI が「二重フロント drift」と誤診する形だった。
        elif isinstance(node, ast.NamedExpr):
            _record(node.target, node.value)
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "update"
                and isinstance(getattr(node.func, "value", None), ast.Name)
                and node.args):
            locals_[node.func.value.id] |= _env_keys_of(node.args[0])

    # propagate copies to a fixed point (dict(dict(x)) chains are rare but
    # cost nothing to follow)
    for _ in range(len(copies) + 1):
        changed = False
        for dest, src in copies:
            if locals_.get(src) and not locals_[src] <= locals_[dest]:
                locals_[dest] |= locals_[src]
                changed = True
        if not changed:
            break
    return locals_


def _python_sets(dispatch_path: pathlib.Path):
    """verb -> BEACON_* names the handler actually PUTS IN the env it passes.

    Structural, not "the name appears somewhere in this function". The first
    version matched any string constant in the enclosing function, so a TODO
    note (``_todo = ["BEACON_X"]``), a help string or a log message naming the
    variable counted as having wired it — a false negative that an agent
    documenting intent before implementing would walk straight into
    (AX review, PR #781 A-1, reproduced with a decoy).

    Tracked here: dict literals passed as the env argument, dict literals
    assigned to a local that is then passed, ``d["BEACON_X"] = ...`` subscript
    writes to such a local, and ``d.update({...})``. Anything subtler is not
    counted — which keeps the error on the reporting side, never the silencing
    side.
    """
    tree = ast.parse(dispatch_path.read_text(encoding="utf-8"))
    sets = collections.defaultdict(set)
    verbs = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        reach = _ReachingPath(fn)

        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "_run_commands_py"
                    and len(node.args) >= 2):
                continue
            v = node.args[1]
            if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
                continue
            verb = v.value
            verbs.add(verb)
            passed = set()
            env_arg = node.args[2] if len(node.args) >= 3 else None
            for kw in node.keywords:
                if kw.arg == "env":
                    env_arg = kw.value
            # ms-185 e-7300: 局所名の解決は **この呼び出しへ到達する経路だけ**から
            # 組む (姉妹分岐は互いに見えない)。
            locals_ = _locals_along(reach.stmts_reaching(node))
            if isinstance(env_arg, ast.Dict):
                passed |= _env_keys_of(env_arg)
            elif isinstance(env_arg, ast.Name):
                passed |= locals_.get(env_arg.id, set())
            elif env_arg is not None:
                # an expression we do not model (a call, a merge, …): fall back
                # to every key this path builds, so an unmodelled shape
                # reports LESS rather than inventing a gap.
                passed |= set().union(*locals_.values()) if locals_ else set()
            sets[verb] |= passed
    return sets, verbs


_DISPATCH_RE = re.compile(r'\$COMMANDS_PY"?\s+([a-z0-9_]+)')
_ASSIGN_RE = re.compile(r'\b(BEACON_[A-Z0-9_]+)=')
# How far above the dispatch line the `VAR=... python3 "$COMMANDS_PY" <verb>`
# idiom spreads its assignments. Generous on purpose: over-reaching only makes
# the checker quieter, never noisier.
_BASH_LOOKBACK = 60


def _quote_spans(line: str):
    """[(start, end)] of quoted regions, and the offset where a comment starts."""
    spans, quote, start, i = [], None, 0, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                spans.append((start, i))
                quote = None
        elif ch in "'\"":
            quote, start = ch, i
        elif ch == "#":
            return spans, i
        i += 1
    if quote:
        spans.append((start, len(line)))
    return spans, len(line)


def _bash_assignments(line: str) -> set:
    """BEACON_* names ASSIGNED on this line.

    A name inside a comment or inside a quoted string is not wiring: an
    `# example: BEACON_X=1` that outlived the code it described, or an echoed
    usage line, must not read as coverage (AX review, PR #781 A-2).

    Positions are filtered rather than the text blanked. Blanking also erased
    `"$COMMANDS_PY"` — the marker that identifies the dispatch line — which
    silently reduced the bash side to ZERO verbs. Caught by measuring; it
    would have shipped a guard that saw nothing at all on that frontend.
    """
    spans, comment_at = _quote_spans(line)
    out = set()
    for m in _ASSIGN_RE.finditer(line):
        pos = m.start(1)
        if pos >= comment_at:
            continue
        if any(a < pos < b for a, b in spans):
            continue
        out.add(m.group(1))
    return out


def _bash_sets(bin_path: pathlib.Path, lib_dir: pathlib.Path):
    """verb -> BEACON_* names assigned on the path that dispatches it.

    Each file is scanned INDEPENDENTLY. Concatenating bin/beacon with every
    bin/lib/*.sh let a dispatch line near the top of a short file reach its
    lookback window back into the tail of an unrelated preceding file and
    count that file's assignments as coverage — and six of the per-command
    files are shorter than the window (AX review, PR #781 A-2).
    """
    sets = collections.defaultdict(set)
    verbs = set()
    for src in [bin_path] + sorted(lib_dir.glob("*.sh")):
        try:
            lines = src.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        assigns = [_bash_assignments(l) for l in lines]
        for i, line in enumerate(lines):
            _, comment_at = _quote_spans(line)
            m = _DISPATCH_RE.search(line[:comment_at])   # not a commented-out call
            if not m:
                continue
            verb = m.group(1)
            verbs.add(verb)
            lo = max(0, i - _BASH_LOOKBACK)              # never crosses into
            for j in range(lo, i + 1):                   # another file
                sets[verb] |= assigns[j]
    return sets, verbs


def collect(root: pathlib.Path = ROOT, exclude=None) -> list:
    """[(verb, env, frontend)] for every env a reaching frontend never sets.

    ``exclude`` defaults to the recorded backlog; pass ``set()`` for the raw
    set. Taking it as an ARGUMENT rather than reading the module global lets
    the stale check ask for the unfiltered answer without briefly emptying a
    constant other code may be reading (maintainability review, PR #781 M-4 —
    and the hazard was not theoretical: the clear/restore broke the moment the
    backlog became a dict).
    """
    if exclude is None:
        exclude = KNOWN_GAPS
    reads = _verb_reads(root / "lib")
    py_sets, py_verbs = _python_sets(root / "beacon_cli" / "dispatch.py")
    sh_sets, sh_verbs = _bash_sets(root / "bin" / "beacon", root / "bin" / "lib")

    out = []
    for verb in sorted(reads):
        for env in sorted(e for e in reads[verb] if not _is_ambient(verb, e)):
            if verb in sh_verbs and env not in sh_sets.get(verb, set()):
                out.append((verb, env, "bash"))
            if verb in py_verbs and env not in py_sets.get(verb, set()):
                out.append((verb, env, "python"))
    return [row for row in out if row not in exclude]


def main() -> int:
    rows = collect()
    # Scan ONCE. Written inline in the comprehension this re-ran the whole
    # scan per allowlist entry (68 scans, 52s instead of under 2).
    raw = set(_collect_raw())
    stale = sorted(g for g in KNOWN_GAPS if g not in raw)
    if not rows and not stale:
        print("[cli-env-parity] OK: no NEW (verb, env, frontend) gap outside "
              "the recorded backlog ({0} rows). Method: per-reaching-path def-use on "
              "the Python side, per-file comment/quote-aware scan on the bash "
              "side — both err toward reporting less, so this is 'nothing new "
              "was detected', not 'every mapping is proven'."
              .format(len(KNOWN_GAPS)))
        return 0
    if rows:
        print("[cli-env-parity] a verb reads an env its frontend never passes:",
              file=sys.stderr)
        for verb, env, front in rows:
            print(f"  {verb}: {env}  (not set by the {front} frontend)",
                  file=sys.stderr)
        print("", file=sys.stderr)
        print("  The flag parses but its value never reaches the implementation,", file=sys.stderr)
        print("  or the flag does not exist on that frontend at all. Name parity", file=sys.stderr)
        print("  (check-cli-help-drift.py) cannot see this — an absent flag is not", file=sys.stderr)
        print("  'unequal'. See ms-160 e-6674.", file=sys.stderr)
        print("  -> wire the flag → env mapping on that frontend, or add the row to", file=sys.stderr)
        print("     AMBIENT_ENV if the value is genuinely not a frontend's to pass.", file=sys.stderr)
    if stale:
        print("[cli-env-parity] KNOWN_GAPS rows that are no longer gaps:", file=sys.stderr)
        for verb, env, front in stale:
            print(f"  {verb}: {env} ({front})", file=sys.stderr)
        print("  -> delete them; a fixed gap left in the list exempts the next "
              "regression at the same spot.", file=sys.stderr)
    return 1


def _collect_raw() -> list:
    """collect() without the backlog subtraction (for stale detection)."""
    return collect(exclude=set())


# The backlog as it stood when this checker was written (ms-160 e-6674). Data,
# not a verdict: every row carries its classification inline. Burning it down is
# follow-up work; the checker exists so the list can only shrink.
#
# WHY A BARE COORDINATE IS ENOUGH HERE — AND WHEN IT STOPS BEING ENOUGH.
# An independent review (PR #781 AX-3, severity high) asked for a content
# fingerprint per row, so that a DIFFERENT defect landing on an already-listed
# (verb, env, frontend) could not hide behind the old entry. That was declined,
# because this checker reports exactly one state — "this frontend does not put
# this key in the env it passes" — and exactly one thing produces it: no
# mapping. There is no second cause for a fingerprint to tell apart.
#
# That argument has an expiry, and it is worth stating because the next person
# to touch this will not re-derive it. It holds only while the checker tests
# KEY PRESENCE. Two things it deliberately does not look at today:
#   (a) the key is passed but its VALUE comes from the wrong flag;
#   (b) the key is passed on only ONE branch of a conditional.
# Both currently count as "passed". The moment this checker is tightened to
# judge either of them, a single coordinate gains two distinct meanings
# ("absent" vs "present but wrong"), the one-cause argument fails, and
# KNOWN_GAPS needs the fingerprint AX-3 asked for. Reconsider it then rather
# than rediscovering the question.
KNOWN_GAPS: dict = {
    ("account_add", "BEACON_ACCOUNT_ASSIGNEE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("account_contact", "BEACON_CONTACT_PHONE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("account_list", "BEACON_AS_PROJECT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("account_list", "BEACON_LINKED", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_directory", "BEACON_DIR_CWD_ONLY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_ALLOW_DUPLICATE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_CLIENT_EVENT_ID", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_CONTEXT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_DEDUP_WINDOW_SEC", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("bus_send", "BEACON_BUS_DEDUP_WINDOW_SEC", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("bus_send", "BEACON_BUS_IS_RETRY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_RATIONALE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("cloud_check_project", "BEACON_CLOUD_PROJECT_ID", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("cloud_check_project", "BEACON_CLOUD_PROJECT_ID", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("cloud_list", "BEACON_JSON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("deploy_list", "BEACON_BACKEND", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("deploy_record", "BEACON_BACKEND", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("deploy_record", "BEACON_JSON", "bash"):
        "bash 未配線 / python は渡す — bash 側の取りこぼし",
    ("deploy_record", "BEACON_VERSION", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("deploy_rollback", "BEACON_REGION", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("doc_add", "BEACON_ACCOUNT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_add", "BEACON_FORCE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_add", "BEACON_OPPORTUNITY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_delete", "BEACON_REASON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_list", "BEACON_ACCOUNT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_list", "BEACON_INCLUDE_TRASHED", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_list", "BEACON_OPPORTUNITY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_update", "BEACON_ACCOUNT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_update", "BEACON_MS_SET", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_update", "BEACON_OPPORTUNITY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_update", "BEACON_OP_SET", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("issue_sync", "BEACON_JSON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("log", "BEACON_RESOLVES_SET", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("log_finalize", "BEACON_RESOLVES_SET", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("member_role", "BEACON_JSON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("opportunity_add", "BEACON_OPP_ASSIGNEE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("opportunity_list", "BEACON_ALL", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("pr_create", "BEACON_GH_ARGS", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("pr_create", "BEACON_GH_ARGS", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("push_record", "BEACON_VERSION", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("rollback", "BEACON_ROLLBACK_CWD", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("rollback", "BEACON_ROLLBACK_CWD", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("rollback", "BEACON_ROLLBACK_NO_RECORD", "bash"):
        "bash 未配線 / python は渡す — bash 側の取りこぼし",
    ("run_record", "BEACON_JSON", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("sales_gmail_permalink", "BEACON_MSGID", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("sales_gmail_permalink", "BEACON_MSGID", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("search", "BEACON_ACTOR", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_CLAIMANT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_INCLUDE_BUS_DM", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_INCLUDE_SESSION_LOGS", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_INCLUDE_TREK", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_SOURCE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("skill_install", "BEACON_SETTINGS_PATH", "bash"):
        "bash 未配線 / python は渡す — bash 側の取りこぼし",
    ("sync", "BEACON_MS_ID", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("sync", "BEACON_MS_ID", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("task_cancel", "BEACON_JSON", "bash"):
        "bash 未配線 / python は渡す — bash 側の取りこぼし",
    # ms-185 e-7300: 解決を到達経路に狭めた時点で露出した既存ギャップ。
    # いずれも bash 側は渡しており python 側だけ未配線 (= Windows・pipx から
    # 旗を打っても値が実装に届かない)。origin/main のツリーに同じ走査を当てて
    # 既存であることを確認済み。直すのは 1 行ずつの判定が要る follow-up で、
    # ここに置くのは「止血を先にする」ため (この file の冒頭の方針どおり)。
    ("push_record", "BEACON_JSON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("deploy_record", "BEACON_JSON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_add", "BEACON_TARGET", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない。"
        "ms-185 e-7071 で beacon-session-end Skill の記述を実測した際に人が見つけた"
        "のと同じギャップで、この scoping 修正で初めて機械が捕まえた",
    ("opportunity_add", "BEACON_OPP_DESCRIPTION", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
}

if __name__ == "__main__":
    sys.exit(main())
