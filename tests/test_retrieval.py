"""Retrieval must find the right code *and* be able to say why."""

from __future__ import annotations

from pathlib import Path

import pytest
from patchpilot_core.enums import RetrievalReason
from patchpilot_indexer import (
    HashEmbedder,
    HybridRetriever,
    LocalVectorStore,
    RepositoryIndexer,
)


@pytest.fixture
def retriever(settings, pipeline_repo: Path) -> HybridRetriever:
    embedder = HashEmbedder(256)
    store = LocalVectorStore(None)
    index = RepositoryIndexer(settings, embedder=embedder, vector_store=store).index(
        pipeline_repo, "sha-pipeline"
    )
    return HybridRetriever(index, embedder, store, settings)


@pytest.fixture
def calc_retriever(settings, fixture_repo: Path) -> HybridRetriever:
    embedder = HashEmbedder(256)
    store = LocalVectorStore(None)
    index = RepositoryIndexer(settings, embedder=embedder, vector_store=store).index(
        fixture_repo, "sha-calc"
    )
    return HybridRetriever(index, embedder, store, settings)


ISSUE = (
    "top_words returns the wrong counts: 'Ship.' and 'ship' are counted as "
    "different words in the analytics output"
)


def paths_of(package) -> list[str]:
    return [item.chunk.path for item in package.chunks]


class TestHybridRetrieval:
    def test_finds_the_defect_one_import_hop_from_the_symptom(
        self, retriever: HybridRetriever
    ) -> None:
        """The issue names analytics; the bug lives in tokenizer, which it imports."""
        package = retriever.retrieve(ISSUE, top_k=10)
        selected = {f"{item.chunk.path}::{item.chunk.symbol}" for item in package.chunks}
        assert "text_pipeline/tokenizer.py::normalize" in selected
        assert "text_pipeline/analytics.py::top_words" in selected

    def test_every_chunk_carries_a_reason_and_a_weight(self, retriever: HybridRetriever) -> None:
        package = retriever.retrieve(ISSUE, top_k=8)
        assert package.chunks
        for item in package.chunks:
            assert item.components, f"{item.chunk.key} has no explanation"
            assert item.score > 0
            assert abs(sum(c.weight for c in item.components) - item.score) < 1e-6
            assert item.explain()

    def test_graph_neighbours_are_labelled_as_such(self, retriever: HybridRetriever) -> None:
        package = retriever.retrieve(ISSUE, top_k=12)
        reasons = {reason for item in package.chunks for reason in item.reasons}
        assert RetrievalReason.SEMANTIC in reasons
        assert RetrievalReason.LEXICAL in reasons
        assert RetrievalReason.IMPORT_NEIGHBOR in reasons

    def test_ranks_are_dense_and_ordered_by_score(self, retriever: HybridRetriever) -> None:
        package = retriever.retrieve(ISSUE, top_k=6)
        assert [item.rank for item in package.chunks] == list(range(1, len(package.chunks) + 1))
        scores = [item.score for item in package.chunks]
        assert scores == sorted(scores, reverse=True)

    def test_a_path_named_in_the_issue_is_boosted_and_credited(
        self, calc_retriever: HybridRetriever
    ) -> None:
        package = calc_retriever.retrieve(
            "Something is wrong in calc_service/operations.py", top_k=6
        )
        operations = [
            item for item in package.chunks if item.chunk.path == "calc_service/operations.py"
        ]
        assert operations
        assert any(RetrievalReason.ISSUE_PATH_MENTION in item.reasons for item in operations)

    def test_failure_output_pulls_in_the_traceback_frame(
        self, calc_retriever: HybridRetriever
    ) -> None:
        failure = (
            'File "calc_service/operations.py", line 22, in percentage\n'
            "    return part / whole * 100.0\n"
            "ZeroDivisionError: division by zero"
        )
        package = calc_retriever.retrieve("tests fail", failure_output=failure, top_k=8)
        traced = [item for item in package.chunks if RetrievalReason.FAILURE_TRACE in item.reasons]
        assert traced
        assert any(item.chunk.symbol == "percentage" for item in traced)

    def test_related_tests_are_pulled_in(self, calc_retriever: HybridRetriever) -> None:
        package = calc_retriever.retrieve("percentage raises ZeroDivisionError", top_k=12)
        assert any(item.chunk.is_test for item in package.chunks)

    def test_top_k_and_the_character_budget_are_respected(self, retriever: HybridRetriever) -> None:
        package = retriever.retrieve(ISSUE, top_k=3)
        assert len(package.chunks) <= 3

        tiny = retriever.retrieve(ISSUE, top_k=12, max_chars=200)
        assert tiny.total_chars <= max(200, len(tiny.chunks[0].chunk.content) if tiny.chunks else 0)
        assert tiny.truncated

    def test_not_truncated_when_everything_fits(self, retriever: HybridRetriever) -> None:
        package = retriever.retrieve(ISSUE, top_k=100, max_chars=10_000_000)
        assert package.truncated is False

    def test_conventions_travel_with_the_package(self, retriever: HybridRetriever) -> None:
        package = retriever.retrieve(ISSUE, top_k=4)
        assert "README.md" in package.conventions
        assert package.repo_tree_excerpt

    def test_empty_index_degrades_gracefully(self, settings, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        embedder = HashEmbedder(64)
        store = LocalVectorStore(None)
        index = RepositoryIndexer(settings, embedder=embedder, vector_store=store).index(
            empty, "sha-empty"
        )
        package = HybridRetriever(index, embedder, store, settings).retrieve("anything")
        assert package.chunks == []

    def test_vector_store_failure_does_not_fail_the_run(self, retriever: HybridRetriever) -> None:
        class Broken:
            def search(self, *_args, **_kwargs):
                raise RuntimeError("qdrant is down")

        retriever.vector_store = Broken()  # type: ignore[assignment]
        package = retriever.retrieve(ISSUE, top_k=5)
        # Lexical and graph signals still work, so retrieval degrades rather than dies.
        assert package.chunks
