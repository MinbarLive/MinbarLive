#!/usr/bin/env bash
# Package the PyInstaller onefile Linux binary (dist/MinbarLive) into a
# desktop-integrated AppImage. Must run on Linux, after `pyinstaller
# MinbarLive.spec`. Called by the build-linux job in .github/workflows/
# release.yml. See docs/ci.md.
#
# The AppImage adds what a bare ELF cannot: a launcher/menu entry with the
# MinbarLive dome icon, plus a single portable file that runs across distros.
set -euo pipefail

ARCH="${ARCH:-x86_64}"
BIN="dist/MinbarLive"
APPDIR="dist/MinbarLive.AppDir"
OUT="dist/MinbarLive-${ARCH}.AppImage"

if [ ! -x "$BIN" ]; then
  echo "::error::$BIN not found or not executable - run pyinstaller first."
  exit 1
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"

cp "$BIN" "$APPDIR/usr/bin/MinbarLive"

# Icon: the dome mark (no lettering) centred on a 256x256 transparent square,
# reusing the exact crop the in-app header logo uses so the launcher icon and
# the window logo match. Pure Pillow/numpy - importing utils.icons pulls in
# tkinter but never creates a root, so no display is required here.
python3 - <<'PY'
from PIL import Image

from utils.icons import logo_mark

mark = logo_mark("public/MinbarLive1.png", 240)
mark.thumbnail((240, 240), Image.LANCZOS)  # guarantee it fits the 256 canvas
canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
canvas.alpha_composite(mark, ((256 - mark.width) // 2, (256 - mark.height) // 2))
canvas.save("dist/MinbarLive.AppDir/MinbarLive.png")
PY
cp "$APPDIR/MinbarLive.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/MinbarLive.png"

# Desktop entry at the AppDir root (required by appimagetool) and the standard
# location (used once the AppImage is integrated into the menu).
cp packaging/minbarlive.desktop "$APPDIR/MinbarLive.desktop"
cp packaging/minbarlive.desktop "$APPDIR/usr/share/applications/MinbarLive.desktop"

# AppRun launches the bundled binary from wherever the AppImage is mounted.
cat > "$APPDIR/AppRun" <<'SH'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "${HERE}/usr/bin/MinbarLive" "$@"
SH
chmod +x "$APPDIR/AppRun"

# A FUSE-less runtime (uruntime) so the finished AppImage runs on a plain
# double-click WITHOUT libfuse2 - modern Ubuntu (22.04+) does not ship it, and
# the default AppImage runtime hard-requires it. uruntime is a static (musl)
# runtime that self-mounts via a bundled squashfuse, or extract-and-runs, so it
# needs no system FUSE at all. Pinned for reproducibility.
URUNTIME_VERSION="v0.5.8"
RUNTIME="uruntime-appimage-squashfs-${ARCH}"
if [ ! -f "$RUNTIME" ]; then
  curl -fsSL -o "$RUNTIME" \
    "https://github.com/VHSgunzo/uruntime/releases/download/${URUNTIME_VERSION}/${RUNTIME}"
  chmod +x "$RUNTIME"
fi

# appimagetool is itself an AppImage. GitHub runners have no FUSE, so run IT with
# APPIMAGE_EXTRACT_AND_RUN=1; --runtime-file embeds uruntime as the output's
# runtime instead of appimagetool's default FUSE-requiring one.
TOOL="appimagetool-${ARCH}.AppImage"
if [ ! -x "$TOOL" ]; then
  curl -fsSL -o "$TOOL" \
    "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  chmod +x "$TOOL"
fi

ARCH="$ARCH" APPIMAGE_EXTRACT_AND_RUN=1 "./$TOOL" --runtime-file "$RUNTIME" "$APPDIR" "$OUT"
echo "Built $OUT"
