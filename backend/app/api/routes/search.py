"""Role-scoped entity + action search (session 2.8).

Powers the ⌘K command palette. Returns a ranked blend of:
- Entities: servers, benches, sites (name/hostname match)
- Jobs: recent jobs matching the action name or target
- Schedules: by name or action
- Navigation: fixed set of route labels
- Actions: typed action cards that the palette can dispatch as real CommandJobs
  via POST /api/jobs — **never raw shell** (golden rule 1).

Role-filtering is server-side (CLAUDE.md rule 7). Read-only sees entities +
navigation but no mutating action cards. Actions map only to existing registered
command templates.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.core.permissions import role_allows
from app.db import get_db
from app.models.bench import Bench
from app.models.job import CommandJob
from app.models.schedule import Schedule
from app.models.server import Server
from app.models.site import Site

router = APIRouter(prefix="/api/search", tags=["search"])

_ILIKE_LIMIT = 10  # max rows fetched per entity type


# --------------------------------------------------------------------------- #
# Result schema                                                                #
# --------------------------------------------------------------------------- #

class SearchResult(BaseModel):
    kind: str          # "server" | "bench" | "site" | "job" | "schedule" | "nav" | "action"
    id: str            # unique palette id (kind:entity_id or "nav:<name>")
    title: str
    subtitle: str | None = None
    url: str | None = None   # client-side route to navigate on Enter
    # For "action" results: the dispatch payload the palette POSTs to /api/jobs.
    action: dict | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]


# --------------------------------------------------------------------------- #
# Navigation items (always returned first when q matches)                     #
# --------------------------------------------------------------------------- #

_NAV_ITEMS = [
    ("dashboard",  "Dashboard",      "/"),
    ("servers",    "Servers",        "/servers"),
    ("benches",    "Benches",        "/benches"),
    ("sites",      "Sites",          "/sites"),
    ("apps",       "Apps",           "/apps"),
    ("backups",    "Backups",        "/backups"),
    ("restore",    "Restore",        "/restore"),
    ("jobs",       "Jobs",           "/jobs"),
    ("schedules",  "Schedules",      "/schedules"),
    ("monitoring", "Monitoring",     "/monitoring"),
    ("logs",       "Logs",           "/logs"),
    ("terminal",   "Terminal",       "/terminal"),
    ("audit-log",  "Audit Log",      "/audit-log"),
    ("settings",   "Settings",       "/settings"),
]


def _nav_results(q: str) -> list[SearchResult]:
    q_lower = q.lower()
    results = []
    for name, label, url in _NAV_ITEMS:
        if q_lower in label.lower() or q_lower in name:
            results.append(SearchResult(kind="nav", id=f"nav:{name}", title=label, url=url))
    return results


# --------------------------------------------------------------------------- #
# Action templates — maps to command_templates only (golden rule 1)           #
# --------------------------------------------------------------------------- #

def _action_results(q: str, sites: list[Site], can_mutate: bool) -> list[SearchResult]:
    if not can_mutate:
        return []
    q_lower = q.lower()
    results: list[SearchResult] = []
    for site in sites:
        if (
            "backup" in q_lower
            or site.name.lower().startswith(q_lower)
            or q_lower in site.name.lower()
        ):
            results.append(
                SearchResult(
                    kind="action",
                    id=f"action:site.backup:{site.id}",
                    title=f"Backup site — {site.name}",
                    subtitle="site.backup · creates a backup job",
                    action={
                        "action_name": "site.backup",
                        "target_type": "site",
                        "target_id": site.id,
                        "params": {},
                    },
                )
            )
        if (
            "terminal" in q_lower
            or site.name.lower().startswith(q_lower)
            or q_lower in site.name.lower()
        ):
            results.append(
                SearchResult(
                    kind="action",
                    id=f"action:terminal:{site.id}",
                    title=f"Open terminal on bench — {site.name}",
                    subtitle="opens Terminal for the site's bench",
                    url="/terminal",
                )
            )
    return results[:_ILIKE_LIMIT]


# --------------------------------------------------------------------------- #
# Main endpoint                                                                #
# --------------------------------------------------------------------------- #

@router.get("", response_model=SearchResponse)
def search(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=20, le=50),
) -> SearchResponse:
    if not q.strip():
        # Empty query → return quick-access recents placeholder; the palette
        # fills recents from localStorage; nav items always shown.
        nav = [
            SearchResult(kind="nav", id=f"nav:{name}", title=label, url=url)
            for name, label, url in _NAV_ITEMS[:6]
        ]
        return SearchResponse(results=nav)

    q_lower = q.strip().lower()
    results: list[SearchResult] = []

    # 1. Navigation
    results.extend(_nav_results(q))

    # 2. Servers
    servers = db.execute(
        select(Server).where(
            or_(
                Server.name.ilike(f"%{q_lower}%"),
                Server.hostname.ilike(f"%{q_lower}%"),
            )
        ).limit(_ILIKE_LIMIT)
    ).scalars().all()
    for s in servers:
        results.append(
            SearchResult(
                kind="server",
                id=f"server:{s.id}",
                title=s.name,
                subtitle=s.hostname,
                url=f"/servers/{s.id}",
            )
        )

    # 3. Benches
    benches = db.execute(
        select(Bench).where(Bench.name.ilike(f"%{q_lower}%")).limit(_ILIKE_LIMIT)
    ).scalars().all()
    for b in benches:
        results.append(
            SearchResult(
                kind="bench",
                id=f"bench:{b.id}",
                title=b.name,
                subtitle=b.frappe_version or "bench",
                url=f"/benches/{b.id}",
            )
        )

    # 4. Sites
    sites = db.execute(
        select(Site).where(
            Site.name.ilike(f"%{q_lower}%"),
            Site.status == "active",
        ).limit(_ILIKE_LIMIT)
    ).scalars().all()
    for s in sites:
        results.append(
            SearchResult(
                kind="site",
                id=f"site:{s.id}",
                title=s.name,
                subtitle="site",
                url=f"/sites/{s.id}",
            )
        )

    # 5. Recent jobs
    jobs = db.execute(
        select(CommandJob).where(
            CommandJob.action_name.ilike(f"%{q_lower}%")
        ).order_by(CommandJob.created_at.desc()).limit(5)
    ).scalars().all()
    for j in jobs:
        results.append(
            SearchResult(
                kind="job",
                id=f"job:{j.id}",
                title=f"{j.action_name} #{j.id}",
                subtitle=j.status,
                url=f"/jobs/{j.id}",
            )
        )

    # 6. Schedules
    schedules = db.execute(
        select(Schedule).where(Schedule.name.ilike(f"%{q_lower}%")).limit(5)
    ).scalars().all()
    for s in schedules:
        results.append(
            SearchResult(
                kind="schedule",
                id=f"schedule:{s.id}",
                title=s.name,
                subtitle=s.action_name,
                url="/schedules",
            )
        )

    # 7. Actions (mutating — Read-only filtered out)
    can_mutate = role_allows(list(user.role.permissions or []), "site:operate")
    results.extend(_action_results(q, sites, can_mutate))

    # De-duplicate by id; preserve insertion order.
    seen: set[str] = set()
    deduped: list[SearchResult] = []
    for r in results:
        if r.id not in seen:
            seen.add(r.id)
            deduped.append(r)

    return SearchResponse(results=deduped[:limit])
