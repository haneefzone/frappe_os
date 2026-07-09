"""Structural CI guard for CLAUDE.md golden rule 7 (RBAC on every endpoint).

Walks every registered route on the real app: any non-GET route outside the
explicit allowlist must carry `get_current_user` somewhere in its dependency
tree (`require(<permission>)` resolves through it). Sessions 1.2+ each add
routers — a router merged without auth dependencies fails here, not in review.
SEC-L8 from the DOO-48 independent review.
"""

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api.deps import get_current_user
from app.main import create_app

# Routes that are deliberately reachable without a session. Additions require
# an explicit security sign-off — do not extend casually.
AUTH_EXEMPT_ROUTES = {
    "/api/auth/login",  # entry point: nothing to authenticate with yet
    "/api/auth/refresh",  # authenticates via refresh cookie + CSRF double-submit
    "/api/auth/logout",  # must work with an expired/broken session
}

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _depends_on_auth(dependant) -> bool:
    if dependant.call is get_current_user:
        return True
    return any(_depends_on_auth(sub) for sub in dependant.dependencies)


def unguarded_mutating_routes(app: FastAPI) -> list[str]:
    offenders = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        mutating = set(route.methods or ()) - _SAFE_METHODS
        if not mutating or route.path in AUTH_EXEMPT_ROUTES:
            continue
        if not _depends_on_auth(route.dependant):
            offenders.append(f"{','.join(sorted(mutating))} {route.path}")
    return sorted(offenders)


def test_every_mutating_route_carries_auth_dependency():
    offenders = unguarded_mutating_routes(create_app())
    assert offenders == [], (
        "Mutating routes without get_current_user/require() in their dependency "
        f"tree (golden rule 7): {offenders}. Add the auth dependency, or — only "
        "with security sign-off — add the route to AUTH_EXEMPT_ROUTES."
    )


def test_walker_trips_on_unguarded_route():
    """Failing-by-construction proof: an unguarded POST must be detected."""
    app = create_app()

    @app.post("/api/_demo/unguarded")
    def unguarded() -> dict:
        return {}

    assert unguarded_mutating_routes(app) == ["POST /api/_demo/unguarded"]


def test_walker_accepts_require_guarded_route():
    """require(<permission>) resolves through get_current_user, so a router
    guarded the standard way passes without being allowlisted."""
    from fastapi import Depends

    from app.api.deps import require

    app = create_app()

    @app.post("/api/_demo/guarded", dependencies=[Depends(require("site:operate"))])
    def guarded() -> dict:
        return {}

    assert unguarded_mutating_routes(app) == []
