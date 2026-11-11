"""節目から思想レビューを撃つ入口が、**そのまま実行できる呼び出し**を指示する
ことの固定 (ms-119 / e-7438)。

**何が起きていた** (独立思想レビュー medium, 2026-11-11): クラッシュ修正
(PR #845 / `origin_provenance`) で思想レビューは実行可能になったが、**節目から
呼ぶ入口すべてが、実行すると落ちる呼び出しを指示していた**。main の lib で実測:

* `bin/beacon-post-commit-hook.sh` の target-close 分岐 →
  `/beacon-review-run --type philosophy --target <target-id>` を指示。原典が
  無いので rc=1 (`--type philosophy needs --origin-doc <doc-id>`)。
* `lib/commands_shared.py` の review-due trigger message →
  `--origin-doc <spec-doc-id>` だけを指示。artifact 源 (`--pr` / `--diff-ref` /
  `--target`) を一言も言わないので rc=1 (`pass --pr <n> or --diff-ref ... or
  --target ...`)。しかも `<spec-doc-id>` は埋まっていない placeholder。
* `skills/beacon-review-run.md` (3 copy) → philosophy は `--pr` / `--diff-ref`
  のどちらか必須と書くだけで、close 節目の `--target` (whole-target) が Skill の
  語彙に存在しなかった。

結果、実際に走らせられるのは **kernel のコードを読んで呼び出しを自分で組み立て
た場合だけ**。ms-119 自身が解こうとした「機構は在るが撃たれない」型。

**この module が測るもの**: 散文の言い回しではなく、**発火した命令が実行できるか**。
review-due trigger の message から `/beacon-review-run` の呼び出しを抜き出し、
flag を `bin/lib/cmd_review.sh` 由来の対応表で環境変数に写して本物の
``cmd_review_context`` を走らせる。欠けていれば `SystemExit` で落ちる。

**構造**: 呼び出しを綴る場所は trigger message の 1 箇所に寄せた (発火時に target
も SPEC doc-id も知っている唯一の層)。hook は「その行をそのまま実行せよ」と言う
だけなので、hook が再び自前の philosophy 呼び出しを綴ったら、同じハーネスが
それを拾って赤くなる。
"""

import json
import os
import re
import sys
import pathlib

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import commands_shared as cs  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC_DOC = "test-e7438-spec-doc"
TARGET = "ms-7438"

# `/beacon-review-run` に続く **コマンドらしいトークン列** だけを拾う。日本語の
# 散文 (「〜の行を」) は文字クラスに入っていないので、そこで止まる = 命令として
# 綴られた呼び出しだけが対象になる。
_INVOCATION = re.compile(r"/beacon-review-run((?:\s+[-\w./=:<>|#]+)*)")


def _flag_to_env():
    """flag → 環境変数の対応を `bin/lib/cmd_review.sh` から機械的に読む。

    テスト側で対応表を書き写すと、CLI が旗の名前や環境変数を変えたときに黙って
    ズレる (このリポジトリが何度も踏んだ「写しが片方だけ直る」型)。実装から引く。
    """
    src = (ROOT / "bin" / "lib" / "cmd_review.sh").read_text(encoding="utf-8")
    flag_var = dict(re.findall(r'^\s*(--[a-z-]+)\)\s+([a-z_]+)="\$\{2:-\}"',
                               src, re.M))
    var_env = {v: e for e, v in
               re.findall(r'(BEACON_[A-Z_]+)="\$([a-z_]+)"', src)}
    mapping = {f: var_env[v] for f, v in flag_var.items() if v in var_env}
    assert "--type" in mapping and "--origin-doc" in mapping, mapping
    return mapping


def review_run_invocations(message: str):
    """message が命令として綴っている **全種別** の呼び出しの flag dict 一覧。

    契約 (「穴の無い行はそのままコピーして実行してよい」) は `/beacon-review-run`
    の行すべてに掛かるので、種別で絞らずに拾う。初版は philosophy だけを見ていた
    ので、**同じ message の目的達成行が穴なしで綴られたまま実行は落ちる**状態を
    検知できなかった (独立 AX レビュー 収束周)。
    """
    out = []
    for m in _INVOCATION.finditer(message):
        toks = (m.group(1) or "").split()
        flags = {}
        i = 0
        while i < len(toks):
            if toks[i].startswith("--"):
                val = toks[i + 1] if i + 1 < len(toks) and not toks[i + 1].startswith("--") else ""
                flags[toks[i]] = val
                i += 2 if val else 1
            else:
                i += 1
        if flags.get("--type"):
            out.append(flags)
    return out


def philosophy_invocations(message: str):
    """message が命令として綴っている philosophy 呼び出しの flag dict 一覧。"""
    out = []
    for m in _INVOCATION.finditer(message):
        toks = (m.group(1) or "").split()
        flags = {}
        i = 0
        while i < len(toks):
            if toks[i].startswith("--"):
                val = toks[i + 1] if i + 1 < len(toks) and not toks[i + 1].startswith("--") else ""
                flags[toks[i]] = val
                i += 2 if val else 1
            else:
                i += 1
        if flags.get("--type") == "philosophy":
            out.append(flags)
    return out


def run_invocation(flags, monkeypatch):
    """抜き出した呼び出しを **本物の kernel で実行** する。

    足りない旗があれば ``cmd_review_context`` が ``SystemExit`` を投げる (= 落ちる
    呼び出しを指示していた、という欠陥がそのまま赤になる)。
    """
    mapping = _flag_to_env()
    for env in set(mapping.values()):
        monkeypatch.delenv(env, raising=False)
    for flag, val in flags.items():
        env = mapping.get(flag)
        assert env, f"{flag} は bin/lib/cmd_review.sh が受け取らない旗です"
        monkeypatch.setenv(env, val)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_review_context()
    raw = buf.getvalue().strip()
    assert raw, "bundle が出ていない"
    return json.loads(raw.splitlines()[-1])


@pytest.fixture
def fired(tmp_path, monkeypatch):
    """SPEC doc を 1 件持つ target で close 節目を発火させ、message を返す。"""
    (tmp_path / ".beacon").mkdir()
    proj_file = tmp_path / ".beacon" / "project.json"
    proj_file.write_text(json.dumps({
        "name": "t",
        "milestones": [{"id": TARGET, "title": "テスト target",
                        "status": "in_progress", "entries": []}],
        "operations": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(proj_file))

    class _Store:
        def load_project(self):
            return json.loads(proj_file.read_text(encoding="utf-8"))

        def list_documents(self):
            return [{"doc_id": SPEC_DOC, "scope": "spec", "milestone": TARGET,
                     "updated_at": "2026-11-11T00:00:00"}]

        def get_document(self, doc_id):
            if doc_id != SPEC_DOC:
                return None
            return {"content": "# SPEC\n\n## 設計方針\n\n1. 構造で閉じる。\n"}

    monkeypatch.setattr(cs, "get_store", lambda: _Store())
    monkeypatch.setattr(commands, "get_store", lambda: _Store())

    cs._fire_review_due_trigger(
        TARGET, "milestone", "in_progress", "observing",
        target_title="テスト target", has_spec=True, gated=True)
    path = tmp_path / ".beacon" / "triggers" / f"review-due-{TARGET}.json"
    assert path.exists(), "review-due trigger が発火していない"
    return json.loads(path.read_text(encoding="utf-8"))["message"]


@pytest.fixture
def fired_ungated(tmp_path, monkeypatch):
    """ゲートを経ない milestone 完了 (= 目的達成の促し + 思想) の message。

    この fixture が覆うのは **目的達成行も穴なしで綴られ、かつ実行できる** こと。
    収束周の独立 AX レビューが、思想行だけ実行可能性を確かめていて目的達成行は
    確かめていなかった (= 契約を書いた側が 2 行のうち 1 行しか守っていなかった)
    のを見つけたので、両方を 1 本で測る。
    """
    (tmp_path / ".beacon").mkdir()
    proj_file = tmp_path / ".beacon" / "project.json"
    proj_file.write_text(json.dumps({
        "name": "t",
        "milestones": [{"id": TARGET, "title": "テスト target",
                        "status": "in_progress", "entries": []}],
        "operations": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(proj_file))

    class _Store:
        def load_project(self):
            return json.loads(proj_file.read_text(encoding="utf-8"))

        def list_documents(self):
            return [{"doc_id": SPEC_DOC, "scope": "spec", "milestone": TARGET,
                     "updated_at": "2026-11-11T00:00:00"}]

        def get_document(self, doc_id):
            if doc_id != SPEC_DOC:
                return None
            return {"content": "# SPEC\n\n## 設計方針\n\n1. 構造で閉じる。\n"}

    monkeypatch.setattr(cs, "get_store", lambda: _Store())
    monkeypatch.setattr(commands, "get_store", lambda: _Store())
    cs._fire_review_due_trigger(
        TARGET, "milestone", "in_progress", "done",
        target_title="テスト target", has_spec=True, gated=False)
    path = tmp_path / ".beacon" / "triggers" / f"review-due-{TARGET}.json"
    assert path.exists(), "review-due trigger が発火していない"
    return json.loads(path.read_text(encoding="utf-8"))["message"]


def test_fired_trigger_spells_a_philosophy_invocation(fired):
    """前提: close 節目の message は philosophy の呼び出しを 1 つ綴る。

    これが崩れると下の本体が「拾うものが無いので緑」になるので、前提として固定する
    (空集合で通る検査を作らない)。
    """
    assert len(philosophy_invocations(fired)) == 1, fired


def test_fired_philosophy_invocation_is_executable(fired, monkeypatch):
    """**本体**: 発火した message の呼び出しが、そのまま実行して bundle になる。

    修正前はここが `SystemExit`(1) だった —— message は artifact 源を一言も
    言わなかったので、指示どおり実行すると必ず
    「pass --pr <n> or --diff-ref ... or --target ...」で落ちた。
    """
    flags = philosophy_invocations(fired)[0]
    bundle = run_invocation(flags, monkeypatch)
    assert (bundle.get("origin") or {}).get("id") == SPEC_DOC
    assert bundle.get("mode") == "whole-target"


def test_fired_invocation_carries_the_resolved_doc_id(fired):
    """原典は **解決済みの doc-id** で入る (placeholder を実行可能に見せない)。"""
    flags = philosophy_invocations(fired)[0]
    assert flags["--origin-doc"] == SPEC_DOC
    assert flags["--target"] == TARGET


def test_harness_rejects_the_pre_fix_invocation(monkeypatch):
    """**反転**: 修正前の綴り (artifact 源なし) はハーネスで落ちる。

    これが緑のままだと、上の本体は「何を書いても通る」検査になる。欠陥そのものを
    流し込んで赤くなることを確かめておく。
    """
    with pytest.raises(SystemExit):
        run_invocation({"--type": "philosophy", "--origin-doc": SPEC_DOC},
                       monkeypatch)
    # 原典だけを欠いた綴り (= 旧 hook の形) も落ちる。
    with pytest.raises(SystemExit):
        run_invocation({"--type": "philosophy", "--target": TARGET},
                       monkeypatch)


def test_hook_close_branch_spells_no_philosophy_invocation_of_its_own():
    """hook は自前の philosophy 呼び出しを綴らない (綴れば必ず不完全になる)。

    原典 (SPEC doc-id) は shell から解決できないので、hook が綴れるのは placeholder
    混じりの実行できない呼び出しだけ。綴る場所を trigger message の 1 箇所に寄せ、
    hook は「その行を実行せよ」と言うだけにした。もし再び綴られたら、ここで拾って
    同じハーネスに掛かる (下の assert が呼び出しの存在自体を拒否する)。
    """
    src = (ROOT / "bin" / "beacon-post-commit-hook.sh").read_text(encoding="utf-8")
    # emit されるのは `emit "..."` の本文。target-close 分岐の emit 行を取る。
    close_emits = [ln for ln in src.splitlines()
                   if ln.strip().startswith("emit ") and "Target close" in ln]
    assert close_emits, "target-close の emit 行が見つからない"
    for ln in close_emits:
        assert philosophy_invocations(ln) == [], ln


# --- data 定義 (descriptor) target も同じ保証を受ける (独立 AX レビュー 最終周) ---
#
# 上の試験は milestone で測っていた。最終周の独立 AX レビューが、**descriptor
# target-class** (職種中立の `beacon target close --class <kind>` 経路) では
# 同じ message が **100% 決定的に** placeholder 入りの fallback に落ちることを
# 実測した。真因は「SPEC は在るか」に 2 つの関数が別の field 規約で答えていたこと:
# descriptor の has_spec 判定は汎用 `target` field を見るのに、呼び出しを組み立てる
# 側は `milestone` / `operation` しか知らなかった。
#
# hook と Skill は「message には必ずそのまま実行できる呼び出しが入っている」と
# **断定** しているので、1 つの target 種で偽なら断定そのものが偽になる。種を
# またいで同じ保証が成り立つことを、ここで固定する。

DESCRIPTOR_SPEC_DOC = "test-e7438-descriptor-spec"
DESCRIPTOR_TARGET = "rel-1"


@pytest.fixture
def fired_descriptor(tmp_path, monkeypatch):
    """descriptor target (kind=release) の完了節目を発火させ、message を返す。"""
    (tmp_path / ".beacon").mkdir()
    proj_file = tmp_path / ".beacon" / "project.json"
    proj_file.write_text(json.dumps({
        "name": "t", "milestones": [], "operations": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(proj_file))

    class _Store:
        def load_project(self):
            return json.loads(proj_file.read_text(encoding="utf-8"))

        def list_documents(self):
            # 紐づけは **汎用 target field** (descriptor の規約)。
            return [{"doc_id": DESCRIPTOR_SPEC_DOC, "scope": "spec",
                     "target": DESCRIPTOR_TARGET,
                     "updated_at": "2026-11-11T00:00:00"}]

        def get_document(self, doc_id):
            if doc_id != DESCRIPTOR_SPEC_DOC:
                return None
            return {"content": "# SPEC\n\n## 設計方針\n\n1. 構造で閉じる。\n"}

    monkeypatch.setattr(cs, "get_store", lambda: _Store())
    monkeypatch.setattr(commands, "get_store", lambda: _Store())
    # descriptor の完了は is_completion=True で binding を直接束ねる経路。
    cs._fire_review_due_trigger(
        DESCRIPTOR_TARGET, "release", "open", "done",
        target_title="テスト release", has_spec=True, is_completion=True)
    path = tmp_path / ".beacon" / "triggers" / f"review-due-{DESCRIPTOR_TARGET}.json"
    assert path.exists(), "descriptor target で review-due が発火していない"
    return json.loads(path.read_text(encoding="utf-8"))["message"]


def _has_placeholder(flags):
    """旗の値に `<...>` が残っていれば「自分で埋めろ」の案内 (コピペ命令でない)。"""
    return any(v.startswith("<") for v in flags.values() if v)


def test_descriptor_target_surfaces_the_doc_but_does_not_claim_runnable(fired_descriptor):
    """descriptor target では「そのまま実行できる」と名乗らない。

    原典の解決は直った (汎用 ``target`` field も引けるようにした) が、
    `--target` で whole-target 差分を集める経路は `core._find_approval_target` が
    引けない種類を支えていないので、**原典が在っても実行は落ちる**。だから message は
    解決済み doc-id を伝えつつ、穴 (プレースホルダ) を残して「artifact 源は自分で
    選べ」と言う —— 実行できない命令をコピペ可能な形で綴らない。
    """
    calls = philosophy_invocations(fired_descriptor)
    assert len(calls) == 1, fired_descriptor
    # 役に立つ情報 (解決済み原典) は落とさない。
    assert calls[0]["--origin-doc"] == DESCRIPTOR_SPEC_DOC, calls[0]
    # が、穴を残して「そのまま実行できる行」には見せない。
    assert _has_placeholder(calls[0]), calls[0]


@pytest.mark.parametrize("fixture_name,expected_runnable", [
    # gated な milestone (承認依頼が在る文面): 目的達成 + 思想の 2 行。
    # `gated` は目的達成行の **文面** を変えるだけで、binding の有無は変えない
    # (初版はここを 1 行と見積もって外した —— 本数を固定していたから気づけた)。
    ("fired", 2),
    # ゲートを経ない milestone 完了 (別の目的達成文面): こちらも 2 行。
    ("fired_ungated", 2),
    # data 定義 (descriptor) target: どちらの行も承認/収集の経路が引けないので、
    # **穴の無い行は 0 本** (= コピペさせない) が正しい状態。
    ("fired_descriptor", 0),
])
def test_every_placeholder_free_invocation_is_executable(fixture_name,
                                                         expected_runnable,
                                                         request, monkeypatch):
    """**種をまたぐ唯一の不変条件**: 穴の無い呼び出しは必ず実行して bundle になる。

    hook と Skill が「穴の無い行ならそのままコピーして実行してよい」と言う根拠が
    これ。milestone (穴なし = 実行できる) と descriptor target (穴あり = コピペ
    させない) の両方に同じ 1 本を当てるので、新しい target 種が増えても「綴ったのに
    落ちる」形を作れない。
    """
    message = request.getfixturevalue(fixture_name)
    # **種別で絞らない** — 契約は message が綴る全ての `/beacon-review-run` の行に
    # 掛かる (思想も目的達成も `--target` を取り、どちらも実行側は同じ resolver を
    # 通る)。philosophy だけ見ていたので目的達成行の穴が見逃されていた。
    runnable = [f for f in review_run_invocations(message)
                if not _has_placeholder(f)]
    # **本数を固定する**。「全ての穴の無い行は実行できる」は 0 本でも真になるので、
    # 期待本数を添えないと、綴るのをやめただけで緑になる形 (空集合で通る検査) が
    # 作れる。種ごとの期待本数は上の parametrize に書いてある。
    assert len(runnable) == expected_runnable, (
        f"穴の無い行の本数が期待と違う (期待 {expected_runnable} / 実際 "
        f"{len(runnable)}): {message}")
    for flags in runnable:
        bundle = run_invocation(flags, monkeypatch)
        assert bundle, flags


def test_the_three_link_field_conventions_all_resolve():
    """「SPEC は在るか」に答える 3 経路が、同じ 1 つの走査規約を共有すること。

    別々の規約を持つと「片方が在ると答え、もう片方が見つけられない」が起きる
    (= この finding の真因)。各 field 規約で紐づけた doc が、対応する kind で
    引けることを 1 本で突き合わせる。
    """
    import cmd_target

    docs = [
        {"doc_id": "d-ms", "scope": "spec", "milestone": "ms-1"},
        {"doc_id": "d-op", "scope": "spec", "operation": "op-1"},
        {"doc_id": "d-desc", "scope": "spec", "target": "rel-1"},
    ]

    class _S:
        def list_documents(self):
            return docs

    import contextlib

    @contextlib.contextmanager
    def _store():
        old_cs, old_ct = cs.get_store, getattr(cmd_target, "get_store", None)
        cs.get_store = lambda: _S()
        cmd_target.get_store = lambda: _S()
        try:
            yield
        finally:
            cs.get_store = old_cs
            if old_ct is not None:
                cmd_target.get_store = old_ct

    with _store():
        assert (cs._spec_doc_for_target("ms-1", "milestone") or {}).get("doc_id") == "d-ms"
        assert (cs._spec_doc_for_target("op-1", "operation") or {}).get("doc_id") == "d-op"
        assert (cs._spec_doc_for_target("rel-1", "release") or {}).get("doc_id") == "d-desc"
        # kind を知らない呼び出し元 (推測せず全部試す)
        for tid, want in (("ms-1", "d-ms"), ("op-1", "d-op"), ("rel-1", "d-desc")):
            assert (cs._spec_doc_for_target(tid, None) or {}).get("doc_id") == want, tid
        # 存在判定の 3 経路も同じ答えを出す
        assert cs._spec_exists_for_ms("ms-1") is True
        assert cs._spec_exists_for_op("op-1") is True
        assert cmd_target._spec_exists_for_descriptor_target("rel-1") is True
        # 種を跨いだ誤一致はしない (milestone の doc を descriptor として引かない)
        assert cs._spec_doc_for_target("ms-1", "release") is None
