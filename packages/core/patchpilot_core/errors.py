"""Actionable error taxonomy. Every error carries a stable ``code`` for the API."""

from __future__ import annotations

from typing import Any


class PatchPilotError(Exception):
    """Base class. ``remediation`` is shown to users, so keep it concrete."""

    code = "patchpilot_error"
    http_status = 500

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation
        self.context = context or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "context": self.context,
        }


class ConfigurationError(PatchPilotError):
    code = "configuration_error"
    http_status = 500


class NotFoundError(PatchPilotError):
    code = "not_found"
    http_status = 404


class ValidationError(PatchPilotError):
    code = "validation_error"
    http_status = 422


class RepositoryError(PatchPilotError):
    code = "repository_error"
    http_status = 400


class IndexingError(PatchPilotError):
    code = "indexing_error"
    http_status = 500


class RetrievalError(PatchPilotError):
    code = "retrieval_error"
    http_status = 500


class ModelAdapterError(PatchPilotError):
    code = "model_adapter_error"
    http_status = 502


class StructuredOutputError(ModelAdapterError):
    code = "structured_output_error"
    http_status = 502


class PatchError(PatchPilotError):
    code = "patch_error"
    http_status = 422


class SandboxError(PatchPilotError):
    code = "sandbox_error"
    http_status = 500


class SandboxUnavailableError(SandboxError):
    code = "sandbox_unavailable"
    http_status = 503
