import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import { AppMenu } from 'resource:///org/gnome/shell/ui/appMenu.js';
import { AppIcon } from 'resource:///org/gnome/shell/ui/appDisplay.js';
import { Extension, InjectionManager, gettext as _ } from 'resource:///org/gnome/shell/extensions/extension.js';

// This extension does no detection or uninstalling itself — that logic
// (and the privileged apt/snap removal) lives entirely in the companion
// GTK4/libadwaita app "Deinstall" (com.pabmartine.Deinstall), distributed
// as a Flatpak. Keeping the extension this thin is what makes it plausible
// to publish on extensions.gnome.org: it never spawns subprocesses, reads
// no files outside what Shell.App already exposes, and only talks to the
// companion app over the session D-Bus (GApplication's org.gtk.Actions,
// exported automatically because the app is DBusActivatable).
const COMPANION_APP_ID = 'com.pabmartine.Deinstall.desktop';
const COMPANION_BUS_NAME = 'com.pabmartine.Deinstall';
const COMPANION_OBJECT_PATH = '/com/pabmartine/Deinstall';

export default class DeinstallExtension extends Extension {
    enable() {
        this.initTranslations();

        this._menus = new Set();
        // InjectionManager handles the save/restore of the overridden method
        // safely, including when other extensions patch the same method —
        // manual prototype swapping breaks that chain on disable.
        this._injectionManager = new InjectionManager();
        this._injectionManager.overrideMethod(AppMenu.prototype, 'setApp', original => {
            const extension = this;
            return function (app) {
                original.call(this, app);
                extension._onSetApp(this, app);
            };
        });
    }

    disable() {
        this._injectionManager.clear();
        this._injectionManager = null;

        // Restoring the method stops *new* menus getting the item, but the
        // menus we already injected into keep their separator + action (and a
        // closure over this now-disabled extension). EGO review requires them
        // gone on disable, so tear them down explicitly.
        for (const menu of this._menus ?? []) {
            menu._deinstallItem?.destroy();
            menu._deinstallSeparator?.destroy();
            delete menu._deinstallItem;
            delete menu._deinstallSeparator;
            delete menu._deinstallApp;
        }
        this._menus = null;
    }

    _ensureItem(menu) {
        if (menu._deinstallItem)
            return menu._deinstallItem;

        menu._deinstallSeparator = new PopupMenu.PopupSeparatorMenuItem();
        menu.addMenuItem(menu._deinstallSeparator);
        // Read our own stored app at click time (not the Shell-private
        // menu._app): the menu may have been re-pointed at another app since,
        // and _deinstallApp always tracks the current one via _onSetApp.
        menu._deinstallItem = menu.addAction(_('Uninstall'), () => {
            this._onActivated(menu._deinstallApp);
        });
        this._menus.add(menu);
        return menu._deinstallItem;
    }

    _onSetApp(menu, app) {
        // AppMenu is shared by every right-click app menu in the shell —
        // the app grid, the dash/taskbar (stock or via dock extensions like
        // dash-to-panel/ubuntu-dock, which reuse this same class), alt-tab,
        // etc. Only AppDisplay.AppIcon identifies the app grid specifically;
        // skip building the item at all anywhere else, so "Uninstall" shows
        // up only on app grid entries, not on taskbar/dock icons.
        if (!(menu.sourceActor instanceof AppIcon)) {
            if (menu._deinstallItem) {
                menu._deinstallItem.visible = false;
                menu._deinstallSeparator.visible = false;
            }
            return;
        }

        // Keep the separator's visibility tied to the item's, so an app with
        // no .desktop file (item hidden) doesn't leave a stray separator
        // dangling at the bottom of the menu.
        const item = this._ensureItem(menu);
        menu._deinstallApp = app;
        const visible = Boolean(app?.app_info?.get_filename());
        item.visible = visible;
        menu._deinstallSeparator.visible = visible;
    }

    _isCompanionAppInstalled() {
        try {
            return Gio.DesktopAppInfo.new(COMPANION_APP_ID) !== null;
        } catch (e) {
            return false;
        }
    }

    _onActivated(app) {
        const desktopPath = app?.app_info?.get_filename();
        if (!desktopPath)
            return;

        Main.overview.hide();

        if (!this._isCompanionAppInstalled()) {
            Main.notify(
                _('The “Deinstall” app is required'),
                _('Install the “Deinstall” companion app to uninstall applications from this menu.'));
            return;
        }

        this._activateCompanion(desktopPath);
    }

    _activateCompanion(desktopPath) {
        const parameters = new GLib.Variant('(sava{sv})', [
            'uninstall-app',
            [new GLib.Variant('s', desktopPath)],
            {},
        ]);

        Gio.DBus.session.call(
            COMPANION_BUS_NAME, COMPANION_OBJECT_PATH, 'org.gtk.Actions', 'Activate',
            parameters, null, Gio.DBusCallFlags.NONE, -1, null,
            (connection, result) => {
                try {
                    connection.call_finish(result);
                } catch (e) {
                    Main.notify(_('Deinstall'), _('Could not reach the Deinstall app: %s').format(e.message));
                }
            });
    }
}
