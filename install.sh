#!/usr/bin/env bash
# Install Claude Terminal for this user: app-menu entry, desktop icon, and a
# `claude-terminal` command. Needs no root; the app runs from this folder.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
ICON="$ICON_DIR/claude-terminal.svg"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

# -- dependencies (checked only; installing them needs sudo) ---------------
missing=()
for pkg in python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-webkit2-4.1 gir1.2-handy-1 \
           python3-markdown python3-pygments; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Missing packages: ${missing[*]}"
    echo "Install them first:  sudo apt install ${missing[*]}"
    exit 1
fi
if ! command -v claude >/dev/null 2>&1 && [[ ! -x "$BIN_DIR/claude" ]]; then
    echo "Warning: the claude command was not found. Install Claude Code, or the app can't start chats."
fi

# -- command + icon --------------------------------------------------------
chmod +x "$APP_DIR/app.py"
mkdir -p "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"
ln -sf "$APP_DIR/app.py" "$BIN_DIR/claude-terminal"
install -m 644 "$APP_DIR/icon.svg" "$ICON"
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

# -- launchers ---------------------------------------------------------------
write_entry() {
    cat > "$1" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Claude Terminal
GenericName=Claude Code Chats
Comment=Start and resume Claude Code chats from a ChatGPT-style window
Exec=python3 $APP_DIR/app.py
Icon=$ICON
Terminal=false
Categories=Development;
Keywords=claude;ai;chat;terminal;code;resume;
StartupNotify=true
StartupWMClass=claude-terminal
EOF
}

write_entry "$APPS_DIR/claude-terminal.desktop"
update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

if [[ -d "$DESKTOP_DIR" ]]; then
    write_entry "$DESKTOP_DIR/claude-terminal.desktop"
    chmod +x "$DESKTOP_DIR/claude-terminal.desktop"
    # Nemo shows an "untrusted launcher" prompt without this.
    gio set "$DESKTOP_DIR/claude-terminal.desktop" metadata::trusted true 2>/dev/null || true
fi

echo "Claude Terminal installed."
echo "  App menu:  Claude Terminal"
[[ -d "$DESKTOP_DIR" ]] && echo "  Desktop:   $DESKTOP_DIR/claude-terminal.desktop"
echo "  Command:   claude-terminal"
