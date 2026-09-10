"""Request/response schemas for the tool installer (session 6.1)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ToolOut(BaseModel):
    """One row of the per-server stack checklist.

    Merges the code registry (display name, group, whether an installer exists)
    with the persisted observation, so the UI can render a complete checklist
    even for tools that have never been scanned on this server.
    """

    model_config = ConfigDict(from_attributes=True)

    tool_id: str
    display_name: str
    group: str
    detected_version: str | None = None
    recommended_version: str | None = None
    # ok | outdated | missing | unknown — see app.core.tools.TOOL_STATUSES.
    status: str = "unknown"
    last_checked_at: datetime | None = None
    # False for tools we deliberately never install (Python, MariaDB): the UI
    # renders guidance instead of an action button.
    installable: bool = False
    # True if the install needs a sudoers-allowlisted root command.
    needs_root: bool = False
    # Required by the bench CLI on every Frappe version (gotcha #2).
    critical: bool = False
    note: str = ""


class ToolGroupOut(BaseModel):
    """A named group plus its "n of m current" header counts."""

    group: str
    tools: list[ToolOut]
    # Tools in this group whose status is `ok`.
    ok_count: int
    total_count: int


class ServerToolsOut(BaseModel):
    """The whole checklist for one server."""

    server_id: int
    # The Frappe major the recommendations were resolved against; None when the
    # server has no bench yet (version-dependent tools then read `unknown`).
    frappe_major: str | None = None
    # None until the first scan — drives the UI's EmptyState.
    last_scanned_at: datetime | None = None
    groups: list[ToolGroupOut]
