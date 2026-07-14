import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk

from .core.config import ConfigManager
from .core.constants import APP_ID
from .core.i18n import setup_locale, translate as _
from .ui.window import DeinstallWindow


class DeinstallApplication(Adw.Application):
    """GApplication ID matches the Flatpak app-id, so it's D-Bus activatable
    automatically once installed — the GNOME Shell extension triggers the
    'uninstall-app' action over the session bus to open us focused on a
    specific app, without the extension itself needing any package-manager
    or privilege-escalation logic."""

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None
        self.config = ConfigManager()

        language = self.config.get("language", "auto")
        setup_locale(language if language != "auto" else None)

        self.connect("activate", self.on_activate)
        self._setup_actions()

    def _setup_actions(self) -> None:
        uninstall_action = Gio.SimpleAction.new("uninstall-app", GLib.VariantType.new("s"))
        uninstall_action.connect("activate", self._on_uninstall_app_action)
        self.add_action(uninstall_action)

        language_action = Gio.SimpleAction.new_stateful(
            "language", GLib.VariantType.new("s"),
            GLib.Variant("s", self.config.get("language", "auto")))
        language_action.connect("activate", self.on_language_changed)
        self.add_action(language_action)

        preferences_action = Gio.SimpleAction.new("preferences", None)
        preferences_action.connect("activate", self.on_preferences)
        self.add_action(preferences_action)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_a: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

    def _add_icon_search_paths(self) -> None:
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display is None:
            return
        from .core.detector import HOST_ROOT

        icon_theme = Gtk.IconTheme.get_for_display(display)
        for path in (
            "/var/lib/flatpak/exports/share/icons",
            "/var/lib/snapd/desktop/icons",
            # /usr is Flatpak-reserved; host-os exposes it read-only under
            # /run/host instead (see core/detector.py's HOST_ROOT).
            HOST_ROOT + "/usr/share/icons",
        ):
            icon_theme.add_search_path(path)

    def on_activate(self, app: Adw.Application) -> None:
        self._add_icon_search_paths()
        if self.win is None:
            self.win = DeinstallWindow(application=app)
        self.win.present()

    def _on_uninstall_app_action(self, _action, parameter: GLib.Variant) -> None:
        desktop_path = parameter.get_string()
        self.on_activate(self)
        self.win.focus_app(desktop_path)

    def on_language_changed(self, action: Gio.SimpleAction, parameter: GLib.Variant) -> None:
        language_code = parameter.get_string()
        action.set_state(parameter)
        if self.win is not None:
            self.win.change_language(language_code)

    def on_preferences(self, *_args) -> None:
        if self.win is None:
            return
        self._show_preferences_dialog()

    def _show_preferences_dialog(self) -> None:
        dialog = Adw.PreferencesWindow()
        dialog.set_title(_("Preferences"))
        dialog.set_modal(True)
        dialog.set_transient_for(self.win)

        page = Adw.PreferencesPage()
        page.set_title(_("General"))

        language_group = Adw.PreferencesGroup()
        language_group.set_title(_("Language"))
        language_row = Adw.ComboRow()
        language_row.set_title(_("Interface Language"))
        language_row.set_subtitle(_("Save the preferred application language"))
        # Endonyms (each language in its own name); order mirrors the header
        # menu in ui/window.py. Only "Auto-detect" is translated.
        codes = ["auto", "en", "es", "de", "fr", "it", "pt", "ru", "zh", "ja", "hi"]
        labels = [_("Auto-detect"), "English", "Español", "Deutsch", "Français",
                  "Italiano", "Português", "Русский", "中文", "日本語", "हिन्दी"]
        language_row.set_model(Gtk.StringList.new(labels))
        current = self.win.current_language
        language_row.set_selected(codes.index(current) if current in codes else 0)
        language_row.connect("notify::selected", self._on_language_row_changed, codes)
        language_group.add(language_row)
        page.add(language_group)

        dialog.add(page)
        dialog.present()

    def _on_language_row_changed(self, combo_row, _param, codes: list[str]) -> None:
        selected = combo_row.get_selected()
        if selected < len(codes):
            action = self.lookup_action("language")
            if action is not None:
                action.activate(GLib.Variant("s", codes[selected]))

    def _on_about(self, *_args) -> None:
        from .core.constants import APP_NAME, APP_VERSION, APP_COPYRIGHT, APP_WEBSITE
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            version=APP_VERSION,
            copyright=APP_COPYRIGHT,
            website=APP_WEBSITE,
            application_icon=APP_ID,
            developer_name="pabmartine",
        )
        about.present(self.win)
