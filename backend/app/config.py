import os
import secrets
import stat
from pathlib import Path

DATA_DIR = Path(os.environ.get("TABLECAST_DATA_DIR", "/data"))
AUDIO_DIR = DATA_DIR / "audio"

# Small volume shared read-only with the transcription worker, used only to
# hand it the auto-generated worker token (see _persisted_secret below).
# Never used for session/audio data — the worker still only ever talks to
# the backend over HTTP.
SHARED_DIR = Path(os.environ.get("TABLECAST_SHARED_DIR", "/shared"))

DATABASE_URL = os.environ.get(
    "DATABASE_URL", f"sqlite:///{DATA_DIR / 'tablecast.db'}"
)


def _persisted_secret(env_value: str | None, path: Path) -> str:
    """Returns env_value if set; otherwise a secret persisted at `path`,
    generating and storing one on first run. This is what lets the stack
    deploy with zero required environment variables (e.g. in Portainer,
    which doesn't load a .env file next to docker-compose.yml the way the
    `docker compose` CLI does) while still using a real per-deployment
    secret rather than a hardcoded default.
    """
    if env_value:
        return env_value
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return value


# Signs auth cookies. Persisted at DATA_DIR/.secret_key if not set explicitly,
# so cookies survive container restarts (but not a volume wipe). Set
# TABLECAST_SECRET_KEY yourself if you want it independent of the volume.
SECRET_KEY = _persisted_secret(
    os.environ.get("TABLECAST_SECRET_KEY"), DATA_DIR / ".secret_key"
)

# Shared secret for the transcription worker's internal API. Persisted at
# SHARED_DIR/.worker_token (a volume mounted read-only into the worker
# container too) so the worker can pick up the same auto-generated value
# without either container needing to specify it explicitly.
WORKER_TOKEN = _persisted_secret(
    os.environ.get("TABLECAST_WORKER_TOKEN"), SHARED_DIR / ".worker_token"
)

SESSION_COOKIE = "tablecast_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

# Allow open registration. Set to "false" once your table has signed up.
REGISTRATION_OPEN = os.environ.get("TABLECAST_REGISTRATION_OPEN", "true").lower() != "false"
