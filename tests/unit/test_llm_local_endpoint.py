"""Local LLM endpoint validation (R4-1, OQ-18 early validation).

Proves the existing LLMProvider works against a local OpenAI-compatible
inference server (llama.cpp server, vLLM, Ollama) without application
logic changes: /v1-prefixed URLs resolve to /v1/chat/completions, no
Authorization header is sent without a key, and llama.cpp's response
shape (OpenAI-compatible plus extra fields) parses cleanly.

The last test is an opt-in live probe: set HARKENIQ_TEST_LLM_URL (e.g.
http://localhost:8080/v1) to run it against a real local server.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harkeniq_sm.llm_provider import LLMProvider

# llama.cpp server response: OpenAI-compatible with extra fields
LLAMACPP_RESPONSE = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "created": 1756000000,
    "model": "mistral-7b-v0.3.Q4_K_M.gguf",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Fan bearing wear is the likely cause.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
    "timings": {"predicted_per_second": 34.2},  # llama.cpp extra
}


def _mock_client(response_json: dict):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = response_json
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestLocalEndpoint:
    async def test_llamacpp_v1_url_and_response_shape(self):
        provider = LLMProvider(
            api_url="http://localhost:8080/v1",
            api_key="",
            model="mistral-7b-v0.3",
        )
        mock_client = _mock_client(LLAMACPP_RESPONSE)
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value = mock_client
            result = await provider.complete(
                [{"role": "user", "content": "explain the fan fault"}]
            )
        assert result == "Fan bearing wear is the likely cause."
        call = mock_client.post.call_args
        url = call.args[0] if call.args else call.kwargs.get("url")
        assert url == "http://localhost:8080/v1/chat/completions"

    async def test_no_auth_header_without_key(self):
        provider = LLMProvider(
            api_url="http://localhost:8080/v1", api_key="", model="m",
        )
        mock_client = _mock_client(LLAMACPP_RESPONSE)
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value = mock_client
            await provider.complete([{"role": "user", "content": "x"}])
        headers = mock_client.post.call_args.kwargs["headers"]
        assert "Authorization" not in headers
        assert headers["Content-Type"] == "application/json"

    async def test_auth_header_present_with_key(self):
        provider = LLMProvider(
            api_url="https://api.example.com/v1", api_key="sk-test", model="m",
        )
        mock_client = _mock_client(LLAMACPP_RESPONSE)
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value = mock_client
            await provider.complete([{"role": "user", "content": "x"}])
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-test"

    async def test_trailing_slash_normalized(self):
        provider = LLMProvider(
            api_url="http://localhost:8080/v1/", api_key="", model="m",
        )
        mock_client = _mock_client(LLAMACPP_RESPONSE)
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value = mock_client
            await provider.complete([{"role": "user", "content": "x"}])
        call = mock_client.post.call_args
        url = call.args[0] if call.args else call.kwargs.get("url")
        assert url == "http://localhost:8080/v1/chat/completions"

    async def test_localhost_not_misdetected_as_anthropic(self):
        provider = LLMProvider(
            api_url="http://localhost:8080/v1", api_key="", model="m",
        )
        assert provider._is_anthropic is False


@pytest.mark.skipif(
    not os.environ.get("HARKENIQ_TEST_LLM_URL"),
    reason="set HARKENIQ_TEST_LLM_URL (e.g. http://localhost:8080/v1) to run "
           "the live local-endpoint probe",
)
class TestLiveLocalEndpoint:
    async def test_live_completion(self):
        provider = LLMProvider(
            api_url=os.environ["HARKENIQ_TEST_LLM_URL"],
            api_key=os.environ.get("HARKENIQ_TEST_LLM_KEY", ""),
            model=os.environ.get("HARKENIQ_TEST_LLM_MODEL", "default"),
            timeout=60.0,
        )
        result = await provider.complete(
            [{"role": "user", "content": "Reply with the single word: pong"}]
        )
        assert result is not None and result.strip()
