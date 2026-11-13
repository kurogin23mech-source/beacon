"""CLI ログインの合言葉 (ペアリングコード) の表 — サーバーの全台で共有する (ms-187 e-7488)。

``beacon auth login`` と盤ビューワーの「クラウドから開く」は、次の 3 段で成り立つ:

1. CLI が ``POST /api/auth/cli-start`` で合言葉をもらう (**start**)
2. 人がブラウザでサインインし、Authorize を押す → ``POST /api/auth/cli-approve`` (**approve**)
3. CLI が ``GET /api/auth/cli-poll`` を 3 秒ごとに叩き、承認済みなら CLI 用トークンを受け取る (**poll**)

この 3 つは **別々のリクエストで、別々のサーバー台に届きうる**。本番 (Cloud Run) は
``--max-instances=3`` で最大 3 台に増える。以前は合言葉の表を各台のメモリ (dict) に
置いていたため、合言葉を発行した台と承認が届いた台が違うと、届いた台は合言葉を
知らずに 404 (「Invalid or expired code」) を返し、承認が 3 回に 2 回ほど失敗していた
(2026-11-13 に、同じ合言葉への問い合わせが途中から 404 に変わることを実測で確認)。

そこで表を **全台から見える保存先** に置く。保存先は ``store_router`` の汎用口
``master_get`` / ``master_put`` (ms-111 e-3620) で、本番は Firestore、ほかに
DynamoDB / MySQL でも同じ形で動く。Redis は本番の Cloud Run に無く (VPS 経路専用・
不通なら黙って素通しする設計)、Cloud Run のセッション固定は CLI とブラウザが別の
クライアントなので効かないため、どちらも採らない。

安全性は以前と同じに保つ:

- 合言葉は 5 分で失効する (``TTL_SECONDS``)。
- 受け取り (poll) は 1 回きり。トークンを渡す **前に** 「受け取り済み」へ書き換える
  ので、書き換えに失敗したら渡さない (= 失敗しても二重発行の側に倒れない)。
- 承認した人のブラウザのトークンは保存しない (以前のメモリ表は保存していたが、
  一度も読まれていなかった)。保存するのは、誰が承認したか (``sub`` / ``email``) だけ。

**残る弱点 (承知の上)**: ``master_put`` は「条件付き書き込み」ではないので、同じ
合言葉への poll が **ほぼ同時に 2 台へ** 届くと、両方がトークンを渡しうる。正規の
CLI は 3 秒おきに 1 本ずつしか問い合わせないので起きず、起きるのは合言葉を知って
いる者が同時に叩いた場合だけである。合言葉を知られていれば、そもそも 1 本目で
トークンを取られるので、二重発行によって悪化する範囲は無い。
"""
from __future__ import annotations

import secrets
import time
from typing import Callable, Protocol

ENTITY = "cli_pairing"
TTL_SECONDS = 300

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_CONSUMED = "consumed"


class PairingError(Exception):
    """承認や受け取りを断るときの理由。``status_code`` はそのまま HTTP の応答に使う。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class PairingPersistence(Protocol):
    """合言葉 1 件を読み書きする口。全サーバー台が同じ中身を見ることが前提。"""

    def get(self, code: str) -> dict | None: ...

    def put(self, code: str, data: dict) -> None: ...


class StorePersistence:
    """``store_router`` の ``master_get`` / ``master_put`` に載せる (本番の配線)。

    ``store`` は backend 選択済みの ``store_router`` モジュール (または同じ 2 関数を
    持つもの)。Firestore では top-level collection ``cli_pairing`` (本番以外は
    ``cli_pairing-dev``)、DynamoDB / MySQL では ``cli_pairing`` テーブルに入る。
    """

    def __init__(self, store):
        for name in ("master_get", "master_put"):
            if not callable(getattr(store, name, None)):
                raise TypeError(
                    f"cli_pairing.StorePersistence: store に {name} がありません "
                    f"(store_router を渡してください)"
                )
        self._store = store

    def get(self, code: str) -> dict | None:
        return self._store.master_get(ENTITY, code)

    def put(self, code: str, data: dict) -> None:
        self._store.master_put(ENTITY, code, data)


def _new_code() -> str:
    # 以前と同じ形 (8 文字、大文字)。人が画面で照合できる短さを保つ。
    return secrets.token_urlsafe(6)[:8].upper()


class PairingTable:
    """合言葉の発行 (start) / 承認 (approve) / 受け取り (poll) を、共有の保存先の上で行う。

    状態を自分では持たない (= 毎回保存先を読み書きする)。だから、どの台で作った
    ``PairingTable`` でも、同じ保存先を見ていれば同じ答えを返す。
    """

    def __init__(
        self,
        persistence: PairingPersistence,
        *,
        clock: Callable[[], float] = time.time,
        ttl_seconds: int = TTL_SECONDS,
        new_code: Callable[[], str] = _new_code,
    ):
        self._p = persistence
        self._clock = clock
        self._ttl = int(ttl_seconds)
        self._new_code = new_code

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def _now(self) -> int:
        # 整数秒で持つ (DynamoDB は float を受け付けないため)。
        return int(self._clock())

    def _live(self, code: str) -> dict:
        """有効な (= 存在し、失効しておらず、まだ受け取られていない) 記録を返す。"""
        code = (code or "").strip().upper()
        rec = self._p.get(code) if code else None
        if not rec or rec.get("status") == STATUS_CONSUMED:
            raise PairingError(404, "Invalid or expired code")
        if self._now() > int(rec.get("expires", 0)):
            raise PairingError(410, "Code expired")
        return rec

    def start(self) -> str:
        """新しい合言葉を発行して保存する。既存の合言葉とは重ならないようにする。"""
        now = self._now()
        for _ in range(5):
            code = self._new_code()
            existing = self._p.get(code)
            if existing is None or now > int(existing.get("expires", 0)):
                self._p.put(code, {
                    "code": code,
                    "status": STATUS_PENDING,
                    "created_at": now,
                    "expires": now + self._ttl,
                })
                return code
        raise PairingError(503, "Could not allocate a pairing code; try again")

    def approve(self, code: str, *, sub: str, email: str) -> dict:
        """サインインした人が合言葉を承認する。押し直し (同じ人) はそのまま成功にする。"""
        rec = self._live(code)
        if rec.get("status") == STATUS_APPROVED:
            if rec.get("sub") != sub:
                # 別の人が先に承認した合言葉を、上書きで乗っ取らせない。
                raise PairingError(409, "Code already approved by another account")
            return {"status": STATUS_APPROVED, "email": rec.get("email", "")}
        self._p.put(rec["code"], {
            **rec,
            "status": STATUS_APPROVED,
            "sub": sub,
            "email": email,
            "approved_at": self._now(),
        })
        return {"status": STATUS_APPROVED, "email": email}

    def poll(self, code: str) -> dict:
        """CLI の問い合わせ。承認済みなら 1 回だけ ``sub`` / ``email`` を返す。

        返り値は ``{"status": "pending"}`` か
        ``{"status": "approved", "sub": ..., "email": ...}``。後者を返す前に
        記録を「受け取り済み」にするので、同じ合言葉で二度は受け取れない。
        """
        rec = self._live(code)
        if rec.get("status") != STATUS_APPROVED:
            return {"status": STATUS_PENDING}
        self._p.put(rec["code"], {**rec, "status": STATUS_CONSUMED, "consumed_at": self._now()})
        return {"status": STATUS_APPROVED, "sub": rec.get("sub", ""), "email": rec.get("email", "")}
