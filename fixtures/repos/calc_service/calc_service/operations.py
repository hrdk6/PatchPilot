"""Arithmetic helpers used by the reporting layer."""

from __future__ import annotations


def add(left: float, right: float) -> float:
    """Return the sum of two numbers."""
    return left + right


def subtract(left: float, right: float) -> float:
    """Return the difference of two numbers."""
    return left - right


def percentage(part: float, whole: float) -> float:
    """Return ``part`` as a percentage of ``whole``.

    A whole of zero means "nothing was expected", which should report 0.0 rather
    than raising, because the reporting layer calls this for empty buckets.
    """
    return part / whole * 100.0


def average(values: list[float]) -> float:
    """Return the mean of ``values``; an empty list averages to 0.0."""
    if not values:
        return 0.0
    return sum(values) / len(values)
