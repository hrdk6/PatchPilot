"""Benchmark dataset and benchmark run endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from patchpilot_core.enums import JobType
from patchpilot_core.errors import NotFoundError
from patchpilot_evals import DEFAULT_DATASET_DIR, discover_datasets, load_dataset
from sqlalchemy.orm import Session

from .. import store
from ..db import get_db
from ..policy import dataset_path
from ..schemas import (
    BenchmarkCreate,
    BenchmarkResponse,
    BenchmarkResultResponse,
    DatasetResponse,
    DatasetTaskResponse,
    JobResponse,
)

datasets_router = APIRouter(prefix="/datasets", tags=["benchmarks"])
router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


def _dataset_response(dataset) -> DatasetResponse:
    return DatasetResponse(
        name=dataset.name,
        path=str(dataset.path),
        description=dataset.description,
        task_count=len(dataset.tasks),
        tags=dataset.all_tags(),
        tasks=[
            DatasetTaskResponse(
                id=task.id,
                repository_url=task.repository_url,
                commit_sha=task.commit_sha,
                title=task.title,
                difficulty=task.difficulty,
                tags=list(task.tags),
                validation_command=task.validation_command,
                baseline_command=task.baseline_command,
                expected_files=list(task.expected_files),
            )
            for task in dataset.tasks
        ],
    )


@datasets_router.get("", response_model=list[DatasetResponse], summary="Available datasets")
def list_datasets() -> list[DatasetResponse]:
    return [_dataset_response(dataset) for dataset in discover_datasets()]


@datasets_router.get("/{name}", response_model=DatasetResponse, summary="Dataset detail")
def dataset_detail(name: str) -> DatasetResponse:
    return _dataset_response(load_dataset(dataset_path(name, DEFAULT_DATASET_DIR)))


@router.post(
    "",
    response_model=JobResponse,
    status_code=202,
    summary="Queue a benchmark run",
    description=(
        "Evaluates every model against the identical task set. Models that are not "
        "configured are recorded as skipped with a reason rather than dropped."
    ),
)
def create(payload: BenchmarkCreate, session: Session = Depends(get_db)) -> JobResponse:
    dataset = load_dataset(dataset_path(payload.dataset, DEFAULT_DATASET_DIR))
    store.ensure_queue_capacity(session)
    benchmark = store.create_benchmark(
        session, dataset=str(dataset.path), models=payload.models, tags=payload.tags
    )
    job = store.enqueue_job(session, JobType.BENCHMARK_RUN, {"benchmark_id": benchmark.id})
    return JobResponse.model_validate(job)


@router.get("", response_model=list[BenchmarkResponse], summary="List benchmark runs")
def index(
    limit: int = Query(default=50, ge=1, le=200), session: Session = Depends(get_db)
) -> list[BenchmarkResponse]:
    return [BenchmarkResponse.model_validate(row) for row in store.list_benchmarks(session, limit)]


@router.get("/{benchmark_id}", response_model=BenchmarkResponse, summary="Benchmark detail")
def detail(
    benchmark_id: str,
    tag: str | None = Query(default=None, description="Filter results by task tag"),
    result: str | None = Query(default=None, description="Filter by pass/fail"),
    session: Session = Depends(get_db),
) -> BenchmarkResponse:
    row = store.get_benchmark(session, benchmark_id)
    results = store.list_benchmark_results(session, benchmark_id)
    if tag:
        results = [item for item in results if tag in (item.tags or [])]
    if result == "pass":
        results = [item for item in results if item.passed]
    elif result == "fail":
        results = [item for item in results if not item.passed]
    response = BenchmarkResponse.model_validate(row)
    response.results = [BenchmarkResultResponse.model_validate(item) for item in results]
    return response


@router.get(
    "/{benchmark_id}/report.json",
    summary="Machine-readable report",
    response_class=Response,
)
def report_json(benchmark_id: str, session: Session = Depends(get_db)) -> Response:
    import json

    row = store.get_benchmark(session, benchmark_id)
    if not row.report:
        raise NotFoundError(
            f"benchmark {benchmark_id} has no report yet",
            remediation="Wait for the benchmark run to finish.",
        )
    return Response(
        content=json.dumps(row.report, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{benchmark_id}.json"'},
    )


@router.get(
    "/{benchmark_id}/report.md",
    summary="Readable Markdown report",
    response_class=Response,
)
def report_markdown(benchmark_id: str, session: Session = Depends(get_db)) -> Response:
    row = store.get_benchmark(session, benchmark_id)
    if not row.markdown:
        raise NotFoundError(
            f"benchmark {benchmark_id} has no report yet",
            remediation="Wait for the benchmark run to finish.",
        )
    return Response(
        content=row.markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{benchmark_id}.md"'},
    )
