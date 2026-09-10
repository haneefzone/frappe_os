"""Request/response models for the sites API (session 1.8)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import Server
from app.models.bench import Bench
from app.models.site import Site


class SiteOut(BaseModel):
    """One site, enriched with its bench + server so the UI can render the
    EnvironmentBadge (inherited from the server) and the "Open site" link
    (http://<server host>:<bench webserver port>)."""

    id: int
    bench_id: int
    bench_name: str
    server_id: int
    server_name: str
    server_hostname: str
    server_env_tag: str
    name: str
    status: str
    scheduler_enabled: bool | None
    maintenance_mode: bool
    health: str
    # The site's own environment classification (dev/staging/prod). Drives the
    # prod-update guardrail; operators set this via POST /api/sites/{id}/environment
    # or at creation time. Defaults to "dev" until explicitly classified.
    environment: str
    # The bench's dev web port (from common_site_config.json); None if unknown.
    webserver_port: int | None
    # Pre-built http URL to reach the site, or None when the port is unknown.
    url: str | None
    # External HTTP uptime checking (session 2.7).
    uptime_enabled: bool
    check_url: str | None
    discovered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, site: Site, bench: Bench, server: Server) -> "SiteOut":
        port = bench.webserver_port
        url = f"http://{server.hostname}:{port}" if port else None
        return cls(
            id=site.id,
            bench_id=site.bench_id,
            bench_name=bench.name,
            server_id=server.id,
            server_name=server.name,
            server_hostname=server.hostname,
            server_env_tag=server.env_tag,
            name=site.name,
            status=site.status,
            scheduler_enabled=site.scheduler_enabled,
            maintenance_mode=site.maintenance_mode,
            health=site.health,
            environment=site.environment,
            webserver_port=port,
            url=url,
            uptime_enabled=site.uptime_enabled,
            check_url=site.check_url,
            discovered_at=site.discovered_at,
            created_at=site.created_at,
            updated_at=site.updated_at,
        )


class CreateSiteRequest(BaseModel):
    """Create a site inside a known bench (session 1.8). The admin password is
    supplied here (over HTTPS) and carried to the worker encrypted; the MariaDB
    root password is NOT sent — it is pulled from the server's settings
    server-side (gotcha #4)."""

    bench_id: int
    name: str
    admin_password: str = Field(min_length=1, max_length=128)
    # Environment classification applied at registration time (DOO-988). Prevents
    # a real prod site from being promoted without the danger/confirm/sign-off
    # chain because nobody set its environment after creation.
    environment: Literal["dev", "staging", "prod"] = "dev"
    # bench new-site is longer-running; default it to the high queue.
    priority: str = "high"


class SiteToggleRequest(BaseModel):
    """Flip a boolean site control (scheduler / maintenance) — fast queue."""

    enabled: bool
    priority: str = "high"


class SiteActionRequest(BaseModel):
    """Launch a parameter-free site maintenance action (migrate / clear-cache /
    clear-website-cache). The body only carries an optional queue override; the
    site + bench come from the path parameter (session 1.10)."""

    priority: str = "default"
