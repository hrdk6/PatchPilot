"""Hybrid, explainable retrieval.

Four families of signal are combined, and every one of them leaves a
:class:`ScoreComponent` behind so the API and the dashboard can answer "why is
this file in the prompt?" with a number and a reason rather than a shrug:

1. **Semantic similarity** -- vector search over symbol-aligned chunks.
2. **Lexical overlap** -- idf-weighted term overlap; catches exact identifiers
   and error strings that embeddings smooth away.
3. **Graph proximity** -- import neighbours and call-graph neighbours of the
   seed set, so the caller and the callee come along with the suspect function.
4. **Task signals** -- paths named in the issue, paths named in the previous
   failure output, and tests that exercise a seed symbol.

The weights are constants in this module. They are deliberately visible and
tunable rather than buried in a prompt.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.enums import RetrievalReason
from patchpilot_core.interfaces import EmbeddingProvider, VectorStore
from patchpilot_core.models import (
    CodeChunk,
    ContextPackage,
    RetrievedChunk,
    ScoreComponent,
)

from .embeddings import code_tokens
from .indexer import RepositoryIndex, embedding_text

W_SEMANTIC = 1.00
W_LEXICAL = 0.85
W_IMPORT_NEIGHBOR = 0.35
W_CALL_NEIGHBOR = 0.45
W_TEST_FOR_SYMBOL = 0.55
W_FAILURE_TRACE = 0.90
W_ISSUE_PATH = 0.70

SEED_COUNT = 6
NEIGHBOR_DECAY = 0.6

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PATH_RE = re.compile(r"[\w./\\-]+\.(?:py|pyi|md|toml|cfg|ini|ya?ml|json|txt)")


@dataclass(slots=True)
class _Accumulator:
    chunk: CodeChunk
    score: float = 0.0
    components: list[ScoreComponent] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.components is None:
            self.components = []

    def add(self, reason: RetrievalReason, weight: float, detail: str = "") -> None:
        if weight <= 0:
            return
        for existing in self.components:
            if existing.reason is reason:
                if weight <= existing.weight:
                    return
                self.score += weight - existing.weight
                existing.weight = round(weight, 4)
                existing.detail = detail or existing.detail
                return
        self.components.append(
            ScoreComponent(reason=reason, weight=round(weight, 4), detail=detail)
        )
        self.score += weight


class HybridRetriever:
    """Combines vector, lexical, graph and task signals over a repository index."""

    def __init__(
        self,
        index: RepositoryIndex,
        embedder: EmbeddingProvider,
        vector_store: VectorStore,
        settings: Settings | None = None,
    ) -> None:
        self.index = index
        self.embedder = embedder
        self.vector_store = vector_store
        self.settings = settings or get_settings()
        self._document_frequency = self._build_document_frequency()
        self._id_by_key: dict[str, str] = {chunk.key: chunk.chunk_id for chunk in index.chunks}

    # ------------------------------------------------------------------ public
    def retrieve(
        self,
        query: str,
        *,
        failure_output: str | None = None,
        top_k: int | None = None,
        max_chars: int | None = None,
        prefer_paths: list[str] | None = None,
    ) -> ContextPackage:
        top_k = top_k or self.settings.retrieval_top_k
        max_chars = max_chars or self.settings.retrieval_max_context_chars
        if not self.index.chunks:
            return ContextPackage(query=query, conventions=self.index.conventions)

        combined_query = query if not failure_output else f"{query}\n\n{failure_output}"
        accumulators: dict[str, _Accumulator] = {
            chunk.chunk_id: _Accumulator(chunk=chunk) for chunk in self.index.chunks
        }

        self._score_semantic(combined_query, accumulators, limit=max(top_k * 4, 24))
        self._score_lexical(combined_query, accumulators)
        self._score_issue_paths(query, accumulators, prefer_paths or [])
        if failure_output:
            self._score_failure_output(failure_output, accumulators)

        seeds = self._top(accumulators, SEED_COUNT)
        self._score_graph_neighbors(seeds, accumulators)
        self._score_related_tests(seeds, accumulators)

        ranked = [item for item in self._top(accumulators, top_k * 3) if item.score > 0]
        selected, truncated, total = self._apply_budget(ranked, top_k, max_chars)

        return ContextPackage(
            query=query,
            chunks=selected,
            conventions=self.index.conventions,
            failure_excerpt=failure_output,
            repo_tree_excerpt=self.index.tree_excerpt(),
            total_chars=total,
            truncated=truncated,
        )

    # ----------------------------------------------------------------- signals
    def _score_semantic(
        self, query: str, accumulators: dict[str, _Accumulator], limit: int
    ) -> None:
        vector = self.embedder.embed_query(query)
        try:
            matches = self.vector_store.search(self.index.namespace, vector, limit)
        except Exception as exc:
            # Degrade rather than fail the run: lexical and graph signals still work.
            for item in accumulators.values():
                item.add(RetrievalReason.SEMANTIC, 0.0, f"vector search unavailable: {exc}")
            return
        if not matches:
            return
        best = max(score for _, score in matches) or 1.0
        for chunk_id, score in matches:
            accumulator = accumulators.get(chunk_id)
            if accumulator is None:
                continue
            normalised = max(0.0, score) / best if best else 0.0
            accumulator.add(
                RetrievalReason.SEMANTIC,
                W_SEMANTIC * normalised,
                f"cosine={score:.3f}",
            )

    def _build_document_frequency(self) -> dict[str, int]:
        frequency: Counter[str] = Counter()
        for chunk in self.index.chunks:
            frequency.update(set(code_tokens(embedding_text(chunk))))
        return dict(frequency)

    def _score_lexical(self, query: str, accumulators: dict[str, _Accumulator]) -> None:
        query_terms = Counter(code_tokens(query))
        if not query_terms:
            return
        total_docs = max(len(self.index.chunks), 1)
        weights = {
            term: math.log(1 + total_docs / (1 + self._document_frequency.get(term, 0)))
            for term in query_terms
        }
        max_possible = sum(weights.values()) or 1.0

        for accumulator in accumulators.values():
            chunk_terms = set(code_tokens(embedding_text(accumulator.chunk)))
            overlap = [term for term in query_terms if term in chunk_terms]
            if not overlap:
                continue
            raw = sum(weights[term] for term in overlap)
            normalised = min(1.0, raw / max_possible * 2.5)
            top_terms = sorted(overlap, key=lambda term: -weights[term])[:4]
            accumulator.add(
                RetrievalReason.LEXICAL,
                W_LEXICAL * normalised,
                "terms: " + ", ".join(top_terms),
            )

    def _score_issue_paths(
        self, issue_text: str, accumulators: dict[str, _Accumulator], prefer_paths: list[str]
    ) -> None:
        mentioned = set(prefer_paths) | self._paths_in_text(issue_text)
        for accumulator in accumulators.values():
            if accumulator.chunk.path in mentioned:
                accumulator.add(
                    RetrievalReason.ISSUE_PATH_MENTION,
                    W_ISSUE_PATH,
                    f"{accumulator.chunk.path} named in the issue",
                )

    def _score_failure_output(
        self, failure_output: str, accumulators: dict[str, _Accumulator]
    ) -> None:
        paths = self._paths_in_text(failure_output)
        line_hits = self._path_line_hits(failure_output)
        identifiers = {match.lower() for match in _IDENTIFIER_RE.findall(failure_output)}
        for accumulator in accumulators.values():
            chunk = accumulator.chunk
            detail_parts: list[str] = []
            weight = 0.0
            if chunk.path in paths:
                weight = max(weight, W_FAILURE_TRACE * 0.7)
                detail_parts.append("path in failure output")
            for path, line in line_hits:
                if path == chunk.path and chunk.start_line <= line <= chunk.end_line:
                    weight = W_FAILURE_TRACE
                    detail_parts.append(f"traceback points at {path}:{line}")
                    break
            bare = chunk.symbol.rpartition(".")[2].lower()
            if bare and len(bare) > 3 and bare in identifiers:
                weight = max(weight, W_FAILURE_TRACE * 0.6)
                detail_parts.append(f"symbol {bare} named in failure output")
            if weight:
                accumulator.add(RetrievalReason.FAILURE_TRACE, weight, "; ".join(detail_parts))

    def _score_graph_neighbors(
        self, seeds: list[_Accumulator], accumulators: dict[str, _Accumulator]
    ) -> None:
        graph = self.index.graph
        chunks_by_path: dict[str, list[CodeChunk]] = defaultdict(list)
        for chunk in self.index.chunks:
            chunks_by_path[chunk.path].append(chunk)
        seed_ids = {seed.chunk.chunk_id for seed in seeds}

        for seed in seeds:
            seed_path = seed.chunk.path
            for path, distance in graph.file_neighbors(seed_path, depth=2).items():
                weight = W_IMPORT_NEIGHBOR * (NEIGHBOR_DECAY ** (distance - 1))
                direction = (
                    "imported by" if seed_path in graph.imports.get(path, set()) else "imports"
                )
                for chunk in chunks_by_path.get(path, []):
                    if chunk.chunk_id in seed_ids:
                        continue
                    accumulators[chunk.chunk_id].add(
                        RetrievalReason.IMPORT_NEIGHBOR,
                        weight,
                        f"{path} {direction} {seed_path} (distance {distance})",
                    )

            for key in graph.callers_of(seed.chunk.key):
                self._boost_key(
                    accumulators,
                    key,
                    RetrievalReason.CALL_NEIGHBOR,
                    W_CALL_NEIGHBOR,
                    f"calls {seed.chunk.symbol}",
                )
            for key in graph.callees_of(seed.chunk.key):
                self._boost_key(
                    accumulators,
                    key,
                    RetrievalReason.CALL_NEIGHBOR,
                    W_CALL_NEIGHBOR * NEIGHBOR_DECAY,
                    f"called by {seed.chunk.symbol}",
                )

    def _score_related_tests(
        self, seeds: list[_Accumulator], accumulators: dict[str, _Accumulator]
    ) -> None:
        seed_symbols = {
            seed.chunk.symbol.rpartition(".")[2]
            for seed in seeds
            if not seed.chunk.symbol.startswith("<")
        }
        seed_symbols = {symbol for symbol in seed_symbols if len(symbol) > 3}
        if not seed_symbols:
            return
        for accumulator in accumulators.values():
            chunk = accumulator.chunk
            if not chunk.is_test:
                continue
            hits = [symbol for symbol in seed_symbols if symbol in chunk.content]
            if hits:
                accumulator.add(
                    RetrievalReason.TEST_FOR_SYMBOL,
                    W_TEST_FOR_SYMBOL,
                    "exercises " + ", ".join(sorted(hits)[:3]),
                )

    # ----------------------------------------------------------------- helpers
    def _boost_key(
        self,
        accumulators: dict[str, _Accumulator],
        key: str,
        reason: RetrievalReason,
        weight: float,
        detail: str,
    ) -> None:
        chunk_id = self._id_by_key.get(key)
        if chunk_id is None:
            return
        accumulator = accumulators.get(chunk_id)
        if accumulator is not None:
            accumulator.add(reason, weight, detail)

    def _paths_in_text(self, text: str) -> set[str]:
        if not text:
            return set()
        normalised = text.replace("\\", "/")
        known = set(self.index.paths())
        found: set[str] = set()
        for candidate in _PATH_RE.findall(normalised):
            cleaned = candidate.lstrip("./")
            if cleaned in known:
                found.add(cleaned)
                continue
            for path in known:
                if path.endswith("/" + cleaned) or path == cleaned:
                    found.add(path)
        return found

    def _path_line_hits(self, text: str) -> list[tuple[str, int]]:
        """Extract ``path/to/file.py:123`` and ``File "x.py", line 12`` references."""
        hits: list[tuple[str, int]] = []
        normalised = text.replace("\\", "/")
        known = set(self.index.paths())

        def resolve(raw: str) -> str | None:
            cleaned = raw.lstrip("./")
            if cleaned in known:
                return cleaned
            for path in known:
                if path.endswith("/" + cleaned):
                    return path
            return None

        for match in re.finditer(r"([\w./-]+\.py):(\d+)", normalised):
            path = resolve(match.group(1))
            if path:
                hits.append((path, int(match.group(2))))
        for match in re.finditer(r'File "([^"]+\.py)", line (\d+)', normalised):
            path = resolve(match.group(1))
            if path:
                hits.append((path, int(match.group(2))))
        return hits

    @staticmethod
    def _top(accumulators: dict[str, _Accumulator], count: int) -> list[_Accumulator]:
        ordered = sorted(
            accumulators.values(),
            key=lambda item: (-item.score, item.chunk.path, item.chunk.start_line),
        )
        return [item for item in ordered[:count] if item.score > 0]

    @staticmethod
    def _apply_budget(
        ranked: list[_Accumulator], top_k: int, max_chars: int
    ) -> tuple[list[RetrievedChunk], bool, int]:
        selected: list[RetrievedChunk] = []
        total = 0
        truncated = False
        for accumulator in ranked:
            if len(selected) >= top_k:
                break
            size = len(accumulator.chunk.content)
            if total + size > max_chars and selected:
                # Dropped because of the character budget, not merely out-ranked.
                truncated = True
                continue
            selected.append(
                RetrievedChunk(
                    chunk=accumulator.chunk,
                    score=round(accumulator.score, 4),
                    components=accumulator.components,
                    rank=len(selected) + 1,
                )
            )
            total += size
        return selected, truncated, total
