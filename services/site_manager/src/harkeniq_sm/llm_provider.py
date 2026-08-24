"""LLM provider: backend-agnostic HTTP client (spec A2.7 contract 6).

Calls any OpenAI-compatible chat completions API (Claude, GPT, vLLM,
Ollama).  The provider accepts structured messages (list of role/content
dicts) so both C1 (LLM Explain) and C2 (Skill Generation) can use it
with different prompt formats.

No vendor SDK imported.  httpx handles the HTTP call.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("harkeniq.sm.llm")

# Anthropic API uses a slightly different schema than OpenAI.
# We detect based on URL and adapt the request format.
_ANTHROPIC_HOSTS = ("api.anthropic.com",)


class LLMProvider:
    """Backend-agnostic LLM API client.

    Accepts structured messages: [{"role": "system", "content": "..."},
    {"role": "user", "content": "..."}].  Returns the completion text
    or None on any failure.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        max_tokens: int = 1024,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._is_anthropic = any(h in self.api_url for h in _ANTHROPIC_HOSTS)

    async def complete(self, messages: list[dict[str, str]]) -> Optional[str]:
        """Send a chat completion request.  Returns text or None on failure."""
        try:
            if self._is_anthropic:
                return await self._call_anthropic(messages)
            return await self._call_openai_compat(messages)
        except httpx.TimeoutException:
            logger.warning("LLM request timed out after %.0fs", self.timeout)
            return None
        except httpx.HTTPStatusError as e:
            logger.warning("LLM API error: %d %s", e.response.status_code, e.response.text[:200])
            return None
        except Exception as e:
            logger.warning("LLM request failed: %s", e)
            return None

    async def _call_openai_compat(self, messages: list[dict[str, str]]) -> Optional[str]:
        """OpenAI-compatible /chat/completions endpoint.

        Works against cloud APIs and local inference servers alike
        (llama.cpp server, vLLM, Ollama). For llama.cpp set api_url to
        http://host:8080/v1 (R4-1 air-gapped validation); local servers
        need no API key, so the Authorization header is only sent when a
        key is configured.
        """
        url = f"{self.api_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def _call_anthropic(self, messages: list[dict[str, str]]) -> Optional[str]:
        """Anthropic Messages API (/v1/messages)."""
        url = f"{self.api_url}/v1/messages"
        # Separate system message from user/assistant messages
        system_text = ""
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                api_messages.append(msg)
        if not api_messages:
            return None

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": self.max_tokens,
        }
        if system_text:
            body["system"] = system_text

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            # Anthropic returns content as a list of blocks
            content_blocks = data.get("content", [])
            texts = [b["text"] for b in content_blocks if b.get("type") == "text"]
            return "\n".join(texts) if texts else None


class NullLLMProvider:
    """No-op provider when LLM is disabled."""

    async def complete(self, messages: list[dict[str, str]]) -> Optional[str]:
        return None
