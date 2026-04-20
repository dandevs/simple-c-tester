#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$SCRIPT_DIR/out"
OUTPUT="$OUT_DIR/ctester.pex"

rm -rf "$OUTPUT" "$SCRIPT_DIR/build" "$SCRIPT_DIR/src/simple_c_tester.egg-info"
mkdir -p "$OUT_DIR"

pex \
    -D "$SCRIPT_DIR/src" \
    -e main:entry \
    --platform manylinux2014_x86_64-cp-39-cp39 \
    --platform manylinux2014_aarch64-cp-39-cp39 \
    --platform macosx_10_9_x86_64-cp-39-cp39 \
    --platform macosx_11_0_arm64-cp-39-cp39 \
    --platform win_amd64-cp-39-cp39 \
    --python-shebang '#!/usr/bin/env python3' \
    -r requirements.txt \
    -o "$OUTPUT"

chmod +x "$OUTPUT"
echo "Built: $OUTPUT"
