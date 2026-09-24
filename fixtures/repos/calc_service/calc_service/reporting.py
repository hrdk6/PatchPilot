"""Turns raw counters into a human readable report."""

from __future__ import annotations

from dataclasses import dataclass

from calc_service.operations import average, percentage


@dataclass(frozen=True)
class Bucket:
    name: str
    completed: int
    expected: int


def completion_rate(bucket: Bucket) -> float:
    """Percentage of expected work that was completed in this bucket."""
    return percentage(bucket.completed, bucket.expected)


def summarize(buckets: list[Bucket]) -> str:
    """Render one line per bucket plus an overall average."""
    lines = []
    rates = []
    for bucket in buckets:
        rate = completion_rate(bucket)
        rates.append(rate)
        lines.append(f"{bucket.name}: {rate:.1f}%")
    lines.append(f"overall: {average(rates):.1f}%")
    return "\n".join(lines)
