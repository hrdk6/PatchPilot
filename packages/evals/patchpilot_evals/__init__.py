"""Benchmark datasets, metrics and reports."""

from __future__ import annotations

from .dataset import (
    DEFAULT_DATASET_DIR,
    BenchmarkTask,
    Dataset,
    discover_datasets,
    load_dataset,
)
from .metrics import (
    SMALL_SAMPLE_THRESHOLD,
    ModelSummary,
    TaskResult,
    median,
    percentile,
    summarize_model,
    wilson_interval,
)
from .report import render_json_report, render_markdown_report
from .runner import BenchmarkReport, BenchmarkRunner
from .similarity import patch_similarity, touched_expected_files

__all__ = [
    "DEFAULT_DATASET_DIR",
    "SMALL_SAMPLE_THRESHOLD",
    "BenchmarkReport",
    "BenchmarkRunner",
    "BenchmarkTask",
    "Dataset",
    "ModelSummary",
    "TaskResult",
    "discover_datasets",
    "load_dataset",
    "median",
    "patch_similarity",
    "percentile",
    "render_json_report",
    "render_markdown_report",
    "summarize_model",
    "touched_expected_files",
    "wilson_interval",
]
