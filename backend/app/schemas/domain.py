"""Request/response models for the domains & SSL API (session 2.4)."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.models.domain import Domain


class DomainOut(BaseModel):
    """One domain bound to a site, with its DNS and TLS bookkeeping. `days_left`
    is derived from `cert_expires_at` so the UI (and the dashboard KPI) can show
    a countdown without recomputing the timezone math."""

    id: int
    site_id: int
    domain: str
    is_primary: bool
    ssl_enabled: bool
    cert_status: str
    cert_expires_at: datetime | None
    days_left: int | None
    dns_ok: bool | None
    last_checked: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, d: Domain) -> "DomainOut":
        days_left: int | None = None
        if d.cert_expires_at is not None:
            expires = d.cert_expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            days_left = (expires - datetime.now(UTC)).days
        return cls(
            id=d.id,
            site_id=d.site_id,
            domain=d.domain,
            is_primary=d.is_primary,
            ssl_enabled=d.ssl_enabled,
            cert_status=d.cert_status,
            cert_expires_at=d.cert_expires_at,
            days_left=days_left,
            dns_ok=d.dns_ok,
            last_checked=d.last_checked,
            last_error=d.last_error,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


class CreateDomainRequest(BaseModel):
    """Add a domain to a site. Validation of the hostname shape is enforced again
    server-side by the DOMAIN_NAME whitelist when a job renders."""

    domain: str = Field(min_length=3, max_length=253)
    is_primary: bool = False


class DomainActionRequest(BaseModel):
    """Generic launcher body for a domain job (dns-check / render-vhost)."""

    priority: str = "default"


class IssueCertRequest(BaseModel):
    """Issue a Let's Encrypt certificate for a domain via certbot."""

    email: str = Field(min_length=3, max_length=254)
    priority: str = "default"
