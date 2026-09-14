"""Frappe app store — the `frappe/marketplace` catalog (DOO-1194).

The catalog is a **git repo, not an API** (`https://github.com/frappe/marketplace`):
a flat `apps.json` index plus one `apps/<name>.json` per app carrying its
releases. We shallow-clone it and refresh on a schedule, then read it off disk —
no auth, no rate limit, works offline once cloned.

This module keeps three concerns apart so each is unit-testable without a network
or a DB:

- ``RegistryReader`` — a pure filesystem reader/parser over an on-disk catalog
  tree. Point it at a fixture directory in tests; it never touches git.
- ``RegistryCache`` — owns the shallow clone + scheduled refresh on top of a
  reader. It **serves the last good cache rather than failing** when the clone is
  unreachable (offline hosts are normal for us — AC1).
- the resolver (``compatible_releases`` / ``best_release`` / ``resolve_plan``) —
  pure functions over parsed data: compatibility against a target bench's Frappe
  version, and transitive dependency resolution with cycle and version-conflict
  detection (AC2, AC3).

Compatibility uses PEP-440 specifiers (each release's ``frappe_core``, e.g.
``>=15.0.0,<16.0.0``) via ``packaging``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

logger = logging.getLogger("app.marketplace")


# --------------------------------------------------------------------------- #
# Parsed catalog data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Release:
    """One publishable release of an app (a row of ``apps/<name>.json``)."""

    version: str
    branch: str
    commit: str
    # PEP-440 specifier the target Frappe core must satisfy (e.g. ">=15.0.0,<16.0.0").
    frappe_core: str
    # app name -> PEP-440 specifier the resolved dependency must satisfy.
    dependencies: dict[str, str] = field(default_factory=dict)
    channel: str = "stable"  # stable | nightly

    @classmethod
    def from_dict(cls, d: dict) -> Release:
        return cls(
            version=str(d.get("version", "")),
            branch=str(d.get("branch", "")),
            commit=str(d.get("commit", "")),
            frappe_core=str(d.get("frappe_core", "")),
            dependencies={str(k): str(v) for k, v in (d.get("dependencies") or {}).items()},
            channel=str(d.get("channel", "stable")),
        )


@dataclass(frozen=True)
class App:
    """One catalog entry (a row of ``apps.json``)."""

    name: str
    title: str
    description: str
    repo: str
    logo_url: str | None = None
    website: str | None = None
    documentation: str | None = None
    categories: tuple[str, ...] = ()
    category: str | None = None
    stars: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> App:
        return cls(
            name=str(d.get("name", "")),
            title=str(d.get("title", "") or d.get("name", "")),
            description=str(d.get("description", "") or ""),
            repo=str(d.get("repo", "") or ""),
            logo_url=d.get("logo_url"),
            website=d.get("website"),
            documentation=d.get("documentation"),
            categories=tuple(d.get("categories") or ()),
            category=d.get("category"),
            stars=int(d.get("stars") or 0),
        )


# --------------------------------------------------------------------------- #
# Errors — every one carries a human-readable message (AC2, AC3)
# --------------------------------------------------------------------------- #


class RegistryUnavailableError(RuntimeError):
    """The catalog has never been cloned and a refresh could not fetch it — there
    is no cache to serve. (A refresh that fails *with* a prior cache present does
    NOT raise: stale is served instead.)"""


class ResolutionError(ValueError):
    """Base for every dependency-resolution failure. The message is legible and
    safe to surface to an operator."""


class UnknownAppError(ResolutionError):
    """An app (or a required dependency) is not in the catalog."""


class IncompatibleError(ResolutionError):
    """No release of an app is compatible with the target Frappe version."""


class VersionConflictError(ResolutionError):
    """Two dependents require the same app under specifiers that no single
    available release can satisfy — never installed partially."""


class DependencyCycleError(ResolutionError):
    """A dependency cycle was detected (app A -> ... -> app A)."""


# --------------------------------------------------------------------------- #
# Compatibility helpers (pure)
# --------------------------------------------------------------------------- #


def _parse_version(v: str) -> Version | None:
    try:
        return Version(v)
    except (InvalidVersion, TypeError):
        return None


def _spec_contains(spec_str: str, version: str) -> bool:
    """Does the PEP-440 specifier ``spec_str`` contain ``version``? A malformed
    specifier or version is treated as *not* matching (conservative — never
    reports an incompatible app as installable)."""
    ver = _parse_version(version)
    if ver is None:
        return False
    try:
        spec = SpecifierSet(spec_str)
    except InvalidSpecifier:
        return False
    # prereleases=True so a dev/prerelease *bench* Frappe still matches a normal
    # bound; a stable bench version never spuriously matches a nightly's lower
    # bound because ordering still holds (16.x < 17.0.0-dev).
    return spec.contains(ver, prereleases=True)


def _release_sort_key(r: Release) -> tuple:
    """Newest-first ordering key; unparseable versions sort last."""
    ver = _parse_version(r.version)
    return (ver is not None, ver or Version("0"))


def compatible_releases(releases: list[Release], frappe_version: str) -> list[Release]:
    """Releases whose ``frappe_core`` specifier contains ``frappe_version``,
    newest first."""
    compat = [r for r in releases if _spec_contains(r.frappe_core, frappe_version)]
    return sorted(compat, key=_release_sort_key, reverse=True)


def best_release(
    releases: list[Release],
    frappe_version: str,
    *,
    extra_specifier: SpecifierSet | None = None,
) -> Release | None:
    """The release to install: newest compatible one, preferring the ``stable``
    channel over ``nightly``. ``extra_specifier`` (a dependency constraint on the
    app's own version) further narrows the candidates. Returns None if nothing
    qualifies."""
    candidates = compatible_releases(releases, frappe_version)
    if extra_specifier is not None:
        candidates = [
            r
            for r in candidates
            if (v := _parse_version(r.version)) is not None
            and extra_specifier.contains(v, prereleases=True)
        ]
    if not candidates:
        return None
    stable = [r for r in candidates if r.channel == "stable"]
    return (stable or candidates)[0]


# --------------------------------------------------------------------------- #
# Filesystem reader over an on-disk catalog tree
# --------------------------------------------------------------------------- #


class RegistryReader:
    """Reads a marketplace catalog tree off disk. No git, no network — a fixture
    directory works in tests. ``apps.json`` is parsed once and memoised; per-app
    release files are read lazily and memoised."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._apps: dict[str, App] | None = None
        self._releases: dict[str, list[Release]] = {}

    @property
    def apps_json(self) -> Path:
        return self.root / "apps.json"

    def is_populated(self) -> bool:
        return self.apps_json.is_file()

    def _load_apps(self) -> dict[str, App]:
        if self._apps is None:
            if not self.apps_json.is_file():
                raise RegistryUnavailableError(
                    f"catalog index not found at {self.apps_json}"
                )
            data = json.loads(self.apps_json.read_text())
            self._apps = {}
            for row in data:
                app = App.from_dict(row)
                if app.name:
                    self._apps[app.name] = app
        return self._apps

    def apps(self) -> list[App]:
        """Every catalog entry, sorted by title (case-insensitive)."""
        return sorted(self._load_apps().values(), key=lambda a: a.title.lower())

    def get_app(self, name: str) -> App | None:
        return self._load_apps().get(name)

    def releases(self, name: str) -> list[Release]:
        """Releases for one app, newest first. Empty list if the app is unknown
        or its release file is missing/malformed."""
        if name in self._releases:
            return self._releases[name]
        rels: list[Release] = []
        path = self.root / "apps" / f"{name}.json"
        if path.is_file():
            try:
                doc = json.loads(path.read_text())
                raw = doc.get("releases", []) if isinstance(doc, dict) else doc
                rels = [Release.from_dict(r) for r in (raw or [])]
                rels.sort(key=_release_sort_key, reverse=True)
            except (json.JSONDecodeError, AttributeError, TypeError):
                logger.warning("marketplace: malformed release file %s", path)
                rels = []
        self._releases[name] = rels
        return rels


# --------------------------------------------------------------------------- #
# Git-backed cache: shallow clone + refresh, serve-stale on failure (AC1)
# --------------------------------------------------------------------------- #


class RegistryCache:
    """Shallow-clone the catalog repo and refresh it on demand, exposing a
    ``RegistryReader`` over the checkout. A refresh that cannot reach the remote
    logs and **serves the existing cache** instead of raising — unless there is
    no cache at all, in which case ``RegistryUnavailableError`` is raised.

    ``git_runner`` is injectable so tests exercise clone/refresh/serve-stale
    without a network."""

    def __init__(
        self,
        cache_dir: str | Path,
        repo_url: str,
        *,
        branch: str = "main",
        git_runner=None,
    ):
        self.cache_dir = Path(cache_dir)
        self.repo_url = repo_url
        self.branch = branch
        self._git = git_runner or _run_git

    @property
    def checkout(self) -> Path:
        return self.cache_dir / "repo"

    def _is_cloned(self) -> bool:
        return (self.checkout / ".git").is_dir()

    def reader(self) -> RegistryReader:
        """A reader over the current checkout. Raises RegistryUnavailableError if
        nothing has ever been cloned."""
        r = RegistryReader(self.checkout)
        if not r.is_populated():
            raise RegistryUnavailableError(
                "marketplace catalog has not been cloned yet — run a refresh "
                "while online"
            )
        return r

    def refresh(self) -> bool:
        """Clone (first run) or fetch+reset (subsequent) the catalog. Returns
        True if the working copy was updated, False if the remote was unreachable
        but a usable cache is being served instead. Raises
        RegistryUnavailableError only when the remote is unreachable AND no cache
        exists yet."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self._is_cloned():
                self._git(
                    ["git", "fetch", "--depth", "1", "origin", self.branch],
                    cwd=self.checkout,
                )
                self._git(
                    ["git", "reset", "--hard", f"origin/{self.branch}"],
                    cwd=self.checkout,
                )
            else:
                self._git(
                    [
                        "git",
                        "clone",
                        "--depth",
                        "1",
                        "--branch",
                        self.branch,
                        self.repo_url,
                        str(self.checkout),
                    ],
                    cwd=self.cache_dir,
                )
            logger.info("marketplace catalog refreshed from %s", self.repo_url)
            return True
        except Exception as exc:  # noqa: BLE001 — offline is a normal case.
            if RegistryReader(self.checkout).is_populated():
                logger.warning(
                    "marketplace refresh failed (%s); serving stale cache", exc
                )
                return False
            raise RegistryUnavailableError(
                f"could not fetch the marketplace catalog and no cache exists: {exc}"
            ) from exc


def build_cache() -> RegistryCache:
    """Construct a RegistryCache from settings (used by the API + scheduler)."""
    from app.config import get_settings

    s = get_settings()
    return RegistryCache(
        s.marketplace_cache_dir,
        s.marketplace_repo_url,
        branch=s.marketplace_branch,
    )


def _run_git(argv: list[str], *, cwd: Path) -> None:
    """Run a git command, raising on non-zero exit. Kept small so RegistryCache
    stays testable with an injected runner."""
    subprocess.run(  # noqa: S603 — fixed argv, no shell.
        argv, cwd=str(cwd), check=True, capture_output=True, timeout=120
    )


# --------------------------------------------------------------------------- #
# Install planning: transitive resolution + cycle/conflict detection (AC3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanStep:
    """One app to fetch+install, in dependency order (dependencies first)."""

    app: str
    version: str
    branch: str
    commit: str
    repo: str
    channel: str
    # Why it is in the plan: "requested" or "dependency of <app>".
    reason: str


def resolve_plan(
    reader: RegistryReader, app_name: str, frappe_version: str
) -> list[PlanStep]:
    """Resolve the full, ordered install plan for ``app_name`` against a target
    bench's ``frappe_version``.

    Transitive: an app's ``dependencies`` are resolved recursively. Dependencies
    come before their dependents (post-order), so installing in list order always
    fetches a dependency first.

    Fails loudly and **never partially**:
    - ``UnknownAppError`` — the app or a dependency is absent from the catalog.
    - ``IncompatibleError`` — no release satisfies the target Frappe version.
    - ``VersionConflictError`` — two dependents' specifiers cannot both be met.
    - ``DependencyCycleError`` — a cycle in the dependency graph.
    """
    chosen: dict[str, Release] = {}
    constraints: dict[str, SpecifierSet] = {}
    reasons: dict[str, str] = {}
    order: list[str] = []
    stack: list[str] = []

    def _pick(app: str) -> Release:
        releases = reader.releases(app)
        if not releases:
            raise UnknownAppError(
                f"App {app!r} is not in the marketplace catalog."
            )
        compat = compatible_releases(releases, frappe_version)
        if not compat:
            avail = ", ".join(sorted({r.frappe_core for r in releases})) or "none"
            raise IncompatibleError(
                f"No release of {app!r} is compatible with Frappe "
                f"{frappe_version} (available Frappe ranges: {avail})."
            )
        spec = constraints.get(app)
        release = best_release(releases, frappe_version, extra_specifier=spec)
        if release is None:
            raise VersionConflictError(
                f"No release of {app!r} satisfies both Frappe {frappe_version} "
                f"and the required version {str(spec)!r} demanded by another app."
            )
        return release

    def visit(app: str, incoming: SpecifierSet | None, why: str) -> None:
        if incoming is not None:
            combined = incoming if app not in constraints else constraints[app] & incoming
            constraints[app] = combined
        if app in stack:
            cycle = " -> ".join([*stack, app])
            raise DependencyCycleError(f"Dependency cycle detected: {cycle}.")

        release = _pick(app)
        if app in chosen:
            # Already placed. If the tightened constraint still holds for the
            # picked release we are done; otherwise a conflict surfaced.
            if chosen[app].version == release.version:
                return
        chosen[app] = release
        reasons.setdefault(app, why)

        stack.append(app)
        for dep_app, dep_spec in release.dependencies.items():
            try:
                dep_specset = SpecifierSet(dep_spec)
            except InvalidSpecifier:
                dep_specset = SpecifierSet()
            visit(dep_app, dep_specset, f"dependency of {app}")
        stack.pop()

        if app in order:
            order.remove(app)
        order.append(app)

    visit(app_name, None, "requested")

    return [
        PlanStep(
            app=a,
            version=chosen[a].version,
            branch=chosen[a].branch,
            commit=chosen[a].commit,
            repo=(reader.get_app(a).repo if reader.get_app(a) else ""),
            channel=chosen[a].channel,
            reason=reasons.get(a, "dependency"),
        )
        for a in order
    ]
