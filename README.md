# Deinstall

Uninstall any application from your GNOME desktop — apt, Snap, Flatpak, or
AppImage — from one place.

This repository holds two components that work together:

- **[`deinstall-app/`](./deinstall-app)** — a GTK4/libadwaita app that lists
  every installed application, automatically detects which package manager owns
  each one (apt, Snap, Flatpak, or AppImage), and uninstalls it cleanly.
- **[`deinstall-extension/`](./deinstall-extension)** — a GNOME Shell extension
  that adds an **Uninstall** item to the app grid's context menu and launches
  the app (over D-Bus) focused on the chosen application.

The extension does no detection or uninstalling itself — it is a thin bridge to
the app, which is where all the logic (and the privileged apt/Snap removal)
lives. The app is fully usable on its own; the extension just adds a convenient
entry point.

## Getting started

See each component's own README:

- [Deinstall app](./deinstall-app/README.md)
- [Deinstall GNOME Shell extension](./deinstall-extension/README.md)

## License

Both components are released under the [GPL-3.0](./deinstall-app/LICENSE)
license.
