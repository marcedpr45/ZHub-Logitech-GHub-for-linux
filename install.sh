#!/bin/sh
# Instala ZHub para el usuario actual, sin necesidad de root.
set -eu
SRC=$(dirname "$(readlink -f "$0")")
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
APP=$DATA/zhub
BIN=$HOME/.local/bin

python3 -c 'import PyQt6, evdev' || { echo "Faltan dependencias: instala python-pyqt6 y python-evdev." >&2; exit 1; }
rm -rf "$APP"
mkdir -p "$APP" "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
cp -r "$SRC/zhub" "$SRC/zhub.py" "$APP/"
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
python3 -m compileall -q "$APP/zhub"
cp "$SRC/data/zhub.svg" "$DATA/icons/hicolor/scalable/apps/zhub.svg"
cat > "$BIN/zhub" <<WRAPPER
#!/bin/sh
exec python3 "$APP/zhub.py" "\$@"
WRAPPER
chmod +x "$BIN/zhub"
cat > "$DATA/applications/zhub.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=ZHub
Comment=Perfiles y ajustes de periféricos Logitech
Exec=$BIN/zhub
Icon=zhub
Categories=Settings;HardwareSettings;
DESKTOP
echo "ZHub instalado. Ábrelo desde el menú o con: zhub"
if [ ! -e /etc/udev/rules.d/70-zhub.rules ] && [ ! -e /etc/udev/rules.d/70-linhub.rules ]; then
    echo "Falta la regla udev (DPI, Hz y batería); consulta el README."
fi
