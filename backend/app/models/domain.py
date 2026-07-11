"""Custom domains + TLS state for a site (session 2.4).

A `Domain` is a hostname that should resolve to a site over nginx — the site's
own name (e.g. ``erp.acme.com``) or an extra alias. The platform generates a
per-domain nginx vhost, checks the domain's public DNS against the server's
public IP, issues/renews a Let's Encrypt certificate via certbot, and tracks the
certificate's expiry so the dashboard can surface "SSL expiring ≤30d".

Nothing here is a secret: a domain, its DNS/cert status and an expiry timestamp
are all public information. The certificate's private key lives on the managed
server under ``/etc/letsencrypt`` (root-only), never in the platform DB.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Certbot/cert lifecycle for a domain:
#   none    — no certificate has been issued yet (HTTP only).
#   issued  — a certificate exists; cert_expires_at holds its notAfter.
#   error   — the last issue/renew attempt failed (see last_error).
CERT_STATUSES = ("none", "issued", "error")


class Domain(Base):
    """One hostname bound to a site, with its DNS and TLS bookkeeping."""

    __tablename__ = "domains"
    __table_args__ = (
        # A hostname is globally unique across the fleet — two sites cannot both
        # claim ``erp.acme.com`` (nginx server_name would collide).
        UniqueConstraint("domain", name="uq_domains_domain"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )

    # The fully-qualified hostname, lowercased (e.g. "erp.acme.com"). Validated
    # by the DOMAIN_NAME whitelist before it is ever stored or reaches a command.
    domain: Mapped[str] = mapped_column(String(253))

    # The site's canonical domain. Exactly one primary per site is expected; the
    # API enforces it by clearing the flag on the others when one is set.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    # Whether a TLS certificate is currently active for this domain (drives the
    # vhost's 443 server block and the dashboard SSL KPI).
    ssl_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # none | issued | error (see CERT_STATUSES).
    cert_status: Mapped[str] = mapped_column(String(20), default="none")
    # The certificate's notAfter (UTC), read from certbot after issue/renew or by
    # the scheduled expiry scan. NULL until a cert is issued.
    cert_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Result of the last DNS A/AAAA check: True = at least one record points at
    # the server's public IP; False = mismatch/unresolved; NULL = never checked.
    dns_ok: Mapped[bool | None] = mapped_column(Boolean)
    # When DNS / cert state was last refreshed by a check or scan job (UTC).
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Short human-readable reason for the last DNS mismatch or cert failure, shown
    # in the UI. Never contains secrets (domain/IP/certbot summary only).
    last_error: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    site: Mapped["Site"] = relationship(back_populates="domains")  # noqa: F821
