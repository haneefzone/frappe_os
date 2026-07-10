from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import __version__
from app.api.routes.apps import router as apps_router
from app.api.routes.auth import router as auth_router
from app.api.routes.backups import router as backups_router
from app.api.routes.benches import router as benches_router
from app.api.routes.job_logs import router as job_logs_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.servers import router as servers_router
from app.api.routes.sites import router as sites_router
from app.api.routes.terminal import router as terminal_router
from app.config import get_settings
from app.core.logging import configure_logging
from app.errors import register_exception_handlers


class SPAStaticFiles(StaticFiles):
    """Serves the built frontend; unknown non-API paths fall back to
    index.html so client-side routes (vue-router history mode) deep-link."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Starlette raises (not returns) 404 for missing files.
            if exc.status_code == 404 and not scope["path"].startswith("/api/"):
                return await super().get_response("index.html", scope)
            raise
        if response.status_code == 404 and not scope["path"].startswith("/api/"):
            return await super().get_response("index.html", scope)
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version=__version__)

    # SEC-M1: rewrite request.client / scheme from X-Forwarded-* ONLY when the
    # socket peer is one of the configured trusted proxies. With the default
    # empty list the middleware is absent, so a client-sent X-Forwarded-For can
    # never change the login-throttle key or audit source IP.
    if settings.trusted_proxy_ip_list:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_ip_list)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(servers_router)
    app.include_router(benches_router)
    app.include_router(sites_router)
    app.include_router(apps_router)
    app.include_router(backups_router)
    app.include_router(jobs_router)
    app.include_router(job_logs_router)
    app.include_router(terminal_router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # Production single-service mode (install.sh): serve the built SPA from
    # the same origin as the API. Mounted last so /api/* routes win; unset
    # FRONTEND_DIST (the dev default) leaves this off and Vite serves the UI.
    dist = Path(settings.frontend_dist) if settings.frontend_dist else None
    if dist is not None and dist.is_dir():
        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="frontend")

    return app


app = create_app()
