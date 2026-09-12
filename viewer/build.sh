#!/usr/bin/env bash
# 配れる形に組み立てる (ms-170 e-6365)。
#
# 「Beacon が入っていない PC でも盤を見られる」は、その PC 向けの実行ファイルが
# 手に入って初めて成立する。受け取った人がファイル 1 つを置いて動かせる状態を作る。
#
# 使い方:
#   ./build.sh            全 OS 向けを dist/ に作る
#   ./build.sh windows    1 つだけ作る (windows / darwin / linux)
#
# 前提: Go だけ。C の道具立ては要らない (SQLite も純 Go の実装を使っているため、
# CGO_ENABLED=0 のままどの OS 向けにも組み立てられる)。
set -euo pipefail

cd "$(dirname "$0")"
OUT="${OUT:-dist}"
NAME="beacon-view"

# -trimpath   … 作った人の手元のパスを実行ファイルに残さない
# -s -w       … デバッグ用の記号を落とす (17.6MB → 11.8MB)
FLAGS=(-trimpath -ldflags=-s\ -w)

targets=(
  "windows amd64"
  "windows arm64"
  "darwin  amd64"
  "darwin  arm64"
  "linux   amd64"
  "linux   arm64"
)

want="${1:-}"
mkdir -p "$OUT"

for t in "${targets[@]}"; do
  read -r os arch <<<"$t"
  if [ -n "$want" ] && [ "$want" != "$os" ]; then continue; fi

  ext=""
  [ "$os" = "windows" ] && ext=".exe"
  file="$OUT/$NAME-$os-$arch$ext"

  printf '%-16s' "$os/$arch"
  CGO_ENABLED=0 GOOS="$os" GOARCH="$arch" go build "${FLAGS[@]}" -o "$file" ./...
  size=$(du -h "$file" | cut -f1)
  echo "→ $file ($size)"
done

echo
echo "組み立てました。受け取った人は、この 1 ファイルを置いて動かすだけです"
echo "(Go も Python も beacon も要りません)。"
