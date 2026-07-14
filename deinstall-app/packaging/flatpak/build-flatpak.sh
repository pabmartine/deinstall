#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_ID="com.pabmartine.Deinstall"
BUILD_DIR="$ROOT_DIR/build-dir"
REPO_DIR="$ROOT_DIR/repo"
MANIFEST="$ROOT_DIR/packaging/flatpak/com.pabmartine.Deinstall.yaml"

echo "Preparing Flatpak build..."

rm -rf "$BUILD_DIR" "$REPO_DIR"

echo "Checking Flatpak runtimes..."
if ! flatpak list --runtime | grep -q "org.gnome.Platform.*50"; then
    echo "Installing GNOME Platform 50 runtime..."
    flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50 -y
fi

echo "Building Flatpak..."
flatpak-builder --user --install --force-clean "$BUILD_DIR" "$MANIFEST"

echo "Creating local repository..."
flatpak-builder --user --repo="$REPO_DIR" --force-clean "$BUILD_DIR" "$MANIFEST"

echo "Creating bundle..."
flatpak build-bundle "$REPO_DIR" "$ROOT_DIR/$APP_ID.flatpak" "$APP_ID"

echo "Flatpak built successfully."
echo
echo "Bundle generated:"
echo "  $ROOT_DIR/$APP_ID.flatpak"
echo
echo "Useful commands:"
echo "  Install the bundle:"
echo "    flatpak install --user \"$ROOT_DIR/$APP_ID.flatpak\""
echo
echo "  Run the installed app:"
echo "    flatpak run $APP_ID"
