#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$SCRIPT_DIR/out"
BUILD_DIR="$SCRIPT_DIR/build/pyinstaller"
DIST_DIR="$OUT_DIR"
ENTRYPOINT="$SCRIPT_DIR/src/main.py"
APP_NAME="${APP_NAME:-ctester}"

if [ ! -f "$ENTRYPOINT" ]; then
    echo "Error: entrypoint not found: $ENTRYPOINT" >&2
    exit 1
fi

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

if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "Installing PyInstaller + dependencies into build venv ..."
    "$PIP" install --quiet pyinstaller
    "$PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

EXCLUDES=(
    bz2
    lzma
    multiprocessing
    pwd
    grp
    fcntl
    posix
    _hashlib
    _ssl
    defusedxml
    uharfbuzz
    reportlab_settings
    unittest
    sqlite3
    pdb
    doctest
    cProfile
    trace
    timeit
)

if [ -n "${PYINSTALLER_EXCLUDE_MODULES:-}" ]; then
    for module in $PYINSTALLER_EXCLUDE_MODULES; do
        EXCLUDES+=("$module")
    done
fi

PYINSTALLER_ARGS=(
    --onefile
    --console
    --clean
    --noconfirm
    --name "$APP_NAME"
    --paths "$SCRIPT_DIR/src"
    --distpath "$DIST_DIR"
    --workpath "$BUILD_DIR/work"
    --specpath "$BUILD_DIR/spec"
)

if [ -n "${PYINSTALLER_ICON:-}" ]; then
    if [ ! -f "$PYINSTALLER_ICON" ]; then
        echo "Error: icon file not found: $PYINSTALLER_ICON" >&2
        exit 1
    fi
    PYINSTALLER_ARGS+=(--icon "$PYINSTALLER_ICON")
fi

for module in "${EXCLUDES[@]}"; do
    PYINSTALLER_ARGS+=(--exclude-module "$module")
done

PYINSTALLER_ARGS+=("$ENTRYPOINT")

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/work" "$BUILD_DIR/spec"

"$PYTHON" -m PyInstaller "${PYINSTALLER_ARGS[@]}"

BINARY="$DIST_DIR/$APP_NAME"
if [ -f "$BINARY.exe" ]; then
    BINARY="$BINARY.exe"
elif [ -f "$BINARY" ]; then
    BINARY="$BINARY"
fi

chmod +x "$BINARY" 2>/dev/null || true
echo "Built: $BINARY"
