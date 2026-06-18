#!/usr/bin/env bash
#
# Build Lambda deployment zip for the Beacon FastAPI app (e-1542).
#
# 何をするか:
#   1. dist/lambda-stage/ にクリーンなビルド領域を作る
#   2. server/requirements-lambda.txt の依存を manylinux 用にインストール
#      (= Mac で build しても Lambda の Amazon Linux で動くバイナリを取る)
#   3. server/ と lib/ をステージ領域にコピー
#   4. dist/beacon-lambda.zip に zip 圧縮
#
# 使い方:
#   ./scripts/build-lambda-zip.sh                 # Python 3.12, x86_64
#   PYTHON_VERSION=3.11 ./scripts/build-lambda-zip.sh
#
# 出力:
#   dist/beacon-lambda.zip   ← terraform から lambda_zip_path で参照する
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
PLATFORM="${PLATFORM:-manylinux2014_x86_64}"
PIP="${PIP:-python3 -m pip}"

STAGE_DIR="dist/lambda-stage"
ZIP_PATH="dist/beacon-lambda.zip"
REQUIREMENTS="server/requirements-lambda.txt"

if [ ! -f "$REQUIREMENTS" ]; then
  echo "error: $REQUIREMENTS not found" >&2
  exit 1
fi

echo "→ cleaning $STAGE_DIR and $ZIP_PATH"
rm -rf "$STAGE_DIR" "$ZIP_PATH"
mkdir -p "$STAGE_DIR" dist

echo "→ installing dependencies (python=$PYTHON_VERSION, platform=$PLATFORM)"
$PIP install \
  --target "$STAGE_DIR" \
  --requirement "$REQUIREMENTS" \
  --platform "$PLATFORM" \
  --python-version "$PYTHON_VERSION" \
  --only-binary=:all: \
  --upgrade \
  --quiet

echo "→ copying server/ and lib/"
# Zip layout (= Lambda /var/task の中身):
#   server/app.py
#   server/lambda_handler.py
#   server/...
#   lib/core.py
#   lib/...
#
# server/ をサブディレクトリのまま残す理由: app.py の sys.path 操作
# `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))`
# が `server/../lib = lib/` を解決する前提で書かれているため、この
# 相対関係を保つ。Lambda 側の handler 設定は `server.lambda_handler.handler`。
mkdir -p "$STAGE_DIR/server" "$STAGE_DIR/lib"
cp -R server/. "$STAGE_DIR/server/"
cp -R lib/. "$STAGE_DIR/lib/"

# bytecode / cache を削って zip を小さくする
find "$STAGE_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$STAGE_DIR" -type f -name "*.pyc" -delete 2>/dev/null || true

echo "→ creating $ZIP_PATH"
( cd "$STAGE_DIR" && zip -rq "../../$ZIP_PATH" . )

SIZE_BYTES=$(stat -f%z "$ZIP_PATH" 2>/dev/null || stat -c%s "$ZIP_PATH" 2>/dev/null)
SIZE_MB=$(( SIZE_BYTES / 1024 / 1024 ))
echo "✓ built $ZIP_PATH (${SIZE_MB} MB)"
echo ""
echo "next: in beacon-cloud, set lambda_zip_path to this file and apply terraform."
