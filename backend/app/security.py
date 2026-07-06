import hashlib
import hmac
import secrets
import time

from . import config

_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _scheme, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


def _sign(payload: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_token(user_id: int) -> str:
    expires = int(time.time()) + config.SESSION_TTL_SECONDS
    payload = f"{user_id}:{expires}"
    return f"{payload}:{_sign(payload)}"


def verify_session_token(token: str) -> int | None:
    """Returns the user id, or None if the token is invalid or expired."""
    try:
        user_id, expires, signature = token.rsplit(":", 2)
        payload = f"{user_id}:{expires}"
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        if int(expires) < time.time():
            return None
        return int(user_id)
    except (ValueError, TypeError):
        return None
