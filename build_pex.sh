#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$SCRIPT_DIR/out"
OUTPUT="$OUT_DIR/ctester.pex"

PYTHON_CMD=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
else
    echo "Error: python is not installed or not on PATH" >&2
    exit 1
fi

VENV_DIR="$SCRIPT_DIR/.venv-build"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating build virtualenv at $VENV_DIR ..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

if [ -f "$VENV_DIR/bin/python" ]; then
    PYTHON="$VENV_DIR/bin/python"
    PIP="$VENV_DIR/bin/pip"
elif [ -f "$VENV_DIR/Scripts/python.exe" ]; then
    PYTHON="$VENV_DIR/Scripts/python.exe"
    PIP="$VENV_DIR/Scripts/pip.exe"
else
    echo "Error: could not find venv Python binary" >&2
    exit 1
fi

if ! "$PYTHON" -c "import pex" >/dev/null 2>&1; then
    echo "Installing pex into build venv ..."
    "$PIP" install --quiet pex
fi

rm -rf "$OUTPUT" "$SCRIPT_DIR/build" "$SCRIPT_DIR/src/simple_c_tester.egg-info"
mkdir -p "$OUT_DIR"

"$PYTHON" -m pex \
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
