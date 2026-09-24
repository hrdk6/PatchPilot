"""Short, sortable, human-readable identifiers."""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford-ish, no ambiguous chars


def _encode(number: int, width: int) -> str:
    out: list[str] = []
    while number:
        number, rem = divmod(number, len(_ALPHABET))
        out.append(_ALPHABET[rem])
    text = "".join(reversed(out)) or "0"
    return text.rjust(width, "0")


def new_id(prefix: str) -> str:
    """Time-prefixed identifier, e.g. ``run_01j9wq2x_7f3a``.

    The leading component is millisecond time so identifiers sort chronologically
    in logs and database listings; the suffix is random for collision safety.
    """
    stamp = _encode(int(time.time() * 1000), 10)
    suffix = _encode(secrets.randbits(30), 6)
    return f"{prefix}_{stamp}{suffix}"


def run_id() -> str:
    return new_id("run")


def repo_id() -> str:
    return new_id("repo")


def task_id() -> str:
    return new_id("task")


def job_id() -> str:
    return new_id("job")


def attempt_id() -> str:
    return new_id("att")


def benchmark_id() -> str:
    return new_id("bench")
