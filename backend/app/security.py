import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

SESSION_COOKIE = "mh_session"
CSRF_COOKIE = "mh_csrf"
# Names from before the rename to MeshHome (0.8 and earlier): still read, so nobody is signed out.
LEGACY_SESSION_COOKIE = "mch_session"
LEGACY_CSRF_COOKIE = "mch_csrf"
CSRF_HEADER = "x-csrf-token"
# Every mutating request must carry this header. Browsers cannot attach custom headers to
# cross-site requests without a CORS preflight (which we never grant), so this blocks CSRF
# even before a session exists (login, setup).
REQUESTED_WITH_HEADER = "x-requested-with"
REQUESTED_WITH_VALUE = "meshhome"
# Still accepted: a page loaded before an upgrade sends the old value until it reloads.
REQUESTED_WITH_VALUES = (REQUESTED_WITH_VALUE, "meshcore-home")

MIN_PASSWORD_LENGTH = 10
# API keys: "mh_" + 256 random bits. Only a SHA-256 digest is stored. Keys made before the
# rename to MeshHome start with "mch_" and keep working.
API_KEY_PREFIX = "mh_"
API_KEY_PREFIXES = (API_KEY_PREFIX, "mch_")

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class RateLimiter:
    """Sliding-window limiter, in memory. Adequate for a single-process app."""

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


login_failures = RateLimiter(limit=5, window_seconds=60)
setup_failures = RateLimiter(limit=5, window_seconds=60)
send_limiter = RateLimiter(limit=20, window_seconds=60)
api_key_failures = RateLimiter(limit=20, window_seconds=60)
