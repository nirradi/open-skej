"""Run one synchronous shape conversation turn and retain every model exchange.

Unlike rule generation this is one model call plus at most one schema-correction call, so it is
part of the HTTP request rather than a background job.  The shared pieces are deliberately only
the ``LLMClient`` seam, ``RecordingClient``, and sha256-keyed ``prompt_versions``: a calendar shape
is data, not executable rule source, and importing the generation loop here would add retries and
sandbox work this request must not perform.
"""

from __future__ import annotations

import hashlib
import json

from generation.llm import LLMClient, RecordedExchange, RecordingClient
from generation.errors import LLMCallError
from shape import DAY_NAMES, Shape, generate_shape
from shape.stub import StubShapeLLMClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.identity.models import (
    PromptAgent,
    PromptVersion,
    SpaceShapeConversation,
    SpaceShapeExchange,
    SpaceShapeMessage,
    ShapeExchangeStatus,
)
from app.rule_generation import build_generation_client
from app.settings import Settings


def build_shape_client(settings: Settings) -> LLMClient:
    """Use the configured transport with the short timeout a synchronous turn requires."""
    if settings.rule_generation_client == "stub":
        return StubShapeLLMClient()
    client = build_generation_client(settings)
    # All network/subprocess clients expose this public constructor setting. Keeping timeout
    # enforcement in those transports means their actual socket/subprocess call is cancelled,
    # rather than merely timing out a wrapper while an expensive model call keeps running behind it.
    if hasattr(client, "timeout_seconds"):
        client.timeout_seconds = settings.shape_conversation_timeout_seconds
    return client


def run_shape_turn(
    session: Session,
    conversation: SpaceShapeConversation,
    *,
    current_document: dict,
    client: LLMClient,
    model: str | None = None,
) -> tuple[Shape, str, str | None]:
    """Call the shape agent and persist each resulting exchange before returning its draft.

    The user message has already been committed by the router's service call.  The recorder hook
    commits every completion as it arrives, including the malformed first completion which prompts
    the agent's one validation retry.  ``LLMCallError`` deliberately propagates: retrying an
    unavailable transport cannot repair it and would hold an admin's request open longer.
    """
    messages = list(
        session.execute(
            select(SpaceShapeMessage)
            .where(SpaceShapeMessage.conversation_id == conversation.id)
            .order_by(SpaceShapeMessage.ordinal)
        )
        .scalars()
        .all()
    )
    transcript = _conversation_text(current_document, messages)
    pending_exchange_ids: list[int] = []

    def begin(system: str, prompt: str, requested_model: str) -> None:
        pending_exchange_ids.append(
            _begin_exchange(session, conversation, system, prompt, requested_model)
        )

    def complete(exchange: RecordedExchange) -> None:
        _complete_exchange(session, pending_exchange_ids.pop(0), exchange)

    def failed(_system: str, _prompt: str, _model: str, error: LLMCallError) -> None:
        _fail_exchange(session, pending_exchange_ids.pop(0), error)

    recorder = RecordingClient(
        wrapped=client,
        before_request=begin,
        on_exchange=complete,
        on_error=failed,
        strict_hooks=True,
    )
    result = generate_shape(transcript, client=recorder, model=model)
    return result.document, result.summary, result.question


def shape_to_document(shape: Shape) -> dict:
    """Serialize the validated dataclass back to the one JSON form storage accepts."""
    return {
        "version": shape.version,
        "operating_blocks": [
            {
                "days": [DAY_NAMES[day] for day in sorted(block.days)],
                "start_time": _format_time(block.start_time),
                "end_time": _format_time(block.end_time),
                "allowed_durations_mins": list(block.allowed_durations_mins),
                **(
                    {"effective_from": block.effective_from.isoformat()}
                    if block.effective_from is not None
                    else {}
                ),
                **(
                    {"effective_to": block.effective_to.isoformat()}
                    if block.effective_to is not None
                    else {}
                ),
            }
            for block in shape.operating_blocks
        ],
        "blackout_windows": [
            {
                "start_time": _format_time(blackout.start_time),
                "end_time": _format_time(blackout.end_time),
                "reason": blackout.reason,
                **(
                    {"days": [DAY_NAMES[day] for day in sorted(blackout.days)]}
                    if blackout.days is not None
                    else {}
                ),
                **({"date": blackout.date.isoformat()} if blackout.date is not None else {}),
                **(
                    {"effective_from": blackout.effective_from.isoformat()}
                    if blackout.effective_from is not None
                    else {}
                ),
                **(
                    {"effective_to": blackout.effective_to.isoformat()}
                    if blackout.effective_to is not None
                    else {}
                ),
            }
            for blackout in shape.blackout_windows
        ],
    }


def _conversation_text(current_document: dict, messages: list[SpaceShapeMessage]) -> str:
    """Give the model the latest request first, while retaining the full prior conversation.

    Latest-first is intentional for the deterministic stub and is natural for an edit command:
    “open at 10” followed by “open at 11” must give the new request precedence.  The current
    complete document and ordered earlier transcript still make this a refinement, never a fresh
    generation.
    """
    latest = messages[-1]
    earlier = messages[:-1]
    history = "\n".join(f"{message.role.value}: {message.content}" for message in earlier)
    return "\n\n".join(
        part
        for part in (
            f"Latest admin request:\n{latest.content}",
            "Current complete shape document:\n" + json.dumps(current_document, sort_keys=True),
            f"Earlier transcript:\n{history}" if history else None,
        )
        if part is not None
    )


def _format_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _begin_exchange(
    session: Session,
    conversation: SpaceShapeConversation,
    system: str,
    prompt: str,
    model: str,
) -> int:
    """Write the prompt before dispatch; strict recording prevents an unprovable model call."""
    version = _prompt_version(session, system)
    row = SpaceShapeExchange(
        conversation_id=conversation.id,
        prompt_version_id=version.id,
        user_prompt=prompt,
        model=model,
        status=ShapeExchangeStatus.PENDING,
    )
    session.add(row)
    session.commit()
    return row.id


def _complete_exchange(session: Session, exchange_id: int, exchange: RecordedExchange) -> None:
    """Finish the pending row after the model answered, rather than adding a second record."""
    row = session.get(SpaceShapeExchange, exchange_id)
    if row is None:
        raise RuntimeError(f"Pending shape exchange {exchange_id} disappeared")
    response = exchange.response
    row.status = ShapeExchangeStatus.COMPLETED
    row.response_text = response.text
    row.model = exchange.model
    row.input_tokens = response.input_tokens
    row.output_tokens = response.output_tokens
    row.duration_ms = response.duration_ms
    row.error = None
    session.commit()


def _fail_exchange(session: Session, exchange_id: int, error: LLMCallError) -> None:
    """Keep a transport-failed dispatch as evidence of the prompt that never completed."""
    row = session.get(SpaceShapeExchange, exchange_id)
    if row is None:
        raise RuntimeError(f"Pending shape exchange {exchange_id} disappeared")
    row.status = ShapeExchangeStatus.FAILED
    row.error = str(error)
    session.commit()


def _prompt_version(session: Session, system: str) -> PromptVersion:
    """Get or create the shared system-prompt row, including the multi-worker race."""
    digest = hashlib.sha256(system.encode("utf-8")).hexdigest()
    existing = session.execute(
        select(PromptVersion).where(PromptVersion.sha256 == digest)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    version = PromptVersion(sha256=digest, agent=PromptAgent.SHAPE, prompt_text=system)
    try:
        with session.begin_nested():
            session.add(version)
        return version
    except IntegrityError:
        return session.execute(
            select(PromptVersion).where(PromptVersion.sha256 == digest)
        ).scalar_one()
