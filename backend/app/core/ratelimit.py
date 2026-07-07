"""Login throttling with temporary lockout.

In-memory, per-process: fine for the single-process dev/self-hosted deployment
this platform targets. Keyed by (email, client IP) so one attacker IP cannot
lock a victim account out from everywhere.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from threading import Lock

from app.config import get_settings


@dataclass
class _Entry:
    failures: int = 0
    locked_until: float = 0.0
    last_seen: float = field(default=0.0)


class LoginThrottle:
    def __init__(
        self,
        *,
        threshold: int,
        lockout_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._lockout_seconds = lockout_seconds
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = Lock()

    def _key(self, email: str, ip: str) -> str:
        return f"{email.strip().lower()}|{ip}"

    def retry_after(self, email: str, ip: str) -> int:
        """Seconds until the pair may try again; 0 if not locked."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(self._key(email, ip))
            if entry is None or entry.locked_until <= now:
                return 0
            return max(1, int(entry.locked_until - now))

    def register_failure(self, email: str, ip: str) -> None:
        now = self._clock()
        with self._lock:
            self._evict(now)
            entry = self._entries.setdefault(self._key(email, ip), _Entry())
            entry.last_seen = now
            entry.failures += 1
            if entry.failures >= self._threshold:
                entry.locked_until = now + self._lockout_seconds
                entry.failures = 0

    def register_success(self, email: str, ip: str) -> None:
        with self._lock:
            self._entries.pop(self._key(email, ip), None)

    def _evict(self, now: float) -> None:
        stale = now - self._lockout_seconds * 2
        for key in [k for k, e in self._entries.items() if e.last_seen < stale]:
            del self._entries[key]


@lru_cache
def get_login_throttle() -> LoginThrottle:
    settings = get_settings()
    return LoginThrottle(
        threshold=settings.login_lockout_threshold,
        lockout_seconds=settings.login_lockout_seconds,
    )
