import gettext
import locale
import os
from pathlib import Path

from .constants import APP_DOMAIN

_current_gettext = lambda text: text


def get_locale_dir() -> str:
    project_locale = Path(__file__).resolve().parents[3] / "locale"
    for locale_dir in (Path("/app/share/locale"), project_locale, Path("/usr/share/locale")):
        if locale_dir.exists():
            return str(locale_dir)
    return str(project_locale)


def setup_locale(language: str | None = None):
    global _current_gettext

    if language and language != "auto":
        try:
            os.environ["LANGUAGE"] = language
            os.environ["LC_MESSAGES"] = language
        except Exception:
            pass

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        try:
            locale.setlocale(locale.LC_ALL, "C.UTF-8")
        except locale.Error:
            pass

    try:
        locale_dir = "/app/share/locale" if (Path("/app").exists() or os.environ.get("FLATPAK_ID")) else get_locale_dir()
        translations = gettext.translation(APP_DOMAIN, locale_dir, fallback=True)
        _current_gettext = translations.gettext
        return _current_gettext
    except Exception:
        _current_gettext = lambda text: text
        return _current_gettext


def translate(text: str) -> str:
    return _current_gettext(text)


setup_locale()
