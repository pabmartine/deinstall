import os

APP_ID = "com.pabmartine.Deinstall"
APP_NAME = "Deinstall"
APP_DOMAIN = "deinstall"
APP_VERSION = "1.0.0"
APP_COPYRIGHT = "© 2026 pabmartine"
APP_WEBSITE = "https://github.com/pabmartine/deinstall-app"

CONFIG_DIR = os.path.expanduser("~/.config/deinstall")

os.makedirs(CONFIG_DIR, exist_ok=True)
