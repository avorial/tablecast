import json
import logging
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

# Public base URL (scheme + host, no trailing slash), used to build absolute
# links in the RSS podcast feed — podcast apps need fully-qualified URLs.
# When unset, links are derived from the incoming request.
PUBLIC_BASE_URL = os.environ.get("TABLECAST_PUBLIC_BASE_URL", "").rstrip("/")

# ICE servers for the WebRTC mesh, as a JSON array. Add a TURN server here
# if players behind symmetric NAT can't connect, e.g.:
# [{"urls":"stun:stun.l.google.com:19302"},
#  {"urls":"turn:turn.example.com:3478","username":"u","credential":"p"}]
_DEFAULT_ICE = [{"urls": "stun:stun.l.google.com:19302"}]
try:
    ICE_SERVERS = json.loads(os.environ.get("TABLECAST_ICE_SERVERS") or "null") or _DEFAULT_ICE
except ValueError:
    logging.getLogger("tablecast.config").error(
        "TABLECAST_ICE_SERVERS is not valid JSON; using default STUN only"
    )
    ICE_SERVERS = _DEFAULT_ICE

# AI recap generation, via any OpenAI-compatible chat-completions endpoint:
# Ollama (http://host:11434/v1), LM Studio, OpenRouter, OpenAI, etc.
# Disabled unless both a base URL and a model are set.
LLM_BASE_URL = os.environ.get("TABLECAST_LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("TABLECAST_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("TABLECAST_LLM_MODEL", "")
LLM_ENABLED = bool(LLM_BASE_URL and LLM_MODEL)

# Seconds to wait after session end before finalizing audio, giving clients
# time to flush their last recording chunk. Tests set this to 0.
FINALIZE_DELAY_S = float(os.environ.get("TABLECAST_FINALIZE_DELAY_S", "10"))

# Podcast intro/outro slots: any ffmpeg-readable audio placed at these paths
# is stitched onto the episode (chapters shift automatically).
PODCAST_DIR = DATA_DIR / "podcast"
INTRO_PATH = PODCAST_DIR / "intro.mp3"
OUTRO_PATH = PODCAST_DIR / "outro.mp3"
