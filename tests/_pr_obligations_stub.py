"""関門の導出結果 (``pr_review_obligations`` の返り値) を試験で作る唯一の場所
(ms-119 e-6859、独立保守性レビュー)。

**なぜ共有するのか。** 初版は 3 つの試験ファイルがこの返り値の形を **それぞれ別に
手書き** していた (しかもキー集合が微妙に違い、1 箇所だけ ``pr_state`` を持ち、他の
2 箇所は ``head_known`` を持っていた)。独立レビューが反転実験で実測した結果:
``derive_pr_review_obligations`` の ``owed`` キー名を変えると本物の経路を通る試験は
12 件が正しく赤くなるのに、**手書き fake を使う試験群は無傷で緑** だった。つまり
fake 経由の試験には、本物の形が変わったことを検知する経路が無かった ——
このPRが主題にしている「偽の緑」と同じ形の弱点を、このPRが新設した試験の中に
残していたことになる。

**どう閉じたか。** 形を手書きしない。``review_spine.derive_pr_review_obligations``
(= 本物の純粋関数) を実際に呼んで土台を作り、その上に試験が必要な差分だけを重ねる。
本物の返り値にキーが増減すれば、ここを通る全ての fake が自動で追従する。

``tests/_gh_head_stub.py`` (gh の head 問い合わせの差し替え) と対の関係にある ——
あちらは「外の世界」、こちらは「導出の結果」。
"""

import review_spine

#: 試験が特に指定しないときの head sha。本物らしい形 (16 進 40 桁) にしておく
#: —— `is_commit_sha_shaped` を通る値でないと、形を検査する経路の試験が書けない。
DEFAULT_HEAD = "5" * 40


def obligations(*, head_sha=DEFAULT_HEAD, owed=None, satisfied=None,
                resolved=True, pr_state="OPEN", head_error="",
                pr_number="", pr_repo="", **overrides):
    """``pr_review_obligations`` と同じ形の dict を返す。

    形の土台は **本物の純粋関数から採る** (手書きしない)。``passed`` も本物の
    判定関数 (``commands_shared._review_gate_passed``) に決めさせる。

    Args:
        head_sha: いまの差分。``owed`` / ``satisfied`` を省略したときは、この sha に
            対して **全必須種別が実施済み** の状態を作る (= 関門が開く形。関門を
            主題にしない試験が主題まで到達するための既定)。
        owed: まだ要る種別を明示したいとき。``satisfied`` は自動で「残り」になり、
            ``owed`` 自体は **本物の純粋関数に導出させる** (手で上書きしない ——
            上書きすると本物のキー名が変わっても追従せず、偽の緑に戻る)。
            ``resolved=False`` と併用した場合は head が不明なので何も差し引けず、
            本物は全必須種別を ``owed`` にする (そちらが正しい)。
        satisfied: 実施済みを明示したいとき (``owed`` と併用可)。
        resolved / pr_state / head_error: 組み立て層 (``pr_review_obligations``) が
            足すフィールド。純粋関数は持たないのでここで載せる。
        **overrides: 上のどれでもないキーを上書き / 追加する (試験が本物に無いキーを
            足したくなったら、それは本物側に足すべき兆候)。
    """
    required = review_spine.required_pr_open_review_types()
    if owed is None and satisfied is None:
        _satisfied, _owed = list(required), []
    elif owed is None:
        _satisfied = [t for t in required if t in (satisfied or [])]
        _owed = [t for t in required if t not in _satisfied]
    else:
        _owed = [t for t in required if t in owed]
        _satisfied = ([t for t in required if t in (satisfied or [])]
                      if satisfied is not None
                      else [t for t in required if t not in _owed])

    # 本物の純粋関数で土台を作る: 「実施済みにしたい種別は、この head に対して
    # pin 済みの記録が在る」という入力から導出させる。こうすると、純粋関数側で
    # キーが増減しても、ここを通る fake が黙って古い形を返し続けることがない。
    base = review_spine.derive_pr_review_obligations(
        required=required,
        performed=[{"review_type": t, "reviewed_sha": head_sha,
                    "sha_pinned": True} for t in _satisfied],
        skipped=[],
        head_sha=head_sha if resolved else "",
    )
    base.update({
        "head_error": head_error,
        "pr_state": pr_state,
        "resolved": bool(resolved),
        "provenance": [{"review_type": t, "reviewed_sha": head_sha,
                        "pr_repo": pr_repo, "recorded_by": "stub",
                        "recorded_at": "2026-11-05T00:00:00Z",
                        "recorded_in_cwd": "/stub", "shared": True}
                       for t in _satisfied],
        "pr_number": str(pr_number or ""),
        "pr_repo": str(pr_repo or ""),
    })
    # **``owed`` を手で上書きしない。** 上書きすると、本物の純粋関数が ``owed`` を
    # 別の名前に変えても、この factory が ``owed`` を足し直してしまい、fake 経由の
    # 試験が再び偽の緑になる (= この finding が閉じようとした穴が factory 側から
    # 開く)。``owed`` は「実施記録が無い種別」として本物に導出させる —— 上の
    # ``performed`` の組み立てがその入力になっている。
    base.update(overrides)
    # ``passed`` (= 関門を通るか) も **本物の判定関数に決めさせる** (ms-119 e-6859、
    # 独立 AX レビュー 2 周目で判定を 1 箇所に畳んだ)。ここで手書きすると、
    # 判定の規則が変わっても fake は古い答えを返し続け、fake 経由の試験だけが
    # 偽の緑になる —— この module が閉じようとしている穴そのもの。
    # ``overrides`` で明示されていればそれを尊重する (「通るはずなのに通らない」
    # 側を測りたい試験のため)。
    if "passed" not in overrides:
        import commands_shared
        base["passed"] = commands_shared._review_gate_passed(base)
    return base


def install(monkeypatch, module, **kwargs):
    """``module.pr_review_obligations`` を :func:`obligations` の返り値に差し替える。

    ``module`` は呼び出し側の名前空間 (``cmd_pr`` / ``commands``) —— それぞれが
    ``from commands_shared import ...`` で独立の名前を束縛しているので、
    ``commands_shared`` 側を差し替えても効かない (tests の e-4320 規則)。

    返り値は実際に渡された dict (試験が中身を確かめたいときのため)。
    """
    ob = obligations(**kwargs)
    monkeypatch.setattr(module, "pr_review_obligations",
                        lambda pr_number, pr_repo="", **kw: ob, raising=False)
    return ob


def install_satisfied_at(monkeypatch, module, sha):
    """「この sha に対してだけ全必須種別が充足」を ``module`` に見せる。

    ``pr_review_obligations`` は呼び出し側が ``head_sha`` を渡せるので、
    「渡された sha が一致したら充足、違えば全部未実施」を返す fake になる。
    印を押す条件 (「その sha に対して全必須種別が充足している」) を測る試験が使う。

    ms-119 e-6859 (独立保守性レビュー 2 周目): これは 2 つの試験ファイルに
    docstring ごと逐語で重複していた。どちらも「手書きすると偽の緑になる」という
    理由でこの module の factory に委譲していたのに、**どの head_sha なら充足を
    返すかを決める分岐そのもの** は委譲せず手書きのまま 2 箇所に在った ——
    この module の docstring が名指しした失敗モードと同じ構造を、その対策コードの
    隣に作っていた形。分岐も 1 箇所にする。
    """
    import review_spine

    def fake(pr_number, pr_repo="", *, head_sha=None):
        if head_sha == sha:
            return obligations(head_sha=sha)
        return obligations(head_sha=head_sha or "", resolved=bool(head_sha),
                           owed=review_spine.required_pr_open_review_types(),
                           satisfied=[])

    monkeypatch.setattr(module, "pr_review_obligations", fake)
    return fake
