"""Running the generation loop as a backend job, and keeping every word of it.

Generate → adversarial suite → sandbox run, up to three retries, is minutes of wall clock and up
to eight model calls. It cannot be the body of an HTTP request, so ``app.routers.rule_drafts``
writes a ``RuleGenerationJob`` row, hands it to ``run_generation_job`` through FastAPI's
``BackgroundTasks``, and the caller polls. This module is that runner.

**In-process, and deliberately so.** No Celery, no Redis, no broker. Nothing here is deployed yet,
and a message queue is an infrastructure decision with its own operational requirements, not a
detail of this feature. What the choice costs is one real failure mode — a process that dies
mid-run leaves a row stuck at ``running`` — and ``sweep_orphaned_generation_jobs`` at boot is the
release valve for it. That sweep is not tidiness: the partial unique index over in-flight rows
means a stuck job blocks its Space from ever generating again.

**Only ``AttemptOutcome.PASSED`` writes a rule type.** A timeout is not a success and a crash is
not a success — ``generation.loop`` says so at the top of its own docstring and this is the second
place it has to be true, because here the consequence is a row in a catalog that judges bookings
rather than a file a developer reads.

**The recording is the other half of the job.** ``RecordingClient`` sits at the ``LLMClient`` seam
and its ``on_exchange`` hook persists one ``RuleGenerationExchange`` per model call *as the run
progresses*, so a process that dies at attempt three still leaves the first two attempts' prompts
behind. The loop itself is untouched — it still returns strings and discards every ``LLMResponse``
(``.claude/rules/rule-engine.md``), and it does not learn that anyone is watching.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import marshal
import platform
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from generation.generator import SYSTEM_PROMPT
from generation.llm import (
    ClaudeCliClient,
    GoogleAIStudioClient,
    LLMClient,
    OllamaClient,
    RecordedExchange,
    RecordingClient,
    read_google_api_key,
)
from generation.loop import LoopResult, run_generation_loop
from generation.stub import StubLLMClient
from generation.tester import TESTER_SYSTEM_PROMPT

from app.db.session import get_session_factory
from app.identity.models import (
    GeneratedRuleType,
    GeneratedRuleTypeStatus,
    PromptAgent,
    PromptVersion,
    RuleGenerationExchange,
    RuleGenerationJob,
    RuleGenerationJobStatus,
)
from app.rule_catalog import catalog
from app.settings import Settings

logger = logging.getLogger(__name__)

#: The four legal values of ``RULE_GENERATION_CLIENT``. Named in the error an unknown value
#: raises, so a typo in a deployment's environment says what the alternatives were.
GENERATION_CLIENTS = ("stub", "google", "ollama", "claude-cli")

#: How much of an attempt's failure text is kept in ``RuleGenerationJob.attempts``. The full text
#: is already stored untruncated in the next attempt's exchange ``user_prompt``, which is where
#: anyone debugging a prompt actually looks; this copy is the admin-facing summary.
ATTEMPT_FAILURE_CHARS = 2000

#: Priority for every generated type — sorts after all seven hand-written ones, so a
#: deliberately-worded hand-written denial always wins when both would deny
#: (``ops/plans/stream-7/OVERVIEW.md``).
GENERATED_RULE_PRIORITY = 100

#: Longest generated ``rule_type`` slug, matching the column's ``String(64)``. The numeric suffix
#: a collision appends is counted inside this, never past it.
_MAX_RULE_TYPE_CHARS = 64


class GenerationClientError(Exception):
    """``RULE_GENERATION_CLIENT`` names a backend this build does not have."""


def build_generation_client(settings: Settings) -> LLMClient:
    """The model backend this deployment is configured to generate against.

    Dispatches on ``settings.rule_generation_client`` and raises at construction — not at the
    first call, minutes into a job — when the value is not one of :data:`GENERATION_CLIENTS`. A
    misconfigured deployment should find out when it tries to start a job, not when an admin is
    already polling one that will never finish.

    ``google`` takes its key from settings first and falls back to ``rules/.env`` via
    ``read_google_api_key``, which is where the benchmark's key already lives — so a developer who
    has run the benchmark does not have to copy the key into a second file to run the backend.
    """
    name = settings.rule_generation_client
    if name == "stub":
        return StubLLMClient()
    if name == "google":
        return GoogleAIStudioClient(api_key=settings.google_studio_api_key or read_google_api_key())
    if name == "ollama":
        return OllamaClient()
    if name == "claude-cli":
        return ClaudeCliClient()
    raise GenerationClientError(
        f"Unknown RULE_GENERATION_CLIENT {name!r}; expected one of "
        f"{', '.join(GENERATION_CLIENTS)}."
    )


def run_generation_job(
    job_id: int,
    *,
    session_factory: sessionmaker[Session] | None = None,
    client: LLMClient | None = None,
) -> None:
    """Run one queued job to completion, and leave the outcome in its row.

    **Synchronous, and owns its own session.** Never the request's: this runs after the response
    has been sent, by which point FastAPI has already closed the request-scoped session. It is
    also what the tests call directly, without an HTTP layer in the way.

    Every exit path leaves the job at ``succeeded`` or ``failed``. The ``except Exception`` around
    the run is deliberately broad — a job that raises out of a background task would otherwise
    leave a row ``running`` forever, and the in-flight unique index turns that into a permanent
    outage for that one tenant rather than one lost generation.
    """
    factory = session_factory or get_session_factory()
    session = factory()
    try:
        job = session.get(RuleGenerationJob, job_id)
        if job is None:
            logger.warning("Rule generation job %s vanished before it could run.", job_id)
            return
        if job.status is not RuleGenerationJobStatus.QUEUED:
            # Not an error: the startup sweep may have failed it, or a retry of the background
            # task may be arriving twice. Either way this job is not ours to run.
            logger.info(
                "Rule generation job %s is %s, not queued; leaving it alone.",
                job_id,
                job.status.value,
            )
            return

        job.status = RuleGenerationJobStatus.RUNNING
        session.commit()

        settings = _settings()
        try:
            result = _run_loop(session, job, client=client or build_generation_client(settings))
        except Exception as exc:  # noqa: BLE001 — see the docstring: nothing may escape.
            logger.exception("Rule generation job %s raised; failing it.", job_id)
            session.rollback()
            job = session.get(RuleGenerationJob, job_id)
            if job is not None:
                job.status = RuleGenerationJobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                session.commit()
            return

        job.attempts = [_attempt_summary(attempt) for attempt in result.attempts]
        if result.succeeded and result.rule_source:
            rule_row = _write_rule_type(session, job, result)
            job.generated_rule_type_id = rule_row.id
            job.status = RuleGenerationJobStatus.SUCCEEDED
            job.error = None
            session.commit()
            # Hoist it live. Without this the type exists in the database but not in this
            # process's catalog until the next restart or the next miss-triggered reload, and the
            # admin who just generated it would be told their own new type does not exist.
            catalog.reload(session)
        else:
            job.status = RuleGenerationJobStatus.FAILED
            job.error = _failure_summary(result)
            session.commit()
    finally:
        session.close()


def sweep_orphaned_generation_jobs(session: Session) -> int:
    """Fail every job left in flight by a process that is no longer running. Returns the count.

    Called from ``app.main``'s lifespan. In-process runs do not survive a restart, so a
    ``running`` row at boot describes a job nobody is executing, and a ``queued`` one describes a
    background task that will never be scheduled. Both would poll forever.

    The real damage is not the stale row, it is the partial unique index over in-flight rows: one
    orphan means that Space can never start another generation. This sweep is what stops a
    restart at the wrong moment from becoming a permanent single-tenant outage.
    """
    in_flight = (
        session.execute(
            select(RuleGenerationJob).where(
                RuleGenerationJob.status.in_(
                    (RuleGenerationJobStatus.QUEUED, RuleGenerationJobStatus.RUNNING)
                )
            )
        )
        .scalars()
        .all()
    )
    for job in in_flight:
        job.status = RuleGenerationJobStatus.FAILED
        job.error = (
            "This generation was interrupted by a backend restart and cannot be resumed."
            " Submit the prompt again."
        )
    if in_flight:
        session.commit()
        logger.info("Swept %d orphaned rule generation job(s) to failed.", len(in_flight))
    return len(in_flight)


def _settings() -> Settings:
    """Imported lazily so tests that clear the settings cache see their own environment."""
    from app.settings import get_settings

    return get_settings()


def _run_loop(session: Session, job: RuleGenerationJob, *, client: LLMClient) -> LoopResult:
    """Drive the loop with the recorder attached, persisting each exchange as it happens."""
    state = {"attempt": 0}

    def persist(exchange: RecordedExchange) -> None:
        _record_exchange(session, job, exchange, state)

    recorder = RecordingClient(wrapped=client, on_exchange=persist)
    settings = _settings()
    kwargs = {}
    if settings.rule_generation_model:
        kwargs["model"] = settings.rule_generation_model
    # `output_dir=None` is load-bearing. `rules/generated/` is a developer-review affordance for
    # someone reading a candidate at a terminal; this path's artifact is a database row, and
    # writing a file the backend never reads again would leave the container accumulating them.
    return run_generation_loop(job.prompt, client=recorder, output_dir=None, **kwargs)


def _record_exchange(
    session: Session,
    job: RuleGenerationJob,
    exchange: RecordedExchange,
    state: dict[str, int],
) -> None:
    """Store one model call, both directions.

    ``attempt`` is derived here rather than carried by the recorder: each Generator turn opens a
    new attempt, so the attempt number is the count of Generator exchanges seen so far. The
    recorder sits at the ``LLMClient`` seam and knows nothing about loops, attempts or retries —
    which is exactly the property that lets one recorder serve both the benchmark and this runner.
    Deriving it here keeps that seam thin and puts the knowledge in the layer that already has it.
    """
    agent = _agent_for(exchange.system)
    if agent is PromptAgent.GENERATOR:
        state["attempt"] += 1

    version = _prompt_version(session, exchange.system, agent)
    response = exchange.response
    session.add(
        RuleGenerationExchange(
            job_id=job.id,
            # A Tester turn belongs to the attempt its Generator turn opened. `max(1, …)` covers
            # the impossible-but-cheap case of a Tester call arriving first.
            attempt=max(1, state["attempt"]),
            agent=agent,
            prompt_version_id=version.id,
            # Verbatim and untruncated: on a retry this carries the previous failing source and
            # the validator or pytest output, which is the row the whole recording exists for.
            user_prompt=exchange.prompt,
            response_text=response.text,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            duration_ms=response.duration_ms,
        )
    )
    session.commit()


def _agent_for(system: str) -> PromptAgent:
    """Which agent sent this system prompt.

    Exact equality against the two module-level constants, the same dispatch ``StubLLMClient``
    uses. A system prompt matching neither is not something to guess at — it is recorded as the
    Generator's only because a row must name something, and the mismatch is logged so a reworded
    prompt that broke this comparison is visible rather than silently mislabelling every row.
    """
    if system == SYSTEM_PROMPT:
        return PromptAgent.GENERATOR
    if system == TESTER_SYSTEM_PROMPT:
        return PromptAgent.TESTER
    logger.warning(
        "Recording an exchange whose system prompt matches neither generation agent; "
        "labelling it 'generator'. Has a system prompt been reworded?"
    )
    return PromptAgent.GENERATOR


def _prompt_version(session: Session, system: str, agent: PromptAgent) -> PromptVersion:
    """The ``prompt_versions`` row for this system prompt, created on first sight.

    Get-or-create on the hash, with the ``IntegrityError`` handled rather than assumed away: two
    jobs running in two workers can both miss the select and both insert. The savepoint is what
    lets the re-select happen on a session whose outer transaction is still usable — without it
    the rollback would discard the exchange rows already written for this job.
    """
    digest = hashlib.sha256(system.encode("utf-8")).hexdigest()
    existing = session.execute(
        select(PromptVersion).where(PromptVersion.sha256 == digest)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    version = PromptVersion(sha256=digest, agent=agent, prompt_text=system)
    try:
        with session.begin_nested():
            session.add(version)
        return version
    except IntegrityError:
        return session.execute(
            select(PromptVersion).where(PromptVersion.sha256 == digest)
        ).scalar_one()


def _write_rule_type(
    session: Session, job: RuleGenerationJob, result: LoopResult
) -> GeneratedRuleType:
    """Store the verified candidate as a catalog row. Only ever called for a PASSED result."""
    source = result.rule_source or ""
    rule_type = _unique_rule_type(session, job.prompt)
    row = GeneratedRuleType(
        rule_type=rule_type,
        label=_label_for(job.prompt),
        description=None,  # 7.8 authors this from the model; a picker entry until then.
        prompt=job.prompt,
        human_code=source,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        executable_bytecode=marshal.dumps(compile(source, "<generated>", "exec")),
        python_version=platform.python_version(),
        bytecode_magic=importlib.util.MAGIC_NUMBER.hex(),
        param_schema=[],  # 7.8 fills this in.
        reads_history=_reads_history(source),
        priority=GENERATED_RULE_PRIORITY,
        status=GeneratedRuleTypeStatus.ACTIVE,
        created_by_space_id=job.space_id,
        created_by_user_id=job.user_id,
    )
    session.add(row)
    session.flush()
    return row


def _reads_history(source: str) -> bool:
    """Whether this rule needs the Space's booking history, judged over-inclusively on purpose.

    A substring check, not an AST walk, and the crudeness is the point: the two errors are not
    symmetric. A false *positive* costs one history query the rule then ignores. A false
    *negative* hands the rule an empty history, so a "no more than three bookings a week" rule
    counts zero existing bookings and **allows** a booking it should have refused — silently
    permissive, which is the one direction this codebase never accepts. When the cheap check is
    wrong in the safe direction, take the cheap check.
    """
    return "history" in source


def _slug(text: str) -> str:
    """A lowercase underscore slug from arbitrary user text, or ``""`` if nothing survives."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return re.sub(r"_+", "_", cleaned)


def _unique_rule_type(session: Session, prompt: str) -> str:
    """A ``rule_type`` id nothing else holds.

    ``rule_type`` is unique and **never reused**: ``space_rules.rule_type`` stores this string, so
    handing the same id to a second type would silently repoint every instance of the first one.
    A collision therefore takes a numeric suffix rather than replacing anything, and the base is
    truncated so the suffix fits inside ``String(64)`` rather than overflowing it.
    """
    base = _slug(prompt)[:48] or "generated_rule"
    candidate = base
    suffix = 2
    while _rule_type_taken(session, candidate):
        tail = f"_{suffix}"
        candidate = f"{base[: _MAX_RULE_TYPE_CHARS - len(tail)]}{tail}"
        suffix += 1
    return candidate


def _rule_type_taken(session: Session, rule_type: str) -> bool:
    """Whether any generated type already holds this id, retired ones included.

    Retired rows count. A retired type is one ``space_rules`` rows may still reference, and
    reusing its id would resurrect it under them wearing different code.
    """
    return (
        session.execute(
            select(GeneratedRuleType.id).where(GeneratedRuleType.rule_type == rule_type)
        ).first()
        is not None
    )


def _label_for(prompt: str) -> str:
    """A human label for the picker, from the prompt, until 7.8 has the model write one."""
    collapsed = " ".join(prompt.split())
    return collapsed[:197] + "…" if len(collapsed) > 200 else collapsed


def _attempt_summary(attempt: object) -> dict[str, object]:
    """One attempt as JSON: what happened, and the failure text, capped.

    Not the rule or test source — those are already stored verbatim as exchange rows, and on a
    retry quoted again inside the next turn's ``user_prompt``. See ``RuleGenerationJob``'s
    docstring for why duplicating the largest field in the schema up to three times per job buys
    nothing.
    """
    failure = getattr(attempt, "failure", None)
    return {
        "number": getattr(attempt, "number", None),
        "outcome": getattr(getattr(attempt, "outcome", None), "value", None),
        "failure": failure[:ATTEMPT_FAILURE_CHARS] if failure else None,
    }


def _failure_summary(result: LoopResult) -> str:
    """The admin-facing reason a generation did not land.

    The last attempt's outcome and failure text, because that is the one that exhausted the
    budget. The earlier attempts are in ``attempts``, and every prompt behind them is in
    ``rule_generation_exchanges``.
    """
    if not result.attempts:
        return "The generation loop produced no attempts."
    last = result.attempts[-1]
    detail = (last.failure or "").strip()
    prefix = f"Generation failed after {len(result.attempts)} attempt(s): {last.outcome.value}."
    return f"{prefix}\n\n{detail[:ATTEMPT_FAILURE_CHARS]}" if detail else prefix
