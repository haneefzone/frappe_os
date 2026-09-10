"""Tool registry + version-matrix recommendation resolver (session 6.1).

The **registry lives in code, not the DB** — a tool is a piece of the product's
domain knowledge (how to detect it, how to install it, which command template
runs the install), not user data. The DB only stores the *observed* state per
server (`ServerTool`).

Three things live here:

1. `ToolDefinition` — one entry per tool the platform knows: how to detect it
   (fixed argv + a version regex) and which parameterized command template
   installs it (golden rule 1 — there is no generic "run this installer"
   template and no shell string built from a tool id).
2. The **version matrix** from CLAUDE.md, expressed as `Requirement` objects.
3. The **resolver**: given the highest Frappe version present on a server's
   benches (plus any Settings -> Defaults override), what version *should* each
   tool be, and is the detected one `ok` / `outdated` / `missing` / `unknown`.

Gotcha #2 is encoded structurally: `uv` is `critical=True`, so it is flagged on
*every* server that has any bench at all, not just v16 ones — the current bench
CLI runs `uv venv env --seed` during `bench init` regardless of Frappe version.

Gotcha #6 is encoded as a `min_version` of `0.12.6.1` for wkhtmltopdf: a distro
`0.12.6` sorts strictly below it and is therefore reported `outdated`, which is
exactly the patched-Qt-vs-distro distinction the product must make.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.version_matrix import MATRIX, MatrixEntry

# --------------------------------------------------------------------------- #
# Status + group vocabularies
# --------------------------------------------------------------------------- #

# Per-tool state on one server:
#   ok       — detected and it satisfies the resolved requirement.
#   outdated — detected but outside the requirement (below the floor OR above
#              the ceiling; both are "does not match what this Frappe version
#              wants", and the recommended_version chip carries the detail).
#   missing  — the detect command found no such binary.
#   unknown  — we cannot judge it: the server has no bench (so no Frappe version
#              to resolve against) or the version string did not parse.
TOOL_STATUSES = ("ok", "outdated", "missing", "unknown")

GROUP_CORE = "core stack"
GROUP_DEV = "dev tools"
GROUP_AI = "AI CLIs"
TOOL_GROUPS = (GROUP_CORE, GROUP_DEV, GROUP_AI)


# --------------------------------------------------------------------------- #
# Version parsing
# --------------------------------------------------------------------------- #

# Up to four dotted numeric components. Four matters: wkhtmltopdf's patched-Qt
# build is 0.12.6.1 and must sort above the distro 0.12.6 (gotcha #6).
_VERSION_RE = re.compile(r"(\d+(?:\.\d+){0,3})")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """Extract the first dotted numeric version from a `--version` line.

    Deliberately permissive about surrounding text, because every tool words its
    banner differently ("git version 2.43.0", "v24.1.0", "jq-1.7", "Redis server
    v=7.0.15 sha=...", "mariadb from 11.8.2-MariaDB, client 15.2 for ..."), and
    strict about the shape of the number itself.
    """
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


# --------------------------------------------------------------------------- #
# Requirements
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Requirement:
    """What version a tool should be, and the chip text the UI shows for it.

    `max_version` is compared against the *prefix* of the detected version at
    the same precision, so a ceiling of `(18,)` accepts every 18.x and rejects
    20.x, and a ceiling of `(3, 12)` accepts every 3.12.x and rejects 3.13.
    """

    label: str
    min_version: tuple[int, ...] | None = None
    max_version: tuple[int, ...] | None = None

    def satisfied_by(self, detected: tuple[int, ...]) -> bool:
        if self.min_version is not None and detected < self.min_version:
            return False
        if self.max_version is not None:
            depth = len(self.max_version)
            if detected[:depth] > self.max_version:
                return False
        return True


# Any present version is acceptable — the tool is a binary dependency with no
# Frappe-version constraint (git, nginx, htop, the CLIs...).
ANY = Requirement(label="any")

def _requirements_for(entry: MatrixEntry) -> dict[str, Requirement]:
    """Turn one matrix row into the requirement slots the resolver evaluates.

    MariaDB gets an *open* ceiling: the matrix's `mariadb_min` is what makes a
    server non-compliant, while `mariadb_recommended` is advice. Encoding the
    recommendation as a floor would report a perfectly supported 10.11 box as
    `outdated`, so it goes in the label instead.
    """
    mariadb_floor = f"{entry.mariadb_min}+"
    return {
        "python": Requirement(
            entry.python_display,
            min_version=parse_version(entry.python_versions[0]),
            max_version=parse_version(entry.python_versions[-1]),
        ),
        "node": Requirement(
            entry.node_display,
            min_version=(entry.node_min_major,),
            max_version=(entry.node_max_major,),
        ),
        "mariadb": Requirement(
            mariadb_floor
            if entry.mariadb_recommended is None
            else f"{mariadb_floor} ({entry.mariadb_recommended} recommended)",
            min_version=parse_version(entry.mariadb_min),
        ),
    }


# The CLAUDE.md version matrix as *requirements*, keyed by Frappe major. Derived
# from `app.core.version_matrix.MATRIX` rather than restated, so the tool
# resolver, the bench pre-flight gate and the Create-Bench wizard can never
# disagree about what v16 needs.
VERSION_MATRIX: dict[str, dict[str, Requirement]] = {
    entry.major: _requirements_for(entry) for entry in MATRIX
}

# Slots that only exist inside VERSION_MATRIX rows — they cannot be judged at
# all until we know which Frappe major a server is running.
VERSION_DEPENDENT_SLOTS = frozenset({"python", "node", "mariadb"})

# Rules that do not vary with the Frappe version.
VERSION_INDEPENDENT: dict[str, Requirement] = {
    # Gotcha #6: only the patched-Qt build carries the 4th component.
    "wkhtmltopdf": Requirement("0.12.6.1 (patched Qt)", min_version=(0, 12, 6, 1)),
    # Frappe has required a Redis 6-era feature set since v13; no upper bound.
    "redis": Requirement("6.0+", min_version=(6, 0)),
}


# --------------------------------------------------------------------------- #
# Tool definitions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolDefinition:
    """One tool the platform can detect and (usually) install.

    `install_action` is the *action_name of a dedicated command template* — one
    template per tool. It is never assembled from `tool_id`; the API looks the
    definition up by id and reads this field, so an unknown id 404s long before
    anything reaches a command.
    """

    tool_id: str
    display_name: str
    group: str
    # Fixed argv for detection. stdout and stderr are both searched, because
    # `nginx -v` writes its banner to stderr.
    detect_argv: tuple[str, ...]
    # Which VERSION_MATRIX / VERSION_INDEPENDENT rule applies. None -> ANY.
    requirement: str | None = None
    # The command template that installs/upgrades it. None = detect-only (we
    # will not install a database server or an init system for you).
    install_action: str | None = None
    # True if the install template needs a line in the sudoers allowlist.
    needs_root: bool = False
    # Flagged on every server that has any bench, regardless of Frappe version.
    critical: bool = False
    note: str = ""


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    # --- core stack -------------------------------------------------------- #
    ToolDefinition(
        tool_id="git",
        display_name="Git",
        group=GROUP_CORE,
        detect_argv=("git", "--version"),
        install_action="tool.install_git",
        needs_root=True,
    ),
    ToolDefinition(
        tool_id="python3",
        display_name="Python",
        group=GROUP_CORE,
        detect_argv=("python3", "--version"),
        requirement="python",
        # Swapping the system interpreter is an OS-level decision (deadsnakes,
        # pyenv, a distro upgrade) with no safe one-command form, so we report
        # and let a human choose.
        install_action=None,
        note="Change the system interpreter manually — no safe one-command upgrade.",
    ),
    ToolDefinition(
        tool_id="uv",
        display_name="uv",
        group=GROUP_CORE,
        detect_argv=("uv", "--version"),
        install_action="tool.install_uv",
        needs_root=False,
        # Gotcha #2: the current bench CLI runs `uv venv env --seed` during
        # `bench init` even for v14/v15 benches.
        critical=True,
        note="Required by the bench CLI for every Frappe version (v14 included).",
    ),
    ToolDefinition(
        tool_id="node",
        display_name="Node.js",
        group=GROUP_CORE,
        detect_argv=("node", "--version"),
        requirement="node",
        install_action="tool.install_node",
        needs_root=False,
        note="Installed per-user via nvm; never replaces a system Node.",
    ),
    ToolDefinition(
        tool_id="mariadb",
        display_name="MariaDB",
        group=GROUP_CORE,
        detect_argv=("mariadb", "--version"),
        requirement="mariadb",
        # A database-server major upgrade is a data-migration event, never a
        # one-click button. Detect-only by design.
        install_action=None,
        note="Upgrade MariaDB manually — a major upgrade is a data migration.",
    ),
    ToolDefinition(
        tool_id="redis-server",
        display_name="Redis",
        group=GROUP_CORE,
        detect_argv=("redis-server", "--version"),
        requirement="redis",
        install_action="tool.install_redis",
        needs_root=True,
    ),
    ToolDefinition(
        tool_id="wkhtmltopdf",
        display_name="wkhtmltopdf",
        group=GROUP_CORE,
        detect_argv=("wkhtmltopdf", "--version"),
        requirement="wkhtmltopdf",
        install_action="tool.install_wkhtmltopdf",
        needs_root=True,
        note="Must be the patched-Qt 0.12.6.1 build — the distro package is not.",
    ),
    ToolDefinition(
        tool_id="bench",
        display_name="Bench CLI",
        group=GROUP_CORE,
        detect_argv=("bench", "--version"),
        install_action="tool.install_bench",
        needs_root=False,
    ),
    ToolDefinition(
        tool_id="nginx",
        display_name="nginx",
        group=GROUP_CORE,
        detect_argv=("nginx", "-v"),
        install_action="tool.install_nginx",
        needs_root=True,
    ),
    ToolDefinition(
        tool_id="supervisor",
        display_name="Supervisor",
        group=GROUP_CORE,
        detect_argv=("supervisord", "--version"),
        install_action="tool.install_supervisor",
        needs_root=True,
    ),
    # --- dev tools --------------------------------------------------------- #
    ToolDefinition(
        tool_id="code-server",
        display_name="code-server",
        group=GROUP_DEV,
        detect_argv=("code-server", "--version"),
        install_action="tool.install_code_server",
        needs_root=False,
    ),
    ToolDefinition(
        tool_id="gh",
        display_name="GitHub CLI",
        group=GROUP_DEV,
        detect_argv=("gh", "--version"),
        install_action="tool.install_gh",
        needs_root=False,
    ),
    ToolDefinition(
        tool_id="htop",
        display_name="htop",
        group=GROUP_DEV,
        detect_argv=("htop", "--version"),
        install_action="tool.install_htop",
        needs_root=True,
    ),
    ToolDefinition(
        tool_id="jq",
        display_name="jq",
        group=GROUP_DEV,
        detect_argv=("jq", "--version"),
        install_action="tool.install_jq",
        needs_root=True,
    ),
    # --- AI CLIs ----------------------------------------------------------- #
    ToolDefinition(
        tool_id="claude-code",
        display_name="Claude Code",
        group=GROUP_AI,
        detect_argv=("claude", "--version"),
        install_action="tool.install_claude_code",
        needs_root=False,
    ),
)

TOOLS_BY_ID: dict[str, ToolDefinition] = {t.tool_id: t for t in TOOL_DEFINITIONS}


def get_tool(tool_id: str) -> ToolDefinition:
    """Look a definition up by id. Raises KeyError -> the API maps it to 404."""
    return TOOLS_BY_ID[tool_id]


# --------------------------------------------------------------------------- #
# Frappe version resolution
# --------------------------------------------------------------------------- #

_MAJOR_RE = re.compile(r"v?(\d+)")


def frappe_major(version: str | None) -> str | None:
    """Reduce a bench's `frappe_version` ("16.24.0", "v15", "15.x") to its major."""
    if not version:
        return None
    match = _MAJOR_RE.match(version.strip())
    return match.group(1) if match else None


def highest_frappe_major(versions: list[str | None]) -> str | None:
    """The highest Frappe major across a server's benches.

    Highest, not lowest: the toolchain is shared, so the newest bench on the box
    sets the bar — a v16 bench needs Node 24 even if a v14 bench sits beside it.
    """
    majors = [frappe_major(v) for v in versions]
    numeric = [int(m) for m in majors if m is not None]
    return str(max(numeric)) if numeric else None


def resolve_requirements(
    major: str | None,
    overrides: dict[str, dict[str, str]] | None = None,
) -> dict[str, Requirement]:
    """The requirement for every slot, given a server's Frappe major version.

    `overrides` is the Settings -> Defaults version-matrix override map (B4.17),
    shaped `{"16": {"node": "24", "python": "3.14"}}`. An override replaces the
    shipped rule with an open-ended floor at the given version — it is an admin
    escape hatch for a fleet that has deliberately moved ahead of the matrix, so
    it must not also impose a ceiling.
    """
    resolved: dict[str, Requirement] = dict(VERSION_INDEPENDENT)
    if major is not None:
        resolved.update(VERSION_MATRIX.get(major, {}))

    for slot, raw in ((overrides or {}).get(major or "", {})).items():
        parsed = parse_version(raw)
        if parsed is not None:
            resolved[slot] = Requirement(label=f"{raw}+", min_version=parsed)
    return resolved


@dataclass(frozen=True)
class ToolAssessment:
    """The resolved verdict for one tool on one server."""

    tool: ToolDefinition
    detected_version: str | None
    recommended_version: str
    status: str


def assess(
    tool: ToolDefinition,
    detected_version: str | None,
    requirements: dict[str, Requirement],
    *,
    has_bench: bool,
    major_known: bool,
) -> ToolAssessment:
    """Judge one detected version against the resolved matrix.

    A tool whose rule depends on the Frappe version is `unknown` — not `ok` —
    when the server has no bench to resolve against: claiming Node 20 is correct
    without knowing which Frappe will run on it would be a false green.

    `uv` is the exception carved out for gotcha #2: it is required by the bench
    CLI itself, so on any server that has a bench its absence is `missing`
    regardless of whether the matrix could be resolved.
    """
    requirement = requirements.get(tool.requirement or "", ANY)
    # Can this tool even be judged? A slot that only exists in VERSION_MATRIX
    # needs a Frappe major to resolve against.
    unresolvable = tool.requirement in VERSION_DEPENDENT_SLOTS and not major_known
    label = "unknown" if unresolvable else requirement.label

    if detected_version is None:
        # `uv` is the gotcha-#2 carve-out: the bench CLI needs it whatever the
        # Frappe version, so on a server that has a bench its absence is a hard
        # `missing`, never softened to `unknown`.
        if unresolvable and not (tool.critical and has_bench):
            return ToolAssessment(tool, None, label, "unknown")
        return ToolAssessment(tool, None, requirement.label, "missing")

    parsed = parse_version(detected_version)
    if parsed is None:
        return ToolAssessment(tool, detected_version, label, "unknown")
    if unresolvable:
        # Installed, but there is no bench to say which matrix row applies —
        # calling it `ok` would be a false green.
        return ToolAssessment(tool, detected_version, label, "unknown")

    status = "ok" if requirement.satisfied_by(parsed) else "outdated"
    return ToolAssessment(tool, detected_version, requirement.label, status)
