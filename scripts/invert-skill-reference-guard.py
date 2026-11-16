#!/usr/bin/env python3
"""Inversion harness for check-skill-reference-drift.py (ms-185 e-7079).

A guard that is green is *trusted*, so a guard that cannot go red is worse than
no guard at all — it converts "nobody checked" into "someone checked and it was
fine". This repo has shipped that mistake more than once (a substring match that
passed on help text, an allowlist that silently covered its own subject), so a
new guard does not count as landed until something has broken it on purpose and
watched it fail.

This script injects each defect the guard claims to catch, runs the guard, and
restores the file immediately. It asserts RED for every defect and GREEN for the
untouched tree. Any case that stays green is a hole in the guard, not a pass.

## なぜ共有部品 (``invert_harness``) を通すのか

最初の版はこの mutate/restore を自前で書き、``python3 -B`` を対策として
挙げていた。**それは誤りで、共有部品の docstring が明示的に否定している**:
Apple 同梱の Python は ``~/Library/Caches/com.apple.python/`` に ``.pyc`` を
書き、``-B`` は **書き込みを止めるだけで読み込みは止めない**。このハーネスは
変異の 2 件で **ガード自身の .py** を書き換えて戻すので、変異版の ``.pyc`` が
残れば次の実行が旧バイトコードから答える — つまり **偽の緑** が出る。偽の安全
を測るための道具が偽の安全を作るのでは、測った意味が消える。

もう 1 つ自前実装が落としていたのが排他ロックである。このハーネスは
``lib/`` の共有ファイルを in-place で書き換えるので、2 本が同時に走ると
互いの変異を読み、復元が相手の書き込みを巻き戻す (lost update)。このリポジトリは
fork 並走が常態なので、「同時に走らせない」という**お願い**では守られない。

共有部品はこの 2 つ (と引数の裁き、環境を汚さない保証) を 1 箇所に持っている。
写すと片方だけ直って drift するので (実際に e-7065 と e-7077 で起きた)、
import して使う。pytest を走らせる姉妹と違い本ハーネスの対象は **スクリプト**
なので、``run_pytest`` / ``classify_pytest_result`` は使わず、終了コードと
メッセージを自前で見る (ガードは終了コードを契約として持っているため、
pytest の 1 と 2〜5 を分ける必要がない)。

使い方:  python3 scripts/invert-skill-reference-guard.py   (引数は取らない)

Exit 0 = the guard is load-bearing. Exit 1 = at least one defect slipped past.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
GUARD = REPO / "scripts" / "check-skill-reference-drift.py"


def run_guard() -> tuple[int, str]:
    """ガードを隔離環境で走らせる。

    ``ih.isolated_env()`` がバイトコードキャッシュを実行ごとの一時ディレクトリ
    へ逃がす (穴 1-a)。``-B`` も併せて渡すが、それは書き込み抑止の二重化で
    あって対策の本体ではない。
    """
    r = subprocess.run(
        [sys.executable, "-B", str(GUARD), "--strict"],
        cwd=REPO, capture_output=True, text=True, env=ih.isolated_env(),
    )
    return r.returncode, (r.stdout + r.stderr)


# The fake Skill names are assembled at runtime. Writing them as literals
# would put "/beacon-<missing>" into THIS file, and the guard scans scripts/ —
# so the harness would trip its own subject and the baseline could never be
# green. Composing them keeps the harness out of its own scan without needing
# an exemption (an exemption here would be the first thing to rot).
_S = "/" + "beacon" + "-"
MISSING_A = _S + "this-skill-does-not-exist"
MISSING_B = _S + "some-other-missing-skill"
# A REAL alias, declared by beacon-session-start in its frontmatter triggers.
# Composed for the same reason as the fake names: this file is scanned, and the
# alias-scoping case below would otherwise make the harness trip its own
# subject. (Writing it as a literal is how the first draft of case 9 turned the
# baseline red — and the guard also caught that the name invented for the
# comment did not exist at all, which is the guard doing its job on its own
# harness.)
REAL_ALIAS = _S + "start"


class _Edits:
    """Apply one or more text edits together, restore them all, no matter what.

    Multi-edit exists for ONE reason, and it is worth stating because a
    single-edit harness quietly produces a worthless case: an exclusion rule
    can only be shown to be load-bearing by removing it **while something that
    needs it is present**. The markdown-link exclusion has nothing in this tree
    that exercises it (measured: zero ``](/beacon-…)`` occurrences), so a
    one-edit "delete the exclusion" mutation cannot go red — and a case that
    cannot go red is as harmful as no case, because the summary line counts it
    as a pass. So the case plants a link AND removes the exclusion in one step.
    """

    def __init__(self, edits):
        self.edits = list(edits)      # [(path, old, new), ...]

    def apply(self):
        originals = {}
        for path, old, new in self.edits:
            text = path.read_text(encoding="utf-8")
            if old not in text:
                self.restore(originals)
                return None, f"anchor not found in {path.name}: {old[:60]!r}"
            originals[path] = text
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            # 編集のたびにキャッシュ鍵を変える (穴 1-b)。対象がガード自身の .py
            # である case では、これが無いと旧バイトコードから答えが返る。
            ih.invalidate_bytecode_cache(path, REPO)
        return originals, ""

    @staticmethod
    def restore(originals):
        if originals:
            ih.restore(originals, REPO)


class ExemptionHolds(_Edits):
    """Assert the guard stays GREEN — i.e. a legitimate shape is NOT reported.

    The sibling of a mutation. A false positive is not a softer failure than a
    miss: a guard that cries wolf gets switched off, and then it catches
    nothing at all. So the exclusions get their own case type instead of being
    taken on trust.
    """

    def __init__(self, label: str, edits):
        super().__init__(edits)
        self.label = label

    def check(self) -> tuple[bool, str]:
        originals, err = self.apply()
        if originals is None:
            return False, err
        try:
            rc, out = run_guard()
        finally:
            self.restore(originals)
        if rc != 0:
            return False, ("guard went RED on a shape it is supposed to exempt "
                           "(false positive):\n         " + out.strip()[:300])
        return True, "GREEN, the legitimate shape is not reported"


class Mutation(_Edits):
    """Apply edit(s), run the guard, restore no matter what. Expect RED."""

    def __init__(self, label: str, path, old: str = "", new: str = "",
                 expect_in_output: str = "", edits=None):
        super().__init__(edits if edits is not None else [(path, old, new)])
        self.label = label
        self.expect_in_output = expect_in_output

    def check(self) -> tuple[bool, str]:
        originals, err = self.apply()
        if originals is None:
            return False, err
        try:
            rc, out = run_guard()
        finally:
            self.restore(originals)

        # 赤の理由を 2 段で見る (穴 3)。
        #
        # 1 段目は共有部品の分類器。終了コードの意味はこのガードでも pytest と
        # 同じ向きに読める: 1 = 指摘あり (= 狙っていた赤)、2 = argparse の使い方
        # エラー (= 壊し方が下手で起動に失敗した) なので INVALID。判定を手元に
        # 写すと 1 本だけ直って姉妹が旧判定のまま並ぶため、ここは共有に委ねる。
        tail = out.strip().split("\n")[-1] if out.strip() else ""
        caught, shown = ih.classify_pytest_result(
            SimpleNamespace(returncode=rc), tail)
        if not caught:
            if rc == 0:
                return False, "guard stayed GREEN with the defect injected"
            return False, shown

        # 2 段目はメッセージの検査。**スクリプトを対象にする本ハーネスでは
        # 終了コードだけでは足りない**: 変異がガードの構文を壊すと python は
        # rc=1 で traceback を出すので、1 段目はそれを「捕まえた」と読む
        # (pytest なら収集失敗が rc=2 に分かれるが、素のスクリプトにはその
        # 区別が無い)。狙った指摘の名前が出力に在ることまで要求して初めて、
        # 「壊し方が下手なだけ」と「ガードが効いた」が分かれる。
        if self.expect_in_output not in out:
            return False, (f"went red, but the message never mentions "
                           f"{self.expect_in_output!r} — the reader would not "
                           f"know what to fix (壊し方が下手なだけの赤かもしれない)")
        return True, "RED, and the message names the defect"


MUTATIONS = [
    # 1. The actual defect of PR #827: production code tells an AI to run a
    #    Skill that does not exist. Injected into a file that has no Trek
    #    references of its own, so a pass cannot come from the allowlist.
    Mutation(
        "a production file names a non-existent Skill",
        REPO / "lib" / "tick_health.py",
        '"""',
        '"""Run ' + MISSING_A + ' to continue.\n\n',
        MISSING_A.lstrip("/"),
    ),
    # 2. The same defect inside a file that IS allowlisted — the allowlist is
    #    keyed by (file, skill), so it must not blanket-excuse the file.
    # ms-185 e-7070: 当て先を lib/trek.py から移した (本体ごと消えたため)。
    # 必要なのは「免除行を持つファイル」であって trek であることではないので、
    # 生き残っている免除行の file (= beacon_cli/skills_helpers/dm_reply.py、
    # beacon-dm-reply で免除されている) に同じ形の欠陥を注いで測る。
    Mutation(
        "an allowlisted file names a DIFFERENT non-existent Skill",
        REPO / "beacon_cli" / "skills_helpers" / "dm_reply.py",
        '"""',
        '"""See ' + MISSING_B + ' for details.\n\n',
        MISSING_B.lstrip("/"),
    ),
    # 3. An allowlist row outliving its subject. Renaming the row's Skill makes
    #    it match nothing, which is exactly how an exemption silently re-opens
    #    the hole it covered.
    Mutation(
        "an allowlist row no longer matches anything (stale exemption)",
        GUARD,
        '("beacon_cli/skills_helpers/dm_reply.py", "beacon-dm-reply"):',
        '("beacon_cli/skills_helpers/dm_reply.py", "beacon-dm-reply-renamed-away"):',
        "no longer match anything",
    ),
    # 5. The scan reaching the Skill dirs. A Skill telling an AI to run a
    #    deleted Skill is the most common shape of this defect, and the first
    #    version of the guard did not look there at all (an injection into all
    #    three copies left the whole strict-guard suite green). Pinned here so
    #    the dirs cannot quietly fall back out of SCAN_DIRS.
    Mutation(
        "a Skill names a non-existent Skill (Skill dirs are in scope)",
        REPO / "skills" / "beacon-task.md",
        "## 制約",
        "## 制約\n\n完了後は " + MISSING_A + " を起動する。\n",
        MISSING_A.lstrip("/"),
    ),
    # 6. The alias model must not degrade into "anything /beacon-* is fine".
    #    Aliases resolve because a Skill DECLARES them in its frontmatter; a
    #    name nobody declares must still fail. Injected in the same frontmatter
    #    shape as a real alias, so only the declaration check separates them.
    Mutation(
        "an undeclared alias-shaped name in a Skill body still fails",
        REPO / "skills" / "beacon-note.md",
        "## 制約",
        "## 制約\n\nサブコマンド hint (" + MISSING_B + ") で起動された場合。\n",
        MISSING_B.lstrip("/"),
    ),
    # 7. The bare-name shape with an imperative — the exact spelling PR #827
    #    used to go quiet: the earlier review fix deleted the leading slash from
    #    five imperative comments and the guard stopped seeing them. If this
    #    stays green, "change the spelling" is a way past the guard again.
    Mutation(
        "a bare name on an imperative line (slash deleted)",
        REPO / "lib" / "tick_health.py",
        '"""',
        '"""launch: ' + MISSING_A.lstrip("/") + ' now.\n\n',
        MISSING_A.lstrip("/"),
    ),
    # 8. The tool-call shape an AI harness actually emits. No slash anywhere.
    Mutation(
        "a tool-call shaped reference (skill=\"...\")",
        REPO / "lib" / "tick_health.py",
        '"""',
        '"""Run Skill(skill="' + MISSING_B.lstrip("/") + '") here.\n\n',
        MISSING_B.lstrip("/"),
    ),
    # 9. Alias scoping. An alias answers for its OWN Skill's files; used
    #    elsewhere it must be reported. The flat global namespace this replaced
    #    meant one line added to any unrelated Skill excused a dangling
    #    reference anywhere. Injected as a REAL alias (declared by
    #    beacon-session-start) referenced from an unrelated lib file, so the
    #    only thing that can make it red is the scoping rule.
    Mutation(
        "another Skill's alias used outside its own files",
        REPO / "lib" / "tick_health.py",
        '"""',
        '"""See ' + REAL_ALIAS + ' for details.\n\n',
        "outside the Skill that declared it",
    ),
    # 10. The markdown-link exclusion, shown to be load-bearing. Two edits in
    #     one step, because the tree has nothing that exercises the exclusion
    #     (measured: zero ``](/beacon-…)`` occurrences), so deleting the rule on
    #     its own cannot turn anything red — see _Edits for why that matters.
    #     Plant a link to a non-existent Skill, delete the exclusion, require
    #     RED. The paired ExemptionHolds case below plants the same link with
    #     the exclusion intact and requires GREEN; the two together say the rule
    #     exempts exactly the link shape and nothing more.
    Mutation(
        "the markdown-link exclusion is deleted while a link is present",
        None,
        expect_in_output=MISSING_A.lstrip("/"),
        edits=[
            (REPO / "skills" / "beacon-task.md", "## 制約",
             "## 制約\n\n詳細は [FAQ](" + MISSING_A + ") を参照。\n"),
            (GUARD,
             'if (m.start(1), m.group(1)) in link_targets:',
             'if False:'),
        ],
    ),
    # 4. Loosening the detector itself: if the token-boundary rule is dropped,
    #    every URL and path matches and the report floods. A flooded report is
    #    not a working guard either, so the harness pins the boundary rule by
    #    requiring that removing it changes the verdict.
    Mutation(
        "the token-boundary rule is removed from the detector",
        GUARD,
        r'''    r"(?:(?<=^)|(?<=[\s`'\"(\[*=,:]))"''',
        r'''    r""''',
        "Drift detected",
    ),
]


# 除外が「効いている」ことも試験する。見逃しだけを測って誤検知を測らないと、
# 緩い除外が静かに入り込む経路が開いたままになる。
EXEMPTIONS = [
    ExemptionHolds(
        "a markdown link to a non-existent Skill is NOT reported",
        [(REPO / "skills" / "beacon-task.md", "## 制約",
          "## 制約\n\n詳細は [FAQ](" + MISSING_A + ") を参照。\n")],
    ),
    ExemptionHolds(
        "a quoted absolute path starting with the prefix is NOT reported",
        [(REPO / "skills" / "beacon-task.md", "## 制約",
          "## 制約\n\n設定は `" + MISSING_A + "/settings.json` に置く。\n")],
    ),
    ExemptionHolds(
        "a Skill's own alias used inside its own file is NOT reported",
        [(REPO / "skills" / "beacon-session-start.md", "## 制約",
          "## 制約\n\n" + REAL_ALIAS + " で起動された場合も同じ。\n")],
    ),
]


def main() -> int:
    rc, out = run_guard()
    if rc != 0:
        print("PRECONDITION FAILED: the guard is already red on an untouched "
              "tree, so nothing below can be concluded.\n")
        print(out)
        return 1
    total = len(MUTATIONS) + len(EXEMPTIONS)
    print(f"baseline: GREEN ({len(MUTATIONS)} defects to inject, "
          f"{len(EXEMPTIONS)} exemptions to confirm)\n")

    failures = 0
    print("  defects must go RED:")
    for m in MUTATIONS:
        ok, detail = m.check()
        mark = "PASS" if ok else "HOLE"
        if not ok:
            failures += 1
        print(f"  [{mark}] {m.label}\n         {detail}")
    print()
    print("  exemptions must stay GREEN:")
    for e in EXEMPTIONS:
        ok, detail = e.check()
        mark = "PASS" if ok else "WOLF"
        if not ok:
            failures += 1
        print(f"  [{mark}] {e.label}\n         {detail}")

    rc_after, _ = run_guard()
    print()
    if rc_after != 0:
        print("RESTORE FAILED: the tree is red after the run — a mutation was "
              "not rolled back. Check `git diff`.")
        return 1
    print("tree restored: GREEN")

    if failures:
        print(f"\n{failures} of {total} cases failed. Either a defect slipped "
              "past the guard, or the guard reported a shape it must exempt — "
              "both make it a worse instrument than its green suggests.")
        return 1
    print(f"\nall {total} cases held ({len(MUTATIONS)} defects caught, "
          f"{len(EXEMPTIONS)} exemptions confirmed); the guard is load-bearing.")
    return 0


if __name__ == "__main__":
    # 入口は共有部品 1 箇所 (穴 4): 未知の引数は何も書かずに拒否し、repo 単位の
    # 排他ロックを取ってから本体を呼ぶ。ロックが取れなければ 1 行断って終わる。
    sys.exit(ih.guarded_main(main, sys.argv, __doc__, REPO))
