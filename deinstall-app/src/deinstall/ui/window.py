import datetime
import os
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk

from ..core import detector, uninstaller
from ..core.constants import APP_ID, APP_NAME
from ..core.i18n import translate as _

def _origin_label(result: detector.DetectionResult) -> str:
    # Built on each call, not once at import: this module is imported before
    # the app applies the configured language, so binding the strings at
    # import time would freeze them to the system locale (and no restart
    # would fix it, since the import order is the same every launch).
    if result.origin == detector.Origin.UNKNOWN:
        # More specific label when we at least narrowed down *why* it's
        # unknown (e.g. "tarball you extracted yourself" vs "no package
        # manager owns this binary") — even though neither auto-uninstalls.
        sublabel = {
            detector.HintKey.WEBAPP: _("Web app (browser)"),
            detector.HintKey.MANUAL_HOME: _("Manual install (home folder)"),
            detector.HintKey.MANUAL_OPT: _("Manual install (/opt)"),
            detector.HintKey.MANUAL_SYSTEM: _("Manual install (no package manager)"),
        }.get(result.hint_key)
        if sublabel:
            return sublabel
    return {
        detector.Origin.FLATPAK: _("Flatpak"),
        detector.Origin.SNAP: _("Snap"),
        detector.Origin.APT: _("System package (apt)"),
        detector.Origin.APPIMAGE: _("AppImage"),
        detector.Origin.UNKNOWN: _("Unidentified origin"),
    }.get(result.origin, result.origin)

ORIGIN_ICONS = {
    detector.Origin.FLATPAK: "application-x-addon-symbolic",
    detector.Origin.SNAP: "application-x-appliance-symbolic",
    detector.Origin.APT: "package-x-generic-symbolic",
    detector.Origin.APPIMAGE: "application-x-executable-symbolic",
    detector.Origin.UNKNOWN: "dialog-question-symbolic",
}


def _translate_usr_path(path: str) -> str:
    """/usr is Flatpak-reserved and only visible under /run/host/usr inside
    the sandbox (see HOST_ROOT in core/detector.py) — any absolute /usr/...
    path handed to us (an icon path, whether from our own lenient parsing or
    from GDesktopAppInfo, which has the same blind spot) needs the same
    translation, or the sandbox looks in its own runtime's /usr instead."""
    if detector.HOST_ROOT and path.startswith("/usr/"):
        return detector.HOST_ROOT + path
    return path


def _fixup_icon(gicon: Gio.Icon | None) -> Gio.Icon | None:
    if isinstance(gicon, Gio.FileIcon):
        path = gicon.get_file().get_path()
        if path:
            fixed = _translate_usr_path(path)
            if fixed != path:
                return Gio.FileIcon.new(Gio.File.new_for_path(fixed))
    return gicon


def _icon_from_entry(entry: dict | None) -> Gio.Icon | None:
    """Mirrors how GDesktopAppInfo resolves Icon=: an absolute path is a
    file icon, anything else is a themed icon name looked up on the
    search path (hicolor, etc)."""
    icon_value = (entry or {}).get("icon")
    if not icon_value:
        return None
    if icon_value.startswith("/"):
        return Gio.FileIcon.new(Gio.File.new_for_path(_translate_usr_path(icon_value)))
    return Gio.ThemedIcon.new(icon_value)


class AppRow(Adw.ActionRow):
    def __init__(self, desktop_path: str, window: "DeinstallWindow", result: detector.DetectionResult | None = None):
        super().__init__()
        self.desktop_path = desktop_path
        self.window = window
        self.result = None

        # Gio.DesktopAppInfo is the "proper" way to get a name/icon, but it
        # validates strictly and fails (raises, doesn't return None) for many
        # real-world .desktop files. When it does, fall back to our own
        # lenient parser instead of the raw filename — the raw filename is
        # frequently just the package/binary name, not what a user calls
        # the app (e.g. "google-chrome.desktop" instead of "Google Chrome").
        try:
            info = Gio.DesktopAppInfo.new_from_filename(desktop_path)
        except (TypeError, GLib.Error):
            info = None
        entry = None if info else detector.parse_desktop_file(desktop_path)

        if info:
            self.app_name = info.get_display_name()
        elif entry and entry.get("name"):
            self.app_name = entry["name"]
        else:
            self.app_name = os.path.splitext(os.path.basename(desktop_path))[0]

        self.set_title(GLib.markup_escape_text(self.app_name))
        if result is None:
            self.set_subtitle(_("Detecting origin…"))

        icon = Gtk.Image()
        icon.set_pixel_size(32)
        gicon = _fixup_icon(info.get_icon()) if info else _icon_from_entry(entry)
        if gicon:
            icon.set_from_gicon(gicon)
        else:
            icon.set_from_icon_name("application-x-executable")
        self.add_prefix(icon)

        self.origin_icon = Gtk.Image.new_from_icon_name("dialog-question-symbolic")
        self.origin_icon.add_css_class("dim-label")
        self.add_suffix(self.origin_icon)

        self.uninstall_button = Gtk.Button(label=_("Uninstall"), valign=Gtk.Align.CENTER)
        self.uninstall_button.add_css_class("flat")
        self.uninstall_button.set_sensitive(False)
        self.uninstall_button.connect("clicked", self._on_uninstall_clicked)
        self.add_suffix(self.uninstall_button)

        try:
            self.install_mtime = os.path.getmtime(desktop_path)
            self.install_date = datetime.datetime.fromtimestamp(self.install_mtime).strftime("%Y-%m-%d")
        except OSError:
            self.install_mtime = 0.0
            self.install_date = None

        if result is not None:
            self.set_result(result)

    def set_result(self, result: detector.DetectionResult) -> None:
        self.result = result
        label = _origin_label(result)
        subtitle = f"{label} · {self.install_date}" if self.install_date else label
        self.set_subtitle(GLib.markup_escape_text(subtitle))
        self.origin_icon.set_from_icon_name(ORIGIN_ICONS.get(result.origin, "dialog-question-symbolic"))
        can_uninstall = (
            result.origin != detector.Origin.UNKNOWN
            or result.hint_key in (detector.HintKey.MANUAL_HOME, detector.HintKey.MANUAL_OPT)
        )
        self.uninstall_button.set_sensitive(can_uninstall)
        if result.origin == detector.Origin.UNKNOWN and result.hint:
            self.set_tooltip_text(result.hint)

    def _on_uninstall_clicked(self, _button) -> None:
        self.window.confirm_uninstall(self)


class DeinstallWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application, title=_("Deinstall"))
        self.set_default_size(640, 720)
        self.rows: dict[str, AppRow] = {}
        self._seen_origin_ids: dict[tuple[str, str], str] = {}

        self.app = application
        self.config = application.config
        self.current_language = self.config.get("language", "auto")

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search applications…"))
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.set_visible(False)  # only meaningful once the list is showing
        header.set_title_widget(self.search_entry)

        self._sort_mode = "date-desc"
        self._setup_sort_action()

        self.sort_button = Gtk.MenuButton(icon_name="view-sort-descending-symbolic")
        self.sort_button.set_tooltip_text(_("Sort order"))
        self.sort_button.set_menu_model(self._build_sort_menu())
        self.sort_button.set_visible(False)  # only meaningful once the list is showing
        header.pack_start(self.sort_button)

        self._scope_filter = "user"
        self._setup_scope_filter_action()

        self.filter_button = Gtk.MenuButton(icon_name="filter-symbolic")
        self.filter_button.set_tooltip_text(_("Filter"))
        self.filter_button.set_menu_model(self._build_scope_filter_menu())
        self.filter_button.set_visible(False)  # only meaningful once the list is showing
        header.pack_start(self.filter_button)

        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        self.menu_button.set_tooltip_text(_("Main menu"))
        self.menu_button.set_menu_model(self._build_app_menu())
        self.menu_button.set_visible(False)  # only meaningful once the list is showing
        header.pack_end(self.menu_button)

        self.spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        scanning_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        scanning_box.append(self.spinner)
        scanning_box.append(Gtk.Label(label=_("Scanning installed applications…"), css_classes=["dim-label"]))

        self.status_page = Adw.StatusPage(
            icon_name=APP_ID,
            title=APP_NAME,
            description=_("Lists every application installed on your system — apt, snap, Flatpak, or AppImage — so you can uninstall it cleanly from one place."),
            child=scanning_box,
        )

        self.list_box = Gtk.ListBox(css_classes=["boxed-list"])
        self.list_box.set_filter_func(self._filter_row)
        self.list_box.set_sort_func(self._sort_rows)
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.set_placeholder(Adw.StatusPage(
            title=_("No matching applications"),
            icon_name="edit-find-symbolic",
            vexpand=False,
        ))

        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=700)
        clamp.set_child(self.list_box)
        clamp.set_margin_top(12)
        clamp.set_margin_bottom(12)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        scroller.set_child(clamp)

        self.stack = Gtk.Stack()
        self.stack.add_named(self.status_page, "loading")
        self.stack.add_named(scroller, "list")
        self.stack.set_visible_child_name("loading")

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.stack)
        toolbar_view.set_content(self.toast_overlay)
        self.set_content(toolbar_view)

        self._add_search_shortcut()
        self._load_apps_async()

    def _add_search_shortcut(self) -> None:
        controller = Gtk.ShortcutController()
        controller.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>f"),
            Gtk.CallbackAction.new(lambda *_a: (self.search_entry.grab_focus(), True)[1]),
        ))
        self.add_controller(controller)

    def _filter_row(self, row: AppRow) -> bool:
        if self._scope_filter != "all" and row.result and row.result.scope != self._scope_filter:
            return False
        query = self.search_entry.get_text().strip().lower()
        if not query:
            return True
        return query in row.app_name.lower()

    def _on_search_changed(self, _entry) -> None:
        self.list_box.invalidate_filter()

    def _setup_sort_action(self) -> None:
        sort_action = Gio.SimpleAction.new_stateful(
            "sort", GLib.VariantType.new("s"), GLib.Variant.new_string(self._sort_mode))
        sort_action.connect("activate", self._on_sort_action_activated)
        self.add_action(sort_action)

    def _build_sort_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        menu.append(_("Name (A–Z)"), "win.sort('name-asc')")
        menu.append(_("Name (Z–A)"), "win.sort('name-desc')")
        menu.append(_("Date (newest first)"), "win.sort('date-desc')")
        menu.append(_("Date (oldest first)"), "win.sort('date-asc')")
        return menu

    def _on_sort_action_activated(self, action: Gio.SimpleAction, parameter: GLib.Variant) -> None:
        action.set_state(parameter)
        self._sort_mode = parameter.get_string()
        self.list_box.invalidate_sort()

    def _setup_scope_filter_action(self) -> None:
        action = Gio.SimpleAction.new_stateful(
            "scope-filter", GLib.VariantType.new("s"), GLib.Variant.new_string(self._scope_filter))
        action.connect("activate", self._on_scope_filter_activated)
        self.add_action(action)

    def _build_scope_filter_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        menu.append(_("All applications"), "win.scope-filter('all')")
        menu.append(_("User-installed"), "win.scope-filter('user')")
        menu.append(_("System"), "win.scope-filter('system')")
        return menu

    def _on_scope_filter_activated(self, action: Gio.SimpleAction, parameter: GLib.Variant) -> None:
        action.set_state(parameter)
        self._scope_filter = parameter.get_string()
        self.list_box.invalidate_filter()

    def _build_app_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        # Language names are shown as endonyms (each in its own script), the
        # standard GNOME approach — so they read the same regardless of the
        # current UI language and don't each need translating into every other.
        language_menu = Gio.Menu()
        language_menu.append(_("Auto-detect"), "app.language::auto")
        language_menu.append("English", "app.language::en")
        language_menu.append("Español", "app.language::es")
        language_menu.append("Deutsch", "app.language::de")
        language_menu.append("Français", "app.language::fr")
        language_menu.append("Italiano", "app.language::it")
        language_menu.append("Português", "app.language::pt")
        language_menu.append("Русский", "app.language::ru")
        language_menu.append("中文", "app.language::zh")
        language_menu.append("日本語", "app.language::ja")
        language_menu.append("हिन्दी", "app.language::hi")
        menu.append(_("Preferences"), "app.preferences")
        menu.append_submenu(_("Language"), language_menu)
        menu.append(_("About Deinstall"), "app.about")
        menu.append(_("Quit"), "app.quit")
        return menu

    def change_language(self, language_code: str) -> None:
        from ..core.i18n import setup_locale
        setup_locale(language_code if language_code != "auto" else None)
        self.config.set("language", language_code)
        self.current_language = language_code
        self.toast_overlay.add_toast(Adw.Toast(
            title=_("Language preference saved. Restart the app to apply it everywhere."), timeout=5))

    def _sort_rows(self, row_a: AppRow, row_b: AppRow) -> int:
        if self._sort_mode == "name-desc":
            a, b = row_b.app_name.lower(), row_a.app_name.lower()
        elif self._sort_mode == "date-desc":
            a, b = row_b.install_mtime, row_a.install_mtime
        elif self._sort_mode == "date-asc":
            a, b = row_a.install_mtime, row_b.install_mtime
        else:  # name-asc
            a, b = row_a.app_name.lower(), row_b.app_name.lower()
        return (a > b) - (a < b)

    def _load_apps_async(self) -> None:
        # Detection now takes ~3s total (see detect_origins_bulk), so it's
        # worth staying on the loading screen a little longer and handing
        # the list over already fully populated — icons, names, origins,
        # sort order all in place at once — rather than showing empty rows
        # that fill in one by one right after the window appears.
        def worker():
            paths = detector.list_desktop_files()
            try:
                results = detector.detect_origins_bulk(paths)
            except Exception as e:
                results = {p: detector.DetectionResult(detector.Origin.UNKNOWN, detail=str(e)) for p in paths}
            GLib.idle_add(self._on_data_ready, paths, results)

        threading.Thread(target=worker, daemon=True).start()

    def _on_data_ready(self, paths: list[str], results: dict[str, detector.DetectionResult]) -> bool:
        for path in paths:
            result = results.get(path)
            row = AppRow(path, self, result)
            self.rows[path] = row  # keep even duplicates reachable for focus_app()

            # Some packages install more than one .desktop launcher for the
            # same underlying app (same origin + id) — show only the first
            # one. Apps genuinely installed twice through *different*
            # channels (e.g. both a snap and an apt build) keep distinct
            # ids and both stay visible, since that's real duplication
            # worth cleaning up.
            if result and result.id:
                key = (result.origin, result.id)
                if key in self._seen_origin_ids:
                    continue
                self._seen_origin_ids[key] = path

            self.list_box.append(row)

        self.stack.set_visible_child_name("list")
        # Controls only meaningful once the list is showing.
        for widget in (self.search_entry, self.sort_button, self.filter_button, self.menu_button):
            widget.set_visible(True)
        return GLib.SOURCE_REMOVE

    def focus_app(self, desktop_path: str) -> None:
        """Opens the confirm dialog for a specific app — used when the
        GNOME Shell extension activates us via D-Bus. Rows only exist once
        the initial scan+detection batch finishes, so retry briefly if the
        window is still on its loading screen."""
        row = self.rows.get(desktop_path)
        if row is not None:
            self.confirm_uninstall(row)
            return
        # Not found (yet). Retry only while the scan is still running; once the
        # list is showing, an absent path just means the app isn't listed —
        # stop, or we'd poll forever.
        if self.stack.get_visible_child_name() != "list":
            GLib.timeout_add(300, lambda: (self.focus_app(desktop_path), False)[1])

    def confirm_uninstall(self, row: AppRow) -> None:
        result = row.result
        is_manual = result and result.hint_key in (detector.HintKey.MANUAL_HOME, detector.HintKey.MANUAL_OPT)
        if result is None or (result.origin == detector.Origin.UNKNOWN and not is_manual):
            if result and result.hint:
                self._show_toast(row.app_name, result.hint)
            return

        label = _origin_label(result)
        body = _("Detected origin: %s.") % label
        if result.origin == detector.Origin.FLATPAK:
            body += "\n" + _("This will also remove its user data (Flatpak --delete-data).")
        elif result.origin in (detector.Origin.APT, detector.Origin.SNAP):
            body += "\n" + _("You will be asked to authenticate as an administrator.")
            if result.scope == detector.Scope.SYSTEM:
                body += "\n" + _("⚠️ This package is part of the base system — removing it may break your desktop or other software. Only proceed if you’re sure.")
        elif is_manual:
            if result.manual_target:
                body += "\n" + _("This isn’t managed by a package manager — this is a guess. The folder below (and the launcher) will be moved to the trash, so you can recover it if the guess was wrong:\n%s") % result.manual_target
            else:
                body += "\n" + _("This isn’t managed by a package manager and the app’s install folder couldn’t be determined — only the launcher will be removed.")

        dialog = Adw.AlertDialog(
            heading=_("Uninstall “%s”?") % row.app_name,
            body=body,
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("uninstall", _("Uninstall"))
        dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_confirm_response, row)
        dialog.present(self)

    def _on_confirm_response(self, dialog, response: str, row: AppRow) -> None:
        if response != "uninstall":
            return

        row.uninstall_button.set_sensitive(False)
        row.set_subtitle(_("Uninstalling… this may take a few seconds."))

        def worker():
            ok, partial, error = uninstaller.uninstall(row.result, row.desktop_path)
            GLib.idle_add(self._on_uninstall_done, row, ok, partial, error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_uninstall_done(self, row: AppRow, ok: bool, partial: bool, error: str | None) -> bool:
        if ok and not partial:
            row.set_subtitle(_("Uninstalled successfully."))
            row.uninstall_button.set_visible(False)
            self.toast_overlay.add_toast(Adw.Toast(title=_("“%s” uninstalled.") % row.app_name, timeout=4))
        elif ok and partial:
            row.set_subtitle(_("Partial uninstall — see details."))
            self._show_toast(row.app_name, error or "")
        else:
            row.uninstall_button.set_sensitive(True)
            if row.result:
                row.set_result(row.result)
            self._show_toast(row.app_name, error or _("Unknown error."))
        return GLib.SOURCE_REMOVE

    def _show_toast(self, app_name: str, detail: str) -> None:
        """A short, non-blocking toast with a "Details" button for the full
        message — avoids a modal dialog for what's often just informational
        feedback, reserving AlertDialog for the actual destructive confirm."""
        toast = Adw.Toast(title=app_name, timeout=6)
        toast.set_button_label(_("Details"))
        toast.connect("button-clicked", lambda _t: self._show_detail_dialog(app_name, detail))
        self.toast_overlay.add_toast(toast)

    def _show_detail_dialog(self, app_name: str, detail: str) -> None:
        dialog = Adw.AlertDialog(heading=app_name, body=detail)
        dialog.add_response("close", _("Close"))
        dialog.present(self)
