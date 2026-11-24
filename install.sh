#!/bin/sh
# Install haylouctl: launcher symlink, systemd user unit, desktop entry.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
APP_DIR="$HOME/.local/share/applications"
UNIT="haylouctl.service"

mkdir -p "$BIN_DIR" "$UNIT_DIR" "$APP_DIR"

chmod +x "$REPO/bin/haylouctl"
ln -sfn "$REPO/bin/haylouctl" "$BIN_DIR/haylouctl"

sed "s|@REPO@|$REPO|g" "$REPO/assets/haylouctl.service" > "$UNIT_DIR/$UNIT"
sed -e "s|@REPO@|$REPO|g" -e "s|@BIN@|$BIN_DIR|g" \
    "$REPO/assets/haylouctl.desktop" > "$APP_DIR/haylouctl.desktop"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APP_DIR"
fi

echo "installed:"
echo "  $BIN_DIR/haylouctl"
echo "  $UNIT_DIR/$UNIT"
echo "  $APP_DIR/haylouctl.desktop"
