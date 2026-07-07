"""Seed the four default roles and the admin user.

Usage (after `alembic upgrade head`):
    python -m app.seed --admin-email admin@example.com --admin-password 'S3cure!pass'

Idempotent: roles are upserted to the canonical permission matrix; if the
admin user already exists its password is reset and the Admin role + active
flag re-applied (stated on stdout so it never happens silently).
"""

import argparse
import sys

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.core.permissions import DEFAULT_ROLES
from app.core.security import hash_password
from app.models import Role, User


def seed_roles(db: Session) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for name, permissions in DEFAULT_ROLES.items():
        role = db.scalars(select(Role).where(Role.name == name)).first()
        if role is None:
            role = Role(name=name, permissions=permissions)
            db.add(role)
            print(f"Created role {name!r} with permissions {permissions}")
        elif list(role.permissions or []) != permissions:
            role.permissions = permissions
            print(f"Updated role {name!r} permissions to {permissions}")
        else:
            print(f"Role {name!r} already up to date")
        roles[name] = role
    db.flush()
    return roles


def seed_admin(db: Session, email: str, password: str, full_name: str) -> User:
    admin_role = db.scalars(select(Role).where(Role.name == "Admin")).one()
    email = email.strip().lower()
    user = db.scalars(select(User).where(User.email == email)).first()
    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(password),
            full_name=full_name,
            is_active=True,
            role_id=admin_role.id,
        )
        db.add(user)
        print(f"Created admin user {email}")
    else:
        user.password_hash = hash_password(password)
        user.role_id = admin_role.id
        user.is_active = True
        print(f"User {email} already exists — password reset, Admin role and active flag applied")
    db.flush()
    return user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed", description="Seed default roles and the admin user."
    )
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--admin-password", required=True)
    parser.add_argument("--admin-name", default="Administrator")
    args = parser.parse_args(argv)

    if len(args.admin_password) < 8:
        parser.error("--admin-password must be at least 8 characters")

    # Imported here so `--help` works without a reachable database.
    from app.db import SessionLocal, engine

    if not inspect(engine).has_table("users"):
        print(
            "Tables are missing. Run migrations first:\n"
            "    .venv/bin/alembic upgrade head",
            file=sys.stderr,
        )
        return 1

    with SessionLocal() as db:
        seed_roles(db)
        seed_admin(db, args.admin_email, args.admin_password, args.admin_name)
        db.commit()
    print("Seed complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
