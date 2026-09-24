"""Repository indexing orchestration.

``scan -> parse -> chunk -> resolve imports -> build graph -> embed -> store``.

The result, :class:`RepositoryIndex`, is a plain serialisable object. The API
persists it to SQL; the retriever consumes it; the evaluation harness rebuilds it
from a pinned commit SHA. Nothing in here knows about HTTP or the database.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.errors import IndexingError
from patchpilot_core.interfaces import EmbeddingProvider, VectorStore
from patchpilot_core.logging import get_logger
from patchpilot_core.models import CodeChunk, FileRecord, ImportEdge, IndexStats, SymbolRef

from .embeddings import build_embedder
from .graph import DependencyGraph, resolve_import_edges
from .registry import parser_for
from .scanner import RepositoryScanner, read_convention_files
from .vectorstore import build_vector_store

logger = get_logger(__name__, component="indexer")


@dataclass(slots=True)
class RepositoryIndex:
    """Everything known about one repository at one commit."""

    repo_sha: str
    root: Path
    files: list[FileRecord] = field(default_factory=list)
    symbols: list[SymbolRef] = field(default_factory=list)
    chunks: list[CodeChunk] = field(default_factory=list)
    edges: list[ImportEdge] = field(default_factory=list)
    conventions: dict[str, str] = field(default_factory=dict)
    stats: IndexStats = field(default_factory=lambda: IndexStats(repo_sha=""))
    _graph: DependencyGraph | None = None

    @property
    def graph(self) -> DependencyGraph:
        if self._graph is None:
            self._graph = DependencyGraph.build(self.edges, self.chunks)
        return self._graph

    @property
    def namespace(self) -> str:
        return self.repo_sha

    def chunk_by_id(self, chunk_id: str) -> CodeChunk | None:
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    def chunk_by_key(self, key: str) -> CodeChunk | None:
        for chunk in self.chunks:
            if chunk.key == key:
                return chunk
        return None

    def chunks_for_path(self, path: str) -> list[CodeChunk]:
        return [chunk for chunk in self.chunks if chunk.path == path]

    def paths(self) -> list[str]:
        return [record.path for record in self.files]

    def test_paths(self) -> list[str]:
        return [record.path for record in self.files if record.is_test]

    def tree_excerpt(self, limit: int = 120) -> str:
        paths = sorted(self.paths())
        shown = paths[:limit]
        suffix = "" if len(paths) <= limit else f"\n... and {len(paths) - limit} more files"
        return "\n".join(shown) + suffix

    # ------------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_sha": self.repo_sha,
            "root": str(self.root),
            "files": [item.model_dump(mode="json") for item in self.files],
            "symbols": [item.model_dump(mode="json") for item in self.symbols],
            "chunks": [item.model_dump(mode="json") for item in self.chunks],
            "edges": [item.model_dump(mode="json") for item in self.edges],
            "conventions": self.conventions,
            "stats": self.stats.model_dump(mode="json"),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RepositoryIndex:
        return cls(
            repo_sha=payload["repo_sha"],
            root=Path(payload["root"]),
            files=[FileRecord.model_validate(item) for item in payload.get("files", [])],
            symbols=[SymbolRef.model_validate(item) for item in payload.get("symbols", [])],
            chunks=[CodeChunk.model_validate(item) for item in payload.get("chunks", [])],
            edges=[ImportEdge.model_validate(item) for item in payload.get("edges", [])],
            conventions=payload.get("conventions", {}),
            stats=IndexStats.model_validate(
                payload.get("stats", {"repo_sha": payload["repo_sha"]})
            ),
        )


class IndexCache(MutableMapping[str, RepositoryIndex]):
    """A bounded, thread-safe, least-recently-used map of ``repo_sha -> index``.

    A long-lived API process is asked about many repositories and commits. An
    unbounded dict would keep every index it ever built -- every chunk's source
    included -- for the life of the process. Reading an entry marks it as
    recently used; inserting past ``maxsize`` evicts the least recently used.
    Worker threads share one instance, hence the lock.
    """

    def __init__(self, maxsize: int = 16) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        self.maxsize = maxsize
        self._entries: OrderedDict[str, RepositoryIndex] = OrderedDict()
        self._lock = threading.Lock()

    def __getitem__(self, key: str) -> RepositoryIndex:
        with self._lock:
            value = self._entries[key]
            self._entries.move_to_end(key)
            return value

    def __setitem__(self, key: str, value: RepositoryIndex) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

    def __delitem__(self, key: str) -> None:
        with self._lock:
            del self._entries[key]

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._entries))

    def __len__(self) -> int:
        return len(self._entries)


class RepositoryIndexer:
    """Builds a :class:`RepositoryIndex` and populates the vector store."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        embedder: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embedder = embedder or build_embedder(self.settings)
        self.vector_store = vector_store or build_vector_store(self.settings)

    def index(
        self,
        root: Path,
        repo_sha: str,
        *,
        embed: bool = True,
    ) -> RepositoryIndex:
        started = time.perf_counter()
        root = Path(root).resolve()
        if not root.is_dir():
            raise IndexingError(
                f"repository path does not exist: {root}",
                remediation="Ingest the repository first so the checkout exists on disk.",
            )

        scanner = RepositoryScanner(
            max_file_bytes=self.settings.index_max_file_bytes,
            max_files=self.settings.index_max_files,
        )
        scan = scanner.scan(root)

        files: list[FileRecord] = []
        symbols: list[SymbolRef] = []
        chunks: list[CodeChunk] = []
        edges: list[ImportEdge] = []
        parse_errors = 0

        for scanned in scan.files:
            parser = parser_for(scanned.path)
            if parser is None:
                scan.skipped["no-parser"] += 1
                continue
            try:
                source = scanned.absolute.read_text(encoding="utf-8", errors="replace")
            except OSError:
                scan.skipped["unreadable"] += 1
                continue

            file_symbols, file_edges, record = parser.parse(scanned.path, source)
            files.append(record)
            if record.parse_error:
                parse_errors += 1
            symbols.extend(file_symbols)
            edges.extend(file_edges)
            chunks.extend(parser.chunk(scanned.path, source, repo_sha))

        resolved_edges = resolve_import_edges(edges, [record.path for record in files])
        graph = DependencyGraph.build(resolved_edges, chunks)
        chunks = graph.annotate_chunks(chunks)

        stats = IndexStats(
            repo_sha=repo_sha,
            files_scanned=scan.scanned,
            files_indexed=len(files),
            files_skipped=sum(scan.skipped.values()),
            skip_reasons=dict(scan.skipped),
            symbols=len(symbols),
            chunks=len(chunks),
            import_edges=len(resolved_edges),
            resolved_import_edges=sum(1 for edge in resolved_edges if edge.resolved),
            parse_errors=parse_errors,
        )

        index = RepositoryIndex(
            repo_sha=repo_sha,
            root=root,
            files=files,
            symbols=symbols,
            chunks=chunks,
            edges=resolved_edges,
            conventions=read_convention_files(root),
            stats=stats,
        )
        index._graph = graph

        if embed and chunks:
            stats.embedded_chunks = self.embed_index(index)

        stats.duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "repository indexed",
            extra={
                "repo_sha": repo_sha,
                "files_indexed": stats.files_indexed,
                "chunks": stats.chunks,
                "import_edges": stats.import_edges,
                "resolved_import_edges": stats.resolved_import_edges,
                "parse_errors": stats.parse_errors,
                "duration_ms": stats.duration_ms,
                "embedded_chunks": stats.embedded_chunks,
            },
        )
        return index

    def embed_index(self, index: RepositoryIndex) -> int:
        """Embed every chunk and upsert into the vector store."""
        texts = [embedding_text(chunk) for chunk in index.chunks]
        vectors = self.embedder.embed_documents(texts)
        if len(vectors) != len(index.chunks):
            raise IndexingError(
                f"embedder returned {len(vectors)} vectors for {len(index.chunks)} chunks"
            )
        self.vector_store.ensure_collection(self.embedder.dimension)
        payloads: list[dict[str, object]] = [
            {
                "path": chunk.path,
                "symbol": chunk.symbol,
                "symbol_type": str(chunk.symbol_type),
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "is_test": chunk.is_test,
                "repo_sha": chunk.repo_sha,
            }
            for chunk in index.chunks
        ]
        self.vector_store.upsert(
            index.namespace,
            [chunk.chunk_id for chunk in index.chunks],
            vectors,
            payloads,
        )
        return len(vectors)


def embedding_text(chunk: CodeChunk) -> str:
    """What we actually embed: path and symbol carry as much signal as the body."""
    parts = [
        chunk.path.replace("/", " "),
        chunk.symbol.replace(".", " "),
        chunk.docstring or "",
        chunk.content,
    ]
    return "\n".join(part for part in parts if part)
