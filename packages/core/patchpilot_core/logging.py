"""Structured JSON logging with correlation IDs.

Every log line emitted anywhere in PatchPilot carries the ambient
``correlation_id`` (one per HTTP request or background job) and, when inside an
agent run, the ``run_id`` and ``state``. That is what makes a run reconstructable
from logs alone.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Any

# The default is an immutable empty mapping: a mutable default on a ContextVar is
# shared across every context, so an accidental in-place update would leak fields
# from one run into another.
_EMPTY: Mapping[str, Any] = MappingProxyType({})
_context: ContextVar[Mapping[str, Any]] = ContextVar("patchpilot_log_context", default=_EMPTY)

_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


def get_context() -> dict[str, Any]:
    return dict(_context.get())


def bind(**fields: Any) -> Token[Mapping[str, Any]]:
    """Bind fields onto the ambient logging context. Returns a reset token."""
    merged = {**_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    return _context.set(merged)


def unbind(token: Token[Mapping[str, Any]]) -> None:
    _context.reset(token)


@contextmanager
def log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    token = bind(**fields)
    try:
        yield get_context()
    finally:
        unbind(token)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


class JsonFormatter(logging.Formatter):
    """One JSON object per line; unknown ``extra`` keys are merged in verbatim."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_context.get())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """``LEVEL logger: message  [key=value ...]`` for terminals.

    The bracket carries the ambient context *and* the record's own ``extra``
    fields -- "skipping model" is useless without the model it skipped. The
    static ``component`` tag is left out; the logger name already says it.
    """

    def format(self, record: logging.LogRecord) -> str:
        fields = dict(_context.get())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "component" and not key.startswith("_"):
                fields[key] = value
        suffix = " ".join(f"{k}={v}" for k, v in fields.items())
        base = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        if suffix:
            base = f"{base}  [{suffix}]"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())
    root.addHandler(handler)
    # uvicorn installs its own noisy handlers; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


class RunLoggerAdapter(logging.LoggerAdapter):
    """Adapter that stamps a fixed set of fields on every record."""

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(name: str, **fields: Any) -> logging.LoggerAdapter:
    return RunLoggerAdapter(logging.getLogger(name), fields)
