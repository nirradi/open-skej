"""Tests for ``GoogleAIStudioClient``, the ``LLMClient`` implementation over AI Studio's REST API.

**No test here makes a network call.** The transport is exercised through the two pure functions
it is built out of — ``build_generate_content_request`` (what would be sent) and
``interpret_google_result`` (what was returned) — and the one socket-touching function,
``_send_generate_content_request``, is monkeypatched wherever a full ``complete()`` round trip is
asserted. Every response body below is shaped exactly as measured against the live API on
2026-08-05 (see ``ops/plans/stream-7/7.1-google-ai-studio-client.md``), not guessed from the docs.
"""

import json
import urllib.error

import pytest

from generation import llm as llm_module
from generation.errors import GenerationError, LLMCallError
from generation.llm import (
    GoogleAIStudioClient,
    LLMClient,
    LLMResponse,
    build_generate_content_request,
    interpret_google_result,
    read_google_api_key,
)

#: A plain, non-thinking success response.
SUCCESS_BODY = json.dumps(
    {
        "candidates": [
            {
                "content": {"parts": [{"text": "class R(BaseRule):\n    pass\n"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 25,
            "candidatesTokenCount": 40,
            "totalTokenCount": 65,
        },
    }
)

#: A thinking-model success response: a thought part ahead of the real answer, and
#: thoughtsTokenCount dwarfing candidatesTokenCount — the exact shape measured live.
THINKING_SUCCESS_BODY = json.dumps(
    {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True, "thoughtSignature": "abc"},
                        {"text": "class R(BaseRule):\n    pass\n"},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 25,
            "candidatesTokenCount": 6,
            "totalTokenCount": 682,
            "thoughtsTokenCount": 651,
        },
    }
)

#: finishReason: MAX_TOKENS comes back with content as an empty object — no `parts` at all.
MAX_TOKENS_BODY = json.dumps(
    {
        "candidates": [{"content": {}, "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {
            "promptTokenCount": 25,
            "candidatesTokenCount": 0,
            "totalTokenCount": 1025,
        },
    }
)

SAFETY_NO_TEXT_BODY = json.dumps({"candidates": [{"content": {}, "finishReason": "SAFETY"}]})

BLOCKED_BODY = json.dumps({"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})

NO_CANDIDATES_BODY = json.dumps({"candidates": []})

EMPTY_TEXT_BODY = json.dumps(
    {"candidates": [{"content": {"parts": [{"text": ""}]}, "finishReason": "STOP"}]}
)

#: Captured shape of an invalid key: HTTP 400, not 401, with API_KEY_INVALID in error.details.
INVALID_KEY_400_BODY = json.dumps(
    {
        "error": {
            "code": 400,
            "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "API_KEY_INVALID",
                    "domain": "googleapis.com",
                }
            ],
        }
    }
)

#: A 400 that is not a key problem — must not be blamed on the key.
GENERIC_400_BODY = json.dumps(
    {
        "error": {
            "code": 400,
            "message": "Request contains an invalid argument.",
            "status": "INVALID_ARGUMENT",
        }
    }
)

MODEL_404_BODY = json.dumps(
    {
        "error": {
            "code": 404,
            "message": (
                "models/bogus-model is not found for API version v1beta, or is not supported "
                "for generateContent. Call ModelService.ListModels to see the list of available "
                "models and their supported methods."
            ),
            "status": "NOT_FOUND",
        }
    }
)

RATE_LIMIT_429_BODY = json.dumps(
    {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted (e.g. check quota).",
            "status": "RESOURCE_EXHAUSTED",
        }
    }
)


# --------------------------------------------------------------------------------------------
# build_generate_content_request — the pure request shape
# --------------------------------------------------------------------------------------------


def test_url_is_the_generate_content_endpoint():
    url, _ = build_generate_content_request(
        "https://generativelanguage.googleapis.com",
        system="be terse",
        prompt="write a rule",
        model="gemini-3.5-flash",
    )
    assert url == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    )


def test_system_prompt_goes_in_system_instruction_not_the_user_turn():
    _, body = build_generate_content_request(
        "https://generativelanguage.googleapis.com", system="be terse", prompt="hi", model="m"
    )
    assert body["systemInstruction"] == {"parts": [{"text": "be terse"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_generation_config_is_omitted_when_no_temperature_given():
    _, body = build_generate_content_request(
        "https://generativelanguage.googleapis.com", system="s", prompt="p", model="m"
    )
    assert "generationConfig" not in body


def test_generation_config_carries_the_temperature_when_given():
    _, body = build_generate_content_request(
        "https://generativelanguage.googleapis.com",
        system="s",
        prompt="p",
        model="m",
        temperature=0.7,
    )
    assert body["generationConfig"] == {"temperature": 0.7}


def test_seed_is_never_sent_anywhere_in_the_request_body():
    _, body = build_generate_content_request(
        "https://generativelanguage.googleapis.com",
        system="s",
        prompt="p",
        model="m",
        temperature=0.7,
    )
    assert "seed" not in body
    assert "seed" not in body["generationConfig"]


def test_base_url_is_joined_verbatim_stripping_is_the_clients_job():
    url, _ = build_generate_content_request(
        "https://generativelanguage.googleapis.com/", system="s", prompt="p", model="m"
    )
    # build_generate_content_request itself does not strip a trailing slash — that is
    # GoogleAIStudioClient's job on the base_url it is constructed with.
    assert url == "https://generativelanguage.googleapis.com//v1beta/models/m:generateContent"


# --------------------------------------------------------------------------------------------
# interpret_google_result — success and metadata mapping
# --------------------------------------------------------------------------------------------


def test_success_body_yields_text_and_metadata():
    response = interpret_google_result(
        status=200, body=SUCCESS_BODY, model="gemini-3.5-flash", base_url="https://x"
    )
    assert response.text == "class R(BaseRule):\n    pass\n"
    assert response.model == "gemini-3.5-flash"
    assert response.input_tokens == 25
    assert response.output_tokens == 40


def test_thought_parts_are_skipped_when_joining_text():
    response = interpret_google_result(
        status=200, body=THINKING_SUCCESS_BODY, model="m", base_url="https://x"
    )
    assert response.text == "class R(BaseRule):\n    pass\n"


def test_output_tokens_sums_candidates_and_thoughts_when_thinking():
    # Measured live: 6 candidatesTokenCount against 651 thoughtsTokenCount. The naive
    # candidatesTokenCount -> output_tokens mapping would understate what the prompt actually
    # cost by two orders of magnitude, which is exactly what this sum exists to avoid.
    response = interpret_google_result(
        status=200, body=THINKING_SUCCESS_BODY, model="m", base_url="https://x"
    )
    assert response.output_tokens == 6 + 651


def test_output_tokens_is_just_candidates_when_there_is_no_thinking():
    response = interpret_google_result(
        status=200, body=SUCCESS_BODY, model="m", base_url="https://x"
    )
    assert response.output_tokens == 40


def test_cost_usd_is_none_not_zero():
    response = interpret_google_result(
        status=200, body=SUCCESS_BODY, model="m", base_url="https://x"
    )
    assert response.cost_usd is None


def test_duration_ms_is_none_the_api_does_not_report_it():
    response = interpret_google_result(
        status=200, body=SUCCESS_BODY, model="m", base_url="https://x"
    )
    assert response.duration_ms is None


def test_raw_payload_is_kept():
    response = interpret_google_result(
        status=200, body=SUCCESS_BODY, model="m", base_url="https://x"
    )
    assert response.raw["usageMetadata"]["candidatesTokenCount"] == 40


# --------------------------------------------------------------------------------------------
# interpret_google_result — every failure path raises LLMCallError naming the cause
# --------------------------------------------------------------------------------------------


def test_401_names_the_key_and_the_env_var():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(status=401, body="{}", model="m", base_url="https://x")
    assert "GOOGLE_STUDIO_API_KEY" in excinfo.value.detail


def test_403_names_the_key_and_the_env_var():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(status=403, body="{}", model="m", base_url="https://x")
    assert "GOOGLE_STUDIO_API_KEY" in excinfo.value.detail


def test_400_with_api_key_invalid_reason_names_the_key():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(
            status=400, body=INVALID_KEY_400_BODY, model="m", base_url="https://x"
        )
    assert "GOOGLE_STUDIO_API_KEY" in excinfo.value.detail
    assert excinfo.value.exit_code == 400


def test_400_without_the_key_shape_is_generic_and_does_not_blame_the_key():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(status=400, body=GENERIC_400_BODY, model="m", base_url="https://x")
    assert "GOOGLE_STUDIO_API_KEY" not in excinfo.value.detail
    assert "400" in excinfo.value.detail


def test_404_names_the_model_id_and_the_model_list_endpoint():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(
            status=404, body=MODEL_404_BODY, model="bogus-model", base_url="https://x"
        )
    assert "bogus-model" in excinfo.value.detail
    assert "/v1beta/models" in excinfo.value.detail


def test_429_names_rate_limiting_and_the_free_tier():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(
            status=429, body=RATE_LIMIT_429_BODY, model="m", base_url="https://x"
        )
    detail = excinfo.value.detail.lower()
    assert "rate" in detail
    assert "free tier" in detail or "per-minute" in detail
    assert "retry" in detail  # documents that this client does not retry on its own


def test_other_non_200_status_is_a_generic_structured_failure():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(status=500, body="internal error", model="m", base_url="https://x")
    assert "500" in excinfo.value.detail
    assert excinfo.value.exit_code == 500


def test_non_json_body_is_a_structured_failure():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(status=200, body="not json", model="m", base_url="https://x")
    assert "did not return JSON" in excinfo.value.detail


def test_json_that_is_not_an_object_is_a_structured_failure():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(status=200, body="[1, 2]", model="m", base_url="https://x")
    assert "not an object" in excinfo.value.detail


def test_block_reason_present_names_it():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(status=200, body=BLOCKED_BODY, model="m", base_url="https://x")
    assert "SAFETY" in excinfo.value.detail
    assert "blocked" in excinfo.value.detail.lower()


def test_no_candidates_is_a_structured_failure():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(
            status=200, body=NO_CANDIDATES_BODY, model="m", base_url="https://x"
        )
    assert "no candidates" in excinfo.value.detail.lower()


def test_safety_finish_reason_with_no_text_names_the_reason():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(
            status=200, body=SAFETY_NO_TEXT_BODY, model="m", base_url="https://x"
        )
    assert "SAFETY" in excinfo.value.detail


def test_max_tokens_with_no_text_names_the_output_limit():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(status=200, body=MAX_TOKENS_BODY, model="m", base_url="https://x")
    assert "MAX_TOKENS" in excinfo.value.detail


def test_empty_completion_text_is_a_structured_failure():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_google_result(status=200, body=EMPTY_TEXT_BODY, model="m", base_url="https://x")
    assert "empty completion" in excinfo.value.detail.lower()


def test_call_failure_shares_a_base_with_generation_errors():
    assert issubclass(LLMCallError, GenerationError)


# --------------------------------------------------------------------------------------------
# GoogleAIStudioClient — construction and the socket-touching wrapper
# --------------------------------------------------------------------------------------------


def test_client_rejects_a_non_positive_timeout():
    with pytest.raises(ValueError):
        GoogleAIStudioClient(timeout_seconds=0, api_key="k")


def test_complete_without_an_api_key_raises_naming_the_env_var():
    client = GoogleAIStudioClient(api_key=None)
    with pytest.raises(LLMCallError) as excinfo:
        client.complete(system="s", prompt="p", model="m")
    assert "GOOGLE_STUDIO_API_KEY" in excinfo.value.detail


def test_complete_sends_the_built_request_and_returns_the_interpreted_response(monkeypatch):
    captured = {}

    def fake_send(url, body, *, api_key, timeout_seconds, base_url):
        captured["url"] = url
        captured["body"] = body
        captured["api_key"] = api_key
        captured["timeout_seconds"] = timeout_seconds
        captured["base_url"] = base_url
        return 200, SUCCESS_BODY

    monkeypatch.setattr(llm_module, "_send_generate_content_request", fake_send)

    client = GoogleAIStudioClient(
        base_url="https://generativelanguage.googleapis.com",
        api_key="secret-key",
        timeout_seconds=5,
        temperature=0.4,
    )
    response = client.complete(system="be terse", prompt="write a rule", model="gemini-3.5-flash")

    assert captured["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    )
    assert captured["api_key"] == "secret-key"
    assert captured["timeout_seconds"] == 5
    assert captured["body"]["systemInstruction"] == {"parts": [{"text": "be terse"}]}
    assert captured["body"]["generationConfig"] == {"temperature": 0.4}
    assert "seed" not in captured["body"]
    assert response.text == "class R(BaseRule):\n    pass\n"
    assert response.output_tokens == 40


def test_complete_surfaces_a_model_not_found_failure(monkeypatch):
    def fake_send(url, body, *, api_key, timeout_seconds, base_url):
        return 404, MODEL_404_BODY

    monkeypatch.setattr(llm_module, "_send_generate_content_request", fake_send)

    with pytest.raises(LLMCallError) as excinfo:
        GoogleAIStudioClient(api_key="k").complete(system="s", prompt="p", model="bogus-model")
    assert "bogus-model" in excinfo.value.detail


def test_client_strips_a_trailing_slash_from_its_base_url(monkeypatch):
    captured = {}

    def fake_send(url, body, *, api_key, timeout_seconds, base_url):
        captured["url"] = url
        return 200, SUCCESS_BODY

    monkeypatch.setattr(llm_module, "_send_generate_content_request", fake_send)
    GoogleAIStudioClient(
        base_url="https://generativelanguage.googleapis.com/", api_key="k"
    ).complete(system="s", prompt="p", model="m")
    assert captured["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent"
    )


class _FakeHTTPResponse:
    """Stands in for what ``urllib.request.urlopen`` returns, used as a context manager."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._body


def test_send_generate_content_request_puts_the_key_in_the_header_never_the_url(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return _FakeHTTPResponse(200, SUCCESS_BODY)

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)

    status, body = llm_module._send_generate_content_request(
        "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
        {"contents": []},
        api_key="super-secret",
        timeout_seconds=5,
        base_url="https://generativelanguage.googleapis.com",
    )

    request = captured["request"]
    # `Request` capitalizes the names it stores, so `get_header` is asked in its capitalization
    # rather than the one the client wrote — the header that goes on the wire is the same either
    # way, HTTP header names being case-insensitive.
    assert request.get_header("X-goog-api-key") == "super-secret"
    assert "super-secret" not in request.full_url
    assert "x-goog-api-key" not in request.full_url.lower()
    assert status == 200
    assert body == SUCCESS_BODY


def test_send_generate_content_request_returns_the_status_and_body_of_an_http_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, fp=_FakeErrorBody(MODEL_404_BODY)
        )

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    status, body = llm_module._send_generate_content_request(
        "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
        {},
        api_key="k",
        timeout_seconds=5,
        base_url="https://generativelanguage.googleapis.com",
    )
    assert status == 404
    assert body == MODEL_404_BODY


def test_send_generate_content_request_raises_on_connection_refused(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMCallError) as excinfo:
        llm_module._send_generate_content_request(
            "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
            {},
            api_key="k",
            timeout_seconds=5,
            base_url="https://generativelanguage.googleapis.com",
        )
    assert "not answering" in excinfo.value.detail


def test_send_generate_content_request_raises_on_a_bare_timeout(monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMCallError) as excinfo:
        llm_module._send_generate_content_request(
            "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
            {},
            api_key="k",
            timeout_seconds=5,
            base_url="https://generativelanguage.googleapis.com",
        )
    assert "did not answer within" in excinfo.value.detail


def test_send_generate_content_request_raises_on_a_timeout_wrapped_in_urlerror(monkeypatch):
    # Confirmed against a real socket, the same non-obvious behaviour _send_chat_request
    # documents: a connect timeout surfaces as URLError(TimeoutError(...)), not a bare TimeoutError.
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMCallError) as excinfo:
        llm_module._send_generate_content_request(
            "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
            {},
            api_key="k",
            timeout_seconds=5,
            base_url="https://generativelanguage.googleapis.com",
        )
    assert "did not answer within" in excinfo.value.detail


def test_send_generate_content_request_raises_on_a_connection_reset(monkeypatch):
    # A reset by the far end *after* the request was sent comes out of http.client while the
    # response is being read, as a bare ConnectionResetError that urlopen never wraps in a
    # URLError. Observed against the live API during a benchmark run, where it escaped this module
    # and aborted the whole run with a traceback rather than one recorded CALL_ERROR the
    # checkpoint could resume past.
    def fake_urlopen(request, timeout):
        raise ConnectionResetError(54, "Connection reset by peer")

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMCallError) as excinfo:
        llm_module._send_generate_content_request(
            "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
            {},
            api_key="k",
            timeout_seconds=5,
            base_url="https://generativelanguage.googleapis.com",
        )
    assert "failed mid-request" in excinfo.value.detail


class _FakeErrorBody:
    """The ``fp`` an ``HTTPError`` reads its body from — just enough of a file object for it."""

    def __init__(self, text: str) -> None:
        self._data = text.encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------------------------
# Protocol shape — a GoogleAIStudioClient is usable wherever an LLMClient is expected
# --------------------------------------------------------------------------------------------


def _call_through_the_llm_client_protocol(client: LLMClient) -> LLMResponse:
    return client.complete(system="be terse", prompt="write a rule", model="gemini-3.5-flash")


def test_google_client_satisfies_the_llm_client_protocol_shape(monkeypatch):
    monkeypatch.setattr(
        llm_module,
        "_send_generate_content_request",
        lambda url, body, *, api_key, timeout_seconds, base_url: (200, SUCCESS_BODY),
    )
    response = _call_through_the_llm_client_protocol(GoogleAIStudioClient(api_key="k"))
    assert response.text


# --------------------------------------------------------------------------------------------
# read_google_api_key — environment-over-file precedence
# --------------------------------------------------------------------------------------------


def test_env_var_takes_precedence_over_the_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_STUDIO_API_KEY=from-file\n")
    monkeypatch.setenv("GOOGLE_STUDIO_API_KEY", "from-env")

    assert read_google_api_key(env_path=env_file) == "from-env"


def test_falls_back_to_the_file_when_no_env_var_is_set(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_STUDIO_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_STUDIO_API_KEY=from-file\n")

    assert read_google_api_key(env_path=env_file) == "from-file"


def test_a_matching_pair_of_quotes_around_the_value_is_stripped(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_STUDIO_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('GOOGLE_STUDIO_API_KEY="quoted-value"\n')

    assert read_google_api_key(env_path=env_file) == "quoted-value"


def test_blank_lines_and_comments_are_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_STUDIO_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("\n# a comment\nOTHER_VAR=1\nGOOGLE_STUDIO_API_KEY=real-value\n")

    assert read_google_api_key(env_path=env_file) == "real-value"


def test_missing_env_var_and_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_STUDIO_API_KEY", raising=False)

    assert read_google_api_key(env_path=tmp_path / "does-not-exist.env") is None
