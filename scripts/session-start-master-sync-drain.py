#!/usr/bin/env python3
"""Operation-independent master-sync outbox drain for session-start (ms-111 / e-4399).

背景 (問題): e-4355 の master-sync outbox (= 投影 Account の rename を master へ透過
できなかった時に立てる「未配送」pending マーカー) は、従来 ``beacon account rename``
の末尾でしか drain されなかった。つまり「次の CLI 操作」駆動で、rename の後にたまたま
別操作が無いと未配送分が master に届かず遅延する。linking go-live を live にすると emit
は時々失敗する (bus 一時不通 / 未 login 明けなど) ので、操作を待たずに未配送を掃く定期
経路が要る。

このスクリプトはその定期経路の 1 つ = **session-start** に載せる drain である。
session-start は日々確実に発火する既存 hook 経路 (/beacon-session-start Skill) なので、
「rename 後に別操作が無くても、次にセッションを開いた時点で未配送分が master へ届く」を
成立させられる。実処理は ``beacon master-sync drain`` サブコマンドに委譲する (drain ロジック
は lib/commands._drain_master_sync_outbox が単一実装、load→drain→save は
cmd_master_sync_drain が担い、save は cloud lost-update guard を通す)。

出力契約 (他の session-start helper と同型):
  * pending が無い / local mode / 未 login では ``beacon master-sync drain`` が静かに
    no-op し、本スクリプトも何も出さない。
  * 未配送を再送した時だけ drain が 1 行出す。それをそのまま素通しする。
  * beacon バイナリが無い / サブプロセス失敗は silent skip。常に exit 0。

Call site:
  * /beacon-session-start Skill (master-sync outbox drain step)
"""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    try:
        r = subprocess.run(
            ["beacon", "master-sync", "drain"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return 0  # beacon 不在 / 実行不能 → silent skip (定期経路を壊さない)
    out = (r.stdout or "").strip()
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
