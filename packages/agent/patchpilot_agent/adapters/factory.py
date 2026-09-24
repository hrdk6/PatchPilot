"""Model selection.

A model is named ``provider:model``. The provider decides which adapter is
built and which credentials are required; the rest of the system only ever sees
the ``LLMAdapter`` protocol.

Supported providers:

``mock``           deterministic test doubles, always available, no network
``openai``         api.openai.com (or any OpenAI-compatible gateway)
``anthropic``      the Anthropic Messages API
``openai-compat``  an explicit OpenAI-compatible base URL
``ollama``/``local``/``vllm``/``lmstudio``  local servers speaking the same format
"""

from __future__ import annotations

from dataclasses import dataclass

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.costs import lookup_pricing
from patchpilot_core.errors import ConfigurationError
from patchpilot_core.interfaces import LLMAdapter

from .anthropic import AnthropicAdapter
from .mock import MOCK_VARIANTS, MockAdapter
from .openai_compat import OpenAICompatAdapter

LOCAL_PROVIDERS = {"ollama": "http://localhost:11434/v1", "lmstudio": "http://localhost:1234/v1"}


@dataclass(slots=True)
class ModelInfo:
    """What the API exposes about a selectable model."""

    id: str
    provider: str
    model: str
    available: bool
    reason: str
    is_test_double: bool
    pricing_known: bool
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None


def split_model(identifier: str) -> tuple[str, str]:
    if ":" not in identifier:
        raise ConfigurationError(
            f"model {identifier!r} must be written as 'provider:model'",
            remediation="For example: mock:deterministic, openai:gpt-4o-mini.",
        )
    provider, model = identifier.split(":", 1)
    return provider.strip().lower(), model.strip()


def build_adapter(identifier: str, settings: Settings | None = None) -> LLMAdapter:
    settings = settings or get_settings()
    provider, model = split_model(identifier)

    if provider == "mock":
        if model not in MOCK_VARIANTS:
            raise ConfigurationError(
                f"unknown mock variant {model!r}",
                remediation=f"Use one of: {', '.join(MOCK_VARIANTS)}.",
            )
        return MockAdapter(model)

    if provider == "openai":
        if not settings.openai_api_key:
            raise ConfigurationError(
                "no OpenAI API key configured",
                remediation=(
                    "Set PATCHPILOT_OPENAI_API_KEY, or use mock:deterministic / a local "
                    "provider for an offline run."
                ),
                context={"model": identifier},
            )
        return OpenAICompatAdapter(
            model,
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            timeout=settings.llm_timeout_seconds,
            name="openai",
        )

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ConfigurationError(
                "no Anthropic API key configured",
                remediation=(
                    "Set PATCHPILOT_ANTHROPIC_API_KEY, or use mock:deterministic for an "
                    "offline run."
                ),
                context={"model": identifier},
            )
        return AnthropicAdapter(
            model,
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    if provider in ("openai-compat", "local", "vllm", *LOCAL_PROVIDERS):
        base_url = settings.openai_base_url
        if provider in LOCAL_PROVIDERS and base_url == "https://api.openai.com/v1":
            base_url = LOCAL_PROVIDERS[provider]
        return OpenAICompatAdapter(
            model,
            base_url=base_url,
            api_key=settings.openai_api_key,
            timeout=settings.llm_timeout_seconds,
            name=provider,
        )

    raise ConfigurationError(
        f"unknown model provider {provider!r}",
        remediation=(
            "Supported providers: mock, openai, anthropic, openai-compat, ollama, "
            "lmstudio, vllm, local."
        ),
    )


def describe_models(settings: Settings | None = None) -> list[ModelInfo]:
    """Everything selectable in the UI, with an honest availability reason."""
    settings = settings or get_settings()
    infos: list[ModelInfo] = []

    for variant in MOCK_VARIANTS:
        identifier = f"mock:{variant}"
        infos.append(
            ModelInfo(
                id=identifier,
                provider="mock",
                model=variant,
                available=True,
                reason="Deterministic test double; runs offline.",
                is_test_double=True,
                pricing_known=True,
                input_per_mtok=0.0,
                output_per_mtok=0.0,
            )
        )

    hosted = [
        ("openai", "gpt-4o-mini", bool(settings.openai_api_key), "PATCHPILOT_OPENAI_API_KEY"),
        ("openai", "gpt-4o", bool(settings.openai_api_key), "PATCHPILOT_OPENAI_API_KEY"),
        (
            "anthropic",
            "claude-opus-5",
            bool(settings.anthropic_api_key),
            "PATCHPILOT_ANTHROPIC_API_KEY",
        ),
        (
            "anthropic",
            "claude-sonnet-5",
            bool(settings.anthropic_api_key),
            "PATCHPILOT_ANTHROPIC_API_KEY",
        ),
        (
            "anthropic",
            "claude-haiku-4-5",
            bool(settings.anthropic_api_key),
            "PATCHPILOT_ANTHROPIC_API_KEY",
        ),
    ]
    for provider, model, configured, env_var in hosted:
        identifier = f"{provider}:{model}"
        pricing = lookup_pricing(identifier)
        infos.append(
            ModelInfo(
                id=identifier,
                provider=provider,
                model=model,
                available=configured,
                reason=("Ready." if configured else f"Not configured: set {env_var} to enable."),
                is_test_double=False,
                pricing_known=pricing is not None,
                input_per_mtok=pricing.input_per_mtok if pricing else None,
                output_per_mtok=pricing.output_per_mtok if pricing else None,
            )
        )

    return infos
