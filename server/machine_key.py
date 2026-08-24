"""Machine API key の発行・格納表現・検証 (ms-151 / e-5474)。

## なぜこの module があるか

Beacon の全 cloud 書き込みは今、人間がログインして得た id_token（もしくは
そこから作った CLI トークン）に依存している。headless な machine（例: PE
detector Lambda）は人間ログイン由来のトークンを持てないので書き込めない
(ms-151 SPEC 問題 P1)。

そこで「project に属する machine 用 API key」を導入する。設計方針 (SPEC §1):

- **単一の共有秘密にしない**。project 単位で複数発行でき、machine 単位で
  識別・失効・rotation できる key テーブルにする。単一秘密だと writer を
  識別できず、失効が全 machine に波及する。
- **key 本体はサーバーに平文で残さない**。発行時に一度だけ raw token を返し、
  サーバーは secret の hash だけを保存する。漏洩時の被害を「その key の失効」に
  閉じ込める。

この module は **pure**（store I/O なし）。key の生成・token 形式・hash・
定数時間比較という「暗号と形式」だけを持つ。実際の格納 (store CRUD) と
require_auth への配線 (e-5475) / 直書き endpoint (e-5476) は別レイヤーが
この module の primitive を呼ぶ。store を渡さないことで単体テストが完結する。

## token 形式

    bmk.<project_id>.<key_id>.<secret>

- ``bmk`` = beacon machine key の固定 prefix。``_verify_id_token`` が bearer を
  受けたとき、この prefix で「これは machine key だ」と分岐できる (人間トークン
  との衝突を形式で排除)。
- ``project_id`` = この key が属する project。token に埋め込むことで、検証側は
  scan なしに「どの project の key テーブルを引けばよいか」を token だけから
  決められる (全 backend で O(1) lookup)。
- ``key_id`` = 公開識別子。project 内で key を一意に指す。失効・一覧の対象キー。
- ``secret`` = 乱数の秘密部。サーバーは ``sha256(secret)`` だけを保存し、raw な
  secret は保持しない。

project_id は dot を含まない slug (例 ``beacon-b95643``) だが、将来 dot を
含んでも壊れないよう、parse は末尾から ``key_id`` / ``secret`` を剥がして
中間を project_id とみなす (rsplit)。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional, Tuple

# token の固定 prefix。CLI トークン (``bcli.``) と同じ「prefix で種別を判定する」
# 先例に倣う。区切りは ``.`` (project_id は dot を含まない)。
TOKEN_PREFIX = "bmk."

# key_id / secret の乱数バイト長。key_id は識別子なので短めで十分、secret は
# 総当たり耐性のため 32 bytes (= 256 bit)。token_urlsafe は base64url なので
# 出力文字数はバイト長より少し長い。
_KEY_ID_BYTES = 9
_SECRET_BYTES = 32


def _rand_key_id() -> str:
    """公開識別子 key_id を生成する (url-safe, dot を含まない)。"""
    return secrets.token_urlsafe(_KEY_ID_BYTES)


def _rand_secret() -> str:
    """秘密部 secret を生成する (url-safe, dot を含まない)。"""
    return secrets.token_urlsafe(_SECRET_BYTES)


def hash_secret(secret: str) -> str:
    """secret を不可逆変換 (sha256 hex) する。サーバーはこの値だけを保存する。

    secret は既に高エントロピー (256 bit 乱数) なので、パスワードのような
    stretching (bcrypt/argon2) は不要 — 総当たりは元々非現実的で、単純 hash で
    「平文を保存しない」目的は満たせる。
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def format_token(project_id: str, key_id: str, secret: str) -> str:
    """3 つの部品から raw token 文字列を組み立てる。"""
    return f"{TOKEN_PREFIX}{project_id}.{key_id}.{secret}"


def looks_like_machine_key(raw: str) -> bool:
    """token が machine key の形 (prefix 一致) かを判定する。

    ``_verify_id_token`` の dispatch 用。中身の妥当性ではなく「この検証経路に
    流すべきか」だけを、network / store を触らずに即断するための cheap check。
    """
    return isinstance(raw, str) and raw.startswith(TOKEN_PREFIX)


def parse_token(raw: str) -> Optional[Tuple[str, str, str]]:
    """raw token を ``(project_id, key_id, secret)`` に分解する。

    形式不正なら ``None``。末尾から key_id / secret を剥がすので、project_id が
    将来 dot を含んでも安全。空要素 (連続 dot 等) は不正として弾く。
    """
    if not looks_like_machine_key(raw):
        return None
    body = raw[len(TOKEN_PREFIX):]
    # 末尾 2 つの区切りで [project_id, key_id, secret] に割る。
    parts = body.rsplit(".", 2)
    if len(parts) != 3:
        return None
    project_id, key_id, secret = parts
    if not project_id or not key_id or not secret:
        return None
    return project_id, key_id, secret


def build_record(
    project_id: str,
    key_id: str,
    secret: str,
    *,
    label: str = "",
    created_by: str = "",
    now: str,
) -> dict:
    """store に保存する key レコードを組み立てる (secret は hash 化して格納)。

    ``now`` は呼び出し側が渡す (ISO8601 文字列)。この module は時刻源を持たない
    ことで pure かつ deterministic にテストできる。``revoked_at`` は未失効を表す
    ``None`` で初期化する。
    """
    return {
        "key_id": key_id,
        "project_id": project_id,
        "secret_hash": hash_secret(secret),
        "label": label,
        "created_by": created_by,
        "created_at": now,
        "revoked_at": None,
    }


def issue(
    project_id: str,
    *,
    label: str = "",
    created_by: str = "",
    now: str,
    key_id: Optional[str] = None,
    secret: Optional[str] = None,
) -> Tuple[str, dict]:
    """新しい machine key を発行し、``(raw_token, record)`` を返す。

    ``raw_token`` は発行時にしか得られない (record は hash しか持たない)。呼び
    出し側は raw_token を発行者に一度だけ見せ、record を store に保存する。

    ``key_id`` / ``secret`` は決定的テストのための注入口。既定は乱数生成。
    """
    key_id = key_id or _rand_key_id()
    secret = secret or _rand_secret()
    raw = format_token(project_id, key_id, secret)
    record = build_record(
        project_id, key_id, secret,
        label=label, created_by=created_by, now=now,
    )
    return raw, record


def verify_token(raw: str, record: Optional[dict]) -> Optional[dict]:
    """raw token と、store から引いた key レコードを突き合わせて検証する。

    成功時は ``record`` (= machine の身元) を返し、失敗時は ``None``。判定:

    1. token が形式として parse できる。
    2. record が存在する (= key_id が実在)。
    3. token 内の project_id / key_id が record と一致する (別 project / 別 key の
       すり替えを弾く)。
    4. record が失効していない (``revoked_at`` が未設定)。
    5. ``sha256(secret)`` が保存 hash と定数時間比較で一致する。

    store lookup 自体は呼び出し側 (e-5475) が担う: token を parse して
    ``(project_id, key_id)`` を得 → store から record を引く → この関数に渡す。
    そうすることで本 module は pure なまま「検証ロジックの単一の真値源」になる。
    """
    parsed = parse_token(raw)
    if parsed is None:
        return None
    project_id, key_id, secret = parsed
    if not record:
        return None
    if record.get("project_id") != project_id:
        return None
    if record.get("key_id") != key_id:
        return None
    if record.get("revoked_at"):
        return None
    stored_hash = record.get("secret_hash", "")
    if not stored_hash:
        return None
    if not hmac.compare_digest(hash_secret(secret), stored_hash):
        return None
    return record


def redacted(record: dict) -> dict:
    """一覧表示用に、secret_hash を落とした安全な公開ビューを返す。

    key 一覧 (CLI / API) は「どの key がいつ発行され、失効済みか」を見せれば十分で、
    hash を露出する必要はない。``revoked`` を導出フラグとして足す。
    """
    return {
        "key_id": record.get("key_id", ""),
        "project_id": record.get("project_id", ""),
        "label": record.get("label", ""),
        "created_by": record.get("created_by", ""),
        "created_at": record.get("created_at", ""),
        "revoked_at": record.get("revoked_at"),
        "revoked": bool(record.get("revoked_at")),
    }
