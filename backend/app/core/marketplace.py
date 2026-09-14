"""Frappe app-store: registry cache + compatibility/dependency resolver (DOO-1192).

The Frappe marketplace catalog is a **git repo, not an API**
(`https://github.com/frappe/marketplace`):

- `apps.json` — a flat index of apps. Each entry carries `name, title,
  description, repo, logo_url, website, documentation, categories, category,
  stars, releases` (`releases` is a relative path like `apps/hrms.json`).
- `apps/<name>.json` — `{"name", "releases": [...]}`. Each release carries
  `version, branch, commit, frappe_core, dependencies, channel`, where
  `frappe_core` is a PEP-440 specifier (e.g. `">=15.0.0,<16.0.0"`),
  `dependencies` maps app name -> specifier, and `channel` is
  `"stable"` | `"nightly"`.

This module keeps three concerns apart so all three are unit-testable without a
network or an SSH session (AC7 — the tests fixture the registry files):

1. `RegistryCache` — a server-local shallow clone refreshed on a TTL. Network or
   git failure degrades gracefully to the last good clone; only a *never-cloned*
   registry is a hard error (AC1).
2. `spec_matches` — a focused **PEP-440 subset** just large enough for the
   `frappe_core`/`dependencies` specifiers the catalog actually uses. The bench
   has no `packaging` dependency, and an unparseable specifier must be treated
   as *not installable* rather than crash the catalog (AC2/AC7), so a hand-rolled
   matcher that raises `SpecifierError` on anything it does not understand is the
   right shape here.
3. The resolver — pure functions over parsed registry data + a bench's installed
   Frappe version: `select_release`, `resolve_catalog`, `resolve_install_plan`.

The rule that is the actual value of the feature (from the issue): read the
target bench's installed Frappe version, and for each app select the newest
release whose `frappe_core` contains that version — preferring `stable` over
`nightly`, never offering a release with a missing/unparseable `frappe_core`,
and surfacing incompatible apps with a reason rather than hiding them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from functools import lru_cache

# --------------------------------------------------------------------------- #
# PEP-440 subset: version + specifier matching
# --------------------------------------------------------------------------- #

# A release version: an optional leading "v", then a dotted numeric release
# segment. Any pre/post/dev/local suffix (e.g. "-nightly", "rc1", "+abc") is
# ignored — the catalog's frappe_core ranges only ever gate on the release
# numbers, and treating suffixes as "the release segment" is both sufficient and
# predictable.
_RELEASE_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")

# One clause of a specifier set: an operator then a dotted numeric version, with
# an optional trailing ".*" wildcard (only meaningful for == / !=).
_CLAUSE_RE = re.compile(r"^\s*(===|==|~=|!=|<=|>=|<|>)\s*v?(\d+(?:\.\d+)*)(\.\*)?\s*$")


class SpecifierError(ValueError):
    """A version or specifier could not be parsed by the PEP-440 subset. The
    resolver maps this to "not installable" rather than letting it escape — a
    malformed `frappe_core` must never be offered as installable (AC2)."""


def parse_release(version: str) -> tuple[int, ...] | None:
    """The dotted-numeric release tuple of a version string, or None.

    "15.42.1" -> (15, 42, 1); "v16.0.0-nightly" -> (16, 0, 0); "" -> None.
    """
    m = _RELEASE_RE.match(version or "")
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Compare two release tuples, zero-padding the shorter to equal length."""
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _clause_matches(op: str, target: tuple[int, ...], wild: bool, ver: tuple[int, ...]) -> bool:
    if wild:  # "X.Y.*" — only valid with == / !=
        prefix = ver[: len(target)]
        prefix = prefix + (0,) * (len(target) - len(prefix))
        eq = prefix == target
        if op in ("==", "==="):
            return eq
        if op == "!=":
            return not eq
        raise SpecifierError(f"the '.*' wildcard is not valid with {op!r}")

    c = _cmp(ver, target)
    if op in ("==", "==="):
        return c == 0
    if op == "!=":
        return c != 0
    if op == "<":
        return c < 0
    if op == "<=":
        return c <= 0
    if op == ">":
        return c > 0
    if op == ">=":
        return c >= 0
    if op == "~=":  # compatible release: >= target AND same leading prefix
        if len(target) < 2:
            raise SpecifierError("'~=' requires at least two version segments")
        if c < 0:
            return False
        prefix_len = len(target) - 1
        vp = ver[:prefix_len]
        vp = vp + (0,) * (prefix_len - len(vp))
        return vp == target[:prefix_len]
    raise SpecifierError(f"unknown operator {op!r}")  # pragma: no cover - regex-gated


def spec_matches(specifier: str, version: str) -> bool:
    """True if `version` satisfies every clause of the PEP-440 `specifier` set.

    Raises `SpecifierError` if the version or any clause is unparseable — the
    caller treats that as "not installable" rather than a silent pass.
    """
    ver = parse_release(version)
    if ver is None:
        raise SpecifierError(f"unparseable version {version!r}")
    spec = (specifier or "").strip()
    if not spec:
        raise SpecifierError("empty specifier")
    for raw in spec.split(","):
        clause = raw.strip()
        if not clause:
            continue
        m = _CLAUSE_RE.match(clause)
        if not m:
            raise SpecifierError(f"unparseable specifier clause {clause!r}")
        op, base, wild = m.group(1), m.group(2), bool(m.group(3))
        target = tuple(int(p) for p in base.split("."))
        if not _clause_matches(op, target, wild, ver):
            return False
    return True


# --------------------------------------------------------------------------- #
# Registry data records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AppRecord:
    """One catalog app: its `apps.json` index metadata joined with the release
    list from `apps/<name>.json`."""

    name: str
    repo: str
    releases: tuple[dict, ...]
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedApp:
    """A catalog entry resolved against a specific bench's Frappe version. Field
    names are the AC2 interface contract shared with the frontend issue."""

    name: str
    title: str
    description: str
    repo: str
    logo_url: str | None
    website: str | None
    documentation: str | None
    categories: tuple[str, ...]
    stars: int | None
    branch: str | None
    commit: str | None
    version: str | None
    channel: str | None
    required_version: str | None
    dependencies: dict
    is_installable: bool
    installed: bool
    incompatible_reason: str | None


@dataclass(frozen=True)
class PlanStep:
    """One app to fetch+install, in dependency order. `source` is the value handed
    to `bench get-app` (the release's repo); the ref is pinned to the release's
    `branch` (AC4)."""

    app: str
    source: str
    branch: str
    commit: str | None
    version: str
    required_version: str | None


# --------------------------------------------------------------------------- #
# Compatibility resolution (pure functions over parsed data)
# --------------------------------------------------------------------------- #


def _release_compatible(release: dict, frappe_version: str) -> bool:
    """True only if the release declares a parseable `frappe_core` that contains
    `frappe_version`. A missing or unparseable range is never compatible (AC2)."""
    spec = release.get("frappe_core")
    if not spec or not isinstance(spec, str):
        return False
    try:
        return spec_matches(spec, frappe_version)
    except SpecifierError:
        return False


def select_release(releases: list[dict] | tuple[dict, ...], frappe_version: str) -> dict | None:
    """The best release to install for a bench on `frappe_version`, or None.

    Among releases whose `frappe_core` contains the bench version and whose own
    `version` is parseable: prefer the `stable` channel over `nightly`, and among
    the preferred channel pick the newest `version` (AC / issue "core rule").
    """
    compatible = [
        r
        for r in releases
        if _release_compatible(r, frappe_version)
        and parse_release(r.get("version", "")) is not None
    ]
    if not compatible:
        return None
    stable = [r for r in compatible if (r.get("channel") or "stable") == "stable"]
    pool = stable or compatible
    return max(pool, key=lambda r: parse_release(r["version"]))


def _newest_release(releases: list[dict] | tuple[dict, ...]) -> dict | None:
    """The newest release by parseable `version`, ignoring compatibility. Used to
    explain *why* an app is incompatible ("needs Frappe >=16")."""
    parseable = [r for r in releases if parse_release(r.get("version", "")) is not None]
    if not parseable:
        return None
    return max(parseable, key=lambda r: parse_release(r["version"]))


def _incompatible_reason(record: AppRecord, frappe_version: str) -> str:
    """A human-readable reason an app has no installable release on this bench."""
    newest = _newest_release(record.releases)
    if newest is None or not newest.get("frappe_core"):
        return (
            f"No release declares a Frappe compatibility range, so "
            f"{record.name!r} cannot be offered on Frappe {frappe_version}."
        )
    return (
        f"{record.name!r} requires Frappe {newest['frappe_core']}, "
        f"but this bench runs {frappe_version}."
    )


def resolve_catalog(
    records: list[AppRecord],
    frappe_version: str,
    installed: set[str],
) -> list[ResolvedApp]:
    """Resolve every catalog app against a bench's Frappe version. Incompatible
    apps are returned with `is_installable=False` and a reason, never hidden."""
    out: list[ResolvedApp] = []
    for rec in records:
        chosen = select_release(rec.releases, frappe_version)
        is_installed = rec.name in installed
        if chosen is not None:
            display = chosen
            installable = True
            reason = None
        else:
            # Fall back to the newest release purely for display (branch/version),
            # so the UI can show what the app *would* need.
            display = _newest_release(rec.releases) or {}
            installable = False
            reason = _incompatible_reason(rec, frappe_version)
        meta = rec.meta
        out.append(
            ResolvedApp(
                name=rec.name,
                title=str(meta.get("title") or rec.name),
                description=str(meta.get("description") or ""),
                repo=rec.repo,
                logo_url=meta.get("logo_url"),
                website=meta.get("website"),
                documentation=meta.get("documentation"),
                categories=tuple(
                    meta.get("categories")
                    or ([meta["category"]] if meta.get("category") else [])
                ),
                stars=meta.get("stars") if isinstance(meta.get("stars"), int) else None,
                branch=display.get("branch"),
                commit=display.get("commit"),
                version=display.get("version"),
                channel=display.get("channel"),
                required_version=display.get("frappe_core"),
                dependencies=dict(display.get("dependencies") or {}),
                is_installable=installable,
                installed=is_installed,
                incompatible_reason=reason,
            )
        )
    return out


class DependencyError(ValueError):
    """A dependency could not be resolved: a cycle, a version conflict, an
    incompatible or missing dependency. The install refuses rather than doing a
    partial install (AC5)."""


def resolve_install_plan(
    target: str,
    frappe_version: str,
    lookup,
    installed: set[str],
) -> list[PlanStep]:
    """Resolve `target` and its transitive dependencies into an ordered install
    plan (dependencies first, `target` last).

    `lookup(name)` returns an `AppRecord` or None. Already-installed apps and the
    Frappe core itself are skipped (not re-installed). Cycles and version
    conflicts raise `DependencyError` — no partial plan is returned.
    """
    order: list[str] = []
    chosen: dict[str, tuple[AppRecord, dict]] = {}
    visiting: set[str] = set()
    resolved: set[str] = set()

    def check_spec(name: str, release: dict, spec: str | None, required_by: str | None) -> None:
        if not spec:
            return
        try:
            ok = spec_matches(spec, release.get("version", ""))
        except SpecifierError as exc:
            raise DependencyError(
                f"{required_by!r} requires {name} {spec!r}, which is not a parseable range"
            ) from exc
        if not ok:
            raise DependencyError(
                f"{required_by!r} requires {name} {spec!r}, but the newest "
                f"compatible release is {release.get('version')!r}"
            )

    def visit(name: str, required_by: str | None, spec: str | None) -> None:
        if name == "frappe":
            return  # the bench core is always present
        if name in installed:
            return  # already on the bench; assume it satisfies the requirement
        if name in resolved:
            check_spec(name, chosen[name][1], spec, required_by)
            return
        if name in visiting:
            raise DependencyError(f"dependency cycle detected involving {name!r}")
        record = lookup(name)
        if record is None:
            where = f" (dependency of {required_by!r})" if required_by else ""
            raise DependencyError(f"{name!r}{where} is not in the app catalog")
        release = select_release(record.releases, frappe_version)
        if release is None:
            where = f" (dependency of {required_by!r})" if required_by else ""
            raise DependencyError(
                f"{name!r}{where} has no release compatible with Frappe {frappe_version}"
            )
        check_spec(name, release, spec, required_by)

        visiting.add(name)
        for dep_name, dep_spec in (release.get("dependencies") or {}).items():
            visit(dep_name, name, dep_spec)
        visiting.discard(name)

        resolved.add(name)
        chosen[name] = (record, release)
        order.append(name)

    visit(target, None, None)

    steps: list[PlanStep] = []
    for name in order:
        record, release = chosen[name]
        steps.append(
            PlanStep(
                app=name,
                source=record.repo,
                branch=str(release.get("branch") or ""),
                commit=release.get("commit"),
                version=str(release.get("version") or ""),
                required_version=release.get("frappe_core"),
            )
        )
    return steps


# --------------------------------------------------------------------------- #
# Registry cache — a server-local git clone refreshed on a TTL
# --------------------------------------------------------------------------- #

_INDEX_FILE = "apps.json"
_REFRESH_MARKER = ".fdm_last_refresh"


class RegistryError(RuntimeError):
    """The registry has never been cloned and cannot be cloned now — the only
    hard error (maps to HTTP 503). A dirty/tampered/stale-but-present clone is
    served, not raised (AC1)."""


def _run_git(args: list[str], *, cwd: str | None = None, timeout: int = 120):
    """Run a fixed git argv with no shell, no credential prompt. Returns the
    CompletedProcess; callers decide whether a non-zero rc is fatal."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
    return subprocess.run(  # noqa: S603 — fixed argv, no shell, validated URL
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


class RegistryCache:
    """A shallow git clone of the marketplace repo, refreshed at most once per
    `ttl_seconds`. All git calls go through an injectable `git` runner so tests
    can drive clone/fetch/tamper behaviour without a network."""

    def __init__(
        self,
        *,
        path: str,
        url: str,
        ttl_seconds: int = 3600,
        git=_run_git,
        clock=time.time,
    ) -> None:
        self.path = path
        self.url = url
        self.ttl_seconds = ttl_seconds
        self._git = git
        self._clock = clock

    # -- state -------------------------------------------------------------- #

    def _is_cloned(self) -> bool:
        return os.path.isdir(os.path.join(self.path, ".git"))

    def _marker_path(self) -> str:
        return os.path.join(self.path, _REFRESH_MARKER)

    def _last_refresh_at(self) -> float:
        """The clock value written at the last successful clone/refresh, or 0.0 if
        never refreshed / unreadable. Stored in the marker file (not the file
        mtime) so the injected clock is authoritative in tests."""
        try:
            with open(self._marker_path(), encoding="utf-8") as fh:
                return float(fh.read().strip())
        except (OSError, ValueError):
            return 0.0

    def _touch_refresh(self) -> None:
        try:
            with open(self._marker_path(), "w", encoding="utf-8") as fh:
                fh.write(str(self._clock()))
        except OSError:
            pass

    def _is_stale(self) -> bool:
        return (self._clock() - self._last_refresh_at()) >= self.ttl_seconds

    # -- refresh ------------------------------------------------------------ #

    def _clone(self) -> None:
        parent = os.path.dirname(self.path.rstrip("/")) or "."
        os.makedirs(parent, exist_ok=True)
        res = self._git(["clone", "--depth", "1", self.url, self.path])
        if getattr(res, "returncode", 1) != 0:
            raise RegistryError(
                f"could not clone the app registry from {self.url}: "
                f"{(getattr(res, 'stderr', '') or '').strip()[:200]}"
            )
        self._touch_refresh()

    def _is_tampered(self) -> bool:
        """A dirty working tree means the clone was edited out-of-band; we do not
        trust a `git reset` to recover it silently, but we still SERVE it (AC1)
        and let the next successful fetch overwrite it."""
        res = self._git(["status", "--porcelain"], cwd=self.path)
        if getattr(res, "returncode", 1) != 0:
            return False
        return bool((getattr(res, "stdout", "") or "").strip())

    def _try_refresh(self) -> None:
        """Best-effort fetch+reset. Any git/network failure is swallowed — the
        existing clone keeps being served (AC1)."""
        try:
            fetch = self._git(["fetch", "--depth", "1", "origin", "HEAD"], cwd=self.path)
            if getattr(fetch, "returncode", 1) != 0:
                return
            self._git(["reset", "--hard", "FETCH_HEAD"], cwd=self.path)
            self._touch_refresh()
        except Exception:
            # Network/git error: serve the last good clone rather than error.
            return

    def ensure_fresh(self) -> None:
        """Clone if never cloned (hard error on failure), else refresh if the TTL
        has elapsed (best-effort)."""
        if not self._is_cloned():
            self._clone()
            return
        if self._is_stale():
            self._try_refresh()

    # -- reads -------------------------------------------------------------- #

    def _read_json(self, rel: str):
        with open(os.path.join(self.path, rel), encoding="utf-8") as fh:
            return json.load(fh)

    def load_index(self) -> list[dict]:
        """The parsed `apps.json` index entries. Raises RegistryError if the
        registry has never been cloned."""
        if not self._is_cloned():
            raise RegistryError("the app registry has not been cloned yet")
        try:
            data = self._read_json(_INDEX_FILE)
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"the app registry index is unreadable: {exc}") from exc
        if isinstance(data, dict) and "apps" in data:
            data = data["apps"]
        return [e for e in data if isinstance(e, dict) and e.get("name")]

    def _releases_for(self, entry: dict) -> tuple[dict, ...]:
        rel = entry.get("releases")
        if not isinstance(rel, str) or not rel:
            return ()
        # Guard against a path escaping the clone (defence in depth; the index is
        # operator-trusted but the file is fetched from the internet).
        if rel.startswith("/") or ".." in rel.split("/"):
            return ()
        try:
            data = self._read_json(rel)
        except (OSError, json.JSONDecodeError):
            return ()
        releases = data.get("releases") if isinstance(data, dict) else None
        if not isinstance(releases, list):
            return ()
        return tuple(r for r in releases if isinstance(r, dict))

    def records(self) -> list[AppRecord]:
        """Every catalog app as an `AppRecord` (index metadata + releases)."""
        out: list[AppRecord] = []
        for entry in self.load_index():
            out.append(
                AppRecord(
                    name=entry["name"],
                    repo=str(entry.get("repo") or ""),
                    releases=self._releases_for(entry),
                    meta=entry,
                )
            )
        return out

    def record(self, name: str) -> AppRecord | None:
        for entry in self.load_index():
            if entry.get("name") == name:
                return AppRecord(
                    name=name,
                    repo=str(entry.get("repo") or ""),
                    releases=self._releases_for(entry),
                    meta=entry,
                )
        return None


@lru_cache
def get_registry_cache() -> RegistryCache:
    """Process-wide registry cache built from settings. Overridden in the API via
    FastAPI dependency-injection in tests (no clone/network)."""
    from app.config import get_settings

    s = get_settings()
    return RegistryCache(
        path=os.path.abspath(s.marketplace_cache_dir),
        url=s.marketplace_registry_url,
        ttl_seconds=s.marketplace_cache_ttl_seconds,
    )
