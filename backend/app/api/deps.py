"""Auth + RBAC dependencies. Every protected router uses `require(<permission>)`."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.permissions import role_allows
from app.core.security import decode_session_token
from app.db import get_db
from app.models import User

ACCESS_COOKIE = "fdm_access_token"
REFRESH_COOKIE = "fdm_refresh_token"
CSRF_COOKIE = "fdm_csrf_token"
CSRF_HEADER = "X-CSRF-Token"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _unauthorized(message: str = "Not authenticated.") -> HTTPException:
    return HTTPException(status_code=401, detail=message)


def get_current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    """Resolves the session user from the access-token cookie.

    For mutating methods it also enforces the CSRF double-submit check: the
    X-CSRF-Token header must match the csrf claim baked into the (httpOnly)
    access token at login.
    """
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise _unauthorized()

    claims = decode_session_token(
        token, expected_type="access", secret=get_settings().jwt_secret
    )
    if claims is None:
        raise _unauthorized("Session expired or invalid.")

    if request.method not in _SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER)
        if not header or header != claims.get("csrf"):
            raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")

    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise _unauthorized("Account is disabled.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require(permission: str):
    """RBAC dependency factory: `Depends(require("site:operate"))`.

    Grants when the user's role holds the action-class (or the "*" wildcard);
    otherwise 403.
    """

    def dependency(user: CurrentUser) -> User:
        if not role_allows(list(user.role.permissions or []), permission):
            raise HTTPException(
                status_code=403,
                detail=f"Role '{user.role.name}' lacks the '{permission}' permission.",
            )
        return user

    return dependency
