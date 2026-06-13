"""AWS Lambda entry point for the Beacon FastAPI app.

API Gateway HTTP API (= v2) のイベントを Mangum (= ASGI adapter) で
FastAPI に橋渡しする。app.py の FastAPI インスタンスをそのまま再利用するため、
REST endpoint の挙動は GCP Cloud Run と AWS Lambda で同一になる。

WebSocket endpoint (/ws/projects/{project_id}) は HTTP API では expose できない
ため、Lambda 上では到達不可になる (= 別途 e-1543 で Fargate コンテナに分離)。
"""
from __future__ import annotations

import os
import sys

# Lambda zip layout (scripts/build-lambda-zip.sh 参照):
#   /var/task/server/lambda_handler.py   ← この file
#   /var/task/server/app.py
#   /var/task/lib/core.py 等
# server/ が package として認識されない平打ち zip でも import が通るよう
# server/ ディレクトリ自身を sys.path に明示追加してから app を import する。
_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from mangum import Mangum

from app import app

# lifespan="off" — Lambda の short-lived process では FastAPI の startup/shutdown
# event を発火させない (= cold start のたびに重い初期化が走るのを避ける)。
# 必要な初期化は app.py のモジュール import 時に実行される設計。
handler = Mangum(app, lifespan="off")
