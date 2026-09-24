"""Import and call graph construction.

Semantic similarity alone retrieves code that *reads* like the issue. The graph
is what retrieves code that is actually *connected* to it: the module that
imports the buggy helper, the test that exercises it, the caller whose
expectations the fix has to preserve.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from patchpilot_core.models import CodeChunk, ImportEdge


def module_name_for_path(path: str) -> str | None:
    """``pkg/sub/mod.py`` -> ``pkg.sub.mod``; ``pkg/__init__.py`` -> ``pkg``."""
    if not path.endswith((".py", ".pyi")):
        return None
    stem = path[: path.rfind(".")]
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    if not stem:
        return None
    return stem.replace("/", ".")


def _package_of(path: str) -> str:
    module = module_name_for_path(path)
    if module is None:
        return ""
    if path.endswith("/__init__.py") or path == "__init__.py":
        return module
    return module.rpartition(".")[0]


def resolve_import_edges(edges: list[ImportEdge], known_paths: list[str]) -> list[ImportEdge]:
    """Point each import edge at a file in this repository when one exists.

    Unresolved edges (third-party or stdlib imports) are kept with
    ``resolved=False`` -- they are still useful signal about what a module does.
    """
    by_module: dict[str, str] = {}
    for path in known_paths:
        module = module_name_for_path(path)
        # Prefer a package __init__ over a same-named module if both exist.
        if module and (module not in by_module or path.endswith("/__init__.py")):
            by_module[module] = path

    # Also index by the top-level-stripped name so a repo laid out as ``src/pkg``
    # still resolves ``import pkg.mod``.
    alias: dict[str, str] = {}
    for module, path in by_module.items():
        head, _, tail = module.partition(".")
        if head in {"src", "lib", "app"} and tail:
            alias.setdefault(tail, path)

    resolved: list[ImportEdge] = []
    for edge in edges:
        target = _resolve_one(edge, by_module, alias)
        resolved.append(
            edge.model_copy(update={"target_path": target, "resolved": target is not None})
        )
    return resolved


def _resolve_one(edge: ImportEdge, by_module: dict[str, str], alias: dict[str, str]) -> str | None:
    module = edge.module
    if module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        remainder = module.lstrip(".")
        package = _package_of(edge.source_path)
        parts = package.split(".") if package else []
        # level 1 == current package, level 2 == parent, ...
        trim = level - 1
        base_parts = parts[: len(parts) - trim] if trim else parts
        base = ".".join([part for part in base_parts if part])
        candidate = f"{base}.{remainder}" if base and remainder else (base or remainder)
    else:
        candidate = module

    for name in _candidates(candidate, edge.symbol):
        if name in by_module:
            return by_module[name]
        if name in alias:
            return alias[name]
    return None


def _candidates(module: str, symbol: str | None) -> list[str]:
    names = []
    if symbol and module:
        names.append(f"{module}.{symbol}")
    if module:
        names.append(module)
    return names


@dataclass(slots=True)
class DependencyGraph:
    """Bidirectional import graph plus a best-effort call graph over chunks."""

    imports: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    imported_by: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    edges: list[ImportEdge] = field(default_factory=list)
    calls: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    called_by: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    symbol_index: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    """Bare symbol name -> chunk keys defining it."""

    @classmethod
    def build(cls, edges: list[ImportEdge], chunks: list[CodeChunk]) -> DependencyGraph:
        graph = cls(
            imports=defaultdict(set),
            imported_by=defaultdict(set),
            edges=list(edges),
            calls=defaultdict(set),
            called_by=defaultdict(set),
            symbol_index=defaultdict(set),
        )
        for edge in edges:
            if edge.target_path and edge.target_path != edge.source_path:
                graph.imports[edge.source_path].add(edge.target_path)
                graph.imported_by[edge.target_path].add(edge.source_path)

        for chunk in chunks:
            bare = chunk.symbol.rpartition(".")[2]
            if bare and not bare.startswith("<"):
                graph.symbol_index[bare].add(chunk.key)

        # Resolve call names to defining chunks. A call is only linked when the
        # callee is visible: same file, or a file this one imports. That keeps the
        # graph precise rather than linking every ``run()`` in the repository.
        chunk_by_key = {chunk.key: chunk for chunk in chunks}
        for chunk in chunks:
            visible = {chunk.path} | graph.imports.get(chunk.path, set())
            for call in chunk.calls:
                bare = call.rpartition(".")[2]
                for candidate_key in graph.symbol_index.get(bare, set()):
                    candidate = chunk_by_key.get(candidate_key)
                    if candidate is None or candidate.key == chunk.key:
                        continue
                    if candidate.path in visible:
                        graph.calls[chunk.key].add(candidate.key)
                        graph.called_by[candidate.key].add(chunk.key)
        return graph

    # ------------------------------------------------------------------ queries
    def file_neighbors(self, path: str, depth: int = 1) -> dict[str, int]:
        """Files within ``depth`` import hops, mapped to their distance."""
        seen: dict[str, int] = {}
        frontier = {path}
        for distance in range(1, depth + 1):
            nxt: set[str] = set()
            for node in frontier:
                nxt |= self.imports.get(node, set())
                nxt |= self.imported_by.get(node, set())
            nxt -= {path}
            nxt -= set(seen)
            for node in nxt:
                seen[node] = distance
            frontier = nxt
            if not frontier:
                break
        return seen

    def callers_of(self, chunk_key: str) -> set[str]:
        return set(self.called_by.get(chunk_key, set()))

    def callees_of(self, chunk_key: str) -> set[str]:
        return set(self.calls.get(chunk_key, set()))

    def annotate_chunks(self, chunks: list[CodeChunk]) -> list[CodeChunk]:
        """Write resolved caller/callee keys onto the chunks.

        ``chunk.calls`` is left untouched -- it holds the raw names the graph is
        built from, so rebuilding the graph from annotated (or persisted) chunks
        produces exactly the same edges.
        """
        return [
            chunk.model_copy(
                update={
                    "calls_resolved": sorted(self.calls.get(chunk.key, set())),
                    "called_by": sorted(self.called_by.get(chunk.key, set())),
                }
            )
            for chunk in chunks
        ]

    def stats(self) -> dict[str, int]:
        return {
            "files_with_imports": len(self.imports),
            "import_edges": len(self.edges),
            "resolved_import_edges": sum(1 for edge in self.edges if edge.resolved),
            "call_edges": sum(len(values) for values in self.calls.values()),
            "indexed_symbols": len(self.symbol_index),
        }
