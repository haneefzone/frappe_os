import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import __version__
from app.api.routes.ai_settings import router as ai_settings_router
from app.api.routes.apps import router as apps_router
from app.api.routes.audit import router as audit_router
from app.api.routes.auth import router as auth_router
from app.api.routes.backups import router as backups_router
from app.api.routes.benches import router as benches_router
from app.api.routes.compliance import router as compliance_router
from app.api.routes.compliance_reports import router as compliance_reports_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.domains import router as domains_router
from app.api.routes.drift import router as drift_router
from app.api.routes.job_logs import router as job_logs_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.monitoring import router as monitoring_router
from app.api.routes.notifications import router as notifications_router
from app.api.routes.reports import router as reports_router
from app.api.routes.restic import router as restic_router
from app.api.routes.schedules import router as schedules_router
from app.api.routes.search import router as search_router
from app.api.routes.servers import router as servers_router
from app.api.routes.settings import router as settings_router
from app.api.routes.sites import router as sites_router
from app.api.routes.storage_targets import router as storage_targets_router
from app.api.routes.terminal import router as terminal_router
from app.config import get_settings
from app.core.logging import configure_logging
from app.errors import register_exception_handlers

logger = logging.getLogger("app.main")


def _build_monitoring_poller():
    """Construct the background monitoring poller from settings, or None when it
    is disabled. A Redis lease elects a single poller across API workers; if
    Redis is unreachable the poller falls back to single-process polling."""
    settings = get_settings()
    if not settings.monitoring_enabled:
        return None
    from app.core.monitoring import MonitoringPoller
    from app.core.ssh import get_ssh_service
    from app.db import SessionLocal

    redis_client = None
    try:
        from redis import Redis

        redis_client = Redis.from_url(settings.redis_url)
    except Exception:  # noqa: BLE001 — no Redis in dev/tests: single-process poll.
        redis_client = None

    return MonitoringPoller(
        get_ssh_service(),
        SessionLocal,
        interval_seconds=settings.monitoring_interval_seconds,
        retention_hours=settings.monitoring_retention_hours,
        redis_client=redis_client,
    )


def _build_uptime_checker():
    """Construct the background uptime checker (session 2.7), or None when it is
    disabled. Like the monitoring poller it uses a Redis lease to run a single
    checker across API workers, falling back to single-process when Redis is
    unreachable."""
    settings = get_settings()
    if not settings.uptime_enabled:
        return None
    from app.core.uptime import UptimeChecker
    from app.db import SessionLocal

    redis_client = None
    try:
        from redis import Redis

        redis_client = Redis.from_url(settings.redis_url)
    except Exception:  # noqa: BLE001 — no Redis in dev/tests: single-process check.
        redis_client = None

    return UptimeChecker(
        SessionLocal,
        interval_seconds=settings.uptime_interval_seconds,
        retention_hours=settings.uptime_retention_hours,
        max_concurrency=settings.uptime_max_concurrency,
        redis_client=redis_client,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    poller = _build_monitoring_poller()
    checker = _build_uptime_checker()
    if poller is not None:
        poller.start()
    if checker is not None:
        checker.start()
    try:
        yield
    finally:
        if poller is not None:
            await poller.stop()
        if checker is not None:
            await checker.stop()


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

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=_lifespan)

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
    app.include_router(domains_router)
    app.include_router(drift_router)
    app.include_router(apps_router)
    app.include_router(backups_router)
    app.include_router(jobs_router)
    app.include_router(job_logs_router)
    app.include_router(terminal_router)
    app.include_router(monitoring_router)
    app.include_router(schedules_router)
    app.include_router(compliance_router)
    app.include_router(compliance_reports_router)
    app.include_router(dashboard_router)
    app.include_router(settings_router)
    app.include_router(storage_targets_router)
    app.include_router(restic_router)
    app.include_router(ai_settings_router)
    app.include_router(audit_router)
    app.include_router(reports_router)
    app.include_router(notifications_router)
    app.include_router(search_router)

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
