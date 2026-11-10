"""レビューの促しが関門と同じ意味になることの試験 (ms-119 e-7359)。

何が困っていたか
----------------
関門 (``beacon pr approve`` / ``merge`` を止める面) は e-6859 で導出に替わり、
作業フォルダ横断の台帳と現 head を見る。しかし促し (``beacon trigger check`` /
session-start が出す「レビューしてください」) は **いまの作業フォルダのトリガー
ファイル** だけを見ていた。結果、**向きが逆の 2 つのズレ** が並んだ:

1. fork で ``review done`` を打つと関門は開くのに、親の促しには出続ける
   (fork は自分の作業フォルダのトリガーしか消せない)。
2. 親で一度実施した後にコミットを積むと、関門は赤に戻るのに促しは出ない
   (トリガーファイルは消えたまま)。

向きが逆なので、促しだけ見ている人は 2 の形で「もう要らない」と誤読する。
独立 AX レビュー (1 周目) は、この誤読が ``BEACON_REVIEW_GATE_CI`` 未設定の
リポジトリで実害になることを足した: ``gh pr merge`` / GitHub UI のマージボタンは
関門を経由しないので、促しの空リストを信じて直接マージ経路へ進むと、この課題が
塞いだはずの事故が **読み取り専用の確認経路側から** 再発する。

どう閉じたか
------------
促しの維持を **tick** に寄せ、両向きを同じ導出 (``pr_review_obligations``) で決める:

* ``_auto_fire_pr_open_review_triggers_for_open_prs`` — head が進んだら促しを戻す
  (マーカーが「どの差分まで見たか」を持つ)。
* ``_retire_satisfied_pr_review_nudges`` — もう止めていない種別の促しを下げる。

``trigger check`` ではなく tick でやるのは、check が安価な読み取りに保つ契約を持つため
(ms-98 e-2764: 以前 check が無条件に cloud を叩き、Skill の各 Step がそれを呼んで
429 の嵐になった)。head は anchor が既に取った ``gh pr list`` の結果を使い回す。

**非対称が正しい**: 関門は止める側に倒し、促しは **出す側** に倒す。促しは止める力を
持たないので、過剰に促して煩いのは実害が小さく、促さないのは事故に直結する。
"""

import json

import commands_shared as cs
import review_spine as rs

HEAD = "1" * 40
NEXT = "2" * 40


def _env(tmp_path, monkeypatch):
    tdir = tmp_path / "triggers"
    tdir.mkdir(exist_ok=True)
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(tdir))
    import cmd_trigger
    monkeypatch.setattr(cmd_trigger, "_get_triggers_dir", lambda: str(tdir),
                        raising=False)
    return tdir


def _seed_nudges(tdir, pr="838", repo="o/r"):
    for t in rs.required_pr_open_review_types():
        (tdir / cs.review_due_filename(t, pr, repo)).write_text(
            json.dumps({"review": t, "pr_repo": repo}), encoding="utf-8")
    return sorted(p.name for p in tdir.iterdir())


def _pr(head, pr="838", repo="o/r"):
    return [{"number": pr, "title": "t", "headRefOid": head,
             "url": f"https://github.com/{repo}/pull/{pr}"}]


def test_a_nudge_is_retired_once_it_no_longer_blocks(tmp_path, monkeypatch):
    """**e-7359 の本体**: もう関門を止めていない種別の促しが下がること。

    fork で実施した場合がこれ (関門は横断台帳で開くが、親の促しは残っていた)。
    """
    import cmd_trigger
    tdir = _env(tmp_path, monkeypatch)
    _seed_nudges(tdir)
    monkeypatch.setattr(cmd_trigger, "pr_review_obligations",
                        lambda num, repo="", **kw: {
                            "resolved": True, "passed": True, "owed": [],
                            "head_sha": HEAD, "pr_state": "OPEN"})
    cmd_trigger._retire_satisfied_pr_review_nudges(_pr(HEAD))
    assert list(tdir.iterdir()) == [], (
        "関門を止めていないのに促しが残った: "
        + str(sorted(p.name for p in tdir.iterdir())))


def test_a_nudge_stays_while_it_still_blocks(tmp_path, monkeypatch):
    """まだ止めている種別の促しは残すこと (境界: 全部消してしまわない)。"""
    import cmd_trigger
    tdir = _env(tmp_path, monkeypatch)
    _seed_nudges(tdir)
    still = rs.required_pr_open_review_types()[:1]
    monkeypatch.setattr(cmd_trigger, "pr_review_obligations",
                        lambda num, repo="", **kw: {
                            "resolved": True, "passed": False,
                            "owed": list(still), "head_sha": HEAD,
                            "pr_state": "OPEN"})
    cmd_trigger._retire_satisfied_pr_review_nudges(_pr(HEAD))
    left = sorted(p.name for p in tdir.iterdir())
    assert len(left) == len(still), left
    assert still[0] in left[0], left


def test_an_unknown_head_leaves_the_nudge_alone(tmp_path, monkeypatch):
    """head が取れない PR は触らないこと (促しは **出す側** に倒す)。

    関門は逆に止める側へ倒すが、促しは止める力が無いので非対称が正しい。
    """
    import cmd_trigger
    tdir = _env(tmp_path, monkeypatch)
    before = _seed_nudges(tdir)
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("head 不明なのに導出を呼んだ")

    monkeypatch.setattr(cmd_trigger, "pr_review_obligations", _boom)
    cmd_trigger._retire_satisfied_pr_review_nudges(_pr(""))   # headRefOid 空
    assert sorted(p.name for p in tdir.iterdir()) == before
    assert called["n"] == 0


def test_an_unresolved_obligation_leaves_the_nudge_alone(tmp_path, monkeypatch):
    """導出が成立しない (resolved=False) ときも触らないこと。"""
    import cmd_trigger
    tdir = _env(tmp_path, monkeypatch)
    before = _seed_nudges(tdir)
    monkeypatch.setattr(cmd_trigger, "pr_review_obligations",
                        lambda num, repo="", **kw: {
                            "resolved": False, "passed": False, "owed": [],
                            "head_sha": "", "pr_state": "OPEN"})
    cmd_trigger._retire_satisfied_pr_review_nudges(_pr(HEAD))
    assert sorted(p.name for p in tdir.iterdir()) == before


def test_another_repository_is_not_touched(tmp_path, monkeypatch):
    """別リポジトリの同番号の促しを消さないこと。"""
    import cmd_trigger
    tdir = _env(tmp_path, monkeypatch)
    t0 = rs.required_pr_open_review_types()[0]
    (tdir / cs.review_due_filename(t0, "838", "other/app")).write_text(
        json.dumps({"review": t0, "pr_repo": "other/app"}), encoding="utf-8")
    monkeypatch.setattr(cmd_trigger, "pr_review_obligations",
                        lambda num, repo="", **kw: {
                            "resolved": True, "passed": True, "owed": [],
                            "head_sha": HEAD, "pr_state": "OPEN"})
    cmd_trigger._retire_satisfied_pr_review_nudges(_pr(HEAD))
    assert len(list(tdir.iterdir())) == 1, "別リポジトリの促しを消した"


def test_the_tick_wires_both_directions(monkeypatch):
    """tick が **両向き** を呼ぶこと (片方だけだとズレが残る)。

    anchor (head が進んだら戻す) と retire (止めていないなら下げる) の両方が
    同じ ``gh pr list`` の結果を共有していることを構造で留める。
    """
    import inspect
    import cmd_trigger
    src = inspect.getsource(cmd_trigger.cmd_trigger_tick)
    assert "_auto_fire_pr_open_review_triggers_for_open_prs" in src
    assert "_retire_satisfied_pr_review_nudges" in src, (
        "促しを下げる側が tick に配線されていない")
    # head をもう一度聞かず、anchor の結果を使い回していること
    assert "_open_prs" in src, "gh の結果を共有していない (head を 2 回聞く形)"
