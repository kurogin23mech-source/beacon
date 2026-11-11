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
