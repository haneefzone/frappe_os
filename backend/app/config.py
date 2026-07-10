from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shipped defaults are placeholders, not credentials: unless DEBUG=true,
# Settings refuses to boot until the operator replaces them (SEC-H1, DOO-66).
JWT_SECRET_PLACEHOLDER = "change-me-long-random-string"
FDM_SECRET_KEY_PLACEHOLDER = "change-me-generate-a-fernet-key"
# RFC 7518 §3.2: HS256 keys must be at least as long as the hash output.
MIN_JWT_SECRET_BYTES = 32


class Settings(BaseSettings):
    """Environment-driven settings. Names map to env vars case-insensitively
    (database_url <- DATABASE_URL, fdm_secret_key <- FDM_SECRET_KEY, ...)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "FDM Platform"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://fdm:fdm@localhost:5432/fdm"
    redis_url: str = "redis://localhost:6379/0"

    # Master key for Fernet secrets-at-rest; never stored in DB.
    fdm_secret_key: str = FDM_SECRET_KEY_PLACEHOLDER
    jwt_secret: str = JWT_SECRET_PLACEHOLDER

    # Session lifetimes (CLAUDE.md: access 15m, refresh 7d).
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 7 * 24 * 3600
    # Secure cookies require HTTPS; set COOKIE_SECURE=false only for plain-HTTP dev.
    cookie_secure: bool = True

    # Login throttling: the Nth consecutive failure locks the (email, IP) pair.
    login_lockout_threshold: int = 6
    login_lockout_seconds: int = 10 * 60
    # Per-email cross-IP backstop (SEC-M1): this many failures inside the
    # window — from ANY combination of source IPs — lock the account for
    # login_lockout_seconds, so rotating IPs cannot spray one account forever.
    login_email_failure_limit: int = 20
    login_email_failure_window_seconds: int = 3600

    # Reverse proxies whose X-Forwarded-For we honour (comma-separated IPs,
    # CIDRs, or literals). Empty (default) = trust no proxy: the throttle keys
    # on the socket peer address and any client-sent X-Forwarded-For is
    # ignored. Behind the deploy/nginx.conf proxy set TRUSTED_PROXY_IPS to the
    # proxy address (127.0.0.1 when nginx runs on the same host) — never "*"
    # on an internet-facing service (SEC-M1).
    trusted_proxy_ips: str = ""

    # Comma-separated list of allowed browser origins.
    cors_origins: str = "http://localhost:5173"

    default_tz: str = "Asia/Dubai"

    # Comma-separated allowlist of git hosts a repo App Source may point at
    # (CLAUDE.md golden rule 1: repo URLs validated against a host allowlist).
    # Marketplace bare names bypass this. Set via REPO_HOST_ALLOWLIST.
    repo_host_allowlist: str = "github.com,gitlab.com"

    # Terminal idle timeout: WS closes if no input within this window. Warning
    # message is injected 60s before. Set via TERMINAL_IDLE_TIMEOUT_SECONDS.
    terminal_idle_timeout_seconds: int = 15 * 60
    # Short-lived ticket TTL (seconds). Single-use, stored in Redis.
    terminal_ticket_ttl_seconds: int = 60

    # Absolute path to the built SPA (frontend/dist). Empty (dev default) =
    # do not serve static files; the Vite dev server owns the UI instead.
    frontend_dist: str = ""

    # Directory for operator uploads (currently the white-label logo). A relative
    # path resolves from the backend working directory. Set via UPLOADS_DIR.
    uploads_dir: str = "uploads"

    # Monitoring poller (session 1.12): how often each server is SSH-polled and
    # how long samples are retained. Set MONITORING_ENABLED=false to run a
    # dedicated poller elsewhere instead of the in-process loop.
    monitoring_enabled: bool = True
    monitoring_interval_seconds: int = 60
    monitoring_retention_hours: int = 168  # 7 days

    # Uptime checker (session 2.7): external HTTP(S) probe per enabled site.
    # Interval 60s per spec; retention 30d so 30-day uptime is always computable;
    # max_concurrency caps in-flight probes so a large fleet doesn't stampede.
    # Set UPTIME_ENABLED=false to run a dedicated checker elsewhere.
    uptime_enabled: bool = True
    uptime_interval_seconds: int = 60
    uptime_retention_hours: int = 720  # 30 days
    uptime_max_concurrency: int = 10

    # Scheduler (session 2.1): the rq-scheduler-driven process ticks this often,
    # firing every schedule whose next_run_at has arrived through the JobRunner.
    # A smaller interval fires closer to the wall-clock minute a cron names, at
    # the cost of more (cheap) DB sweeps. Set via SCHEDULER_TICK_SECONDS.
    scheduler_tick_seconds: int = 30
    # The RQ queue the recurring tick job is enqueued onto (a normal worker runs
    # it). The CommandJobs each fire then land on the schedule's own priority.
    scheduler_queue: str = "default"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_proxy_ip_list(self) -> list[str]:
        return [host.strip() for host in self.trusted_proxy_ips.split(",") if host.strip()]

    @property
    def repo_host_allowlist_set(self) -> set[str]:
        return {h.strip().lower() for h in self.repo_host_allowlist.split(",") if h.strip()}

    @model_validator(mode="after")
    def _require_real_secrets_outside_debug(self) -> "Settings":
        """Fail closed (SEC-H1): with DEBUG unset/false, refuse to start on
        placeholder or weak signing secrets — a forged token would grant Admin —
        or an FDM_SECRET_KEY that is not a valid Fernet key (SecretsService would
        otherwise fail only on first use, silently storing unreadable rows)."""
        if self.debug:
            return self
        problems = []
        if (
            self.jwt_secret == JWT_SECRET_PLACEHOLDER
            or len(self.jwt_secret.encode("utf-8")) < MIN_JWT_SECRET_BYTES
        ):
            problems.append(
                "JWT_SECRET is the placeholder or shorter than 32 bytes"
                " (RFC 7518 §3.2). Generate one with:"
                " python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        if self.fdm_secret_key == FDM_SECRET_KEY_PLACEHOLDER:
            problems.append(
                "FDM_SECRET_KEY is the placeholder. Generate one with:"
                " python3 -c \"from cryptography.fernet import Fernet;"
                " print(Fernet.generate_key().decode())\""
            )
        else:
            from cryptography.fernet import Fernet

            try:
                Fernet(self.fdm_secret_key.encode())
            except (ValueError, TypeError):
                problems.append(
                    "FDM_SECRET_KEY is not a valid Fernet key. Generate one with:"
                    " python3 -c \"from cryptography.fernet import Fernet;"
                    " print(Fernet.generate_key().decode())\""
                )
        if problems:
            raise ValueError(
                "Refusing to start with DEBUG=false: "
                + " | ".join(problems)
                + " | Put the generated value(s) in your environment or .env file"
                " (see .env.example). Set DEBUG=true only for local development."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
