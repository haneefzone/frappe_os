"""Parameterized command templates + the central `render()` (golden rule 1).

A template is a *fixed argv list of constants* with `{param}` placeholders. User
input never becomes part of a shell string: each param is validated against a
whitelist (regex or enum), substituted into its own argv element, and
`shlex.quote`-joined only to build a human-readable display string. There is no
code path that interpolates raw input into a command.

`render()` returns:
- `argv`   — the real argv to execute (secrets in the clear, in memory only),
- `display`— the same command with every secret masked as `••••`,
- `params_sanitized` — the validated params with secrets masked, safe to persist.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# CLAUDE.md rule 6: secrets render as this, never as their value.
MASK = "••••"


class RenderError(ValueError):
    """A parameter failed validation, or is missing/unknown. Maps to HTTP 422."""


class SecretParamUnresolved(RenderError):
    """A template has a `secret=True` param but render() was asked to build from
    *sanitized* params (worker re-render or manual retry), where every secret is
    masked as `••••`. Running with that mask would be a silent security bug, so
    we refuse loudly until secret resolution from the Fernet credential store is
    wired at render time. Subclasses RenderError -> maps to HTTP 422."""


class UnknownAction(KeyError):
    """No template is registered for the requested action_name. Maps to 404."""


@dataclass(frozen=True)
class ParamSpec:
    """Validation rule for one template parameter.

    Exactly one of `regex`/`enum` constrains the value; `secret` marks it for
    masking in displays, logs and the persisted params.
    """

    name: str
    regex: str | None = None
    enum: tuple[str, ...] | None = None
    secret: bool = False
    required: bool = True

    def validate(self, value: str) -> str:
        if self.enum is not None and value not in self.enum:
            raise RenderError(
                f"parameter {self.name!r} must be one of {list(self.enum)}"
            )
        if self.regex is not None and re.fullmatch(self.regex, value) is None:
            # Deliberately do NOT echo the offending value — it may be a secret,
            # and error messages must never leak shell-injection attempts back.
            raise RenderError(f"parameter {self.name!r} contains disallowed characters")
        return value


@dataclass(frozen=True)
class CommandTemplate:
    """A single remote action the platform knows how to run safely."""

    action_name: str
    argv: tuple[str, ...]
    cwd: str | None
    params: tuple[ParamSpec, ...]
    # The Action subclass that orchestrates this template's steps. Typed as
    # object to avoid importing actions here (they depend on this module).
    action_class: type
    idempotent: bool
    requires_lock: bool
    # RBAC action-class required to launch this (checked in POST /api/jobs).
    required_permission: str
    # OS user to run the command as (via `sudo -u`); None = the SSH login user.
    run_as: str | None = None

    @property
    def secret_params(self) -> set[str]:
        return {spec.name for spec in self.params if spec.secret}


@dataclass(frozen=True)
class RenderedCommand:
    argv: list[str] = field(default_factory=list)
    cwd: str | None = None
    display: str = ""
    params_sanitized: dict[str, str] = field(default_factory=dict)
    # Plaintext secret values, for the log redactor — never persisted.
    secret_values: tuple[str, ...] = ()


def _substitute(token: str, values: dict[str, str]) -> str:
    """Fill `{name}` placeholders in a *template constant* with validated values.

    The format string is the developer-authored token, never user input, so a
    brace inside a value is inserted literally and cannot re-trigger formatting.
    """
    return token.format(**values)


def render(
    template: CommandTemplate,
    params: dict[str, object],
    *,
    from_sanitized: bool = False,
) -> RenderedCommand:
    """Validate every parameter and build the final argv + masked display.

    Raises RenderError on a missing/unknown/invalid parameter — nothing is
    executed unless every value passed its whitelist.

    `from_sanitized=True` marks that `params` came from a persisted
    `params_sanitized` map (worker re-render / manual retry), where secrets are
    masked. If the template declares any secret param, that mask cannot be
    executed — raise `SecretParamUnresolved` instead of running with `••••`.
    Callers on the create path pass real values (from_sanitized=False) and are
    unaffected. This guard is the tripwire for a secret-bearing template landing
    before the Fernet secret-resolution-at-render-time work exists.
    """
    if from_sanitized and template.secret_params:
        raise SecretParamUnresolved(
            f"action {template.action_name!r} declares secret parameter(s) "
            f"{sorted(template.secret_params)} that cannot be re-rendered from "
            "masked params; secret resolution from the credential store is not "
            "yet wired"
        )

    known = {spec.name for spec in template.params}
    unknown = set(params) - known
    if unknown:
        raise RenderError(f"unknown parameter(s): {sorted(unknown)}")

    real: dict[str, str] = {}
    masked: dict[str, str] = {}
    for spec in template.params:
        raw = params.get(spec.name)
        if raw is None or raw == "":
            if spec.required:
                raise RenderError(f"missing required parameter {spec.name!r}")
            continue
        value = spec.validate(str(raw))
        real[spec.name] = value
        masked[spec.name] = MASK if spec.secret else value

    argv = [_substitute(tok, real) for tok in template.argv]
    display_argv = [_substitute(tok, masked) for tok in template.argv]
    cwd = _substitute(template.cwd, real) if template.cwd else None

    display = shlex.join(display_argv)
    if cwd is not None:
        display = f"cd {shlex.quote(cwd)} && {display}"

    return RenderedCommand(
        argv=argv,
        cwd=cwd,
        display=display,
        params_sanitized=masked,
        secret_values=tuple(real[name] for name in template.secret_params if name in real),
    )
