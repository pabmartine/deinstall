"""Detects how an installed application was installed (apt, snap, flatpak,
AppImage, or unknown). Ported from the GNOME Shell extension's detector.js,
adapted for running inside a Flatpak sandbox: anything that needs to touch
the host outside the directories we've explicitly bind-mounted (checking if
a binary exists, resolving symlinks, asking dpkg who owns a file) goes
through hostexec.run_host() instead of local filesystem/PATH calls.
"""

import configparser
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .hostexec import is_flatpak, run_host, run_host_stdin

# /usr is a Flatpak-reserved path — it can't be bind-mounted directly via
# --filesystem=/usr/..., only exposed (read-only) under /run/host via the
# host-os permission. Everything outside /usr (snap, flatpak dirs) mounts at
# its real absolute path instead, since those aren't reserved.
HOST_ROOT = "/run/host" if is_flatpak() else ""

FLATPAK_DIRS = [
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
]
SNAP_DIR = "/var/lib/snapd/desktop/applications"
SYSTEM_APPS_DIR = HOST_ROOT + "/usr/share/applications"
USER_APPS_DIR = os.path.expanduser("~/.local/share/applications")

# "webapp-<name><digits>.desktop" is the ICE/browser generator's naming
# scheme for a per-site shortcut (e.g. "webapp-AmazonMusic1370.desktop").
# A bare "^webapp-" prefix is too broad — it also matches real installed
# apps that happen to be named that way (e.g. Linux Mint's own
# "webapp-manager.desktop", the "Web Apps" management tool itself, a real
# apt package). Require the trailing numeric ID that only the generator
# produces; chrome-/msedge- (profile-hash based) don't have that problem.
WEBAPP_BASENAME = re.compile(r"^(chrome-|msedge-)|^webapp-.*\d\.desktop$")

# Snaps Ubuntu itself installs as part of the base desktop image, as opposed
# to something the user chose to install. No official "system snap" flag
# exists, so this is a curated allowlist — same best-effort spirit as the
# rest of this module. core*/bare are runtime bases, never user-facing apps.
SYSTEM_SNAP_NAMES = {
    "snapd", "bare", "snap-store", "firmware-updater",
    "snapd-desktop-integration", "gtk-common-themes",
    "desktop-security-center", "prompting-client",
}
_SYSTEM_SNAP_CORE_RE = re.compile(r"^core\d*$")
# apt priorities that mean "base system", regardless of apt-mark's manual
# flag — some OEM/distro customizations mark these manual too, but a user
# didn't consciously choose to install them the way they chose an app.
_SYSTEM_APT_PRIORITIES = {"required", "important", "standard"}

# Safety net on top of the apt-mark/priority heuristic: core desktop/session
# infrastructure that Ubuntu packages as priority "optional" and often
# apt-mark manual (a packaging quirk, not a sign the user chose to install
# it) — confirmed on a live system that gnome-shell itself is exactly this
# case. Uninstalling any of these can break the desktop or login entirely,
# so they're always treated as system regardless of what apt-mark says.
CRITICAL_APT_PACKAGES = {
    "gnome-shell", "gnome-session", "gdm3", "mutter", "gnome-shell-common",
    "kwin", "plasma-workspace", "kded6", "kio6", "sddm",
    "xorg", "xserver-xorg", "wayland-protocols",
    "systemd", "systemd-sysv", "dbus", "dbus-daemon", "polkitd",
    "network-manager", "networkmanager",
    "pipewire", "pulseaudio", "sudo",
}


class Origin:
    FLATPAK = "flatpak"
    SNAP = "snap"
    APT = "apt"
    APPIMAGE = "appimage"
    UNKNOWN = "unknown"


class Scope:
    USER = "user"
    SYSTEM = "system"


class HintKey:
    """Sub-reason for an UNKNOWN result — lets the UI show something more
    useful than a flat "Unidentified origin" even when we can't map the app
    to a package manager, e.g. distinguishing a manually-extracted tarball
    in the user's home from a vendor-dropped binary with no package at all."""
    UNREADABLE = "unreadable"
    WEBAPP = "webapp"
    MANUAL_HOME = "manual-home"
    MANUAL_OPT = "manual-opt"
    MANUAL_SYSTEM = "manual-system"
    NO_CLUES = "no-clues"


@dataclass
class DetectionResult:
    origin: str
    id: str | None = None
    detail: str | None = None
    hint: str | None = None
    hint_key: str | None = None
    scope: str = Scope.USER
    # Best-guess install folder for a MANUAL_HOME/MANUAL_OPT result — set
    # only when we're confident it's a dedicated app folder (never the home
    # directory or /opt itself). None means "don't know", so uninstalling
    # can only remove the launcher, not the app's files.
    manual_target: str | None = None


def parse_desktop_file(path: str) -> dict | None:
    """Parses the [Desktop Entry] group of a .desktop file into a plain dict."""
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    section = parser["Desktop Entry"]
    return {
        "path": path,
        "name": section.get("Name"),
        "icon": section.get("Icon"),
        "exec": section.get("Exec"),
        "x_flatpak": section.get("X-Flatpak"),
        "try_exec": section.get("TryExec"),
    }


def extract_exec_binary(exec_line: str | None) -> str | None:
    """Strips field codes (%U, %f...) and env/wrapper prefixes from an Exec=
    line, returning the first real executable token."""
    if not exec_line:
        return None
    tokens = [t for t in exec_line.split() if not t.startswith("%")]
    for token in tokens:
        if token == "env" or re.match(r"^[A-Z_][A-Z0-9_]*=", token):
            continue
        return token
    return None


# Resolves many binaries and their owning apt package in ONE host round
# trip instead of one flatpak-spawn call per app. Two phases matter here:
# `command -v`/`readlink -f` are cheap (~a few ms, no state to load), but
# `dpkg -S` reloads its whole file database from disk on every single
# invocation (~250ms flat, regardless of how many patterns you give it) —
# so the one thing that must NOT happen per-token is a separate dpkg -S
# call. Resolve every binary first, then hand dpkg -S the full list of
# candidate paths in a single call.
_BULK_RESOLVE_SCRIPT = r"""
tokens=()
while IFS= read -r -d '' token; do
    tokens+=("$token")
done

resolved=()
canonical=()
for token in "${tokens[@]}"; do
    if [ -z "$token" ]; then
        resolved+=("")
        canonical+=("")
        continue
    fi
    case "$token" in
        /*) r="$token"; [ -e "$r" ] || r="" ;;
        *) r="$(command -v -- "$token" 2>/dev/null)" ;;
    esac
    resolved+=("$r")
    if [ -n "$r" ]; then
        c="$(readlink -f -- "$r" 2>/dev/null)"
        [ -z "$c" ] && c="$r"
    else
        c=""
    fi
    canonical+=("$c")
done

mapfile -t candidates < <(printf '%s\n' "${resolved[@]}" "${canonical[@]}" | grep -v '^$' | sort -u)

declare -A pkg_of
if [ "${#candidates[@]}" -gt 0 ]; then
    # dpkg -S lines are "pkg[:arch][, pkg2...]: /path" — splitting naively
    # on the first ":" breaks on multi-arch packages ("openjdk-19-jre:amd64:
    # /path"), since that colon comes before the real "pkg: path" separator.
    # The actual separator is always ": " (colon-SPACE); match on that.
    while IFS= read -r line; do
        pkg="${line%%: *}"
        path="${line#*: }"
        pkg_of["$path"]="${pkg%%,*}"
    done < <(dpkg -S "${candidates[@]}" 2>/dev/null)
fi

for i in "${!tokens[@]}"; do
    r="${resolved[$i]}"
    c="${canonical[$i]}"
    pkg=""
    [ -n "$c" ] && pkg="${pkg_of[$c]}"
    [ -z "$pkg" ] && [ -n "$r" ] && pkg="${pkg_of[$r]}"
    printf '%s\x1f%s\n' "$r" "$pkg"
done
"""


def resolve_tokens_bulk(tokens: list[str | None]) -> list[tuple[str | None, str | None]]:
    """Resolves each token (binary name or absolute path) to (resolved_path,
    owning_apt_package), both None when not found, in a single host call.
    A None token (some .desktop files have no usable Exec=) short-circuits
    to (None, None) without spending a slot in that call. Output order
    matches input order."""
    real_tokens = [t for t in tokens if t]
    resolved_by_token: dict[str, tuple[str | None, str | None]] = {}

    if real_tokens:
        stdin_bytes = b"".join(t.encode("utf-8", "surrogateescape") + b"\0" for t in real_tokens)
        ok, stdout, _err = run_host_stdin(["bash", "-c", _BULK_RESOLVE_SCRIPT], stdin_bytes)
        lines = stdout.decode("utf-8", "surrogateescape").split("\n") if ok else []
        for i, token in enumerate(real_tokens):
            if i < len(lines) and lines[i]:
                resolved, _sep, pkg = lines[i].partition("\x1f")
                resolved_by_token[token] = (resolved or None, pkg or None)
            else:
                resolved_by_token[token] = (None, None)

    return [resolved_by_token.get(t, (None, None)) if t else (None, None) for t in tokens]


def _dpkg_owners_bulk(paths: list[str]) -> dict[str, str]:
    """dpkg -S on a batch of exact paths, in one call. Used as a fallback
    for the .desktop file itself: some system components (KDE's kded6,
    baloo, GNOME's Extensions app...) ship a DBusActivatable .desktop with
    no useful Exec= to resolve a binary from (empty, or a placeholder like
    "false") — but the .desktop file is still a real, package-owned file."""
    if not paths:
        return {}
    # dpkg -S exits non-zero as soon as ANY pattern has no owner — expected
    # here, since we're deliberately batching paths we already suspect are
    # unowned alongside ones that might not be. Ignore the exit code and
    # parse stdout regardless; the paths that did match are still printed.
    _ok, out, _err = run_host(["dpkg", "-S", *paths])
    owners: dict[str, str] = {}
    for line in out.splitlines():
        # Same "pkg[:arch][, pkg2]: /path" format as the bulk resolve
        # script — the real separator is ": " (colon-space), not the
        # first colon (which for multi-arch packages appears earlier,
        # e.g. "openjdk-19-jre:amd64: /path").
        pkg, sep, path = line.partition(": ")
        if not sep:
            continue
        path = path.strip()
        if path in paths:
            owners[path] = pkg.split(",")[0].strip()
    return owners


def _get_manual_apt_packages() -> set[str]:
    """Packages apt-mark considers explicitly requested, as opposed to
    pulled in automatically as another package's dependency."""
    ok, out, _err = run_host(["apt-mark", "showmanual"])
    return set(out.split()) if ok else set()


def _get_apt_priorities(packages: set[str]) -> dict[str, str]:
    """Package -> dpkg Priority (required/important/standard/optional/...),
    for every package in one call."""
    if not packages:
        return {}
    ok, out, _err = run_host(["dpkg-query", "-Wf", "${Package} ${Priority}\n", *sorted(packages)])
    priorities: dict[str, str] = {}
    if ok:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                priorities[parts[0]] = parts[1]
    return priorities


def _apt_scope(pkg: str, manual: set[str], priorities: dict[str, str]) -> str:
    bare_name = pkg.split(":")[0]  # strip a multi-arch suffix like ":amd64"
    if bare_name in CRITICAL_APT_PACKAGES:
        return Scope.SYSTEM
    if pkg in manual and priorities.get(pkg) not in _SYSTEM_APT_PRIORITIES:
        return Scope.USER
    return Scope.SYSTEM


def _snap_scope(snap_id: str | None) -> str:
    if not snap_id:
        return Scope.USER
    if snap_id in SYSTEM_SNAP_NAMES or _SYSTEM_SNAP_CORE_RE.match(snap_id):
        return Scope.SYSTEM
    return Scope.USER


def _is_under_any(path: str, dirs: list[str]) -> bool:
    return any(path.startswith(d + "/") for d in dirs)


def _guess_manual_install_root(exec_path: str, boundary: str) -> str | None:
    """Best-effort guess at the dedicated folder a manually-extracted app
    lives in, so it (and not just its .desktop launcher) can be offered for
    removal. Returns None — meaning "don't know, only remove the launcher"
    — unless the guess is strictly inside `boundary` (never the boundary
    itself, e.g. never the home directory or /opt as a whole)."""
    install_dir = os.path.dirname(exec_path)
    # Portable app archives commonly nest the real binary one level under a
    # generic wrapper dir (bin/, usr/...); the actual install root — the
    # thing worth deleting — is one level up from that.
    if os.path.basename(install_dir) in ("bin", "sbin", "usr", "AppRun"):
        parent = os.path.dirname(install_dir)
        if parent.startswith(boundary + "/") and parent != boundary:
            return parent
    if install_dir.startswith(boundary + "/") and install_dir != boundary:
        return install_dir
    return None


def _build_unknown_hint(desktop_path: str, entry: dict, resolved: str | None, exec_token: str | None) -> tuple[str, str, str | None]:
    """Returns (hint_text, hint_key, manual_target)."""
    from .i18n import translate as _

    basename = os.path.basename(desktop_path)
    home = os.path.expanduser("~")
    exec_path = resolved or exec_token

    if exec_path and exec_path.startswith(home + "/"):
        target = _guess_manual_install_root(exec_path, home)
        return (
            _("Manually installed in your home folder: %s. Check that location to remove it by hand.") % exec_path,
            HintKey.MANUAL_HOME, target,
        )
    if exec_path and exec_path.startswith("/opt/"):
        target = _guess_manual_install_root(exec_path, "/opt")
        return (
            _("Manually installed under /opt: %s. Usually uninstalled by deleting that folder.") % exec_path,
            HintKey.MANUAL_OPT, target,
        )
    if exec_path:
        return (
            _("No package manager owns this executable (%s) — likely installed directly by a script or vendor image, bypassing apt/snap/flatpak entirely. Check that location to remove it by hand.") % exec_path,
            HintKey.MANUAL_SYSTEM, None,
        )
    name = (entry or {}).get("name") or basename
    return (
        _("Could not determine how “%s” was installed. Its .desktop file doesn’t give enough clues.") % name,
        HintKey.NO_CLUES, None,
    )


def detect_origins_bulk(desktop_paths: list[str]) -> dict[str, DetectionResult]:
    """Classifies every .desktop file's install origin (and, for apt/snap,
    whether it's a user-installed app or part of the base system) — all
    apps in one pass, resolving each pending app's binary + dpkg owner in a
    single host round trip instead of one per app (a couple of seconds
    instead of a couple of minutes on a system with a few hundred
    apt-installed apps)."""
    from .i18n import translate as _

    results: dict[str, DetectionResult] = {}
    pending: list[tuple[str, dict, str]] = []  # (desktop_path, entry, exec_token)

    for desktop_path in desktop_paths:
        if _is_under_any(desktop_path, FLATPAK_DIRS):
            basename = Path(desktop_path).name
            app_id = basename[:-len(".desktop")] if basename.endswith(".desktop") else basename
            results[desktop_path] = DetectionResult(Origin.FLATPAK, id=app_id, detail=desktop_path)
            continue

        if desktop_path.startswith(SNAP_DIR + "/"):
            entry = parse_desktop_file(desktop_path)
            binary = extract_exec_binary(entry.get("exec") if entry else None)
            match = re.search(r"/snap/bin/([^./]+)", binary or "")
            snap_id = match.group(1) if match else None
            results[desktop_path] = DetectionResult(
                Origin.SNAP, id=snap_id, detail=binary or desktop_path, scope=_snap_scope(snap_id))
            continue

        entry = parse_desktop_file(desktop_path)
        if not entry:
            results[desktop_path] = DetectionResult(
                Origin.UNKNOWN, detail="unreadable .desktop file",
                hint=_("The .desktop file could not be read."), hint_key=HintKey.UNREADABLE)
            continue

        if entry.get("x_flatpak"):
            results[desktop_path] = DetectionResult(Origin.FLATPAK, id=entry["x_flatpak"], detail=desktop_path)
            continue

        basename = Path(desktop_path).name
        if WEBAPP_BASENAME.match(basename) or "--app=" in (entry.get("exec") or ""):
            results[desktop_path] = DetectionResult(
                Origin.UNKNOWN, detail=desktop_path,
                hint=_("This is a web app shortcut created from the browser (Chrome/Edge). Remove it from the browser’s own app management instead."),
                hint_key=HintKey.WEBAPP)
            continue

        exec_token = extract_exec_binary(entry.get("exec") or entry.get("try_exec"))
        if exec_token and exec_token.lower().endswith(".appimage"):
            results[desktop_path] = DetectionResult(Origin.APPIMAGE, id=exec_token, detail=exec_token)
            continue

        pending.append((desktop_path, entry, exec_token))

    resolved_pairs = resolve_tokens_bulk([token for _p, _e, token in pending])

    # Fallback for apps whose Exec= gave no usable package: check if the
    # .desktop file itself (a real file dpkg may track) is package-owned.
    # Common for DBusActivatable components with a placeholder Exec=
    # (GNOME Extensions app ships Exec=false; several KDE/Plasma daemons
    # have no Exec= at all). Paths must be translated back to their real
    # host location first — SYSTEM_APPS_DIR carries the sandbox-only
    # /run/host prefix, which doesn't exist from the host's own point of
    # view where dpkg actually runs.
    unowned_desktop_paths = [
        desktop_path for (desktop_path, _e, _t), (_resolved, pkg) in zip(pending, resolved_pairs)
        if not pkg and desktop_path.startswith(SYSTEM_APPS_DIR + "/")
    ]
    host_paths = [p[len(HOST_ROOT):] if HOST_ROOT else p for p in unowned_desktop_paths]
    owners_by_host_path = _dpkg_owners_bulk(host_paths)
    desktop_owners = {
        orig: owners_by_host_path[host]
        for orig, host in zip(unowned_desktop_paths, host_paths)
        if host in owners_by_host_path
    }

    # One extra pair of host round trips for the whole batch (not per app)
    # to tell user-installed apt packages apart from base-system ones.
    found_packages = {pkg for _r, pkg in resolved_pairs if pkg} | set(desktop_owners.values())
    manual_packages = _get_manual_apt_packages() if found_packages else set()
    priorities = _get_apt_priorities(found_packages)

    for (desktop_path, entry, exec_token), (resolved, pkg) in zip(pending, resolved_pairs):
        if resolved and resolved.lower().endswith(".appimage"):
            results[desktop_path] = DetectionResult(Origin.APPIMAGE, id=resolved, detail=resolved)
        elif resolved and pkg:
            results[desktop_path] = DetectionResult(
                Origin.APT, id=pkg, detail=resolved, scope=_apt_scope(pkg, manual_packages, priorities))
        elif desktop_path in desktop_owners:
            owner_pkg = desktop_owners[desktop_path]
            results[desktop_path] = DetectionResult(
                Origin.APT, id=owner_pkg, detail=desktop_path, scope=_apt_scope(owner_pkg, manual_packages, priorities))
        elif resolved:
            hint, hint_key, manual_target = _build_unknown_hint(desktop_path, entry, resolved, exec_token)
            results[desktop_path] = DetectionResult(
                Origin.UNKNOWN, id=resolved, detail=desktop_path, hint=hint, hint_key=hint_key,
                manual_target=manual_target)
        elif desktop_path.startswith(USER_APPS_DIR + "/"):
            # AppImage integrators (appimaged, Gear Lever) commonly drop the
            # entry in the user dir without a resolvable Exec binary on PATH.
            results[desktop_path] = DetectionResult(Origin.APPIMAGE, detail=entry.get("exec") or "unresolved Exec")
        else:
            hint, hint_key, manual_target = _build_unknown_hint(desktop_path, entry, resolved, exec_token)
            results[desktop_path] = DetectionResult(
                Origin.UNKNOWN, id=exec_token, detail=desktop_path, hint=hint, hint_key=hint_key,
                manual_target=manual_target)

    return results


def list_desktop_files() -> list[str]:
    """Lists every .desktop file under the standard application directories."""
    dirs = [SYSTEM_APPS_DIR, SNAP_DIR, USER_APPS_DIR, *FLATPAK_DIRS]
    results = []
    for directory in dirs:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.endswith(".desktop"):
                results.append(os.path.join(directory, name))
    return results
