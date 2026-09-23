#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.rfc.prices.daily"
PLIST_SRC="$SCRIPT_DIR/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$(command -v python3)"

mkdir -p "$HOME/Library/LaunchAgents" "$SCRIPT_DIR/data"

tmp="$(mktemp)"
sed \
  -e "s|__SCRIPT_PATH__|$SCRIPT_DIR|g" \
  -e "s|/usr/bin/python3|$PYTHON|g" \
  "$PLIST_SRC" > "$tmp"
mv "$tmp" "$PLIST_DST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Установлен ежедневный запуск в 09:00"
echo "Plist: $PLIST_DST"
echo "Лог:   $SCRIPT_DIR/data/daily.log"
echo
echo "Проверить сейчас:"
echo "  launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "Отключить:"
echo "  launchctl bootout gui/$(id -u)/$LABEL"
