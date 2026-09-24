"""Structured output parsing and validation.

Models are asked for JSON. Some of them wrap it in prose or a code fence anyway.
This module recovers the object when recovery is unambiguous, validates it
against :class:`RepairPlan`, and raises a recorded, actionable
:class:`StructuredOutputError` when it cannot -- it never silently invents a
plan, and it never lets a malformed plan reach patch generation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from patchpilot_core.errors import StructuredOutputError
from patchpilot_core.models import RepairPlan
from pydantic import ValidationError as PydanticValidationError

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

PLAN_KEYS = {
    "root_cause",
    "files_to_change",
    "tests_to_run",
    "patch_strategy",
    "assumptions",
    "uncertainties",
    "confidence",
}


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first complete JSON object out of a model response."""
    if not text or not text.strip():
        raise StructuredOutputError(
            "the model returned an empty response where a JSON plan was required",
            remediation="Retry, or choose a model that follows JSON instructions.",
        )

    candidates: list[str] = []
    for block in _FENCE_RE.findall(text):
        candidates.append(block.strip())
    candidates.append(text.strip())
    balanced = _first_balanced_object(text)
    if balanced:
        candidates.append(balanced)

    errors: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            continue
        if isinstance(payload, dict):
            return payload
        errors.append(f"expected a JSON object, got {type(payload).__name__}")

    raise StructuredOutputError(
        "the model response did not contain a JSON object",
        remediation="The prompt asks for a bare JSON object; this model did not comply.",
        context={"first_error": errors[0] if errors else "no candidates", "excerpt": text[:300]},
    )


def _first_balanced_object(text: str) -> str | None:
    """Scan for the first ``{...}`` with balanced braces, ignoring braces in strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def coerce_plan(payload: dict[str, Any]) -> RepairPlan:
    """Validate a plan payload, tolerating shape differences but not missing substance."""
    unknown = set(payload) - PLAN_KEYS
    normalised: dict[str, Any] = {key: payload[key] for key in payload if key in PLAN_KEYS}

    for key in ("files_to_change", "tests_to_run", "assumptions", "uncertainties"):
        value = normalised.get(key)
        if value is None:
            normalised[key] = []
        elif isinstance(value, str):
            normalised[key] = [item.strip() for item in value.split(",") if item.strip()]
        elif isinstance(value, list):
            normalised[key] = [str(item) for item in value]

    confidence = normalised.get("confidence")
    if isinstance(confidence, str):
        try:
            normalised["confidence"] = float(confidence)
        except ValueError:
            normalised.pop("confidence")
    if isinstance(normalised.get("confidence"), int | float):
        normalised["confidence"] = max(0.0, min(1.0, float(normalised["confidence"])))

    try:
        plan = RepairPlan.model_validate(normalised)
    except PydanticValidationError as exc:
        missing = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
        raise StructuredOutputError(
            "the repair plan did not match the required schema",
            remediation=(
                "The model must emit root_cause, patch_strategy, files_to_change, "
                "tests_to_run, assumptions, uncertainties and confidence."
            ),
            context={
                "invalid_fields": missing,
                "unknown_fields": sorted(unknown),
                "detail": exc.errors()[0].get("msg") if exc.errors() else "",
            },
        ) from exc
    return plan


def parse_plan(text: str) -> RepairPlan:
    """Full pipeline: recover JSON, validate, return a typed plan."""
    return coerce_plan(extract_json_object(text))
