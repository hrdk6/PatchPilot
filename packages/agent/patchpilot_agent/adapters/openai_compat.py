"""OpenAI-compatible chat completions adapter.

Works against api.openai.com and against anything that speaks the same wire
format -- Ollama, vLLM, LM Studio, llama.cpp server, Together, Groq, an internal
gateway. That is the point: the same benchmark can compare a hosted frontier
model against a model running on the machine under the desk.

Token usage is taken from the provider response when present. When a local
server omits it, the usage is estimated and flagged so cost numbers are never
silently invented.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from patchpilot_core.errors import ModelAdapterError
from patchpilot_core.models import ChatMessage, LLMResponse, TokenUsage
from patchpilot_core.textutil import estimate_tokens


class OpenAICompatAdapter:
    """Implements ``LLMAdapter`` against a ``/chat/completions`` endpoint."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        name: str = "openai",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.extra_headers = extra_headers or {}

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if stop:
            payload["stop"] = stop

        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        started = time.perf_counter()
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ModelAdapterError(
                f"request to {self.base_url} failed: {exc}",
                remediation=(
                    "Check the base URL and that the server is reachable. For a fully "
                    "offline run use model 'mock:deterministic'."
                ),
                context={"model": self.model},
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            raise ModelAdapterError(
                f"{self.name} returned HTTP {response.status_code}: {response.text[:400]}",
                remediation=remediation_for_status(response.status_code),
                context={"model": self.model, "status": response.status_code},
            )

        try:
            body = response.json()
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelAdapterError(
                f"{self.name} returned an unparseable response: {response.text[:400]}",
                context={"model": self.model},
            ) from exc

        usage_payload = body.get("usage") or {}
        if usage_payload.get("prompt_tokens") is not None:
            usage = TokenUsage(
                input_tokens=int(usage_payload.get("prompt_tokens", 0)),
                output_tokens=int(usage_payload.get("completion_tokens", 0)),
            )
            estimated = False
        else:
            usage = TokenUsage(
                input_tokens=sum(estimate_tokens(m.content) for m in messages),
                output_tokens=estimate_tokens(text),
            )
            estimated = True

        return LLMResponse(
            text=text,
            model=self.model,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
            raw={"usage_estimated": estimated, "provider": self.name},
        )


def remediation_for_status(status: int) -> str:
    if status in (401, 403):
        return "The API key is missing or rejected. Check the provider key env var."
    if status == 404:
        return "The model name or base URL is wrong for this provider."
    if status == 429:
        return "Rate limited. Retry later or reduce benchmark concurrency."
    if status >= 500:
        return "The provider is failing. Retry, or benchmark a different model."
    return "Check the request parameters for this provider."
