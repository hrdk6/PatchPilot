"""Anthropic Messages API adapter.

Kept separate from the OpenAI-compatible adapter because the wire format differs
in ways that matter:

* the system prompt is a top-level field rather than a message;
* usage is reported as ``input_tokens``/``output_tokens``;
* current Claude models (Opus 4.7 onwards, Sonnet 5, Fable) reject sampling
  parameters with an HTTP 400, so ``temperature`` is only sent to the older
  models that still accept it;
* a safety decline arrives as HTTP 200 with ``stop_reason: "refusal"``, which
  must not be mistaken for an empty answer.

No server-side model fallback is requested. A benchmark attributes every result
to the model it named; a silent fallback to another model would corrupt that.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from patchpilot_core.errors import ModelAdapterError
from patchpilot_core.models import ChatMessage, LLMResponse, TokenUsage

from .http import post_with_retries
from .openai_compat import remediation_for_status

# Room for adaptive thinking plus a plan or a diff. Thinking tokens count
# against this limit, so a small value truncates the answer, not the thinking.
DEFAULT_MAX_TOKENS = 16_000
API_VERSION = "2023-06-01"

# Models that still accept ``temperature``: the Claude 3 family and Claude 4
# releases up to 4.6. Everything newer rejects sampling parameters, and so will
# models released after this was written, so the list names the old ones.
_ACCEPTS_SAMPLING = re.compile(r"^claude-(?:3|(?:opus|sonnet|haiku)-4(?:-[0-6])?(?:-\d{8})?$)")


def accepts_sampling_parameters(model: str) -> bool:
    return bool(_ACCEPTS_SAMPLING.match(model))


class AnthropicAdapter:
    """Implements ``LLMAdapter`` against the Anthropic Messages API."""

    name = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 120.0,
        max_retries: int = 0,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        system_parts = [m.content for m in messages if m.role == "system"]
        conversation = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        if not conversation:
            raise ModelAdapterError("an Anthropic request needs at least one user message")

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "messages": conversation,
        }
        sampling = accepts_sampling_parameters(self.model)
        if sampling:
            payload["temperature"] = temperature
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if stop:
            payload["stop_sequences"] = stop

        started = time.perf_counter()
        try:
            response = post_with_retries(
                f"{self.base_url}/messages",
                json=payload,
                headers={
                    "content-type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": API_VERSION,
                },
                timeout=self.timeout,
                max_retries=self.max_retries,
                provider=self.name,
            )
        except httpx.HTTPError as exc:
            raise ModelAdapterError(
                f"request to the Anthropic API failed: {exc}",
                remediation="Check network access and PATCHPILOT_ANTHROPIC_API_KEY.",
                context={"model": self.model},
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            raise ModelAdapterError(
                f"Anthropic returned HTTP {response.status_code}: {response.text[:400]}",
                remediation=remediation_for_status(response.status_code),
                context={"model": self.model, "status": response.status_code},
            )

        try:
            body = response.json()
            blocks = body["content"]
            text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ModelAdapterError(
                f"Anthropic returned an unparseable response: {response.text[:400]}",
                context={"model": self.model},
            ) from exc

        stop_reason = body.get("stop_reason")
        if stop_reason == "refusal":
            details = body.get("stop_details") or {}
            raise ModelAdapterError(
                "the model declined the request"
                + (f" (category: {details['category']})" if details.get("category") else ""),
                remediation=(
                    "Review the issue text and retrieved context for content the model "
                    "will not act on, or benchmark a different model."
                ),
                context={"model": self.model, "stop_details": details},
            )

        usage_payload = body.get("usage") or {}
        return LLMResponse(
            text=text,
            model=self.model,
            usage=TokenUsage(
                input_tokens=int(usage_payload.get("input_tokens", 0)),
                output_tokens=int(usage_payload.get("output_tokens", 0)),
            ),
            latency_ms=latency_ms,
            finish_reason=stop_reason,
            raw={
                "provider": self.name,
                "usage_estimated": not usage_payload,
                "sampling_parameters_sent": sampling,
            },
        )
