"""Tests for ``OllamaClient``, the ``LLMClient`` implementation over a local Ollama daemon.

**No test here makes a network call.** The transport is exercised through the two pure functions
it is built out of — ``build_chat_request`` (what would be sent) and ``interpret_ollama_result``
(what was returned) — and the one socket-touching function, ``_send_chat_request``, is
monkeypatched wherever a full ``complete()`` round trip is asserted. The success payload shape below
matches ``/api/chat`` with ``stream: false`` as documented by Ollama; nothing here talks to a daemon
to get it.
"""

import json
import urllib.error

import pytest

from generation import llm as llm_module
from generation.errors import GenerationError, LLMCallError
from generation.llm import (
    DEFAULT_CLI_MODEL,
    DEFAULT_GOOGLE_MODEL,
    DEFAULT_OLLAMA_MODEL,
    ClaudeCliClient,
    GoogleAIStudioClient,
    LLMClient,
    LLMResponse,
    OllamaClient,
    build_chat_request,
    interpret_ollama_result,
)

#: One non-streaming `/api/chat` response, shaped like Ollama's own docs.
SUCCESS_BODY = json.dumps(
    {
        "model": "qwen2.5:1.5b",
        "created_at": "2026-07-31T00:00:00Z",
        "message": {"role": "assistant", "content": "class R(BaseRule):\n    pass\n"},
        "done": True,
        "total_duration": 4_883_583_458,
        "load_duration": 1_334_875,
        "prompt_eval_count": 26,
        "prompt_eval_duration": 342_546_000,
        "eval_count": 298,
        "eval_duration": 4_535_599_000,
    }
)

#: Captured shape of Ollama's 404 for a model that has not been pulled.
MODEL_404_BODY = json.dumps({"error": 'model "llama2" not found, try pulling it first'})


# --------------------------------------------------------------------------------------------
# build_chat_request — the pure request shape
# --------------------------------------------------------------------------------------------


def test_url_is_the_chat_endpoint_not_generate():
    url, _ = build_chat_request(
        "http://localhost:11434", system="be terse", prompt="write a rule", model="m"
    )
    assert url == "http://localhost:11434/api/chat"


def test_system_prompt_is_its_own_message_not_folded_into_the_user_turn():
    _, body = build_chat_request(
        "http://localhost:11434", system="be terse", prompt="hi", model="m"
    )
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert len(body["messages"]) == 2


def test_stream_is_false():
    _, body = build_chat_request("http://localhost:11434", system="s", prompt="p", model="m")
    assert body["stream"] is False


def test_model_is_passed_through():
    _, body = build_chat_request(
        "http://localhost:11434", system="s", prompt="p", model="qwen2.5:1.5b"
    )
    assert body["model"] == "qwen2.5:1.5b"


def test_options_key_is_omitted_when_none_given():
    _, body = build_chat_request("http://localhost:11434", system="s", prompt="p", model="m")
    assert "options" not in body


def test_options_are_merged_in_when_given():
    _, body = build_chat_request(
        "http://localhost:11434",
        system="s",
        prompt="p",
        model="m",
        options={"seed": 1, "temperature": 0},
    )
    assert body["options"] == {"seed": 1, "temperature": 0}


def test_base_url_is_joined_verbatim_stripping_is_the_clients_job():
    url, _ = build_chat_request("http://localhost:11434/", system="s", prompt="p", model="m")
    # build_chat_request itself does not strip trailing slashes — that is OllamaClient's job on
    # the base_url it is constructed with — so this pins the raw join it is responsible for.
    assert url == "http://localhost:11434//api/chat"


def test_client_strips_a_trailing_slash_from_its_base_url(monkeypatch):
    captured = {}

    def fake_send(url, body, *, timeout_seconds, base_url):
        captured["url"] = url
        return 200, SUCCESS_BODY

    monkeypatch.setattr(llm_module, "_send_chat_request", fake_send)
    OllamaClient(base_url="http://localhost:11434/").complete(system="s", prompt="p", model="m")
    assert captured["url"] == "http://localhost:11434/api/chat"


# --------------------------------------------------------------------------------------------
# interpret_ollama_result — success and metadata mapping
# --------------------------------------------------------------------------------------------


def test_success_body_yields_text_and_metadata():
    response = interpret_ollama_result(
        status=200, body=SUCCESS_BODY, model="qwen2.5:1.5b", base_url="http://localhost:11434"
    )
    assert response.text == "class R(BaseRule):\n    pass\n"
    assert response.model == "qwen2.5:1.5b"
    assert response.input_tokens == 26
    assert response.output_tokens == 298


def test_nanosecond_duration_is_converted_to_milliseconds():
    response = interpret_ollama_result(
        status=200, body=SUCCESS_BODY, model="m", base_url="http://localhost:11434"
    )
    assert response.duration_ms == 4_883_583_458 // 1_000_000


def test_cost_usd_is_none_not_zero():
    response = interpret_ollama_result(
        status=200, body=SUCCESS_BODY, model="m", base_url="http://localhost:11434"
    )
    assert response.cost_usd is None


def test_raw_payload_is_kept():
    response = interpret_ollama_result(
        status=200, body=SUCCESS_BODY, model="m", base_url="http://localhost:11434"
    )
    assert response.raw["eval_count"] == 298


# --------------------------------------------------------------------------------------------
# interpret_ollama_result — every failure path raises LLMCallError
# --------------------------------------------------------------------------------------------


def test_model_not_pulled_names_the_ollama_pull_command():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_ollama_result(
            status=404, body=MODEL_404_BODY, model="llama2", base_url="http://localhost:11434"
        )
    assert "ollama pull" in excinfo.value.detail
    assert "llama2" in excinfo.value.detail
    assert excinfo.value.exit_code == 404
    assert excinfo.value.stderr == MODEL_404_BODY


def test_other_non_200_status_is_a_generic_structured_failure():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_ollama_result(
            status=500, body="internal error", model="m", base_url="http://localhost:11434"
        )
    assert "500" in excinfo.value.detail
    assert excinfo.value.exit_code == 500


def test_non_json_body_is_a_structured_failure():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_ollama_result(
            status=200, body="not json", model="m", base_url="http://localhost:11434"
        )
    assert "did not return JSON" in excinfo.value.detail


def test_json_that_is_not_an_object_is_a_structured_failure():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_ollama_result(
            status=200, body="[1, 2]", model="m", base_url="http://localhost:11434"
        )
    assert "not an object" in excinfo.value.detail


def test_missing_message_object_is_a_structured_failure():
    body = json.dumps({"done": True})
    with pytest.raises(LLMCallError) as excinfo:
        interpret_ollama_result(status=200, body=body, model="m", base_url="http://localhost:11434")
    assert "message" in excinfo.value.detail


def test_empty_message_content_is_a_structured_failure():
    body = json.dumps({"message": {"role": "assistant", "content": ""}})
    with pytest.raises(LLMCallError) as excinfo:
        interpret_ollama_result(status=200, body=body, model="m", base_url="http://localhost:11434")
    assert "no completion text" in excinfo.value.detail


def test_missing_message_content_is_a_structured_failure():
    body = json.dumps({"message": {"role": "assistant"}})
    with pytest.raises(LLMCallError) as excinfo:
        interpret_ollama_result(status=200, body=body, model="m", base_url="http://localhost:11434")
    assert "no completion text" in excinfo.value.detail


def test_missing_metadata_is_none_rather_than_zero():
    body = json.dumps({"message": {"role": "assistant", "content": "ok"}})
    response = interpret_ollama_result(
        status=200, body=body, model="m", base_url="http://localhost:11434"
    )
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.duration_ms is None
    assert response.cost_usd is None


def test_call_failure_shares_a_base_with_generation_errors():
    assert issubclass(LLMCallError, GenerationError)


# --------------------------------------------------------------------------------------------
# OllamaClient — construction and the socket-touching wrapper
# --------------------------------------------------------------------------------------------


def test_client_rejects_a_non_positive_timeout():
    with pytest.raises(ValueError):
        OllamaClient(timeout_seconds=0)


def test_each_client_declares_its_own_default_model():
    """There is no package-wide default any more, and that is the point.

    One shared `DEFAULT_MODEL` of `claude-opus-4-8` was what every agent passed on to whichever
    client was configured, so a Google-backed deployment asked Google for an Anthropic model and
    got a 404 that reads exactly like a bad API key. A default belongs to the backend that can
    serve it.
    """
    assert OllamaClient().default_model == DEFAULT_OLLAMA_MODEL
    assert ClaudeCliClient().default_model == DEFAULT_CLI_MODEL
    assert GoogleAIStudioClient(api_key="unused").default_model == DEFAULT_GOOGLE_MODEL
    assert DEFAULT_CLI_MODEL == "claude-opus-4-8"
    assert len({DEFAULT_OLLAMA_MODEL, DEFAULT_CLI_MODEL, DEFAULT_GOOGLE_MODEL}) == 3


def test_complete_with_no_model_sends_this_clients_default_not_another_clients(monkeypatch):
    """The regression, at the layer it actually bit."""
    captured = {}

    def fake_send(url, body, *, timeout_seconds, base_url):
        captured["body"] = body
        return 200, SUCCESS_BODY

    monkeypatch.setattr(llm_module, "_send_chat_request", fake_send)

    response = OllamaClient().complete(system="s", prompt="p")

    assert captured["body"]["model"] == DEFAULT_OLLAMA_MODEL
    assert captured["body"]["model"] != DEFAULT_CLI_MODEL
    # The response names the model actually used, so a recorder at this seam stores a real id
    # rather than the None the caller passed.
    assert response.model == DEFAULT_OLLAMA_MODEL


def test_complete_sends_the_built_request_and_returns_the_interpreted_response(monkeypatch):
    captured = {}

    def fake_send(url, body, *, timeout_seconds, base_url):
        captured["url"] = url
        captured["body"] = body
        captured["timeout_seconds"] = timeout_seconds
        captured["base_url"] = base_url
        return 200, SUCCESS_BODY

    monkeypatch.setattr(llm_module, "_send_chat_request", fake_send)

    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=5)
    response = client.complete(system="be terse", prompt="write a rule", model="qwen2.5:1.5b")

    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["body"]["messages"][0]["role"] == "system"
    assert captured["timeout_seconds"] == 5
    assert response.text == "class R(BaseRule):\n    pass\n"
    assert response.output_tokens == 298


def test_complete_surfaces_a_model_not_pulled_failure(monkeypatch):
    def fake_send(url, body, *, timeout_seconds, base_url):
        return 404, MODEL_404_BODY

    monkeypatch.setattr(llm_module, "_send_chat_request", fake_send)

    with pytest.raises(LLMCallError) as excinfo:
        OllamaClient().complete(system="s", prompt="p", model="llama2")
    assert "ollama pull llama2" in excinfo.value.detail


def test_client_options_flow_into_the_request_body(monkeypatch):
    captured = {}

    def fake_send(url, body, *, timeout_seconds, base_url):
        captured["body"] = body
        return 200, SUCCESS_BODY

    monkeypatch.setattr(llm_module, "_send_chat_request", fake_send)

    client = OllamaClient(options={"seed": 42, "temperature": 0})
    client.complete(system="s", prompt="p", model="m")
    assert captured["body"]["options"] == {"seed": 42, "temperature": 0}


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


def test_send_chat_request_returns_status_and_body_on_success(monkeypatch):
    monkeypatch.setattr(
        llm_module.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeHTTPResponse(200, SUCCESS_BODY),
    )
    status, body = llm_module._send_chat_request(
        "http://localhost:11434/api/chat",
        {"model": "m"},
        timeout_seconds=5,
        base_url="http://localhost:11434",
    )
    assert status == 200
    assert body == SUCCESS_BODY


def test_send_chat_request_returns_the_status_and_body_of_an_http_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, fp=_FakeErrorBody(MODEL_404_BODY)
        )

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    status, body = llm_module._send_chat_request(
        "http://localhost:11434/api/chat", {}, timeout_seconds=5, base_url="http://localhost:11434"
    )
    assert status == 404
    assert body == MODEL_404_BODY


def test_send_chat_request_raises_on_connection_refused(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMCallError) as excinfo:
        llm_module._send_chat_request(
            "http://localhost:11434/api/chat",
            {},
            timeout_seconds=5,
            base_url="http://localhost:11434",
        )
    assert "not answering" in excinfo.value.detail
    assert "http://localhost:11434" in excinfo.value.detail


def test_send_chat_request_raises_on_a_bare_timeout(monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMCallError) as excinfo:
        llm_module._send_chat_request(
            "http://localhost:11434/api/chat",
            {},
            timeout_seconds=5,
            base_url="http://localhost:11434",
        )
    assert "did not answer within" in excinfo.value.detail


def test_send_chat_request_raises_on_a_timeout_wrapped_in_urlerror(monkeypatch):
    # Confirmed against a real socket: a connect timeout surfaces as URLError(TimeoutError(...)),
    # not as a bare TimeoutError, so this is the path that actually fires in practice.
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMCallError) as excinfo:
        llm_module._send_chat_request(
            "http://localhost:11434/api/chat",
            {},
            timeout_seconds=5,
            base_url="http://localhost:11434",
        )
    assert "did not answer within" in excinfo.value.detail


def test_send_chat_request_raises_on_a_connection_reset(monkeypatch):
    # A reset by the far end after the request was sent comes out of http.client as a bare
    # ConnectionResetError, which urlopen never wraps in a URLError — so none of the branches
    # above catch it and it would escape this module. The same hole the Google client had, closed
    # here too so a caller's "every failure is an LLMCallError" holds for both.
    def fake_urlopen(request, timeout):
        raise ConnectionResetError(54, "Connection reset by peer")

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMCallError) as excinfo:
        llm_module._send_chat_request(
            "http://localhost:11434/api/chat",
            {},
            timeout_seconds=5,
            base_url="http://localhost:11434",
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
# Protocol shape — an OllamaClient is usable wherever an LLMClient is expected
# --------------------------------------------------------------------------------------------


def _call_through_the_llm_client_protocol(client: LLMClient) -> LLMResponse:
    return client.complete(system="be terse", prompt="write a rule", model="qwen2.5:1.5b")


def test_ollama_client_satisfies_the_llm_client_protocol_shape(monkeypatch):
    monkeypatch.setattr(
        llm_module,
        "_send_chat_request",
        lambda url, body, *, timeout_seconds, base_url: (200, SUCCESS_BODY),
    )
    response = _call_through_the_llm_client_protocol(OllamaClient())
    assert response.text
