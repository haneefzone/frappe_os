"""Resolve a job's secret params to plaintext at execution time (session 1.8).

This is the Fernet secret-resolution-at-render-time the DOO-94 tripwire
(`SecretParamUnresolved`) was waiting for. The worker only persists MASKED
params, so a secret-bearing template can't be re-rendered from them alone. Each
secret param instead declares a *source* on its template (`secret_sources`), and
this module fetches the plaintext from that source — never from the stored job
params, never from the browser (rule 6):

- ``"job"``            — a value the user supplied at create time (e.g. a new
  site's admin password), carried on the job as ONE Fernet token
  (`CommandJob.secrets_enc`) encrypting a JSON map. Decrypted only here, in
  memory, only to render the command.
- ``"server:<column>"`` — a Fernet token stored on the Server row (e.g. the
  MariaDB root password). Re-read at execution time so a rotated server secret
  takes effect and the value is never copied onto the job.

A secret param whose template declares NO source is left unresolved on purpose:
`render(..., from_sanitized=True)` then still raises `SecretParamUnresolved`, so
a new secret-bearing template can never silently run with a `••••` mask.
"""

from __future__ import annotations

import json

from app.core.commands.templates import CommandTemplate
from app.core.security import SecretsService


class SecretResolutionError(RuntimeError):
    """A declared secret source is missing/unreadable. Fail loud, never run with
    a masked or empty secret. Surfaces as a job failure (or HTTP 422 on create)."""


def encrypt_job_secrets(
    secrets: SecretsService, values: dict[str, str]
) -> str | None:
    """Fernet-encrypt a user-supplied secret map to one token, or None if empty."""
    if not values:
        return None
    return secrets.encrypt(json.dumps(values))


def decrypt_job_secrets(secrets: SecretsService, token: str | None) -> dict[str, str]:
    """Inverse of `encrypt_job_secrets`. Empty/None token -> empty map."""
    if not token:
        return {}
    data = json.loads(secrets.decrypt(token))
    if not isinstance(data, dict):
        raise SecretResolutionError("job secret bundle is not a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def _resolve_one(
    name: str,
    source: str,
    *,
    job_bundle: dict[str, str],
    server: object | None,
    secrets: SecretsService,
) -> str:
    if source == "job":
        if name not in job_bundle:
            raise SecretResolutionError(f"job is missing the {name!r} secret")
        return job_bundle[name]
    if source.startswith("server:"):
        column = source.split(":", 1)[1]
        token = getattr(server, column, None) if server is not None else None
        if not token:
            label = getattr(server, "name", "?")
            raise SecretResolutionError(
                f"server {label!r} has no {column} configured — set it on the "
                "server's settings before running this action"
            )
        return secrets.decrypt(token)
    raise SecretResolutionError(f"unknown secret source {source!r} for {name!r}")


def resolve_secrets(
    template: CommandTemplate,
    *,
    secrets_enc: str | None,
    server: object | None,
    secrets: SecretsService,
) -> dict[str, str]:
    """Return {param_name: plaintext} for every secret param the template
    declares a source for. Params without a declared source are omitted (the
    render tripwire handles them). Raises SecretResolutionError if a declared
    source is missing."""
    sources = getattr(template, "secret_sources", None) or {}
    if not sources:
        return {}
    optional = {
        spec.name for spec in template.params if spec.secret and not spec.required
    }
    job_bundle: dict[str, str] | None = None
    resolved: dict[str, str] = {}
    for name in template.secret_params:
        source = sources.get(name)
        if source is None:
            continue
        is_optional = name in optional
        if source == "job":
            if job_bundle is None:
                job_bundle = decrypt_job_secrets(secrets, secrets_enc)
            # An OPTIONAL job secret that the operator didn't supply (e.g. no
            # deploy key for a public repo, or no admin password on a same-site
            # restore) is simply left unresolved — the action treats its absence
            # as "not applicable". A REQUIRED one still fails loud below.
            if name not in job_bundle and is_optional:
                continue
        try:
            resolved[name] = _resolve_one(
                name,
                source,
                job_bundle=job_bundle or {},
                server=server,
                secrets=secrets,
            )
        except SecretResolutionError:
            # An OPTIONAL secret whose source can't be resolved (e.g. a
            # server-sourced db root password on a same-site restore that never
            # needs it) is skipped; the action enforces presence when the path
            # actually requires it. A REQUIRED one propagates.
            if is_optional:
                continue
            raise
    return resolved
