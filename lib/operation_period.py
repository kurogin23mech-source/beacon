"""Operation-fires の claim を「発火期 (period) バケット」に一般化する (ms-151 / e-5477)。

## なぜ

operation-fires claim (= 定期発火の二重駆動を防ぐ first-write-wins な発火権先取り) の
キーは元々 ``(project, op, date)`` の **日付粒度** だった。tick (Beacon 既定) と外部
self-drive の冗長構成で、単一裁定者 (= claim) が二重発火を構造的に防ぐ設計 (SPEC 方針3)。

ところが日付粒度だと、**同日内に複数回発火すべき cadence** (= 実行間隔。例: 5 分ごと /
毎時) の Operation で「その日の最初の 1 回」しか claim できず、同日の 2 回目以降が
既存 claim を見て弾かれる (= 1 日 1 回しか発火しない)。SPEC 問題 P3。

そこで claim キーを cadence に応じた **period バケット** に一般化する。同じ period に
入る発火は同じキー = 二重発火を弾き、別 period は別キー = それぞれ 1 回発火できる。

## 契約

``period_key(frequency, now) -> str``:

- **日以上の粒度** (``daily`` / ``weekdays`` / ``weekly`` / ``monthly`` / 未知 / 空) は
  **日付文字列 (YYYY-MM-DD)** を返す。これは日付粒度だった従来キーと **完全一致** する
  ので、既存 Operation の claim 挙動は一切変わらない (= backward compat)。
- **時間以下の粒度** はより細かいバケットを返す:
    - ``hourly``           → ``YYYY-MM-DDTHH``            (1 時間バケット)
    - ``<N>min`` / ``<N>m`` → ``YYYY-MM-DD-mN-<bucket>``  (N 分バケット)
    - ``<N>sec`` / ``<N>s`` → ``YYYY-MM-DD-sN-<bucket>``  (N 秒バケット、循環 Operation 用)

``now`` は timezone-aware な UTC datetime を渡す (server clock)。バケット境界は全 caller
が server 時刻で合意する (= timezone 混在マシンでもズレない、従来の date と同じ思想)。

この module は **pure** (I/O 無し)。store 側 (claim_operation_fire_if_new) はこの文字列を
不透明なバケットキーとして doc_id に使うだけなので、backend は一切変更しない。
"""
from __future__ import annotations

import re

# 「日以上の粒度」= 日付バケットで足りる cadence。ここに載る frequency と、未知 / 空は
# すべて日付文字列を返す (= 従来キーと一致)。
_DAY_OR_COARSER = {"", "daily", "weekdays", "weekly", "monthly"}

_MIN_RE = re.compile(r"^(\d+)\s*(?:m|min|mins|minute|minutes)$")
_SEC_RE = re.compile(r"^(\d+)\s*(?:s|sec|secs|second|seconds)$")


def period_key(frequency: str, now) -> str:
    """cadence (frequency) と現在時刻から、claim の period バケット文字列を返す。

    詳細な対応表は module docstring 参照。日以上の粒度は必ず ``YYYY-MM-DD`` を返し、
    従来の日付粒度キーと一致する (backward compat)。
    """
    date_str = now.strftime("%Y-%m-%d")
    freq = (frequency or "").strip().lower()

    if freq in _DAY_OR_COARSER:
        return date_str

    if freq == "hourly":
        return now.strftime("%Y-%m-%dT%H")

    m = _MIN_RE.match(freq)
    if m:
        n = int(m.group(1))
        if n > 0:
            bucket = (now.hour * 60 + now.minute) // n
            return f"{date_str}-m{n}-{bucket}"

    s = _SEC_RE.match(freq)
    if s:
        n = int(s.group(1))
        if n > 0:
            secs = now.hour * 3600 + now.minute * 60 + now.second
            bucket = secs // n
            return f"{date_str}-s{n}-{bucket}"

    # 未知の frequency は安全側 = 日付粒度 (= 従来挙動を壊さない)。
    return date_str
