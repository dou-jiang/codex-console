#!/bin/bash
# Packaging script. Run it on the target OS you want to package for.

set -e

OS=$(uname -s)
ARCH=$(uname -m)

case "$OS" in
  Darwin)
    PLATFORM="macos"
    EXT=""
    ;;
  Linux)
    PLATFORM="linux"
    EXT=""
    ;;
  MINGW*|CYGWIN*|MSYS*)
    PLATFORM="windows"
    EXT=".exe"
    ;;
  *)
    PLATFORM="$OS"
    EXT=""
    ;;
esac

OUTPUT_NAME="codex-console-${PLATFORM}-${ARCH}${EXT}"

echo "=== Build platform: ${PLATFORM} (${ARCH}) ==="
echo "=== Output file: dist/${OUTPUT_NAME} ==="

# Install build dependency.
pip install pyinstaller --quiet 2>/dev/null || \
  uv run --with pyinstaller pyinstaller --version > /dev/null 2>&1

# Run PyInstaller. Prefer uv when available.
if command -v uv &>/dev/null; then
  uv run --with pyinstaller pyinstaller codex_register.spec --clean --noconfirm
else
  pyinstaller codex_register.spec --clean --noconfirm
fi

# Rename the generated binary to include platform metadata.
rm -f "dist/${OUTPUT_NAME}"
mv "dist/codex-console${EXT}" "dist/${OUTPUT_NAME}" 2>/dev/null || \
  mv "dist/codex-console" "dist/${OUTPUT_NAME}" 2>/dev/null

echo "=== Build complete: dist/${OUTPUT_NAME} ==="
ls -lh "dist/${OUTPUT_NAME}"
