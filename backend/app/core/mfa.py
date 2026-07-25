"""TOTP 2FA + recovery-code core helpers (session 6.5).

RFC 6238 TOTP via the pinned `pyotp` library, 30s step (its default), verified
with a +/-1 step window (allows for clock skew / slow typing). Secrets are
Fernet-encrypted at rest (CLAUDE.md rule 6) via the existing SecretsService and
never returned once enrolment is confirmed.

Replay rejection: `UserTOTP.last_used_step` records the RFC 6238 time-step
index of the most recently *accepted* code. `verify_totp_code` only accepts a
step strictly greater than that, so a captured code cannot be replayed even
within its own 30s validity window, and clock-skew acceptance never walks
backwards once a later step has been consumed.

Recovery codes are ten single-use codes minted at confirm time, shown exactly
once (same pattern as `ApiToken`), and stored only as a SHA-256 hash.
"""

import datetime
import hashlib
import secrets

import pyotp

from app.core.security import get_secrets_service

TOTP_ISSUER = "FDM Platform"
RECOVERY_CODE_COUNT = 10
# Unambiguous uppercase alphabet: no 0/O or 1/I.
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str, issuer: str = TOTP_ISSUER) -> str:
    """`otpauth://` URI an authenticator app scans as a QR code."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def encrypt_totp_secret(secret: str) -> str:
    return get_secrets_service().encrypt(secret)


def decrypt_totp_secret(encrypted: str) -> str:
    return get_secrets_service().decrypt(encrypted)


def current_totp_step(secret: str) -> int:
    return pyotp.TOTP(secret).timecode(datetime.datetime.now(datetime.UTC))


def verify_totp_code(secret: str, code: str, *, last_used_step: int | None) -> int | None:
    """Checks `code` against the +/-1 step window around now.

    Returns the accepted step index (to persist as the new `last_used_step`),
    or None if the code is malformed, wrong, expired, or a replay of an
    already-consumed step.
    """
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret)
    step = totp.timecode(datetime.datetime.now(datetime.UTC))
    floor = -1 if last_used_step is None else last_used_step
    for candidate_step in (step - 1, step, step + 1):
        if candidate_step <= floor:
            continue
        if secrets.compare_digest(totp.generate_otp(candidate_step), code):
            return candidate_step
    return None


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """`count` single-use plaintext codes, formatted XXXX-XXXX. Callers must
    hash each with `hash_recovery_code` before persisting and show the
    plaintext to the user exactly once."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    normalized = (code or "").strip().upper().replace(" ", "")
    return hashlib.sha256(normalized.encode()).hexdigest()


def looks_like_recovery_code(code: str) -> bool:
    """Recovery codes are XXXX-XXXX; TOTP codes are 6 digits. Used to route a
    /2fa/verify submission to the right check without a separate field."""
    code = (code or "").strip()
    return "-" in code or not code.isdigit()
