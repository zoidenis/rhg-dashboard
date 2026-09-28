"""Settings for the RHG identity service (read from .env next to this file)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env():
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def get(key, default=None):
    return os.environ.get(key, default)


APP_HOST = get("APP_HOST", "127.0.0.1")
APP_PORT = int(get("APP_PORT", "8767"))

DB_PATH = BASE_DIR / "data" / "identity.db"

# Sessions live in the database, so a restart does not sign everyone out.
SESSION_HOURS = int(get("SESSION_HOURS", "12"))
IDLE_MINUTES = int(get("IDLE_MINUTES", "60"))

# Lockout: the same numbers the apps used, kept so behaviour does not change.
MAX_FAILURES = int(get("MAX_FAILURES", "5"))
LOCK_MINUTES = int(get("LOCK_MINUTES", "15"))

PBKDF2_ITERATIONS = int(get("PBKDF2_ITERATIONS", "240000"))
MIN_PASSWORD = int(get("MIN_PASSWORD", "12"))

# Only services holding this secret may validate a session. Generated on the
# server; never commit it.
SERVICE_TOKEN = get("SERVICE_TOKEN", "")

COOKIE_NAME = get("COOKIE_NAME", "rhg_session")
COOKIE_SECURE = get("COOKIE_SECURE", "true").lower() != "false"
