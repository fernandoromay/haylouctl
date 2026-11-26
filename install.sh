#!/bin/sh
# Install haylouctl for the current user — no root needed.
#   1. copy bin/haylouctl + haylouctl/ package -> ~/.local/share/haylouctl/
#      (re-run to update; the repo can be removed afterwards)
#   2. symlink the installed launcher -> ~/.local/bin/haylouctl
#   3. render assets/haylouctl.service   -> ~/.config/systemd/user/
#      (@INSTALL@ replaced with the install dir)
#   4. render assets/haylouctl.desktop   -> ~/.local/share/applications/
#      (@BIN@ replaced with ~/.local/bin)
#   5. copy assets/haylouctl.svg         -> hicolor scalable icon dir
#   6. daemon-reload, enable --now haylouctl.service,
#      update-desktop-database, gtk-update-icon-cache
#
# Uninstall: systemctl --user disable --now haylouctl.service, then delete
# ~/.local/share/haylouctl, the unit, the desktop entry and the icon.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)
INSTALL_DIR="$HOME/.local/share/haylouctl"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
UNIT="haylouctl.service"

mkdir -p "$INSTALL_DIR/bin" "$BIN_DIR" "$UNIT_DIR" "$APP_DIR" "$ICON_DIR"

rm -rf "$INSTALL_DIR/haylouctl"
cp -R "$REPO/haylouctl" "$INSTALL_DIR/haylouctl"
find "$INSTALL_DIR/haylouctl" -type d -name __pycache__ -exec rm -rf {} +

install -m 755 "$REPO/bin/haylouctl" "$INSTALL_DIR/bin/haylouctl"
ln -sfn "$INSTALL_DIR/bin/haylouctl" "$BIN_DIR/haylouctl"

sed "s|@INSTALL@|$INSTALL_DIR|g" "$REPO/assets/haylouctl.service" \
    > "$UNIT_DIR/$UNIT"
sed -e "s|@INSTALL@|$INSTALL_DIR|g" -e "s|@BIN@|$BIN_DIR|g" \
    "$REPO/assets/haylouctl.desktop" > "$APP_DIR/haylouctl.desktop"

install -m 644 "$REPO/assets/haylouctl.svg" "$ICON_DIR/haylouctl.svg"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APP_DIR"
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

echo "installed:"
echo "  $INSTALL_DIR"
echo "  $BIN_DIR/haylouctl"
echo "  $UNIT_DIR/$UNIT"
echo "  $APP_DIR/haylouctl.desktop"
echo "  $ICON_DIR/haylouctl.svg"
