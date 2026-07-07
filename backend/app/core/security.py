"""Password hashing (argon2), JWT session tokens, CSRF and API-token helpers."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

TokenType = Literal["access", "refresh"]


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
) -> str:
    """JWT bound to a CSRF value (double-submit: cookie is httpOnly, the CSRF
    value travels back in a header and must match this claim)."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "csrf": csrf,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


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
