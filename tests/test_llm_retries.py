"""Transient provider failures are retried; permanent ones are not."""

from __future__ import annotations

import httpx
import pytest
from patchpilot_agent.adapters import http as transport
from patchpilot_agent.adapters.anthropic import AnthropicAdapter
from patchpilot_agent.adapters.factory import build_adapter
from patchpilot_agent.adapters.openai_compat import OpenAICompatAdapter
from patchpilot_core.config import Settings
from patchpilot_core.errors import ModelAdapterError
from patchpilot_core.models import ChatMessage

MESSAGES = [ChatMessage(role="user", content="hi")]
OK_OPENAI = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
OK_ANTHROPIC = {
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 1},
}


class Script:
    """Replays a sequence of responses (or exceptions) for ``httpx.post``."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, url, **kwargs):
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        status, body, headers = step
        return httpx.Response(
            status, json=body, headers=headers or {}, request=httpx.Request("POST", url)
        )


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr(transport, "_sleep", delays.append)
    return delays


def openai(max_retries: int = 3) -> OpenAICompatAdapter:
    return OpenAICompatAdapter(
        "gpt-4o-mini", base_url="https://example.test/v1", api_key="k", max_retries=max_retries
    )


class TestRetries:
    def test_a_rate_limit_is_retried_after_the_servers_hint(
        self, monkeypatch: pytest.MonkeyPatch, slept: list[float]
    ) -> None:
        script = Script((429, {}, {"retry-after": "2"}), (200, OK_OPENAI, None))
        monkeypatch.setattr(httpx, "post", script)
        assert openai().complete(MESSAGES).text == "hello"
        assert script.calls == 2
        assert slept == [2.0]

    def test_an_overloaded_anthropic_api_is_retried(
        self, monkeypatch: pytest.MonkeyPatch, slept: list[float]
    ) -> None:
        script = Script((529, {"type": "error"}, None), (200, OK_ANTHROPIC, None))
        monkeypatch.setattr(httpx, "post", script)
        adapter = AnthropicAdapter("claude-sonnet-5", api_key="k", max_retries=2)
        assert adapter.complete(MESSAGES).text == "hello"
        assert script.calls == 2
        assert len(slept) == 1

    def test_a_dropped_connection_is_retried(
        self, monkeypatch: pytest.MonkeyPatch, slept: list[float]
    ) -> None:
        script = Script(httpx.ConnectError("reset"), (200, OK_OPENAI, None))
        monkeypatch.setattr(httpx, "post", script)
        assert openai().complete(MESSAGES).text == "hello"
        assert script.calls == 2

    def test_retries_are_bounded(self, monkeypatch: pytest.MonkeyPatch, slept: list[float]) -> None:
        script = Script((503, {}, None))
        monkeypatch.setattr(httpx, "post", script)
        with pytest.raises(ModelAdapterError) as caught:
            openai(max_retries=2).complete(MESSAGES)
        assert script.calls == 3
        assert len(slept) == 2
        assert "503" in caught.value.message

    def test_a_persistent_network_failure_still_becomes_an_adapter_error(
        self, monkeypatch: pytest.MonkeyPatch, slept: list[float]
    ) -> None:
        script = Script(httpx.ReadTimeout("slow"))
        monkeypatch.setattr(httpx, "post", script)
        with pytest.raises(ModelAdapterError):
            openai(max_retries=1).complete(MESSAGES)
        assert script.calls == 2

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_permanent_errors_are_not_retried(
        self, monkeypatch: pytest.MonkeyPatch, slept: list[float], status: int
    ) -> None:
        script = Script((status, {}, None))
        monkeypatch.setattr(httpx, "post", script)
        with pytest.raises(ModelAdapterError):
            openai().complete(MESSAGES)
        assert script.calls == 1
        assert slept == []


class TestDelays:
    def test_backoff_grows_and_is_capped(self) -> None:
        assert 0.5 <= transport.backoff_delay(0) <= 1.0
        assert 4.0 <= transport.backoff_delay(3) <= 8.0
        assert transport.backoff_delay(20) <= transport.MAX_DELAY_SECONDS

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"retry-after": "3"}, 3.0),
            ({"retry-after-ms": "1500"}, 1.5),
            ({"retry-after": "86400"}, transport.MAX_RETRY_AFTER_SECONDS),
            ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
            ({}, None),
        ],
    )
    def test_retry_after_is_parsed_and_capped(self, headers: dict, expected) -> None:
        response = httpx.Response(429, headers=headers)
        assert transport.retry_after_seconds(response) == expected


class TestWiring:
    def test_the_factory_applies_the_configured_retry_budget(self) -> None:
        settings = Settings(openai_api_key="k", anthropic_api_key="k", llm_max_retries=5)
        assert build_adapter("openai:gpt-4o", settings).max_retries == 5  # type: ignore[attr-defined]
        assert build_adapter("anthropic:claude-sonnet-5", settings).max_retries == 5  # type: ignore[attr-defined]
        assert build_adapter("ollama:qwen", settings).max_retries == 5  # type: ignore[attr-defined]
