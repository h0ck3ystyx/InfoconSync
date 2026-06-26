#!/usr/bin/env bash
# package-macos.sh — Build a self-contained InfoCon Librarian .app for macOS
#
# Prerequisites:
#   brew install libtorrent-rasterbar python@3.12
#   pip install pyinstaller
#
# Usage:
#   ./scripts/package-macos.sh [--sign] [--notarize TEAM_ID]
#
# Output: dist/InfoConLibrarian.app (unsigned) or a signed/notarized DMG

set -euo pipefail

SIGN=0
NOTARIZE=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --sign)      SIGN=1; shift ;;
    --notarize)  NOTARIZE="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$PROJECT_ROOT/dist"
APP_NAME="InfoConLibrarian"
SPEC="$PROJECT_ROOT/build/macos.spec"

echo ">>> Building $APP_NAME"

# Activate the venv with libtorrent access
if [[ -d "$PROJECT_ROOT/.venv" ]]; then
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.venv/bin/activate"
fi

# Verify libtorrent is importable before spending time building
python -c "import libtorrent; print('libtorrent', libtorrent.version)" || {
  echo "ERROR: libtorrent not importable. Run: brew install libtorrent-rasterbar" >&2
  exit 1
}

# Run the full test suite; abort if anything fails
echo ">>> Running test suite"
python -m pytest tests/unit tests/integration -q --tb=short

# Generate PyInstaller spec if it does not exist
if [[ ! -f "$SPEC" ]]; then
  mkdir -p "$(dirname "$SPEC")"
  cat > "$SPEC" <<'SPEC_EOF'
# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['src/infocon_librarian/__main__.py'],
    pathex=[str(Path('src').resolve())],
    binaries=[],
    datas=[
        ('src/infocon_librarian/web/static', 'infocon_librarian/web/static'),
    ],
    hiddenimports=['libtorrent'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='infocon-librarian',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='InfoConLibrarian',
)

app = BUNDLE(
    coll,
    name='InfoConLibrarian.app',
    icon=None,
    bundle_identifier='org.infocon.librarian',
    info_plist={
        'CFBundleShortVersionString': '0.1.0',
        'NSHighResolutionCapable': True,
    },
)
SPEC_EOF
fi

# Build
python -m PyInstaller --clean --noconfirm "$SPEC" --distpath "$DIST_DIR"

echo ">>> Built: $DIST_DIR/$APP_NAME.app"

if [[ $SIGN -eq 1 ]]; then
  echo ">>> Code-signing"
  codesign --deep --force --verify --verbose \
    --sign "Developer ID Application" \
    "$DIST_DIR/$APP_NAME.app"
fi

if [[ -n "$NOTARIZE" ]]; then
  echo ">>> Creating DMG for notarization"
  DMG="$DIST_DIR/$APP_NAME.dmg"
  hdiutil create -volname "$APP_NAME" -srcfolder "$DIST_DIR/$APP_NAME.app" \
    -ov -format UDZO "$DMG"
  xcrun notarytool submit "$DMG" --team-id "$NOTARIZE" --wait
  xcrun stapler staple "$DMG"
  echo ">>> Notarized: $DMG"
fi

echo ">>> Done"
