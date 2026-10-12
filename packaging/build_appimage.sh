#!/usr/bin/env bash
# Builds Cat_Printer-x86_64.AppImage inside an Ubuntu 22.04 container so that it only needs
# glibc >= 2.35 on the machine that runs it. Requires docker. Output: dist/
set -euo pipefail
cd "$(dirname "$0")/.."

docker run --rm -v "$PWD":/src -w /src ubuntu:22.04 bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-pip python3-venv libpython3.10 bluez binutils file wget ca-certificates \
    libglib2.0-0 libgl1 libegl1 libdbus-1-3 libfontconfig1 libxkbcommon0 libxkbcommon-x11-0 \
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
    libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 >/dev/null

python3 -m venv /tmp/venv
/tmp/venv/bin/pip install -q -r requirements.txt pyinstaller

rm -rf /tmp/pyi && /tmp/venv/bin/pyinstaller --noconfirm --clean --name CatPrinter --windowed \
    --distpath /tmp/pyi/dist --workpath /tmp/pyi/work --specpath /tmp/pyi \
    --collect-submodules bleak --collect-submodules dbus_next \
    --paths /src /src/gui.py

APPDIR=/tmp/AppDir
rm -rf $APPDIR && mkdir -p $APPDIR
cp -r /tmp/pyi/dist/CatPrinter $APPDIR/CatPrinter
cp packaging/catprinter.desktop $APPDIR/
cp packaging/catprinter.png $APPDIR/catprinter.png
cp packaging/catprinter.png $APPDIR/.DirIcon
cat > $APPDIR/AppRun <<"RUN"
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/CatPrinter/CatPrinter" "$@"
RUN
chmod +x $APPDIR/AppRun

wget -q -O /tmp/appimagetool https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x /tmp/appimagetool
mkdir -p dist
ARCH=x86_64 /tmp/appimagetool --appimage-extract-and-run --no-appstream $APPDIR dist/Cat_Printer-x86_64.AppImage
chown -R '"$(id -u):$(id -g)"' dist
'
echo "Done: dist/Cat_Printer-x86_64.AppImage"
