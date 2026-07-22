#!/usr/bin/env python3
"""ms-96 / e-3052 — N 並行 smoke テスト + 総 DB 接続数チェック。

ms-96 cutover 後に踏んだ並行破損 (= 複数スレッドが 1 本の PyMySQL 接続を共有して
socket が None になり `'NoneType'...settimeout` で 500/ハング) の **再発を検知する**
手動 / CI 手順。PR #349 で接続を thread-local 化して直したが、それが戻ったら本番で
また 500 が出る。このスクリプトは稼働サーバへ N 並行リクエストを投げ、

  - 1 本でも失敗 (非 2xx / 例外) したら → 並行破損の疑いとして exit 1
  - /health の総 DB 接続数 (live_connections) が閾値を超えたら → exit 1

で気付けるようにする。構造ガードの単体テストは
tests/test_mysql_threadlocal_concurrency_e3052.py (DB 不要・CI 常時) にあり、本
スクリプトは **実サーバ相手のエンドツーエンド** 確認を担う。

使い方 (手動手順):

    # 既定は /api/version を 20 並行 x 10 周 = 200 リクエスト叩く (認証不要)
    python scripts/smoke-concurrent.py --base-url https://beacon-ai.dev

    # store (MySQL) を実際に叩く authed endpoint で並行 DB アクセスを stress する
    python scripts/smoke-concurrent.py \
        --base-url https://beacon-ai.dev \
        --path "/api/projects/<pid>" \
        --token "$(python3 -c 'import json;print(json.load(open(".beacon/cloud.json"))["id_token"])')" \
        --concurrency 40 --rounds 10 --max-conns 60

exit code: 0 = 健全 / 1 = 失敗検出 or 接続数超過 / 2 = 使い方エラー。
標準ライブラリのみ (urllib + ThreadPoolExecutor) で追加依存なし。
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

_SSL_CTX: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """Up-to-date CA を信頼する SSL context を返す (lib/api_client と同じ回避策)。

    Windows 等では stdlib 既定のトラストストアが stale で、有効な cert でも
    `CERTIFICATE_VERIFY_FAILED: certificate has expired` になる (= curl / browser は
    通る)。$BEACON_API_CAFILE → certifi → stdlib 既定 の順で解決する。
    """
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    cafile = os.environ.get("BEACON_API_CAFILE", "").strip() or None
    if cafile is None:
        try:
            import certifi
            cafile = certifi.where()
        except Exception:
            cafile = None
    try:
        _SSL_CTX = ssl.create_default_context(cafile=cafile)
    except Exception:
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def _get(url: str, token: str = "", timeout: float = 15.0):
    """1 リクエスト。(status_code, elapsed_sec, error_str) を返す。"""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    ctx = _ssl_context()
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp.read()
            return resp.status, time.monotonic() - t0, ""
    except urllib.error.HTTPError as e:
        return e.code, time.monotonic() - t0, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 — smoke 用途、全部拾って報告
        return 0, time.monotonic() - t0, f"{type(e).__name__}: {e}"


def _health_db(base_url: str, timeout: float = 15.0) -> dict:
    """/health の db 観測フィールドを返す (無ければ {})。"""
    try:
        with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/health", timeout=timeout,
            context=_ssl_context(),
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("db") or {}
    except Exception as e:  # noqa: BLE001
        print(f"  (warn: /health db 観測に失敗: {e})")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="ms-96 e-3052 concurrent smoke")
    ap.add_argument("--base-url", default="https://beacon-ai.dev")
    ap.add_argument("--path", default="/api/version",
                    help="叩く endpoint パス (store を叩く authed path 推奨)")
    ap.add_argument("--token", default="", help="Bearer token (authed path 用)")
    ap.add_argument("--concurrency", type=int, default=20, help="同時実行数 N")
    ap.add_argument("--rounds", type=int, default=10,
                    help="N を何周投げるか (総リクエスト = N x rounds)")
    ap.add_argument("--max-conns", type=int, default=60,
                    help="/health の live_connections がこれを超えたら異常")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    if args.concurrency < 1 or args.rounds < 1:
        print("concurrency と rounds は 1 以上にしてください", file=sys.stderr)
        return 2

    url = f"{args.base_url.rstrip('/')}{args.path}"
    total = args.concurrency * args.rounds
    print(f"target: {url}")
    print(f"plan:   concurrency={args.concurrency} rounds={args.rounds} "
          f"(= {total} requests)")

    before = _health_db(args.base_url, args.timeout)
    if before:
        print(f"before: db={before}")

    results: list[tuple[int, float, str]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(_get, url, args.token, args.timeout)
            for _ in range(total)
        ]
        for f in as_completed(futures):
            results.append(f.result())

    after = _health_db(args.base_url, args.timeout)

    ok = [r for r in results if 200 <= r[0] < 300]
    failed = [r for r in results if not (200 <= r[0] < 300)]
    lat = sorted(r[1] for r in results)
    p50 = lat[len(lat) // 2] if lat else 0.0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0.0

    print(f"result: ok={len(ok)}/{total} failed={len(failed)} "
          f"p50={p50*1000:.0f}ms p95={p95*1000:.0f}ms")
    if after:
        print(f"after:  db={after}")

    problems: list[str] = []
    if failed:
        # 代表的な失敗を数種類だけ表示 (= settimeout None / 500 の再発が一目で分かる)
        kinds: dict[str, int] = {}
        for code, _, err in failed:
            key = err or f"HTTP {code}"
            kinds[key] = kinds.get(key, 0) + 1
        detail = ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items()))
        problems.append(f"{len(failed)} 件失敗 [{detail}]")

    live = after.get("live_connections") if after else None
    if isinstance(live, int) and live > args.max_conns:
        problems.append(
            f"live_connections={live} が閾値 {args.max_conns} を超過 "
            "(= 接続リーク / 並行破損の疑い)"
        )

    if problems:
        print("NG: " + " / ".join(problems))
        return 1
    print("OK: 全リクエスト成功、接続数も閾値内。並行破損の兆候なし。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
