"""Host-run administrative recovery commands (session 6.5).

Usage (after `alembic upgrade head`):
    python -m app.manage disable-2fa --email admin@example.com

Documented recovery path for an Admin locked out of TOTP (constraint: "a
documented, audited recovery path"). Requires a shell on the host running the
backend — there is no HTTP equivalent, so this cannot be triggered remotely.
Deletes the user's `UserTOTP` row and any unused `RecoveryCode` rows and
writes an `AuditLog` row attributed to that user (source_ip NULL — there is
no request) so the disable is traceable the same way the in-app
`/api/auth/2fa/disable` endpoint is.
"""

import argparse
import sys

from sqlalchemy import delete, select

from app.audit import record_audit
from app.models import RecoveryCode, User, UserTOTP


def disable_2fa(db, email: str) -> bool:
    """Returns True if a UserTOTP row was removed, False if the account had
    none (still succeeds — the goal state is "2FA is off" either way)."""
    email = email.strip().lower()
    user = db.scalars(select(User).where(User.email == email)).first()
    if user is None:
        print(f"No user with email {email!r}.", file=sys.stderr)
        return False

    existing = db.scalars(select(UserTOTP).where(UserTOTP.user_id == user.id)).first()
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    db.execute(delete(UserTOTP).where(UserTOTP.user_id == user.id))
    record_audit(
        db,
        action="auth.mfa_disable",
        summary=f"Disabled 2FA for {email} via host CLI (admin lockout recovery)",
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
    )
    db.commit()
    if existing is None:
        print(f"{email} had no 2FA enrolled. Nothing to do (audit row still recorded).")
    else:
        print(f"Disabled 2FA for {email} and cleared their recovery codes.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.manage", description="Host-run administrative recovery commands."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    disable_parser = subparsers.add_parser(
        "disable-2fa", help="Force-disable TOTP 2FA for a locked-out account."
    )
    disable_parser.add_argument("--email", required=True)

    args = parser.parse_args(argv)

    from app.db import SessionLocal

    with SessionLocal() as db:
        if args.command == "disable-2fa":
            ok = disable_2fa(db, args.email)
            return 0 if ok else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
