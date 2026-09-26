"""HTTP transport shared by the hosted-model adapters: POST with bounded retries.

A run makes a handful of model calls over several minutes. Without retries, one
rate-limit response or one overloaded-server response -- routine for any hosted
provider -- ended the whole run as ``error``, and in a benchmark it was scored
against the model. Here those responses, and transport failures such as a reset
connection, are retried with exponential backoff and jitter, honouring the
provider's ``Retry-After`` when it sends one. Everything else, a 400 or a 401
among them, fails at once: repeating the request cannot change the answer.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx
from patchpilot_core.logging import get_logger

logger = get_logger(__name__, component="llm")

# 408 request timeout, 429 rate limited, 5xx server trouble, 529 Anthropic's
# "overloaded". All describe the server's state at that moment, not the request.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 60.0

# Looked up at call time, so tests can replace it without waiting.
_sleep = time.sleep


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, so retrying clients do not synchronise."""
    ceiling = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2**attempt))
    return random.uniform(ceiling / 2, ceiling)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The server's own retry hint, capped so a hostile value cannot stall a run."""
    for header, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = response.headers.get(header)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except ValueError:
            continue  # An HTTP-date; rare from model APIs, so fall back to backoff.
        if seconds >= 0:
            return min(seconds, MAX_RETRY_AFTER_SECONDS)
    return None


def post_with_retries(
    url: str,
    *,
    json: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    max_retries: int,
    provider: str,
    sleep: Callable[[float], None] | None = None,
) -> httpx.Response:
    """POST, retrying transient failures up to ``max_retries`` times.

    Returns the final response, which may still be an error status for the
    caller to translate. Raises the last ``httpx.TransportError`` if every
    attempt failed at the transport level.
    """
    attempt = 0
    while True:
        try:
            response = httpx.post(url, json=json, headers=headers, timeout=timeout)
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise
            delay = backoff_delay(attempt)
            reason = type(exc).__name__
        else:
            if response.status_code not in RETRYABLE_STATUS or attempt >= max_retries:
                return response
            hinted = retry_after_seconds(response)
            delay = backoff_delay(attempt) if hinted is None else hinted
            reason = f"HTTP {response.status_code}"
        attempt += 1
        logger.warning(
            "retrying a model request",
            extra={
                "provider": provider,
                "reason": reason,
                "retry": attempt,
                "of": max_retries,
                "delay_s": round(delay, 2),
            },
        )
        (sleep or _sleep)(delay)
