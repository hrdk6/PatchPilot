"""Indexing: gitignore matching, scanning, AST parsing, graph construction."""

from __future__ import annotations

from pathlib import Path

import pytest
from patchpilot_core.enums import SymbolType
from patchpilot_indexer import (
    DependencyGraph,
    GitignoreMatcher,
    HashEmbedder,
    IndexCache,
    LocalVectorStore,
    PythonParser,
    RepositoryIndex,
    RepositoryIndexer,
    RepositoryScanner,
    code_tokens,
    module_name_for_path,
    resolve_import_edges,
)
from patchpilot_indexer.scanner import detect_language, is_binary, looks_like_test


class TestGitignore:
    def test_plain_and_wildcard_patterns(self) -> None:
        matcher = GitignoreMatcher(["*.log", "secret.txt"])
        assert matcher.match("app.log")
        assert matcher.match("nested/deep/app.log")
        assert matcher.match("secret.txt")
        assert not matcher.match("app.py")

    def test_directory_only_pattern_ignores_contents_not_a_like_named_file(self) -> None:
        matcher = GitignoreMatcher(["build/"])
        assert matcher.match("build/main.o")
        assert matcher.match("build", is_dir=True)
        assert not matcher.match("build")

    def test_anchored_pattern_only_matches_at_the_root(self) -> None:
        matcher = GitignoreMatcher(["/dist"])
        assert matcher.match("dist/index.js")
        assert not matcher.match("packages/dist/index.js")

    def test_negation_reinstates_a_file(self) -> None:
        matcher = GitignoreMatcher([".env", ".env.*", "!.env.example"])
        assert matcher.match(".env")
        assert matcher.match(".env.production")
        assert not matcher.match(".env.example")

    def test_double_star_crosses_directories(self) -> None:
        matcher = GitignoreMatcher(["docs/**/*.tmp"])
        assert matcher.match("docs/a/b/note.tmp")
        assert not matcher.match("docs/a/b/note.md")

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        matcher = GitignoreMatcher(["# a comment", "", "   ", "*.pyc"])
        assert len(matcher) == 1
        assert matcher.match("x.pyc")

    def test_loads_patterns_from_the_repository(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
        matcher = GitignoreMatcher.from_repository(tmp_path)
        assert matcher.match("x.log")
        assert matcher.match("build/x.o")


class TestScanner:
    def test_skips_caches_secrets_and_binaries(self, fixture_repo: Path) -> None:
        (fixture_repo / "__pycache__").mkdir()
        (fixture_repo / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01binary")
        (fixture_repo / ".env").write_text("SECRET=hunter2", encoding="utf-8")
        (fixture_repo / "blob.bin").write_bytes(bytes(range(256)) * 20)

        result = RepositoryScanner().scan(fixture_repo)
        paths = result.paths()

        assert "calc_service/operations.py" in paths
        assert not any(path.startswith("__pycache__") for path in paths)
        assert ".env" not in paths
        assert "blob.bin" not in paths
        assert result.skipped

    def test_respects_the_size_ceiling(self, fixture_repo: Path) -> None:
        (fixture_repo / "big.py").write_text("x = 1\n" * 50_000, encoding="utf-8")
        result = RepositoryScanner(max_file_bytes=1000).scan(fixture_repo)
        assert "big.py" not in result.paths()
        assert result.skipped["too-large"] >= 1

    def test_stops_at_max_files_and_says_so(self, fixture_repo: Path) -> None:
        result = RepositoryScanner(max_files=2).scan(fixture_repo)
        assert len(result.files) == 2
        assert result.truncated

    def test_language_and_test_detection(self) -> None:
        assert detect_language("a/b.py") == "python"
        assert detect_language("a/b.tsx") == "typescript"
        assert detect_language("a/b.unknown") == "other"
        assert looks_like_test("tests/test_x.py")
        assert looks_like_test("pkg/x_test.py")
        assert not looks_like_test("pkg/contest.py")

    def test_binary_sniffing(self) -> None:
        assert is_binary(b"\x00\x01\x02")
        assert not is_binary(b"def add(a, b):\n    return a + b\n")
        assert not is_binary(b"")


class TestPythonParser:
    SOURCE = '''\
"""Module docstring."""

import os
from pkg.helpers import helper

CONSTANT = 3


class Greeter:
    """A greeter."""

    prefix = "hi"

    def greet(self, name: str) -> str:
        """Greet someone."""
        return helper(f"{self.prefix} {name}")


@staticmethod
def free_function(value):
    return os.path.join(value, "x")
'''

    def test_extracts_symbols_with_line_ranges(self) -> None:
        symbols, _, record = PythonParser().parse("pkg/mod.py", self.SOURCE)
        by_name = {symbol.qualified_name: symbol for symbol in symbols}

        assert by_name["Greeter"].symbol_type is SymbolType.CLASS
        assert by_name["Greeter.greet"].symbol_type is SymbolType.METHOD
        assert by_name["free_function"].symbol_type is SymbolType.FUNCTION
        assert by_name["Greeter.greet"].start_line < by_name["Greeter.greet"].end_line
        assert record.parse_error is None

    def test_includes_decorators_in_the_symbol_range(self) -> None:
        symbols, _, _ = PythonParser().parse("pkg/mod.py", self.SOURCE)
        decorated = next(s for s in symbols if s.qualified_name == "free_function")
        decorator_line = self.SOURCE.splitlines().index("@staticmethod") + 1
        assert decorated.start_line == decorator_line

    def test_extracts_imports_including_relative(self) -> None:
        _, edges, _ = PythonParser().parse(
            "pkg/mod.py", "from . import sibling\nfrom ..up import thing\nimport os\n"
        )
        modules = {(edge.module, edge.symbol) for edge in edges}
        assert (".", "sibling") in modules
        assert ("..up", "thing") in modules
        assert ("os", None) in modules

    def test_chunks_are_symbol_aligned_and_capture_calls(self) -> None:
        chunks = PythonParser().chunk("pkg/mod.py", self.SOURCE, "sha")
        by_symbol = {chunk.symbol: chunk for chunk in chunks}

        assert "<module>" in by_symbol
        assert "Greeter.greet" in by_symbol
        assert "helper" in by_symbol["Greeter.greet"].calls
        assert by_symbol["Greeter.greet"].docstring == "Greet someone."
        assert "def greet" in by_symbol["Greeter.greet"].content
        # The class chunk keeps the attributes but not the method body.
        assert "prefix" in by_symbol["Greeter"].content
        assert "return helper" not in by_symbol["Greeter"].content

    def test_module_chunk_holds_imports_and_constants(self) -> None:
        chunks = PythonParser().chunk("pkg/mod.py", self.SOURCE, "sha")
        module_chunk = next(chunk for chunk in chunks if chunk.symbol == "<module>")
        assert "CONSTANT = 3" in module_chunk.content
        assert "import os" in module_chunk.content

    def test_a_syntax_error_is_recorded_not_raised(self) -> None:
        symbols, edges, record = PythonParser().parse("bad.py", "def broken(:\n")
        assert symbols == [] and edges == []
        assert record.parse_error is not None

    def test_unparseable_files_are_still_retrievable(self) -> None:
        chunks = PythonParser().chunk("bad.py", "def broken(:\n", "sha")
        assert len(chunks) == 1
        assert chunks[0].symbol == "<unparsed>"

    def test_chunk_ids_are_stable_across_runs(self) -> None:
        first = PythonParser().chunk("pkg/mod.py", self.SOURCE, "sha")
        second = PythonParser().chunk("pkg/mod.py", self.SOURCE, "sha")
        assert [c.chunk_id for c in first] == [c.chunk_id for c in second]

    def test_chunk_ids_change_with_the_repo_sha(self) -> None:
        first = PythonParser().chunk("pkg/mod.py", self.SOURCE, "sha-a")
        second = PythonParser().chunk("pkg/mod.py", self.SOURCE, "sha-b")
        assert {c.chunk_id for c in first}.isdisjoint({c.chunk_id for c in second})


class TestDependencyGraph:
    def test_module_naming(self) -> None:
        assert module_name_for_path("pkg/sub/mod.py") == "pkg.sub.mod"
        assert module_name_for_path("pkg/__init__.py") == "pkg"
        assert module_name_for_path("README.md") is None

    def test_resolves_absolute_and_relative_imports(self) -> None:
        parser = PythonParser()
        _, edges_a, _ = parser.parse("pkg/a.py", "from pkg.b import thing\n")
        _, edges_b, _ = parser.parse("pkg/sub/c.py", "from .. import a\nimport json\n")
        resolved = resolve_import_edges(
            edges_a + edges_b, ["pkg/a.py", "pkg/b.py", "pkg/sub/c.py", "pkg/__init__.py"]
        )
        targets = {(edge.source_path, edge.module, edge.target_path) for edge in resolved}
        assert ("pkg/a.py", "pkg.b", "pkg/b.py") in targets
        assert any(
            edge.source_path == "pkg/sub/c.py" and edge.module == ".." and edge.resolved
            for edge in resolved
        )
        assert any(edge.module == "json" and not edge.resolved for edge in resolved)

    def test_builds_a_bidirectional_import_graph(self) -> None:
        parser = PythonParser()
        _, edges, _ = parser.parse("pkg/a.py", "from pkg.b import thing\n")
        graph = DependencyGraph.build(resolve_import_edges(edges, ["pkg/a.py", "pkg/b.py"]), [])
        assert graph.imports["pkg/a.py"] == {"pkg/b.py"}
        assert graph.imported_by["pkg/b.py"] == {"pkg/a.py"}
        assert graph.file_neighbors("pkg/b.py") == {"pkg/a.py": 1}

    def test_links_calls_only_to_visible_definitions(self) -> None:
        parser = PythonParser()
        caller_src = "from pkg.b import helper\n\n\ndef run():\n    return helper(1)\n"
        callee_src = "def helper(value):\n    return value\n"
        other_src = "def helper(value):\n    return 0\n"

        _, edges, _ = parser.parse("pkg/a.py", caller_src)
        chunks = (
            parser.chunk("pkg/a.py", caller_src, "sha")
            + parser.chunk("pkg/b.py", callee_src, "sha")
            + parser.chunk("pkg/unrelated.py", other_src, "sha")
        )
        resolved = resolve_import_edges(edges, ["pkg/a.py", "pkg/b.py", "pkg/unrelated.py"])
        graph = DependencyGraph.build(resolved, chunks)

        assert graph.callees_of("pkg/a.py::run") == {"pkg/b.py::helper"}
        assert graph.callers_of("pkg/b.py::helper") == {"pkg/a.py::run"}
        # The identically-named function in an unimported module is not linked.
        assert graph.callers_of("pkg/unrelated.py::helper") == set()

    def test_annotation_is_idempotent(self) -> None:
        """Rebuilding the graph from annotated chunks must produce the same edges."""
        parser = PythonParser()
        source = "def helper(v):\n    return v\n\n\ndef run():\n    return helper(1)\n"
        chunks = parser.chunk("pkg/a.py", source, "sha")
        graph = DependencyGraph.build([], chunks)
        annotated = graph.annotate_chunks(chunks)
        rebuilt = DependencyGraph.build([], annotated)
        assert rebuilt.calls == graph.calls
        assert graph.calls["pkg/a.py::run"] == {"pkg/a.py::helper"}


class TestEmbeddings:
    def test_tokenizer_splits_identifiers(self) -> None:
        tokens = code_tokens("parse_config_file and parseConfigFile")
        assert "parse_config_file" in tokens
        assert "config" in tokens
        assert "parseconfigfile" in tokens

    def test_embeddings_are_deterministic_and_normalised(self) -> None:
        embedder = HashEmbedder(64)
        first = embedder.embed_query("percentage zero division")
        second = embedder.embed_query("percentage zero division")
        assert first == second
        assert len(first) == 64
        assert abs(sum(value * value for value in first) - 1.0) < 1e-6

    def test_similar_text_scores_higher_than_unrelated_text(self) -> None:
        from patchpilot_indexer.embeddings import cosine_similarity

        embedder = HashEmbedder(256)
        query = embedder.embed_query("percentage divide by zero guard")
        close = embedder.embed_documents(["def percentage(part, whole): return part / whole"])[0]
        far = embedder.embed_documents(["class HttpServer: def listen(self, port): ..."])[0]
        assert cosine_similarity(query, close) > cosine_similarity(query, far)

    def test_rejects_a_useless_dimension(self) -> None:
        from patchpilot_core.errors import ConfigurationError

        with pytest.raises(ConfigurationError):
            HashEmbedder(4)


class TestLocalVectorStore:
    def test_round_trips_and_ranks_by_similarity(self, tmp_path: Path) -> None:
        store = LocalVectorStore(tmp_path)
        store.ensure_collection(3)
        store.upsert(
            "ns",
            ["a", "b"],
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            [{"path": "a.py"}, {"path": "b.py"}],
        )
        results = store.search("ns", [0.9, 0.1, 0.0], 2)
        assert results[0][0] == "a"
        assert results[0][1] > results[1][1]

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        LocalVectorStore(tmp_path).upsert("ns", ["a"], [[1.0, 0.0]], [{}])
        assert LocalVectorStore(tmp_path).count("ns") == 1

    def test_namespaces_are_isolated(self, tmp_path: Path) -> None:
        store = LocalVectorStore(tmp_path)
        store.upsert("one", ["a"], [[1.0]], [{}])
        store.upsert("two", ["b"], [[1.0]], [{}])
        assert [chunk for chunk, _ in store.search("one", [1.0], 5)] == ["a"]
        store.delete_namespace("one")
        assert store.count("one") == 0
        assert store.count("two") == 1

    def test_mismatched_input_is_rejected(self, tmp_path: Path) -> None:
        from patchpilot_core.errors import RetrievalError

        with pytest.raises(RetrievalError):
            LocalVectorStore(tmp_path).upsert("ns", ["a", "b"], [[1.0]], [{}])


class TestRepositoryIndexer:
    def test_indexes_a_fixture_end_to_end(self, settings, fixture_repo: Path) -> None:
        indexer = RepositoryIndexer(
            settings, embedder=HashEmbedder(128), vector_store=LocalVectorStore(None)
        )
        index = indexer.index(fixture_repo, "sha-test")

        assert index.stats.files_indexed >= 4
        assert index.stats.chunks > 5
        assert index.stats.embedded_chunks == index.stats.chunks
        assert index.stats.resolved_import_edges >= 1
        assert index.chunk_by_key("calc_service/operations.py::percentage") is not None
        assert "README.md" in index.conventions

    def test_index_serialises_and_reloads(self, settings, fixture_repo: Path) -> None:
        indexer = RepositoryIndexer(
            settings, embedder=HashEmbedder(128), vector_store=LocalVectorStore(None)
        )
        index = indexer.index(fixture_repo, "sha-test")
        reloaded = RepositoryIndex.from_dict(index.to_dict())

        assert len(reloaded.chunks) == len(index.chunks)
        assert reloaded.graph.stats() == index.graph.stats()


class TestIndexCache:
    @staticmethod
    def _index(sha: str) -> RepositoryIndex:
        return RepositoryIndex(repo_sha=sha, root=Path("."))

    def test_evicts_the_least_recently_used_index(self) -> None:
        cache = IndexCache(maxsize=2)
        cache["a"] = self._index("a")
        cache["b"] = self._index("b")
        assert cache.get("a") is not None  # reading "a" makes "b" the oldest
        cache["c"] = self._index("c")

        assert set(cache) == {"a", "c"}
        assert cache.get("b") is None
        assert len(cache) == 2

    def test_replacing_an_entry_does_not_grow_the_cache(self) -> None:
        cache = IndexCache(maxsize=2)
        first, second = self._index("a"), self._index("a")
        cache["a"] = first
        cache["a"] = second
        assert len(cache) == 1
        assert cache["a"] is second

    def test_rejects_a_useless_size(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            IndexCache(maxsize=0)
