"""Password hashing (argon2), JWT session tokens, CSRF and API-token helpers,
and the Fernet SecretsService for secrets-at-rest."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_hasher = PasswordHasher()

# "mfa_pending" is a short-lived, single-purpose token issued after a correct
# password when 2FA is active: it proves the password step happened but is
# never accepted by get_current_user, so it cannot satisfy any RBAC dependency
# (session 6.5). Only /api/auth/2fa/verify accepts it.
TokenType = Literal["access", "refresh", "mfa_pending"]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def create_session_token(
    *,
    user_id: int,
    token_type: TokenType,
    csrf: str,
    ttl_seconds: int,
    secret: str,
    token_version: int,
    sid: str | None = None,
) -> str:
    """JWT bound to a CSRF value (double-submit: cookie is httpOnly, the CSRF
    value travels back in a header and must match this claim).

    `token_version` is baked in as the `tv` claim (SEC-M2, ISO 27001 A.5.17):
    get_current_user and /refresh reject any token whose `tv` no longer matches
    the user's current DB value, so bumping it server-side revokes every
    outstanding token for that user.

    `sid` (session 6.5) is the `UserSession.jti` shared by every access/refresh
    token issued for one login/refresh chain — revoking that row (or its
    idle/absolute timeout expiring) rejects the very next request on any
    device, not just the next login. `mfa_pending` tokens pass no `sid`."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "csrf": csrf,
        "tv": token_version,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    if sid is not None:
        payload["sid"] = sid
    return jwt.encode(payload, secret, algorithm="HS256")


def bump_token_version(user) -> None:
    """Advance a user's token_version to invalidate all of their outstanding
    JWTs (SEC-M2). Call inside the same DB transaction as the triggering change
    — password change, role change, deactivation, or an explicit "log out
    everywhere" — then commit. Duck-typed to avoid a models import cycle."""
    user.token_version = (user.token_version or 0) + 1


def decode_session_token(token: str, *, expected_type: TokenType, secret: str) -> dict | None:
    """Returns the claims dict, or None for any invalid/expired/mistyped token."""
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    if payload.get("type") != expected_type or "sub" not in payload:
        return None
    return payload


def hash_api_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Password policy (session 6.5's SecurityPolicy: length/complexity/reuse).
# Pure function — no active self-service password-change endpoint exists yet
# to call it from; wire it in when one lands, and it is unit-tested directly.
# --------------------------------------------------------------------------- #


def validate_password_policy(
    password: str,
    *,
    min_length: int,
    require_complexity: bool,
    reuse_history: int = 0,
    previous_hashes: list[str] = (),
) -> list[str]:
    """Returns a list of human-readable violations (empty = compliant).

    `require_complexity` demands at least one upper, one lower, one digit, and
    one symbol. `previous_hashes` (newest first) are checked with the same
    argon2 verify used at login, so reuse of any of the last `reuse_history`
    passwords is rejected without ever storing them in plaintext.
    """
    problems = []
    if len(password) < min_length:
        problems.append(f"Password must be at least {min_length} characters.")
    if require_complexity:
        if not any(c.islower() for c in password):
            problems.append("Password must include a lowercase letter.")
        if not any(c.isupper() for c in password):
            problems.append("Password must include an uppercase letter.")
        if not any(c.isdigit() for c in password):
            problems.append("Password must include a digit.")
        if not any(not c.isalnum() for c in password):
            problems.append("Password must include a symbol.")
    for old_hash in list(previous_hashes)[:reuse_history]:
        if verify_password(old_hash, password):
            problems.append(
                f"Password must not match any of the last {reuse_history} passwords."
            )
            break
    return problems


# --------------------------------------------------------------------------- #
# Secrets at rest (CLAUDE.md rule 6): Fernet symmetric encryption keyed by the
# master FDM_SECRET_KEY, which lives only in the environment — never the DB.
# --------------------------------------------------------------------------- #


class SecretKeyError(RuntimeError):
    """The FDM_SECRET_KEY is missing or not a valid Fernet key — fail closed."""


class SecretsService:
    """Encrypts/decrypts short secrets (SSH private keys, passphrases,
    passwords) to opaque Fernet tokens. Constructing it validates the key, so
    a missing/malformed FDM_SECRET_KEY refuses to boot rather than silently
    storing unreadable rows."""

    def __init__(self, key: str | bytes) -> None:
        key_bytes = key.encode() if isinstance(key, str) else key
        try:
            self._fernet = Fernet(key_bytes)
        except (ValueError, TypeError) as exc:
            raise SecretKeyError(
                "FDM_SECRET_KEY is missing or malformed. Generate one with: "
                'python3 -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        """Plaintext -> URL-safe Fernet token (str, safe to store in a Text column)."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Fernet token -> plaintext. Raises SecretKeyError on a tampered/foreign token."""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretKeyError(
                "Could not decrypt a stored secret. FDM_SECRET_KEY likely "
                "changed since it was written."
            ) from exc


@lru_cache
def get_secrets_service() -> SecretsService:
    """Process-wide SecretsService built from settings. Cached so the key is
    validated once; raises SecretKeyError at first use if the key is bad."""
    from app.config import get_settings

    return SecretsService(get_settings().fdm_secret_key)


def generate_ed25519_keypair() -> tuple[str, str]:
    """Generate an ed25519 keypair server-side.

    Returns (private_openssh_pem, public_openssh_line). The caller encrypts the
    private key immediately and shows the public line once so the user can
    install it in the target's authorized_keys — the private half never leaves
    the control plane in plaintext.
    """
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_line = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    return private_pem, public_line
