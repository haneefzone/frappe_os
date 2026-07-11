"""Pure helpers for Domains & SSL (session 2.4).

Everything here is a pure function over strings so it unit-tests without SSH,
root, nginx or certbot: the nginx vhost renderer, the DNS-resolution comparison,
and the certbot output parsers. The privileged/remote parts (writing the vhost
under a flock, `nginx -t`, `systemctl reload`, `certbot`) live in the Action
handlers in ``app/core/commands/actions.py``; they call into these.

nginx write policy (documented for the ISO review — TLS/nginx/sudo):
    The platform NEVER writes to root-owned nginx paths. Generated per-domain
    vhosts are written to a **bench-user-writable** include directory,
    ``<bench_path>/config/nginx-vhosts/<domain>.conf`` (VHOSTS_SUBDIR), so no
    broad file-write sudo is ever needed. The operator wires
    ``include <bench_path>/config/nginx-vhosts/*.conf;`` into the server nginx
    config once (documented in docs/production-setup.md; naturally paired with
    the 2.5 `bench setup production` conversion). The only privileged steps are
    the ratified ``sudo -n /usr/sbin/nginx -t`` validation gate and
    ``sudo -n /usr/bin/systemctl reload nginx`` — both fixed argv, both allowlist
    lines. If `nginx -t` fails the action restores the pre-change backup and
    aborts, so the live config is never left broken (golden rule 5).
"""

from __future__ import annotations

import re
from datetime import datetime

# Where per-domain vhosts are written, relative to the bench path. The bench
# owner (the SSH login user) owns this tree, so writing needs no sudo.
VHOSTS_SUBDIR = "config/nginx-vhosts"


def render_vhost(
    domain: str,
    *,
    upstream_host: str,
    upstream_port: int,
    ssl_enabled: bool,
    webroot: str,
    cert_dir: str,
) -> str:
    """Render the nginx server block(s) for one domain.

    Always serves the ACME HTTP-01 challenge on :80 (so certbot --webroot can
    complete). Without a cert it proxies :80 straight to the bench's Frappe
    upstream; with a cert it redirects :80→:443 and proxies the TLS vhost using
    the certbot-managed ``fullchain.pem``/``privkey.pem`` under ``cert_dir``.

    ``domain``/paths are pre-validated by the template ParamSpecs (DOMAIN_NAME /
    ABS_PATH), so no untrusted text reaches the file — this only assembles known
    strings.
    """
    upstream = f"{upstream_host}:{upstream_port}"
    proxy_block = (
        "    location / {\n"
        f"        proxy_pass http://{upstream};\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "        proxy_read_timeout 120s;\n"
        "    }\n"
    )
    acme_block = (
        "    location ^~ /.well-known/acme-challenge/ {\n"
        f"        root {webroot};\n"
        '        default_type "text/plain";\n'
        "        try_files $uri =404;\n"
        "    }\n"
    )

    header = (
        "# Managed by FDM Platform (session 2.4) — do not edit by hand.\n"
        f"# domain={domain} ssl={'on' if ssl_enabled else 'off'}\n"
    )

    if not ssl_enabled:
        return (
            header
            + "server {\n"
            "    listen 80;\n"
            "    listen [::]:80;\n"
            f"    server_name {domain};\n\n"
            + acme_block
            + "\n"
            + proxy_block
            + "}\n"
        )

    return (
        header
        + "server {\n"
        "    listen 80;\n"
        "    listen [::]:80;\n"
        f"    server_name {domain};\n\n"
        + acme_block
        + "\n"
        "    location / {\n"
        "        return 301 https://$host$request_uri;\n"
        "    }\n"
        "}\n\n"
        "server {\n"
        "    listen 443 ssl;\n"
        "    listen [::]:443 ssl;\n"
        f"    server_name {domain};\n\n"
        f"    ssl_certificate {cert_dir}/fullchain.pem;\n"
        f"    ssl_certificate_key {cert_dir}/privkey.pem;\n"
        "    ssl_protocols TLSv1.2 TLSv1.3;\n\n"
        + proxy_block
        + "}\n"
    )


# --------------------------------------------------------------------------- #
# DNS
# --------------------------------------------------------------------------- #

_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def parse_getent_ips(stdout: str) -> set[str]:
    """Extract IPs from ``getent ahosts`` / ``getent ahostsv4`` output.

    Each line looks like ``93.184.216.34  STREAM  example.com``; the first token
    is the address. Returns the unique set (v4 and v6).
    """
    ips: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        first = line.split()[0]
        ips.add(first)
    return ips


def dns_ok(resolved_ips: set[str], server_ips: set[str]) -> bool:
    """True if the domain resolves to at least one of the server's public IPs.

    An empty resolution (NXDOMAIN / no A record) or a pure mismatch is False.
    """
    return bool(resolved_ips) and bool(resolved_ips & server_ips)


def parse_public_ips(stdout: str) -> set[str]:
    """Parse the server's own public IP(s) from an ``ipify``/``curl`` response or
    an ``ip -o addr`` fallback — any IPv4-looking tokens on the output."""
    ips: set[str] = set()
    for token in re.split(r"[\s,]+", stdout.strip()):
        if _IPV4.match(token):
            ips.add(token)
    return ips


# --------------------------------------------------------------------------- #
# certbot
# --------------------------------------------------------------------------- #

# "  Expiry Date: 2026-10-08 12:34:56+00:00 (VALID: 89 days)"
_EXPIRY_RE = re.compile(
    r"Expiry Date:\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?)"
)
_CERTNAME_RE = re.compile(r"Certificate Name:\s*(\S+)")


def parse_certbot_certificates(stdout: str) -> dict[str, datetime]:
    """Parse ``certbot certificates`` output into ``{cert_name: expiry_dt}``.

    The block for each managed certificate carries a ``Certificate Name:`` line
    and an ``Expiry Date:`` line; we pair them. Returns aware UTC datetimes when
    the offset is present, naive otherwise (the caller normalises).
    """
    out: dict[str, datetime] = {}
    current: str | None = None
    for raw in stdout.splitlines():
        line = raw.strip()
        m = _CERTNAME_RE.search(line)
        if m:
            current = m.group(1)
            continue
        m = _EXPIRY_RE.search(line)
        if m and current is not None:
            dt = _parse_dt(m.group(1))
            if dt is not None:
                out[current] = dt
    return out


def _parse_dt(text: str) -> datetime | None:
    text = text.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
