"""Login throttling with temporary lockout.

In-memory, per-process: fine for the single-process dev/self-hosted deployment
this platform targets. Two layers (SEC-M1):

- (email, client IP) pair: N consecutive failures lock the pair, so one
  attacker IP cannot lock a victim account out from everywhere.
- per-email backstop: M failures within a sliding window — from ANY
  combination of IPs — lock the account, so an attacker rotating source
  addresses (or spoofing X-Forwarded-For) cannot spray one account forever.

The IP passed in must be trustworthy: the app only rewrites request.client
from X-Forwarded-For when the socket peer is a configured trusted proxy
(see TRUSTED_PROXY_IPS and main.create_app).
"""

import time
from collections import deque
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


@dataclass
class _EmailEntry:
    failure_times: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0
    last_seen: float = field(default=0.0)


class LoginThrottle:
    def __init__(
        self,
        *,
        threshold: int,
        lockout_seconds: int,
        email_failure_limit: int,
        email_failure_window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._lockout_seconds = lockout_seconds
        self._email_failure_limit = email_failure_limit
        self._email_window = email_failure_window_seconds
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._email_entries: dict[str, _EmailEntry] = {}
        self._lock = Lock()

    def _key(self, email: str, ip: str) -> str:
        return f"{email.strip().lower()}|{ip}"

    def _email_key(self, email: str) -> str:
        return email.strip().lower()

    def retry_after(self, email: str, ip: str) -> int:
        """Seconds until (email, ip) may try again; 0 if not locked."""
        now = self._clock()
        with self._lock:
            locked_until = 0.0
            entry = self._entries.get(self._key(email, ip))
            if entry is not None:
                locked_until = entry.locked_until
            email_entry = self._email_entries.get(self._email_key(email))
            if email_entry is not None:
                locked_until = max(locked_until, email_entry.locked_until)
            if locked_until <= now:
                return 0
            return max(1, int(locked_until - now))

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

            # Cross-IP backstop: count this failure against the email itself.
            email_entry = self._email_entries.setdefault(self._email_key(email), _EmailEntry())
            email_entry.last_seen = now
            email_entry.failure_times.append(now)
            horizon = now - self._email_window
            while email_entry.failure_times and email_entry.failure_times[0] < horizon:
                email_entry.failure_times.popleft()
            if len(email_entry.failure_times) >= self._email_failure_limit:
                email_entry.locked_until = now + self._lockout_seconds

    def register_success(self, email: str, ip: str) -> None:
        with self._lock:
            self._entries.pop(self._key(email, ip), None)
            # The account owner proved the password; stale spray counts must
            # not keep penalising them from their next device.
            self._email_entries.pop(self._email_key(email), None)

    def _evict(self, now: float) -> None:
        stale = now - self._lockout_seconds * 2
        for key in [k for k, e in self._entries.items() if e.last_seen < stale]:
            del self._entries[key]
        email_stale = now - max(self._email_window, self._lockout_seconds) * 2
        for key in [k for k, e in self._email_entries.items() if e.last_seen < email_stale]:
            del self._email_entries[key]


@lru_cache
def get_login_throttle() -> LoginThrottle:
    settings = get_settings()
    return LoginThrottle(
        threshold=settings.login_lockout_threshold,
        lockout_seconds=settings.login_lockout_seconds,
        email_failure_limit=settings.login_email_failure_limit,
        email_failure_window_seconds=settings.login_email_failure_window_seconds,
    )


@lru_cache
def get_mfa_throttle() -> LoginThrottle:
    """Same mechanism as the login throttle (session 6.5), keyed by the user's
    id in place of an email: N consecutive wrong 2FA codes locks that user's
    /2fa/verify attempts out, so brute-forcing a 6-digit code is infeasible."""
    settings = get_settings()
    return LoginThrottle(
        threshold=settings.mfa_lockout_threshold,
        lockout_seconds=settings.mfa_lockout_seconds,
        email_failure_limit=settings.mfa_email_failure_limit,
        email_failure_window_seconds=settings.mfa_email_failure_window_seconds,
    )
