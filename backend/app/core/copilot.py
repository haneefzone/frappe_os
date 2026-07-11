"""Panel copilot (session 5.2 — Phase 5 AI): analyze failed jobs + NL palette.

Two pieces, both bound by the golden rules:

1. **Analyze a failed job.** `analyze_job()` assembles a *sanitized* payload —
   the already-redacted log tail (`LogEntry.content`, masked at write time,
   rule 6) + the action/template name + per-step statuses + the failed step's
   traceback — and hands it to the shared 5.0 `AnthropicClient` (deep model,
   default ``claude-opus-4-8``) with a JSON-schema structured-output contract, so
   the reply is a `{root_cause, suggested_fix, summary}` object. Every outbound
   prompt is passed through `app.core.ai.build_prompt_payload` with the job's own
   secret plaintexts, so even an un-redacted traceback line is scrubbed before it
   can leave the box. The call runs in an RQ worker (`run_job_analysis`), never in
   the request (rule 3).

2. **NL → template resolver.** `resolve_nl_command()` maps a natural-language
   request to an **existing registered job template + validated params ONLY**
   (never raw shell, rule 1). It refuses anything that looks like a shell command
   or that doesn't map to a known intent, and returns a proposal the palette
   confirms before running via the action's own already-RBAC'd endpoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.core.ai import AIError, AnthropicClient
from app.core.commands import get_template
from app.core.permissions import role_allows
from app.core.secrets_resolve import decrypt_job_secrets
from app.core.security import get_secrets_service
from app.models import Bench, CommandJob, JobAnalysis, LogEntry, Server, Site

# --------------------------------------------------------------------------- #
# 1. Analyze a failed job                                                     #
# --------------------------------------------------------------------------- #

# How many trailing log lines to include. Bounded so a huge job log can't blow
# the prompt (and the cost) up — the tail is where a failure surfaces.
LOG_TAIL_LINES = 200
MAX_ANALYSIS_TOKENS = 1500

# RQ job timeout for one analysis (kept in sync with the enqueue in the API).
ANALYSIS_JOB_TIMEOUT = 600
# A 'running' analysis that has outlived the job timeout + grace is presumed dead
# (a hard worker crash the failure_callback couldn't catch) and reaped so the
# panel stops polling it. Generous over the timeout to never race a live call.
ANALYSIS_STALE_AFTER = timedelta(seconds=ANALYSIS_JOB_TIMEOUT + 300)

# The structured-output contract (output_config.format). The deep model must
# return exactly these fields, so the panel can render them without parsing.
ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "root_cause": {
            "type": "string",
            "description": "The single most likely root cause of the failure.",
        },
        "suggested_fix": {
            "type": "string",
            "description": "A concrete, actionable fix or next diagnostic step.",
        },
        "summary": {
            "type": "string",
            "description": "One-sentence summary of the failure.",
        },
    },
    "required": ["root_cause", "suggested_fix", "summary"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are a senior Frappe/ERPNext bench operations engineer helping diagnose a "
    "failed remote command run by a self-hosted control panel. You are given the "
    "action name, the per-step statuses, the failed step's traceback, and the tail "
    "of the job log (secrets are already masked as ••••). Identify the single most "
    "likely root cause and a concrete suggested fix. Be specific to Frappe/bench "
    "(uv, MariaDB, redis queue :11000/cache :13000, wkhtmltopdf, site_config, "
    "migrations). Never invent log content you were not given. Respond only via the "
    "required structured fields."
)


def _job_secret_values(db: Session, job: CommandJob) -> tuple[str, ...]:
    """The plaintext of every user-supplied secret carried on the job, so the
    redaction gate can scrub it from the outbound prompt.

    Server-sourced secrets (e.g. the MariaDB root password) are re-read at run
    time and never copied onto the job, and the log writer already masks them at
    write time, so the job's own bundle is the set that could ride a traceback.
    Best-effort: a decrypt failure must never block the analysis.
    """
    if not job.secrets_enc:
        return ()
    try:
        bundle = decrypt_job_secrets(get_secrets_service(), job.secrets_enc)
    except Exception:  # noqa: BLE001 — never let a secret-decrypt fault block analysis.
        return ()
    return tuple(v for v in bundle.values() if v)


def build_analysis_prompt(db: Session, job: CommandJob) -> tuple[list[dict], str]:
    """Assemble the (messages, system) for a failed job's analysis.

    Everything here is drawn from persisted, write-time-redacted data; the caller
    still routes it through `build_prompt_payload` with the job's secrets for a
    second, value-based scrub before the SDK sees it.
    """
    steps = sorted(job.steps, key=lambda s: (s.attempt, s.order))
    step_lines = [
        f"  {s.attempt}.{s.order} [{s.status}] {s.name}" for s in steps
    ]
    failed = next((s for s in reversed(steps) if s.status == "failure"), None)

    tail = (
        db.execute(
            select(LogEntry)
            .where(LogEntry.job_id == job.id)
            .order_by(LogEntry.seq.desc())
            .limit(LOG_TAIL_LINES)
        )
        .scalars()
        .all()
    )
    tail = list(reversed(tail))
    log_text = "\n".join(f"[{e.stream}] {e.content}" for e in tail) or "(no log output)"

    parts = [
        f"Action: {job.action_name}",
        f"Target: {job.target_type} {job.target_id or '-'}",
        f"Job status: {job.status}  exit_code: {job.exit_code}",
        "",
        "Steps:",
        *(step_lines or ["  (no steps recorded)"]),
    ]
    if failed and failed.error_traceback:
        parts += ["", f"Failed step: {failed.name}", "Traceback:", failed.error_traceback]
    parts += ["", "Log tail:", log_text]

    messages = [{"role": "user", "content": "\n".join(parts)}]
    return messages, _SYSTEM_PROMPT


def analyze_job(db: Session, analysis: JobAnalysis, ai: AnthropicClient) -> JobAnalysis:
    """Run one analysis to completion, folding the result onto `analysis`.

    Pure and testable: no RQ/Redis. The RQ entrypoint (`run_job_analysis`) wires
    the DB session + a real `AnthropicClient`; tests pass a mock client. Never
    raises — a failure is recorded on the row so the panel can show it.
    """
    analysis.status = "running"
    db.commit()

    job = db.get(CommandJob, analysis.job_id)
    if job is None:  # pragma: no cover — FK-guaranteed while the analysis exists.
        analysis.status = "failure"
        analysis.error = "Job no longer exists."
        analysis.completed_at = datetime.now(UTC)
        db.commit()
        return analysis

    messages, system = build_analysis_prompt(db, job)
    secrets = _job_secret_values(db, job)

    try:
        result = ai.complete(
            messages=messages,
            system=system,
            tier="deep",
            schema=ANALYSIS_SCHEMA,
            secrets=secrets,
            max_tokens=MAX_ANALYSIS_TOKENS,
        )
    except AIError as exc:
        # AIError messages are secret-free by construction (see app.core.ai).
        analysis.status = "failure"
        analysis.error = str(exc)
        analysis.completed_at = datetime.now(UTC)
        db.commit()
        return analysis
    except Exception:  # noqa: BLE001 — never surface an SDK traceback (rule 6).
        analysis.status = "failure"
        analysis.error = "Analysis failed."
        analysis.completed_at = datetime.now(UTC)
        db.commit()
        return analysis

    parsed = result.parsed if isinstance(result.parsed, dict) else {}
    analysis.model = result.model
    analysis.root_cause = parsed.get("root_cause") or (result.text or None)
    analysis.suggested_fix = parsed.get("suggested_fix")
    analysis.summary = (parsed.get("summary") or "")[:400] or None
    analysis.status = "success"
    analysis.completed_at = datetime.now(UTC)
    db.commit()

    # Best-effort per-call audit, mirroring the 5.0 /test endpoint. The user's
    # request itself was audited at the API edge (job.analyze.request).
    from app.models.ai_settings import AISettings  # noqa: PLC0415 — avoid import cycle.

    row = AISettings.get_or_create(db)
    if row.audit_calls:
        record_audit(
            db,
            action="job.analyze.completed",
            summary=f"AI analyzed failed job #{job.id} with {result.model}",
            user_id=analysis.requested_by,
            entity_type="job_analysis",
            entity_id=analysis.id,
            params={
                "job_id": job.id,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )
    return analysis


def run_job_analysis(analysis_id: int) -> None:
    """RQ job function: build a real session + client and analyze. Kept tiny so
    ``queue.enqueue('app.core.copilot.run_job_analysis')`` imports cleanly."""
    from app.db import SessionLocal  # noqa: PLC0415
    from app.models.ai_settings import AISettings  # noqa: PLC0415

    db = SessionLocal()
    try:
        analysis = db.get(JobAnalysis, analysis_id)
        if analysis is None:
            return
        row = AISettings.get_or_create(db)
        client = AnthropicClient(row, get_secrets_service(), db=db)
        analyze_job(db, analysis, client)
    finally:
        db.close()


def mark_analysis_failed(job, connection, exc_type, exc_value, tb) -> None:  # noqa: ANN001, ARG001
    """RQ failure_callback: a `job_timeout` mid-call or a raised exception leaves
    the row stuck `running` (``analyze_job`` commits it before the AI call), so
    the panel would poll it forever. Flip it to `failure`.

    Best-effort and **secret-free**: no exception detail is persisted — a raised
    SDK/timeout message could carry log content (rule 6). RQ passes the failing
    ``job``; its first arg is the analysis id.
    """
    from app.db import SessionLocal  # noqa: PLC0415

    analysis_id = job.args[0] if getattr(job, "args", None) else None
    if analysis_id is None:  # pragma: no cover — the enqueue always passes the id.
        return
    db = SessionLocal()
    try:
        analysis = db.get(JobAnalysis, analysis_id)
        if analysis is None or analysis.status in ("success", "failure"):
            return
        analysis.status = "failure"
        analysis.error = "Analysis timed out or the worker stopped before finishing."
        analysis.completed_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


def reap_if_stale(db: Session, analysis: JobAnalysis) -> JobAnalysis:
    """Backstop for a hard worker crash (SIGKILL/OOM) that the failure_callback
    can't catch: if a `running` analysis has outlived the job timeout + grace,
    mark it `failure` so the panel stops polling a dead row. Called lazily from
    the poll endpoint — no separate reaper process to run."""
    if analysis.status != "running":
        return analysis
    started = analysis.created_at
    if started is None:  # pragma: no cover — created_at has a server default.
        return analysis
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if datetime.now(UTC) - started > ANALYSIS_STALE_AFTER:
        analysis.status = "failure"
        analysis.error = "Analysis did not complete (the worker stopped)."
        analysis.completed_at = datetime.now(UTC)
        db.commit()
    return analysis


# --------------------------------------------------------------------------- #
# 2. Natural-language → registered-template resolver                          #
# --------------------------------------------------------------------------- #

# Shell metacharacters / command shapes that must never be treated as an action.
# A hit means "this is a raw command, not a natural-language request" -> refuse.
_SHELL_METACHARS = re.compile(r"[;&|`$><\\]|\$\(|\|\||&&|\n")
_COMMAND_WORDS = re.compile(
    r"\b(bench|sudo|ssh|scp|rm|mv|cp|cat|curl|wget|bash|sh|zsh|python|pip|npm|"
    r"psql|mysql|mariadb|redis-cli|drop\s+database|delete\s+from|chmod|chown|"
    r"kill|reboot|shutdown|dd|mkfs|systemctl|supervisorctl|export)\b",
    re.IGNORECASE,
)

# English filler stripped before matching a site name.
_STOPWORDS = {
    "all", "the", "a", "an", "of", "for", "on", "to", "my", "our", "please",
    "site", "sites", "site's", "run", "do", "now", "and", "then",
}


@dataclass(frozen=True)
class Intent:
    """One recognised natural-language action → an existing registered template.

    `run_path` is a python format string over the site id: the action's own
    already-RBAC'd endpoint. The palette POSTs there after the confirm step, so
    the request rides the same server-side permission + audit gate as any other
    launch. `template` names the registered command template (asserts the map is
    to a known template, never raw shell)."""

    key: str
    template: str
    keywords: tuple[str, ...]
    title_verb: str
    run_path: str
    body: dict = field(default_factory=dict)


# Ordered: earlier, more-specific intents win (website-cache before cache).
_INTENTS: tuple[Intent, ...] = (
    Intent(
        key="clear_website_cache",
        template="site.clear_website_cache",
        keywords=("clear website cache", "website cache", "clear web cache"),
        title_verb="Clear website cache on",
        run_path="/api/sites/{id}/clear-website-cache",
    ),
    Intent(
        key="clear_cache",
        template="site.clear_cache",
        keywords=("clear cache", "clear-cache", "flush cache", "clear the cache"),
        title_verb="Clear cache on",
        run_path="/api/sites/{id}/clear-cache",
    ),
    Intent(
        key="migrate",
        template="site.migrate",
        keywords=("migrate", "run migrate", "migration"),
        title_verb="Migrate",
        run_path="/api/sites/{id}/migrate",
    ),
    Intent(
        key="backup",
        template="site.backup",
        keywords=("backup", "back up", "back-up", "snapshot"),
        title_verb="Back up",
        run_path="/api/sites/{id}/backups",
        body={"with_files": False},
    ),
)


@dataclass
class NLProposal:
    """A single confirm-before-run action mapped to an existing template."""

    title: str
    action_name: str
    summary: str
    site_id: int
    site_name: str
    params: dict
    allowed: bool
    run: dict
    confirm: bool = True


@dataclass
class NLResolution:
    resolved: bool
    intent: str | None = None
    reason: str | None = None
    proposals: list[NLProposal] = field(default_factory=list)


def _looks_like_shell(text: str) -> bool:
    return bool(_SHELL_METACHARS.search(text) or _COMMAND_WORDS.search(text))


def _match_intent(text: str) -> Intent | None:
    low = text.lower()
    for intent in _INTENTS:
        if any(kw in low for kw in intent.keywords):
            return intent
    return None


def _wants_files(text: str) -> bool:
    return bool(re.search(r"\bwith files\b|\+files\b|and files\b", text, re.IGNORECASE))


def _match_sites(db: Session, text: str, intent_keywords: tuple[str, ...]) -> list[Site]:
    """Sites whose name is referenced by the request.

    The verb keywords and English filler are stripped, then each remaining token
    (>=3 chars) is matched as a case-insensitive substring of the site name. An
    explicit "all ... sites" phrasing (no specific token) is left to the caller
    to reject rather than silently fanning out to every site.
    """
    low = text.lower()
    for kw in intent_keywords:
        low = low.replace(kw, " ")
    tokens = [
        t for t in re.split(r"[\s,]+", low)
        if t and t not in _STOPWORDS and len(t) >= 3 and not t.isdigit()
    ]
    if not tokens:
        return []

    active = (
        db.execute(select(Site).where(Site.status == "active")).scalars().all()
    )
    matched: list[Site] = []
    for site in active:
        name = site.name.lower()
        if any(tok in name for tok in tokens):
            matched.append(site)
    return matched


def _proposal_for(db: Session, site: Site, intent: Intent, perms: list[str],
                  with_files: bool) -> NLProposal | None:
    bench = db.get(Bench, site.bench_id)
    if bench is None:  # pragma: no cover — FK-guaranteed.
        return None
    server = db.get(Server, bench.server_id)
    if server is None:  # pragma: no cover
        return None

    template = get_template(intent.template)  # asserts the template exists.
    allowed = role_allows(perms, template.required_permission)

    body = dict(intent.body)
    if intent.key == "backup":
        body["with_files"] = with_files

    # The validated params, for display + audit. site + bench_path are the two
    # the template needs; both come from trusted rows, not the free-text request.
    params = {"site": site.name, "bench_path": bench.path}
    if intent.key == "backup":
        params["with_files"] = "true" if with_files else "false"

    return NLProposal(
        title=f"{intent.title_verb} — {site.name}",
        action_name=intent.template,
        summary=(
            f"{intent.template} · {template.required_permission} · "
            "requires confirmation"
        ),
        site_id=site.id,
        site_name=site.name,
        params=params,
        allowed=allowed,
        run={"method": "POST", "path": intent.run_path.format(id=site.id), "body": body},
    )


def resolve_nl_command(
    db: Session, text: str, perms: list[str], *, max_proposals: int = 10
) -> NLResolution:
    """Map a natural-language request to existing template proposals, or refuse.

    Refuses (resolved=False) when the text looks like a raw shell command, does
    not match a known intent, or matches no site. Never returns anything but a
    proposal onto a registered template + validated params (rule 1)."""
    text = (text or "").strip()
    if not text:
        return NLResolution(resolved=False, reason="Type a request, e.g. “backup erp.acme.com”.")

    if _looks_like_shell(text):
        return NLResolution(
            resolved=False,
            reason="That looks like a raw command. The palette only runs known, "
            "confirmed actions — try “backup <site>” or “migrate <site>”.",
        )

    intent = _match_intent(text)
    if intent is None:
        return NLResolution(
            resolved=False,
            reason="Couldn’t map that to a known action. Try backup, migrate, or "
            "clear cache for a site.",
        )

    sites = _match_sites(db, text, intent.keywords)
    if not sites:
        return NLResolution(
            resolved=False,
            intent=intent.key,
            reason="No matching site found. Name a site, e.g. “backup erp.acme.com”.",
        )

    with_files = _wants_files(text)
    proposals: list[NLProposal] = []
    for site in sites[:max_proposals]:
        p = _proposal_for(db, site, intent, perms, with_files)
        if p is not None:
            proposals.append(p)

    if not proposals:  # pragma: no cover — only if every matched site lost its bench.
        return NLResolution(
            resolved=False, intent=intent.key, reason="No runnable site matched."
        )
    return NLResolution(resolved=True, intent=intent.key, proposals=proposals)
