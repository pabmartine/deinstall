"""Executes the actual uninstall for a DetectionResult. Flatpak/AppImage
need no privilege escalation (just host execution, via hostexec.run_host,
since the Flatpak sandbox can't otherwise reach the host's package state).
apt/snap do need root, obtained via pkexec — following the pattern proven in
Looker's GRUB theme installer, but passing the validated script inline to
`pkexec bash -c` (through flatpak-spawn --host when sandboxed) rather than
staging it as a file: an on-disk script under xdg-data is user-writable, so
a local process could swap its contents between write and root execution
(TOCTOU). Inlining removes that window and the staging dir entirely. No
custom PolicyKit .policy file is needed: pkexec's built-in
`org.freedesktop.policykit.exec` action (auth_admin by default) covers this,
exactly like Looker already does for its privileged installer.
"""

import subprocess

from .detector import HintKey, Origin
from .hostexec import host_argv
from .i18n import translate as _

_PRIVILEGED_SCRIPT = r"""
set -euo pipefail
manager="$1"
pkg="$2"
if [[ ! "$pkg" =~ ^[a-z0-9][a-z0-9+.-]*$ ]]; then
    echo "Invalid package name: $pkg" >&2
    exit 65
fi
case "$manager" in
    apt) exec apt-get remove -y --purge -- "$pkg" ;;
    snap) exec snap remove -- "$pkg" ;;
    *) echo "Usage: deinstall <apt|snap> <package>" >&2; exit 64 ;;
esac
"""


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(host_argv(argv), capture_output=True, text=True, timeout=timeout, check=False)


def _uninstall_privileged(manager: str, pkg_id: str | None) -> tuple[bool, bool, str | None]:
    """Returns (ok, partial, error)."""
    if not pkg_id:
        return False, False, _("Could not determine the package name.")

    try:
        # `bash -c SCRIPT name arg1 arg2` → $0=name, $1=manager, $2=pkg.
        result = _run(
            ["pkexec", "bash", "-c", _PRIVILEGED_SCRIPT, "deinstall", manager, pkg_id],
            timeout=180)
    except subprocess.TimeoutExpired:
        return False, False, _("The operation timed out.")
    except FileNotFoundError as e:
        return False, False, _("Required command not available: %s") % e
    except Exception as e:
        return False, False, str(e)

    if result.returncode == 0:
        return True, False, None
    if result.returncode == 126:
        return False, False, _("Authentication was cancelled or denied.")
    if result.returncode == 127:
        return False, False, _("Authentication failed or no PolicyKit agent is available.")

    err = (result.stderr or result.stdout or "").strip()
    return False, False, err or _("The helper exited with code %d.") % result.returncode


def _uninstall_flatpak(result) -> tuple[bool, bool, str | None]:
    if not result.id:
        return False, False, _("Could not determine the Flatpak app ID.")
    try:
        proc = _run(["flatpak", "uninstall", "-y", "--delete-data", result.id], timeout=180)
    except Exception as e:
        return False, False, str(e)
    if proc.returncode == 0:
        return True, False, None

    stderr = (proc.stderr or "").strip()
    if "permission" in stderr.lower() or "not installed for the current user" in stderr.lower():
        return False, False, _("This looks like a system-wide Flatpak install (requires administrator privileges): %s") % stderr
    return False, False, stderr or _("flatpak uninstall failed with no further details.")


def _uninstall_appimage(result, desktop_path: str) -> tuple[bool, bool, str | None]:
    target = result.id  # resolved .AppImage path, when known
    file_removed = False
    if target:
        proc = _run(["gio", "trash", "--", target], timeout=15)
        file_removed = proc.returncode == 0

    _run(["gio", "trash", "--", desktop_path], timeout=15)

    if file_removed:
        return True, False, None
    return True, True, _(
        "Could not locate the .AppImage file; only the launcher was removed. "
        "Find and delete it by hand (hint: %s).") % desktop_path


# Only these two UNKNOWN sub-cases ever get an install folder we're
# confident enough about to offer deletion for (see
# detector._guess_manual_install_root) — never MANUAL_SYSTEM (that lives
# under /usr, a shared system location, not something to guess-delete) or
# the no-clues-at-all cases.
_MANUAL_UNINSTALLABLE_HINT_KEYS = (HintKey.MANUAL_HOME, HintKey.MANUAL_OPT)


def _uninstall_manual(result, desktop_path: str) -> tuple[bool, bool, str | None]:
    """Moves the guessed install folder (and the launcher) to the trash —
    never a permanent delete, since we're acting on a heuristic guess in
    the user's own files, not a package manager's authoritative record."""
    folder_removed = False
    if result.manual_target:
        proc = _run(["gio", "trash", "--", result.manual_target], timeout=30)
        folder_removed = proc.returncode == 0
        folder_error = (proc.stderr or "").strip()
    else:
        folder_error = None

    _run(["gio", "trash", "--", desktop_path], timeout=15)

    if folder_removed:
        return True, False, None

    if result.manual_target:
        return True, True, _(
            "Could not move “%s” to the trash; only the launcher was removed. "
            "Remove it by hand.") % result.manual_target
    return True, True, _(
        "Could not determine which folder to remove; only the launcher was removed. "
        "Find and delete the app’s files by hand.")


def uninstall(result, desktop_path: str) -> tuple[bool, bool, str | None]:
    """Attempts to uninstall the app described by `result`.
    Returns (ok, partial, error)."""
    if result.origin == Origin.FLATPAK:
        return _uninstall_flatpak(result)
    if result.origin == Origin.APPIMAGE:
        return _uninstall_appimage(result, desktop_path)
    if result.origin == Origin.APT:
        return _uninstall_privileged("apt", result.id)
    if result.origin == Origin.SNAP:
        return _uninstall_privileged("snap", result.id)
    if result.origin == Origin.UNKNOWN and result.hint_key in _MANUAL_UNINSTALLABLE_HINT_KEYS:
        return _uninstall_manual(result, desktop_path)
    return False, False, _("Unidentified origin; cannot uninstall automatically.")
