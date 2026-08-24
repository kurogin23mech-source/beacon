"""Reference ``beacon_sink`` for headless machines (PE detector Lambda 等) — ms-151.

外部の常駐プログラム (人が介在しない machine) が、自分の run_record (運転記録) と
incident (異常記録) を Beacon cloud へ直接書くための **差し替え口の参照実装**。PE
detector Lambda は既存の ``beacon_sink`` stub をこのクラスに差し替えるだけで書ける。

依存は標準ライブラリのみ (urllib)。Lambda に requests 等を同梱しなくても動く。

## 使い方

    sink = BeaconSink(
        base_url="https://beacon-ai.dev",          # Beacon cloud の base URL
        machine_key="bmk.<project>.<key_id>.<secret>",  # `beacon machine-key issue` で発行
        project_id="beacon-b95643",                 # 書き込み先 project
    )
    sink.write_run_record("op-5", batch="nightly", status="ok",
                          description="all detectors green")
    sink.write_incident("op-5", title="latency spike", priority="high")

machine_key は Beacon の owner が ``beacon machine-key issue --label "PE detector"``
で発行し、発行時に一度だけ表示される raw token を安全な経路 (環境変数 / secrets
manager) で machine に渡す。サーバーは hash しか保持しないので再取得はできない。

## 契約 (詳細は docs/integrations/machine-write-contract.md)

- POST {base_url}/api/projects/{project_id}/operations/{op_id}/run-records
    body: {"batch": str, "status": "ok"|"warning"|"error", "description": str?, "date": str?}
- POST {base_url}/api/projects/{project_id}/operations/{op_id}/incidents
    body: {"title": str, "description": str?, "priority": str?, "opened_at": str?}
- header: ``Authorization: Bearer <machine_key>``
- envelope は不要 (記録は外向き action ではないため)。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Optional


class BeaconWriteError(RuntimeError):
    """Beacon への書き込みが失敗したとき送出する (status_code + body を運ぶ)。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"Beacon write failed [{status_code}]: {message}")
        self.status_code = status_code
        self.message = message


# http_post(url, headers, body_bytes) -> (status_code, response_text)。
# 差し替え可能 (テストは TestClient 経由に差し替える)。default は urllib。
HttpPost = Callable[[str, dict, bytes], "tuple[int, str]"]


def _urllib_post(url: str, headers: dict, body: bytes) -> "tuple[int, str]":
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class BeaconSink:
    """Beacon cloud へ run_record / incident を直書きする最小クライアント。"""

    def __init__(self, base_url: str, machine_key: str, project_id: str,
                 *, http_post: Optional[HttpPost] = None):
        self._base = base_url.rstrip("/")
        self._key = machine_key
        self._project_id = project_id
        self._http_post = http_post or _urllib_post

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self._base}{path}"
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        status, text = self._http_post(url, headers, json.dumps(body).encode("utf-8"))
        if status < 200 or status >= 300:
            raise BeaconWriteError(status, text)
        return json.loads(text) if text else {}

    def write_run_record(self, op_id: str, *, batch: str, status: str,
                         description: str = "", date: str = "") -> dict:
        """Operation ``op_id`` に run_record を書く。返り: 作成された entry。

        ``status`` は ``ok`` / ``warning`` / ``error``。``date`` 省略時は server 時刻。
        """
        body = {"batch": batch, "status": status, "description": description}
        if date:
            body["date"] = date
        return self._post(
            f"/api/projects/{self._project_id}/operations/{op_id}/run-records",
            body,
        )

    def write_incident(self, op_id: str, *, title: str, description: str = "",
                       priority: str = "", opened_at: str = "") -> dict:
        """Operation ``op_id`` に incident を開く。返り: 作成された entry。

        ``priority`` 省略可。``opened_at`` 省略時は server 時刻 (= 発生時刻を
        machine が持つなら渡す)。
        """
        body = {"title": title, "description": description}
        if priority:
            body["priority"] = priority
        if opened_at:
            body["opened_at"] = opened_at
        return self._post(
            f"/api/projects/{self._project_id}/operations/{op_id}/incidents",
            body,
        )
