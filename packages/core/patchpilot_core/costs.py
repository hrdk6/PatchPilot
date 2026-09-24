"""Model pricing and cost estimation.

Prices change. This table is a *static snapshot* used to produce an estimate, and
every number it produces is labelled "estimated" in the API, the UI and the
benchmark report. Override it without touching code by pointing
``PATCHPILOT_PRICING_FILE`` at a JSON file of the same shape:

```json
{"openai:gpt-4o-mini": {"input_per_mtok": 0.15, "output_per_mtok": 0.6}}
```
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .models import CostEstimate, TokenUsage


@dataclass(frozen=True, slots=True)
class Pricing:
    input_per_mtok: float
    output_per_mtok: float
    source: str = "builtin-snapshot"


# USD per 1M tokens. Snapshot only -- verify against your provider before quoting.
DEFAULT_PRICING: dict[str, Pricing] = {
    "mock:deterministic": Pricing(0.0, 0.0, "local mock model, no provider cost"),
    "mock:stubborn": Pricing(0.0, 0.0, "local mock model, no provider cost"),
    "mock:broken": Pricing(0.0, 0.0, "local mock model, no provider cost"),
    "openai:gpt-4o": Pricing(2.50, 10.00),
    "openai:gpt-4o-mini": Pricing(0.15, 0.60),
    "openai:gpt-4.1": Pricing(2.00, 8.00),
    "openai:gpt-4.1-mini": Pricing(0.40, 1.60),
    "anthropic:claude-opus-5": Pricing(5.00, 25.00),
    "anthropic:claude-sonnet-5": Pricing(2.00, 10.00),
    "anthropic:claude-haiku-4-5": Pricing(1.00, 5.00),
    "anthropic:claude-haiku-4-5-20251001": Pricing(1.00, 5.00),
}

_LOCAL_PREFIXES = ("ollama:", "local:", "lmstudio:", "vllm:")


def _load_overrides() -> dict[str, Pricing]:
    path = os.environ.get("PATCHPILOT_PRICING_FILE")
    if not path:
        return {}
    file = Path(path)
    if not file.exists():
        return {}
    raw = json.loads(file.read_text(encoding="utf-8"))
    return {
        key: Pricing(
            float(value["input_per_mtok"]),
            float(value["output_per_mtok"]),
            source=f"override:{file.name}",
        )
        for key, value in raw.items()
    }


def pricing_table() -> dict[str, Pricing]:
    table = dict(DEFAULT_PRICING)
    table.update(_load_overrides())
    return table


def lookup_pricing(model: str) -> Pricing | None:
    table = pricing_table()
    if model in table:
        return table[model]
    if model.startswith(_LOCAL_PREFIXES):
        return Pricing(0.0, 0.0, "self-hosted model, no per-token provider cost")
    # Allow "provider:model" to fall back to a bare "model" key and vice versa.
    if ":" in model:
        _, bare = model.split(":", 1)
        for key, value in table.items():
            if key.split(":", 1)[-1] == bare:
                return value
    return None


def estimate_cost(model: str, usage: TokenUsage) -> CostEstimate:
    """Convert token usage into an estimated USD cost."""
    pricing = lookup_pricing(model)
    if pricing is None:
        return CostEstimate(
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            pricing_known=False,
            note=(
                "No pricing entry for this model. Token counts are exact where the "
                "provider reported them; USD cost is not estimated."
            ),
        )
    input_usd = usage.input_tokens / 1_000_000 * pricing.input_per_mtok
    output_usd = usage.output_tokens / 1_000_000 * pricing.output_per_mtok
    return CostEstimate(
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        input_usd=round(input_usd, 6),
        output_usd=round(output_usd, 6),
        total_usd=round(input_usd + output_usd, 6),
        pricing_known=True,
        note=f"Estimated from {pricing.source}; verify against current provider pricing.",
    )
