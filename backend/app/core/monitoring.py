"""Lightweight per-server monitoring poller (session 1.12).

Every ~60s (configurable) the platform SSHes each active server and runs one
fixed, read-only probe script — `/proc/stat` for CPU, `/proc/meminfo` for RAM,
`df` for disk on `/`, `/proc/loadavg`, and `systemctl is-active` for the four
managed services. The result is one `MonitoringSample` row; rows older than the
retention window are pruned after each insert, so the table is a rolling buffer.

The probe is a *constant* shell script (no interpolation, no user input), passed
as a single argv element to `bash -lc` and executed with the same pooled SSH
service the job engine uses. It reads only — service *restarts* go through the
audited `server.restart_service` job (sudoers allowlist), never here.

`parse_sample()` is pure and unit-tested against captured probe output; the
scheduling glue (`MonitoringPoller`) is a thin asyncio loop guarded by a Redis
lease so only one process polls even if several API workers run.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import MonitoringSample, Server
from app.models.monitoring import MANAGED_SERVICES

logger = logging.getLogger("app.monitoring")

# Fixed, read-only probe. Two /proc/stat reads 1s apart give a CPU-utilisation
# delta; the rest is a single snapshot. `systemctl is-active` is a read every
# user may run — no sudo — so a 60s poll never touches the sudoers allowlist
# (restarts do; see server.restart_service). Emits one prefixed line per metric.
MONITOR_SNIPPET = r"""
echo "CPU1 $(grep '^cpu ' /proc/stat)"
sleep 1
echo "CPU2 $(grep '^cpu ' /proc/stat)"
echo "MEMTOTAL $(awk '/^MemTotal:/{print $2}' /proc/meminfo)"
echo "MEMAVAIL $(awk '/^MemAvailable:/{print $2}' /proc/meminfo)"
echo "DISK $(df -kP / | awk 'NR==2{print $2, $3, $5}')"
echo "LOAD $(awk '{print $1}' /proc/loadavg)"
for s in nginx mariadb redis-server supervisor; do
  echo "SVC $s $(systemctl is-active $s 2>/dev/null || echo unknown)"
done
""".strip()

MONITOR_ARGV = ["bash", "-lc", MONITOR_SNIPPET]

# Probe must return fast; a hung server should fail the poll, not stall the loop.
POLL_TIMEOUT_SECONDS = 20.0

# Redis key for the single-poller lease (leader election across API workers).
_LEADER_KEY = "fdm:monitoring:leader"


@dataclass
class SampleFields:
    """Parsed metrics from one probe run (all optional — a partial probe still
    yields whatever it could read)."""

    cpu_pct: float | None = None
    mem_pct: float | None = None
    mem_used_mb: int | None = None
    mem_total_mb: int | None = None
    disk_pct: float | None = None
    disk_used_gb: float | None = None
    disk_total_gb: float | None = None
    load1: float | None = None
    services: dict[str, str] = field(default_factory=dict)


def _cpu_counters(line: str) -> tuple[int, int]:
    """Return (idle, total) jiffies from a `cpu ...` /proc/stat line.

    idle = idle + iowait; total = sum of every counter. Non-numeric or short
    lines yield (0, 0) so the delta math degrades to "unknown" rather than crash.
    """
    parts = line.split()
    # parts[0] is the "cpu" label; the rest are counters.
    nums = []
    for tok in parts[1:]:
        try:
            nums.append(int(tok))
        except ValueError:
            break
    if len(nums) < 5:
        return 0, 0
    idle = nums[3] + nums[4]  # idle + iowait
    return idle, sum(nums)


def parse_sample(stdout: str) -> SampleFields:
    """Parse the prefixed probe output into metrics. Tolerant of missing lines."""
    fields = SampleFields()
    cpu1_idle = cpu1_total = cpu2_idle = cpu2_total = None
    mem_avail_kb: int | None = None

    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        tag, _, rest = line.partition(" ")
        rest = rest.strip()
        try:
            if tag == "CPU1":
                cpu1_idle, cpu1_total = _cpu_counters(rest)
            elif tag == "CPU2":
                cpu2_idle, cpu2_total = _cpu_counters(rest)
            elif tag == "MEMTOTAL":
                fields.mem_total_mb = int(int(rest) / 1024)
            elif tag == "MEMAVAIL":
                mem_avail_kb = int(rest)
            elif tag == "DISK":
                total_kb, used_kb, pct = rest.split()
                fields.disk_total_gb = round(int(total_kb) / 1024 / 1024, 2)
                fields.disk_used_gb = round(int(used_kb) / 1024 / 1024, 2)
                fields.disk_pct = float(pct.rstrip("%"))
            elif tag == "LOAD":
                fields.load1 = float(rest)
            elif tag == "SVC":
                name, _, state = rest.partition(" ")
                if name in MANAGED_SERVICES:
                    fields.services[name] = state.strip() or "unknown"
        except (ValueError, IndexError):
            continue  # skip a malformed line; keep whatever else parsed.

    if cpu1_total and cpu2_total and cpu2_total > cpu1_total:
        idle_delta = (cpu2_idle or 0) - (cpu1_idle or 0)
        total_delta = cpu2_total - cpu1_total
        busy = 1.0 - (idle_delta / total_delta if total_delta else 1.0)
        fields.cpu_pct = round(max(0.0, min(1.0, busy)) * 100, 1)

    # Memory %: used = total - available (kB), rendered against total.
    if fields.mem_total_mb and mem_avail_kb is not None:
        total_kb = fields.mem_total_mb * 1024
        used_kb = max(0, total_kb - mem_avail_kb)
        fields.mem_used_mb = int(used_kb / 1024)
        fields.mem_pct = round(used_kb / total_kb * 100, 1) if total_kb else None

    # Any service the probe never reported is unknown (e.g. not installed).
    for name in MANAGED_SERVICES:
        fields.services.setdefault(name, "unknown")

    return fields


def store_sample(
    db: Session,
    server_id: int,
    fields: SampleFields | None,
    *,
    ok: bool,
    error: str | None = None,
    retention_hours: int = 168,
) -> MonitoringSample:
    """Insert one sample (ok or failed) and prune rows past the retention window."""
    fields = fields or SampleFields()
    sample = MonitoringSample(
        server_id=server_id,
        ok=ok,
        error=(error or None) if not ok else None,
        cpu_pct=fields.cpu_pct,
        mem_pct=fields.mem_pct,
        mem_used_mb=fields.mem_used_mb,
        mem_total_mb=fields.mem_total_mb,
        disk_pct=fields.disk_pct,
        disk_used_gb=fields.disk_used_gb,
        disk_total_gb=fields.disk_total_gb,
        load1=fields.load1,
        services=fields.services or {},
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)

    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    # synchronize_session=False: a bulk prune never needs to reconcile in-session
    # objects, and evaluating the criteria in Python would compare the aware
    # cutoff against SQLite's tz-naive `ts` (a real deployment uses Postgres,
    # where ts is tz-aware) — let the database do the comparison.
    db.execute(
        delete(MonitoringSample).where(
            MonitoringSample.server_id == server_id,
            MonitoringSample.ts < cutoff,
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return sample


async def poll_server(ssh, server: Server) -> SampleFields:
    """Run the probe on one server and return parsed metrics.

    Raises on connection/probe failure so the caller records a failed sample.
    """
    cred = server.credential
    if cred is None:
        raise RuntimeError("server has no SSH credential")
    conn = await ssh.connect(server, cred)
    out = await ssh.run(conn, MONITOR_ARGV, timeout=POLL_TIMEOUT_SECONDS)
    if out.exit_status != 0 and not out.stdout.strip():
        raise RuntimeError(out.stderr.strip()[:280] or f"probe exited {out.exit_status}")
    return parse_sample(out.stdout)


async def poll_and_store(
    ssh, db: Session, server: Server, *, retention_hours: int = 168
) -> MonitoringSample:
    """Poll one server and persist the result (success or failure)."""
    try:
        fields = await poll_server(ssh, server)
        sample = store_sample(db, server.id, fields, ok=True, retention_hours=retention_hours)
        server.status = "online"
        server.last_seen = datetime.now(UTC)
        db.commit()
        return sample
    except Exception as exc:  # noqa: BLE001 — every failure becomes a recorded sample.
        reason = str(exc) or exc.__class__.__name__
        logger.warning("monitoring poll failed for server %s: %s", server.id, reason)
        sample = store_sample(
            db, server.id, None, ok=False, error=reason, retention_hours=retention_hours
        )
        server.status = "offline"
        db.commit()
        return sample


class MonitoringPoller:
    """Background asyncio loop that polls every active server on an interval.

    Started from the FastAPI lifespan. A Redis lease (`fdm:monitoring:leader`,
    TTL = 2 intervals) elects a single poller across API workers, so N processes
    don't each SSH every server. With no Redis (tests / SQLite dev) the guard is
    skipped and the single process polls.
    """

    def __init__(
        self,
        ssh,
        session_factory,
        *,
        interval_seconds: int,
        retention_hours: int,
        redis_client=None,
    ) -> None:
        self._ssh = ssh
        self._session_factory = session_factory
        self._interval = max(15, interval_seconds)
        self._retention = retention_hours
        self._redis = redis_client
        # A token unique to this poller instance so a leader recognises its own
        # lease and renews it (a bare "1" couldn't tell self from another holder).
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
                # Lease is free — take it.
                return bool(self._redis.set(_LEADER_KEY, self._token, nx=True, ex=ttl))
            if isinstance(holder, bytes):
                holder = holder.decode()
            if holder == self._token:
                # We already lead: RENEW the lease and keep polling every tick.
                # (The previous nx-only check failed on our own live key, so the
                # leader skipped every other tick — halving the real cadence.)
                self._redis.set(_LEADER_KEY, self._token, ex=ttl)
                return True
            # Another live leader holds the lease.
            return False
        except Exception:  # noqa: BLE001 — Redis blip: fall back to polling.
            return True

    async def _tick(self) -> None:
        db: Session = self._session_factory()
        try:
            servers = list(
                db.scalars(select(Server).where(Server.status != "archived")).all()
            )
        finally:
            pass
        for server in servers:
            if self._stop.is_set():
                break
            # A fresh session per server keeps one bad row from poisoning the rest.
            sdb: Session = self._session_factory()
            try:
                fresh = sdb.get(Server, server.id)
                if fresh is not None and fresh.credential is not None:
                    await poll_and_store(
                        self._ssh, sdb, fresh, retention_hours=self._retention
                    )
            except Exception:  # noqa: BLE001
                logger.exception("monitoring tick failed for server %s", server.id)
            finally:
                sdb.close()
        db.close()

    async def _loop(self) -> None:
        logger.info("monitoring poller started (interval=%ss)", self._interval)
        while not self._stop.is_set():
            try:
                if self._is_leader():
                    await self._tick()
            except Exception:  # noqa: BLE001 — never let the loop die.
                logger.exception("monitoring loop iteration failed")
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
