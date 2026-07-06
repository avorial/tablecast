import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("TABLECAST_DATA_DIR", "/data"))
AUDIO_DIR = DATA_DIR / "audio"

DATABASE_URL = os.environ.get(
    "DATABASE_URL", f"sqlite:///{DATA_DIR / 'tablecast.db'}"
)

# Signs auth cookies. Set a stable value in production or every restart
# logs everyone out.
SECRET_KEY = os.environ.get("TABLECAST_SECRET_KEY") or secrets.token_hex(32)

# Shared secret for the transcription worker's internal API.
WORKER_TOKEN = os.environ.get("TABLECAST_WORKER_TOKEN", "")

SESSION_COOKIE = "tablecast_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

# Allow open registration. Set to "false" once your table has signed up.
REGISTRATION_OPEN = os.environ.get("TABLECAST_REGISTRATION_OPEN", "true").lower() != "false"
