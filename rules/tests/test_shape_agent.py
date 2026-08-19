"""Tests for the shape agent and its deterministic LLM seam.

No test calls a model. The agent tests use a recording fake so retry behaviour is asserted at the
actual LLMClient boundary; the stub tests use its real canned envelope to prove the future chat flow
can visibly change a projection without network access.
"""

from __future__ import annotations

import json

import pytest

from generation.errors import LLMCallError
from generation.llm import LLMResponse
from shape import (
    SYSTEM_PROMPT,
    InvalidShapeError,
    ShapeAgentResponseError,
    StubShapeLLMClient,
    generate_shape,
    parse_shape_response,
)


def _document(**overrides):
    document = {
        "version": 1,
        "operating_blocks": [
            {
                "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                "start_time": "09:00",
                "end_time": "17:00",
                "allowed_durations_mins": [60],
            }
        ],
        "blackout_windows": [],
    }
    document.update(overrides)
    return document


def _completion(document=None, *, summary="Open 09:00–17:00 every day.", question=None):
    return json.dumps(
        {"document": document or _document(), "summary": summary, "question": question}
    )


class FakeClient:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[dict[str, str | None]] = []

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        self.calls.append({"system": system, "prompt": prompt, "model": model})
        return LLMResponse(text=self.texts.pop(0), model=model or "fake")


class ExplodingClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        self.calls += 1
        raise LLMCallError("model unavailable", exit_code=503)


def test_valid_completion_returns_a_typed_validated_document():
    client = FakeClient(_completion())

    result = generate_shape("Open weekdays", client=client)

    assert result.document.operating_blocks[0].start_time == 9 * 60
    assert result.summary == "Open 09:00–17:00 every day."
    assert result.question is None
    assert client.calls[0]["system"] == SYSTEM_PROMPT


def test_invalid_candidate_retries_once_with_verbatim_error_and_completion():
    invalid = _completion(_document(operating_blocks=[{"days": ["MON"]}]))
    client = FakeClient(invalid, _completion())

    generate_shape("Open Mondays", client=client)

    assert len(client.calls) == 2
    retry = client.calls[1]["prompt"]
    assert "operating_blocks[0].start_time: is required" in retry
    assert invalid in retry


def test_second_invalid_candidate_raises_instead_of_retrying_again():
    invalid = _completion(_document(operating_blocks=[{"days": ["MON"]}]))
    client = FakeClient(invalid, invalid)

    with pytest.raises(InvalidShapeError, match="operating_blocks\\[0\\].start_time"):
        generate_shape("Open Mondays", client=client)

    assert len(client.calls) == 2


def test_llm_call_error_propagates_without_retry():
    client = ExplodingClient()

    with pytest.raises(LLMCallError, match="model unavailable"):
        generate_shape("Open Mondays", client=client)

    assert client.calls == 1


def test_fenced_markdown_response_is_unwrapped_before_json_parsing():
    result = generate_shape(
        "Open weekdays",
        client=FakeClient(f"Here is the shape:\n\n```json\n{_completion()}\n```\n"),
    )

    assert result.document.operating_blocks[0].end_time == 17 * 60


def test_unbookable_valid_shape_requires_and_returns_a_do_not_publish_question():
    closed = _document(operating_blocks=[])
    result = generate_shape(
        "Close until further notice",
        client=FakeClient(
            _completion(
                closed,
                summary="The venue is closed until further notice.",
                question="When should bookings be available?",
            )
        ),
    )

    assert result.document.operating_blocks == ()
    assert result.question == "When should bookings be available?"


def test_unbookable_shape_without_question_is_a_retryable_response_failure():
    closed = _completion(_document(operating_blocks=[]))
    client = FakeClient(closed, _completion())

    generate_shape("Close until further notice", client=client)

    assert len(client.calls) == 2
    assert "question must be set when the document is unbookable" in client.calls[1]["prompt"]


def test_a_block_shorter_than_its_smallest_duration_is_unbookable():
    short = _completion(
        _document(
            operating_blocks=[
                {
                    "days": ["MON"],
                    "start_time": "09:00",
                    "end_time": "09:30",
                    "allowed_durations_mins": [60],
                }
            ]
        ),
        summary="A 30-minute Monday window offers 60-minute bookings.",
        question="Should the window or booking length change?",
    )

    assert generate_shape("Open briefly Monday", client=FakeClient(short)).question is not None


def test_blackout_closing_the_only_viable_date_is_unbookable():
    closed = _completion(
        _document(
            operating_blocks=[
                {
                    "days": ["MON"],
                    "start_time": "09:00",
                    "end_time": "10:00",
                    "allowed_durations_mins": [60],
                    "effective_from": "2026-08-17",
                    "effective_to": "2026-08-17",
                }
            ],
            blackout_windows=[
                {
                    "date": "2026-08-17",
                    "start_time": "09:00",
                    "end_time": "10:00",
                    "reason": "Closed",
                }
            ],
        ),
        summary="The only Monday opening is closed.",
        question="Which hours should remain available?",
    )

    assert generate_shape("Closed Monday", client=FakeClient(closed)).question is not None


def test_strict_envelope_rejects_an_extra_field():
    payload = json.loads(_completion())
    payload["extra"] = True

    with pytest.raises(ShapeAgentResponseError, match="unexpected keys"):
        parse_shape_response(json.dumps(payload))


def test_summary_must_be_member_neutral():
    with pytest.raises(ShapeAgentResponseError, match="member-neutral"):
        parse_shape_response(_completion(summary="You can book from 09:00 to 17:00."))


def test_question_is_rejected_for_a_bookable_shape():
    with pytest.raises(ShapeAgentResponseError, match="question must be null"):
        parse_shape_response(_completion(question="Should it stay open?"))


def test_prompt_contains_v1_constraints_and_all_three_worked_examples():
    assert "`end_time` may exceed `24:00`" in SYSTEM_PROMPT
    assert "inclusive at both\nends" in SYSTEM_PROMPT
    assert "never both." in SYSTEM_PROMPT
    assert "strict ascending order with no duplicates" in SYSTEM_PROMPT
    assert "teacher and parents" in SYSTEM_PROMPT
    assert "lab equipment runs" in SYSTEM_PROMPT
    assert "music room is bookable" in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.count('"version": 1') >= 4
    assert SYSTEM_PROMPT.count('"days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]') >= 4


def test_stub_is_deterministic_and_recognisably_time_responsive():
    client = StubShapeLLMClient()

    early = generate_shape("Please open at 4", client=client)
    ranged = generate_shape("Open from 8 to 5pm", client=client)

    assert early.document.operating_blocks[0].start_time == 4 * 60
    assert early.document.operating_blocks[0].end_time == 12 * 60
    assert ranged.document.operating_blocks[0].start_time == 8 * 60
    assert ranged.document.operating_blocks[0].end_time == 17 * 60


def test_stub_rejects_an_unrecognised_agent_prompt():
    with pytest.raises(LLMCallError):
        StubShapeLLMClient().complete(system="not the shape agent", prompt="open at 9")
