import os
import subprocess


def is_flatpak() -> bool:
    return os.path.exists("/.flatpak-info") or bool(os.environ.get("FLATPAK_ID"))


def host_argv(argv: list[str]) -> list[str]:
    """Prefixes argv with flatpak-spawn --host when sandboxed, so it runs on
    the host; a no-op otherwise."""
    return (["flatpak-spawn", "--host"] + argv) if is_flatpak() else argv


def run_host(argv: list[str], timeout: float = 10.0) -> tuple[bool, str, str]:
    """Runs argv on the host (escaping the Flatpak sandbox via flatpak-spawn
    when needed). Returns (ok, stdout, stderr); never raises."""
    cmd = host_argv(argv)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", str(e)


def run_host_stdin(argv: list[str], input_bytes: bytes, timeout: float = 60.0) -> tuple[bool, bytes, str]:
    """Like run_host(), but feeds `input_bytes` to the process's stdin and
    returns raw stdout bytes (the caller may need exact byte offsets/NULs).
    Used to batch many lookups (binary resolution + dpkg -S) into a single
    flatpak-spawn round trip instead of one per app."""
    cmd = host_argv(argv)
    try:
        result = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout, check=False)
        return result.returncode == 0, result.stdout, result.stderr.decode("utf-8", "replace")
    except Exception as e:
        return False, b"", str(e)


def run_host_async(argv: list[str], on_done) -> None:
    """Non-blocking variant using GLib's child-watch, so callers on the main
    loop (GTK) never freeze the UI. on_done(ok, stdout, stderr) runs on the
    main context once the process exits."""
    from gi.repository import GLib, Gio

    cmd = host_argv(argv)
    try:
        proc = Gio.Subprocess.new(
            cmd, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
    except GLib.Error as e:
        on_done(False, "", str(e))
        return

    def _on_communicated(source, result):
        try:
            ok, stdout, stderr = source.communicate_utf8_finish(result)
            on_done(source.get_successful(), stdout or "", stderr or "")
        except GLib.Error as e:
            on_done(False, "", str(e))

    proc.communicate_utf8_async(None, None, _on_communicated)
