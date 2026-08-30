"""Centralized configuration loaded from the environment / .env file."""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Jellyfin ---
SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "betterposters.db")
POSTER_LANG = os.getenv("POSTER_LANG", "en")
IDS_CHUNK = 100
RUN_INTERVAL_MINUTES = int(os.getenv("RUN_INTERVAL_MINUTES", "0") or 0)

# --- REST API ("" = API disabled) ---
API_PORT = os.getenv("API_PORT", "")
API_TOKEN = os.getenv("API_TOKEN", "")
API_RATE_LIMIT = int(os.getenv("API_RATE_LIMIT", "5") or 5)
API_GLOBAL_REFRESH_LIMIT = int(os.getenv("API_GLOBAL_REFRESH_LIMIT", "20") or 20)
API_STATUS_RATE_LIMIT = int(os.getenv("API_STATUS_RATE_LIMIT", "30") or 30)
AUTH_FAIL_LIMIT = int(os.getenv("AUTH_FAIL_LIMIT", "5") or 5)
AUTH_LOCKOUT_MINUTES = int(os.getenv("AUTH_LOCKOUT_MINUTES", "10") or 10)
