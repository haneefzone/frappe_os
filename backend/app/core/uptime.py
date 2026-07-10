"""External HTTP(S) uptime checker (session 2.7).

Every ~60s the platform makes one **external, read-only** HTTP(S) request to
each enabled site and records an `UptimeSample` (up/down, status code, latency).
Rolling uptime % (24h / 30d) and the last response time feed the site Overview
health card, the Sites-list health dot, the dashboard Sites-Up figure, and the
**real** Fleet Health uptime component (weight 40, replacing the 1.12 placeholder).

Scheduling reuses the **1.12 monitoring leader/lease pattern** (a single-poller
Redis lease across API workers) — NOT rq-scheduler — so the check runs from the
same in-process asyncio loop the monitoring poller uses. This keeps the checker
out of the RQ job engine (it is telemetry, not an audited command) while still
being safe under multiple API workers.

Design points:
- **External / read-only.** A plain GET to the site's health path; it never sends
  cookies, never mutates, and a non-2xx/3xx is recorded as "down", not retried.
- **Backpressure.** Sites are probed through an `asyncio.Semaphore`, so 100 sites
  do not open 100 sockets at once — at most `max_concurrency` are in flight.
- **Per-site enable.** `site.uptime_enabled=False` skips the site entirely.
- **Ring buffer.** Rows past the retention window are pruned after each insert.

`check_site()` (one probe) and `rolling_uptime()` (the summary math) are pure and
unit-tested against a stub transport; the loop (`UptimeChecker`) is a thin
asyncio wrapper mirroring `MonitoringPoller`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from app.models import Bench, Server, Site, UptimeSample

logger = logging.getLogger("app.uptime")

# Frappe ships a cheap, auth-free health endpoint that exercises the whole app
# stack (nginx -> gunicorn -> app), not just the web server. It returns
# {"message":"pong"} with 200 when the site is truly up. Overridable per site.
DEFAULT_CHECK_PATH = "/api/method/ping"

# A probe must return fast; a hung site should fail the check, not stall the loop.
CHECK_TIMEOUT_SECONDS = 10.0

# Redis key for the single-checker lease (leader election across API workers).
_LEADER_KEY = "fdm:uptime:leader"

# 30 days in hours — the widest rolling window we report and the Fleet Health
# uptime window. Retention defaults to this so 30d uptime is always computable.
HOURS_30D = 30 * 24


@dataclass
class CheckResult:
    """The outcome of one external probe."""

    up: bool
    status_code: int | None = None
    latency_ms: float | None = None
    error: str | None = None


def build_check_target(site: Site, bench: Bench, server: Server) -> tuple[str, str | None]:
    """Return (url, host_header) for a site's external probe.

    A `check_url` override is used verbatim (its own Host). Otherwise the target
    is derived like the "Open site" link — http://<server host>:<web port> — with
    the Frappe ping path appended, and the site name sent as the Host header so a
    name-based vhost / dev server routes to the right site.
    """
    if site.check_url:
        return site.check_url, None
    host = server.hostname
    port = bench.webserver_port
    base = f"http://{host}:{port}" if port else f"http://{host}"
    return f"{base}{DEFAULT_CHECK_PATH}", site.name


async def check_site(
    client: httpx.AsyncClient,
    url: str,
    host_header: str | None,
    *,
    timeout: float = CHECK_TIMEOUT_SECONDS,
) -> CheckResult:
    """Perform one external GET and classify the result.

    Up = a response with status < 400. A >= 400 response is "down" with the code;
    a transport failure (refused / timeout / DNS) is "down" with a short reason
    and no code. Latency is wall-clock ms measured around the request; on a
    transport failure it is the time spent before the failure (still useful).
    """
    headers = {"Host": host_header} if host_header else None
    start = time.perf_counter()
    try:
        resp = await client.get(
            url, headers=headers, timeout=timeout, follow_redirects=False
        )
    except httpx.TimeoutException:
        return CheckResult(up=False, error="timeout")
    except httpx.HTTPError as exc:
        reason = (str(exc) or exc.__class__.__name__)[:280]
        return CheckResult(up=False, error=reason)
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    up = resp.status_code < 400
    return CheckResult(
        up=up,
        status_code=resp.status_code,
        latency_ms=latency_ms,
        error=None if up else f"HTTP {resp.status_code}",
    )


def store_uptime_sample(
    db: Session,
    site: Site,
    result: CheckResult,
    *,
    retention_hours: int = HOURS_30D,
) -> UptimeSample:
    """Insert one sample, update the site's health dot, and prune old rows.

    `site.health` is denormalised from the latest check ("ok"/"err") so the Sites
    list and dashboard read the current state without touching this table.
    """
    # Capture the previous health state before overwriting it so we can detect a
    # state flip (ok→err or err→ok) for notification dispatch.
    prev_up: bool | None = None
    if site.health == "ok":
        prev_up = True
    elif site.health == "err":
        prev_up = False

    sample = UptimeSample(
        site_id=site.id,
        up=result.up,
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        error=result.error or None,
    )
    db.add(sample)
    # Reflect the check on the site's health dot (real HTTP check, session 2.7).
    site.health = "ok" if result.up else "err"
    db.commit()
    db.refresh(sample)

    # Fire uptime notifications on state flip (2.8). Import lazily so tests
    # that skip the notification stack continue to work without extra fixtures.
    try:
        from app.core.notifications import dispatch_uptime_event
        dispatch_uptime_event(
            db, site_id=site.id, site_name=site.name, up=result.up, was_up=prev_up
        )
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("app.uptime").exception(
            "notification dispatch failed for site %s", site.id
        )

    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    # synchronize_session=False: a bulk prune never needs to reconcile in-session
    # objects, and letting the DB compare timestamps avoids the aware/naive
    # mismatch SQLite would otherwise hit (mirrors monitoring.store_sample).
    db.execute(
        delete(UptimeSample).where(
            UptimeSample.site_id == site.id,
            UptimeSample.ts < cutoff,
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return sample


def _uptime_fraction(db: Session, site_id: int, since: datetime) -> tuple[int, int]:
    """Return (up_samples, total_samples) for a site since `since`."""
    up_count, total = db.execute(
        select(
            func.sum(case((UptimeSample.up.is_(True), 1), else_=0)),
            func.count(UptimeSample.id),
        ).where(UptimeSample.site_id == site_id, UptimeSample.ts >= since)
    ).one()
    return int(up_count or 0), int(total or 0)


def _pct(up: int, total: int) -> float | None:
    """Uptime percentage, or None when there is no data to divide by."""
    return round(up / total * 100, 2) if total else None


def rolling_uptime(db: Session, site_id: int) -> dict:
    """Summary for a site: 24h and 30d uptime %, plus the latest sample facts."""
    now = datetime.now(UTC)
    up24, tot24 = _uptime_fraction(db, site_id, now - timedelta(hours=24))
    up30, tot30 = _uptime_fraction(db, site_id, now - timedelta(hours=HOURS_30D))
    latest = db.scalars(
        select(UptimeSample)
        .where(UptimeSample.site_id == site_id)
        .order_by(UptimeSample.ts.desc())
        .limit(1)
    ).first()
    return {
        "uptime_24h_pct": _pct(up24, tot24),
        "uptime_30d_pct": _pct(up30, tot30),
        "samples_24h": tot24,
        "samples_30d": tot30,
        "currently_up": latest.up if latest else None,
        "last_status_code": latest.status_code if latest else None,
        "last_latency_ms": latest.latency_ms if latest else None,
        "last_checked_at": latest.ts.isoformat() if latest else None,
    }


def fleet_uptime_fraction(db: Session) -> tuple[float, int]:
    """Fleet-wide 30-day uptime fraction across active, uptime-enabled sites.

    Returns (fraction, sites_with_data). The fraction pools every sample in the
    window (all sites are checked on the same 60s cadence, so pooling ≈ a
    per-site average). With no samples at all it returns (1.0, 0) — "nothing
    measured yet" is treated as compliant so a fresh fleet is not penalised.
    """
    since = datetime.now(UTC) - timedelta(hours=HOURS_30D)
    active_ids = set(
        db.scalars(
            select(Site.id).where(
                Site.status == "active", Site.uptime_enabled.is_(True)
            )
        ).all()
    )
    if not active_ids:
        return 1.0, 0
    up_count, total = db.execute(
        select(
            func.sum(case((UptimeSample.up.is_(True), 1), else_=0)),
            func.count(UptimeSample.id),
        ).where(UptimeSample.site_id.in_(active_ids), UptimeSample.ts >= since)
    ).one()
    total = int(total or 0)
    if total == 0:
        return 1.0, 0
    return int(up_count or 0) / total, len(active_ids)


async def check_and_store(
    client: httpx.AsyncClient,
    db: Session,
    site: Site,
    bench: Bench,
    server: Server,
    *,
    retention_hours: int = HOURS_30D,
) -> UptimeSample:
    """Probe one site and persist the result (up or down)."""
    url, host_header = build_check_target(site, bench, server)
    result = await check_site(client, url, host_header)
    return store_uptime_sample(db, site, result, retention_hours=retention_hours)


class UptimeChecker:
    """Background asyncio loop that externally probes every enabled site.

    Started from the FastAPI lifespan. A Redis lease (`fdm:uptime:leader`,
    TTL = 2 intervals) elects a single checker across API workers — the same
    leader/lease pattern as `MonitoringPoller` (NOT rq-scheduler). With no Redis
    (tests / SQLite dev) the guard is skipped and the single process checks.

    Backpressure: an `asyncio.Semaphore(max_concurrency)` caps in-flight probes so
    a large fleet does not stampede. A shared `httpx.AsyncClient` pools sockets.
    """

    def __init__(
        self,
        session_factory,
        *,
        interval_seconds: int,
        retention_hours: int,
        max_concurrency: int = 10,
        redis_client=None,
    ) -> None:
        self._session_factory = session_factory
        self._interval = max(15, interval_seconds)
        self._retention = retention_hours
        self._max_concurrency = max(1, max_concurrency)
        self._redis = redis_client
        # A token unique to this checker instance so a leader recognises its own
        # lease and renews it (mirrors MonitoringPoller).
        self._token = f"{os.getpid()}:{id(self)}"
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def _is_leader(self) -> bool:
        if self._redis is None:
            return True
        try:
            ttl = self._interval * 2
            holder = self._redis.get(_LEADER_KEY)
            if holder is None:
                return bool(self._redis.set(_LEADER_KEY, self._token, nx=True, ex=ttl))
            if isinstance(holder, bytes):
                holder = holder.decode()
            if holder == self._token:
                # Already leading: RENEW the lease every tick (a bare nx check
                # would fail on our own live key and skip alternate ticks).
                self._redis.set(_LEADER_KEY, self._token, ex=ttl)
                return True
            return False
        except Exception:  # noqa: BLE001 — Redis blip: fall back to checking.
            return True

    def _enabled_targets(self, db: Session) -> list[tuple[int, int, int]]:
        """(site_id, bench_id, server_id) for every active, enabled site."""
        rows = db.execute(
            select(Site.id, Site.bench_id, Bench.server_id)
            .join(Bench, Bench.id == Site.bench_id)
            .where(Site.status == "active", Site.uptime_enabled.is_(True))
        ).all()
        return [(r[0], r[1], r[2]) for r in rows]

    async def _check_one(
        self, client: httpx.AsyncClient, sem: asyncio.Semaphore, target: tuple[int, int, int]
    ) -> None:
        site_id, bench_id, server_id = target
        async with sem:
            if self._stop.is_set():
                return
            # A fresh session per site keeps one bad row from poisoning the rest.
            db: Session = self._session_factory()
            try:
                site = db.get(Site, site_id)
                bench = db.get(Bench, bench_id)
                server = db.get(Server, server_id)
                if site is None or bench is None or server is None:
                    return
                await check_and_store(
                    client, db, site, bench, server, retention_hours=self._retention
                )
            except Exception:  # noqa: BLE001 — a failed check is recorded inside.
                logger.exception("uptime check failed for site %s", site_id)
            finally:
                db.close()

    async def _tick(self) -> None:
        db: Session = self._session_factory()
        try:
            targets = self._enabled_targets(db)
        finally:
            db.close()
        if not targets:
            return
        sem = asyncio.Semaphore(self._max_concurrency)
        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                *(self._check_one(client, sem, t) for t in targets)
            )

    async def _loop(self) -> None:
        logger.info("uptime checker started (interval=%ss)", self._interval)
        while not self._stop.is_set():
            try:
                if self._is_leader():
                    await self._tick()
            except Exception:  # noqa: BLE001 — never let the loop die.
                logger.exception("uptime loop iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
