import sys
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from .app import DeinstallApplication


def main() -> int:
    try:
        app = DeinstallApplication()
        return app.run(sys.argv)
    except Exception as exc:
        print(f"CRITICAL ERROR in main: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
