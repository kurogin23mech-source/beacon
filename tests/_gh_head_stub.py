"""gh の「PR の head sha / 状態」の問いを試験で差し替える helper (ms-119 e-6859).

独立レビューの関門は「**現在の差分** に対して全必須種別が実施済みか」を問うので、
毎回 GitHub に head sha を聞く。素通しだと試験が外部状態に依存するため
``commands_shared.resolve_pr_head`` は試験中 (= BEACON_TEST_MODE /
PYTEST_CURRENT_TEST) に **既定の subprocess adapter のままなら gh を叩かず
「確かめられなかった」を返す**。方針どおり関門は閉じたままになる。

関門を開ける / 特定の head を見せる試験はここで宣言した port の面を差し替える
(tests の e-4320 規則が canonical と定めている stub 位置。adapter を差し替えて
あれば上の guard は素通りする)。
"""

import gh_port


class FakeGhHead:
    """``pr_head`` だけを答える最小の adapter。

    ``heads`` は ``{PR番号(str): sha}`` か ``{(PR番号, repo): sha}``。
    与えられていない PR を聞かれたら RuntimeError (= gh が答えられなかったのと
    同じ形) を投げるので、「聞かれると思っていなかった呼び出し」が緑にならない。
    """

    def __init__(self, heads, state="OPEN"):
        self.heads = dict(heads or {})
        self.state = state
        self.calls = []

    def pr_head(self, ref, repo=""):
        self.calls.append((str(ref), str(repo or "")))
        key = (str(ref), str(repo or ""))
        if key in self.heads:
            sha = self.heads[key]
        elif str(ref) in self.heads:
            sha = self.heads[str(ref)]
        else:
            raise RuntimeError(
                f"FakeGhHead: PR {ref!r} (repo={repo!r}) の head を用意していません")
        return {"headRefOid": sha, "state": self.state,
                "url": f"https://github.com/{repo or 'o/r'}/pull/{ref}"}

    # 他の port 動詞は試験で使わない。呼ばれたら黙らずに落とす。
    def __getattr__(self, name):
        raise AttributeError(
            f"FakeGhHead は {name} を実装していません (pr_head だけの stub です)")


def install(monkeypatch, heads, state="OPEN"):
    """``gh_port`` の adapter を :class:`FakeGhHead` に差し替えて返す。

    ``monkeypatch`` 経由なので試験の終わりに元の adapter へ戻る
    (``gh_port.set_adapter`` を直に呼ぶと後続の試験に漏れる)。
    """
    fake = FakeGhHead(heads, state=state)
    monkeypatch.setattr(gh_port, "_adapter", fake, raising=False)
    return fake
