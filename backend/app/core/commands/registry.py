"""The command-template registry: the single source of every action the
platform can run. Future sessions register bench/site/backup templates here.

Starter templates (session 1.3):
- `system.echo_demo`   — multi-step demo, locked, used for the acceptance test.
- `server.detect_tools`— read-only toolchain inventory, lock-free.
"""

from __future__ import annotations

from app.core.commands.actions import DetectToolsAction, EchoDemoAction
from app.core.commands.templates import (
    CommandTemplate,
    ParamSpec,
    UnknownAction,
)
from app.core.permissions import SERVER_MANAGE

# Whitelist for free-text demo input: word chars plus a few safe punctuation
# marks. Excludes ; ` $ ( ) & | < > \n and quotes, so shell metacharacters and
# command substitution can never pass validation.
SAFE_TEXT = r"[\w .,:@/=+-]{1,200}"


_TEMPLATES: dict[str, CommandTemplate] = {}


def register(template: CommandTemplate) -> CommandTemplate:
    if template.action_name in _TEMPLATES:
        raise ValueError(f"duplicate template {template.action_name!r}")
    _TEMPLATES[template.action_name] = template
    return template


def get_template(action_name: str) -> CommandTemplate:
    try:
        return _TEMPLATES[action_name]
    except KeyError as exc:
        raise UnknownAction(action_name) from exc


def all_templates() -> list[CommandTemplate]:
    return sorted(_TEMPLATES.values(), key=lambda t: t.action_name)


register(
    CommandTemplate(
        action_name="system.echo_demo",
        argv=("echo", "{message}"),
        cwd=None,
        params=(ParamSpec("message", regex=SAFE_TEXT),),
        action_class=EchoDemoAction,
        idempotent=True,
        requires_lock=True,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)

register(
    CommandTemplate(
        action_name="server.detect_tools",
        argv=("true",),  # nominal; DetectToolsAction runs its own fixed commands.
        cwd=None,
        params=(),
        action_class=DetectToolsAction,
        idempotent=True,
        requires_lock=False,
        required_permission=SERVER_MANAGE,
        run_as=None,
    )
)
