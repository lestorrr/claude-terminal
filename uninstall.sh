#!/usr/bin/env bash
# Remove Claude Terminal's launchers, icon and command.
# Your Claude Code chats in ~/.claude are never touched.
# --purge also removes the app's own settings and index cache.
set -euo pipefail

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

rm -f "$HOME/.local/bin/claude-terminal" \
      "$HOME/.local/share/applications/claude-terminal.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/claude-terminal.svg" \
      "$DESKTOP_DIR/claude-terminal.desktop"
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true

if [[ "${1:-}" == "--purge" ]]; then
    rm -f "$HOME/.config/claude-terminal/config.json" "$HOME/.cache/claude-terminal/index.json"
    rmdir "$HOME/.config/claude-terminal" "$HOME/.cache/claude-terminal" 2>/dev/null || true
    echo "Removed launchers, settings and cache."
else
    echo "Removed launchers. Settings kept in ~/.config/claude-terminal (use --purge to remove)."
fi
