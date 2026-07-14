import json
import os

from .constants import CONFIG_DIR

SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")


class ConfigManager:
    def __init__(self) -> None:
        self.defaults = {"language": "auto"}
        self.settings = self.load()

    def load(self) -> dict:
        if not os.path.exists(SETTINGS_FILE):
            return self.defaults.copy()
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            settings = self.defaults.copy()
            settings.update(data)
            return settings
        except Exception:
            return self.defaults.copy()

    def save(self) -> None:
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4)
        except Exception:
            pass

    def get(self, key: str, default=None):
        # load() already merged defaults into settings, so a plain lookup
        # covers every known key.
        return self.settings.get(key, default)

    def set(self, key: str, value) -> None:
        self.settings[key] = value
        self.save()
