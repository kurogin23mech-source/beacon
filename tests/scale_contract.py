"""規模の次元をテストするための土台 (e-5370)。

2026-08-20 の本番停止は「件数が少ないと無害、多いと致命的」なバグ 2 件で起きた。
契約 (何を返すか) は全部テストされていたのに、規模 (どれだけ読むか) は一度も
テストされていなかった。

  * list_bus_events が project の全イベントを Python へ読み込んでいた
    (本番 98,943 件 / 72MB を毎秒 json.loads していた)
  * 再送の重複チェックが最古 100 件しか見ていなかった
    (N=2 のテストでは最古と最新が一致するので永久に検出できない)

ここで測るのは **DB から Python 側へ何行渡ったか**。絞り込みを SQL に押し下げて
いれば、表が何行あっても渡るのは limit 件まで。Python 側で絞る実装は全行を
受け取るので、同じ物差しで一発で分かる。

使い方 (既存の契約テストの隣に置ける):

    from scale_contract import measure_rows_into_python

    def test_一万件でも読み込む量が増えない(monkeypatch):
        rows = fake_rows(100_000)
        result, stat = measure_rows_into_python(
            monkeypatch, mysql_client,
            lambda: mysql_client.list_bus_events("p1", since="...", limit=100),
            table_rows=rows)
        assert stat["rows_into_python"] <= 100

新しく list 系を足すときは、契約テストと一緒にこれを 1 本書く。
"""
import json
import re


def fake_rows(n: int, *, sk_prefix: str = "ev", **fields):
    """(sk, data) を持つ偽の行を n 件作る。data は JSON 文字列 (本物と同じ形)。"""
    out = []
    for i in range(n):
        payload = {"seq": i}
        payload.update({k: (v(i) if callable(v) else v) for k, v in fields.items()})
        out.append({"sk": f"{sk_prefix}-{i:08d}", "pk": "p1",
                    "data": json.dumps(payload, ensure_ascii=False)})
    return out


_LIMIT_RE = re.compile(r"\bLIMIT\s+(%s|\d+)", re.IGNORECASE)


class _Cursor:
    """SQL を記録し、LIMIT があればそれだけ返す偽カーソル。

    完全な SQL エンジンではない (WHERE は解釈しない)。目的は『押し下げたか』の
    測定であって問い合わせの再現ではないため、LIMIT だけを尊重すれば足りる。
    WHERE を無視するぶん測定は **多め** に出るので、通れば本物でも通る。
    """

    def __init__(self, table_rows, stat):
        self._table = table_rows
        self._stat = stat
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=()):
        self._stat["queries"].append(sql)
        m = _LIMIT_RE.search(sql)
        n = None
        if m:
            tok = m.group(1)
            if tok == "%s":
                n = int(params[-1]) if params else None
            else:
                n = int(tok)
        self._result = self._table if n is None else self._table[:n]
        self._stat["rows_into_python"] += len(self._result)

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _Conn:
    def __init__(self, cursor):
        self._c = cursor

    def cursor(self):
        return self._c


def measure_rows_into_python(monkeypatch, mysql_module, call, *, table_rows):
    """``call()`` を実行し、DB から Python へ渡った行数を数えて返す。"""
    stat = {"rows_into_python": 0, "queries": []}
    monkeypatch.setattr(mysql_module, "_conn", lambda: _Conn(_Cursor(table_rows, stat)))
    result = call()
    return result, stat
