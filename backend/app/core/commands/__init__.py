"""Command template registry (golden rule 1): safe, parameterized remote
commands. Import the registry for side effects so the starter templates load."""

from app.core.commands.actions import Action
from app.core.commands.registry import (
    all_templates,
    get_template,
    register,
)
from app.core.commands.templates import (
    MASK,
    CommandTemplate,
    ParamSpec,
    RenderedCommand,
    RenderError,
    UnknownAction,
    render,
)

__all__ = [
    "MASK",
    "Action",
    "CommandTemplate",
    "ParamSpec",
    "RenderError",
    "RenderedCommand",
    "UnknownAction",
    "all_templates",
    "get_template",
    "register",
    "render",
]
