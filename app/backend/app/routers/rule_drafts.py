"""The API over rule generation: submit a prompt, poll a job.

**This router is registered conditionally** — ``app.main`` includes it only when
``RULE_GENERATION_ENABLED`` is set, exactly as it does for ``app.routers.sandbox``. The guard is
the registration and not a check inside a handler, so a caller against a normally-configured
backend gets a genuine 404: whether the capability is even present should not be discoverable by
whether a request to it succeeds.

**The job is Space-scoped; the rule type it produces is global.** Every route here goes through
``require_space_role`` at admin+, because spending a Space's model calls and writing to the shared
catalog is an administrative act. What comes out the other end is *not* scoped to that Space — a
generated type joins the catalog every Space can pick from, exactly like the seven hand-written
ones (``ops/plans/stream-7/OVERVIEW.md``, "Generated rules are global, with provenance recorded").
The two statements are deliberately different, and reading the ``space_id`` on a job as scoping
its artifact would be wrong; ``created_by_space_id`` on the type is what a later migration would
use to scope generated types down.

Note also what a successful job is *not*: it makes a type **available**, not enforced. Nothing
judges a booking until an admin adds an instance of it through the existing rules API, and that
act — not the generation — is the human gate.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_session, get_session_factory
from app.identity.authz import SpaceContext, require_space_role
from app.identity.models import (
    GeneratedRuleType,
    MembershipRole,
    RuleGenerationJob,
    RuleGenerationJobStatus,
)
from app.identity.schemas import RuleDraftCreate, RuleDraftRead
from app.rule_generation import run_generation_job

router = APIRouter(prefix="/spaces", tags=["rule-drafts"])

SessionDep = Annotated[Session, Depends(get_session)]
AdminContext = Annotated[SpaceContext, Depends(require_space_role(MembershipRole.ADMIN))]

IN_FLIGHT_DETAIL = (
    "A rule is already being generated for this Space. Wait for it to finish before starting"
    " another."
)
JOB_NOT_FOUND_DETAIL = "No such rule draft in this Space."

#: How many jobs the list route returns. A page reload should not lose a job an admin is waiting
#: on, which is all this endpoint is for — it is not a history browser.
RECENT_JOB_LIMIT = 20


@router.post(
    "/{public_id}/rule-drafts",
    response_model=RuleDraftRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_rule_draft(
    payload: RuleDraftCreate,
    context: AdminContext,
    session: SessionDep,
    background: BackgroundTasks,
) -> RuleDraftRead:
    """Ask for a rule type to be written from one sentence of English. Admin+.

    **202, not 201.** Generation is generate → adversarial suite → sandbox run with up to three
    retries: minutes of wall clock and up to eight model calls. Nothing is created yet when this
    returns — a job has been accepted, and the caller polls ``GET`` on the id it gets back.

    The job belongs to this Space; the rule type it may eventually produce does not (see the
    module docstring). Authorisation to generate is a Space authorisation, and the provenance
    columns on the resulting type are what a later migration would use to scope it down.

    409 when this Space already has a job queued or running. The check below is the friendly
    half; the partial unique index over in-flight rows is the real one, because two concurrent
    POSTs can both pass a check and only one can pass the index.
    """
    if _in_flight_job(session, context.space.id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=IN_FLIGHT_DETAIL)

    job = RuleGenerationJob(
        space_id=context.space.id,
        user_id=context.user.id,
        prompt=payload.prompt,
        status=RuleGenerationJobStatus.QUEUED,
        attempts=[],
    )
    session.add(job)
    try:
        session.commit()
    except IntegrityError:
        # The index caught what the check above raced past. Same 409, same message: from the
        # caller's side these are one situation, and which of the two noticed is our problem.
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=IN_FLIGHT_DETAIL)
    session.refresh(job)

    # `BackgroundTasks` rather than a thread: it runs after the response is sent, has no
    # lifecycle to manage and no daemon left hanging at shutdown, and under `TestClient` it runs
    # deterministically before the test's `post` returns — which is what makes the whole flow
    # assertable without sleeping on a poll loop.
    #
    # The session *factory*, never the request's session: that one is closed by the time this
    # runs, and a multi-minute job holding a request-scoped connection would be worse if it were
    # not.
    background.add_task(run_generation_job, job.id, session_factory=get_session_factory())
    return RuleDraftRead.build(job)


@router.get("/{public_id}/rule-drafts", response_model=list[RuleDraftRead])
def list_rule_drafts(context: AdminContext, session: SessionDep) -> list[RuleDraftRead]:
    """This Space's recent generation jobs, newest first. Admin+.

    Exists so a page reload does not lose a job an admin is waiting on: without it, the job id
    lives only in the tab that submitted it.
    """
    jobs = (
        session.execute(
            select(RuleGenerationJob)
            .where(RuleGenerationJob.space_id == context.space.id)
            .order_by(RuleGenerationJob.id.desc())
            .limit(RECENT_JOB_LIMIT)
        )
        .scalars()
        .all()
    )
    return [RuleDraftRead.build(job, rule_type=_rule_type_for(session, job)) for job in jobs]


@router.get("/{public_id}/rule-drafts/{draft_id}", response_model=RuleDraftRead)
def read_rule_draft(draft_id: int, context: AdminContext, session: SessionDep) -> RuleDraftRead:
    """One job: its status, its attempt history, and the rule type it produced. Admin+.

    A job id belonging to another Space gets the **identical 404** as one that names nothing at
    all, resolved in a single query scoped to ``space_id`` rather than a lookup followed by an
    ownership check. Two branches would be two chances to return two different answers, and the
    difference between "no such job" and "not your job" is itself information about another
    tenant.
    """
    job = session.execute(
        select(RuleGenerationJob).where(
            RuleGenerationJob.id == draft_id,
            RuleGenerationJob.space_id == context.space.id,
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=JOB_NOT_FOUND_DETAIL)
    return RuleDraftRead.build(job, rule_type=_rule_type_for(session, job))


def _in_flight_job(session: Session, space_id: int) -> RuleGenerationJob | None:
    """This Space's queued-or-running job, if it has one."""
    return session.execute(
        select(RuleGenerationJob).where(
            RuleGenerationJob.space_id == space_id,
            RuleGenerationJob.status.in_(
                (RuleGenerationJobStatus.QUEUED, RuleGenerationJobStatus.RUNNING)
            ),
        )
    ).scalar_one_or_none()


def _rule_type_for(session: Session, job: RuleGenerationJob) -> str | None:
    """The ``rule_type`` string this job produced, or ``None`` if it produced nothing.

    The string, not the integer id: it is what ``space_rules.rule_type`` stores, so it is the
    value a page needs to offer "add an instance of this" as the next step.
    """
    if job.generated_rule_type_id is None:
        return None
    return session.execute(
        select(GeneratedRuleType.rule_type).where(
            GeneratedRuleType.id == job.generated_rule_type_id
        )
    ).scalar_one_or_none()
