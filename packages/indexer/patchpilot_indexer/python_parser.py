"""Python structural parser built on the standard library ``ast`` module.

Chunking is symbol-aligned, not token-windowed: the unit of retrieval is a
function, method, class body or module preamble, with its decorators, signature
and docstring intact. That is what makes a retrieved chunk something a model can
actually patch, and what lets the UI say "we included ``calc.py::add``" instead
of "we included bytes 4000-6000".

``tree-sitter`` is the natural choice for languages without a first-class Python
parser; :class:`PythonParser` implements the same ``CodeParser`` protocol, so a
tree-sitter backed ``TypeScriptParser`` drops straight into the registry.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

from patchpilot_core.enums import SymbolType
from patchpilot_core.models import CodeChunk, FileRecord, ImportEdge, SymbolRef

from .scanner import looks_like_test

MIN_CHUNK_CHARS = 12
MAX_CHUNK_CHARS = 8_000


def chunk_identifier(repo_sha: str, path: str, symbol: str, start: int, end: int) -> str:
    digest = hashlib.sha1(f"{repo_sha}:{path}:{symbol}:{start}:{end}".encode()).hexdigest()
    return digest[:24]


@dataclass(slots=True)
class _Definition:
    node: ast.AST
    qualified_name: str
    symbol_type: SymbolType
    start_line: int
    end_line: int


def _decorated_start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", None) or []
    lines = [getattr(d, "lineno", None) for d in decorators]
    own = getattr(node, "lineno", 1)
    candidates = [line for line in lines if line] + [own]
    return min(candidates)


def _dotted_name(node: ast.AST) -> str:
    """Render ``a.b.c`` from an attribute/name expression; ``""`` when not a name."""
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


class _CallCollector(ast.NodeVisitor):
    """Collects call targets inside a subtree without descending into nested defs."""

    def __init__(self, root: ast.AST) -> None:
        self.root = root
        self.calls: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted_name(node.func)
        if name:
            self.calls.append(name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is not self.root:
            return
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is not self.root:
            return
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node is not self.root:
            return
        self.generic_visit(node)


class PythonParser:
    """Implements the ``CodeParser`` protocol for Python sources."""

    language: str = "python"
    extensions: tuple[str, ...] = (".py", ".pyi")

    # ------------------------------------------------------------------ parse
    def parse(self, path: str, source: str) -> tuple[list[SymbolRef], list[ImportEdge], FileRecord]:
        record = FileRecord(
            path=path,
            language=self.language,
            size_bytes=len(source.encode("utf-8")),
            sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            line_count=source.count("\n") + (0 if source.endswith("\n") else 1),
            is_test=looks_like_test(path),
        )
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            record.parse_error = f"{exc.msg} (line {exc.lineno})"
            return [], [], record

        definitions = self._definitions(tree)
        symbols = [
            SymbolRef(
                path=path,
                qualified_name=definition.qualified_name,
                symbol_type=definition.symbol_type,
                start_line=definition.start_line,
                end_line=definition.end_line,
            )
            for definition in definitions
        ]
        return symbols, self._imports(path, tree), record

    # ----------------------------------------------------------------- chunks
    def chunk(self, path: str, source: str, repo_sha: str) -> list[CodeChunk]:
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            return self._fallback_chunks(path, source, repo_sha)

        lines = source.splitlines()
        is_test = looks_like_test(path)
        module_imports = [edge.module for edge in self._imports(path, tree)]
        chunks: list[CodeChunk] = []

        definitions = self._definitions(tree)
        covered: set[int] = set()
        for definition in definitions:
            if definition.symbol_type is SymbolType.CLASS:
                # The class chunk covers only the header/attribute region; its methods
                # are separate chunks so retrieval can target one method precisely.
                continue
            covered.update(range(definition.start_line, definition.end_line + 1))

        for definition in definitions:
            body = self._slice(lines, definition.start_line, definition.end_line)
            if definition.symbol_type is SymbolType.CLASS:
                body = self._class_header(lines, definition, covered)
            if len(body.strip()) < MIN_CHUNK_CHARS:
                continue
            docstring = None
            if isinstance(
                definition.node,
                ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module,
            ):
                docstring = ast.get_docstring(definition.node)
            collector = _CallCollector(definition.node)
            collector.visit(definition.node)
            chunks.append(
                CodeChunk(
                    chunk_id=chunk_identifier(
                        repo_sha,
                        path,
                        definition.qualified_name,
                        definition.start_line,
                        definition.end_line,
                    ),
                    repo_sha=repo_sha,
                    path=path,
                    language=self.language,
                    symbol=definition.qualified_name,
                    symbol_type=definition.symbol_type,
                    start_line=definition.start_line,
                    end_line=definition.end_line,
                    content=body[:MAX_CHUNK_CHARS],
                    docstring=docstring,
                    imports=module_imports,
                    calls=sorted(set(collector.calls)),
                    is_test=is_test,
                    token_estimate=max(1, len(body) // 4),
                )
            )

        preamble = self._module_preamble(lines, covered)
        if len(preamble.strip()) >= MIN_CHUNK_CHARS:
            end_line = max(
                (index for index in range(1, len(lines) + 1) if index not in covered),
                default=len(lines),
            )
            chunks.insert(
                0,
                CodeChunk(
                    chunk_id=chunk_identifier(repo_sha, path, "<module>", 1, end_line),
                    repo_sha=repo_sha,
                    path=path,
                    language=self.language,
                    symbol="<module>",
                    symbol_type=SymbolType.MODULE,
                    start_line=1,
                    end_line=end_line,
                    content=preamble[:MAX_CHUNK_CHARS],
                    docstring=ast.get_docstring(tree),
                    imports=module_imports,
                    calls=[],
                    is_test=is_test,
                    token_estimate=max(1, len(preamble) // 4),
                ),
            )
        return chunks

    # ----------------------------------------------------------------- helpers
    def _definitions(self, tree: ast.Module) -> list[_Definition]:
        found: list[_Definition] = []

        def walk(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    qualified = f"{prefix}{child.name}"
                    found.append(
                        _Definition(
                            node=child,
                            qualified_name=qualified,
                            symbol_type=SymbolType.CLASS,
                            start_line=_decorated_start(child),
                            end_line=child.end_lineno or child.lineno,
                        )
                    )
                    walk(child, f"{qualified}.")
                elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    qualified = f"{prefix}{child.name}"
                    found.append(
                        _Definition(
                            node=child,
                            qualified_name=qualified,
                            symbol_type=(SymbolType.METHOD if prefix else SymbolType.FUNCTION),
                            start_line=_decorated_start(child),
                            end_line=child.end_lineno or child.lineno,
                        )
                    )
                    walk(child, f"{qualified}.")

        walk(tree, "")
        return found

    def _imports(self, path: str, tree: ast.Module) -> list[ImportEdge]:
        edges: list[ImportEdge] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append(
                        ImportEdge(
                            source_path=path,
                            target_path=None,
                            module=alias.name,
                            symbol=None,
                            line=node.lineno,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                module = "." * (node.level or 0) + (node.module or "")
                for alias in node.names:
                    edges.append(
                        ImportEdge(
                            source_path=path,
                            target_path=None,
                            module=module,
                            symbol=alias.name,
                            line=node.lineno,
                        )
                    )
        return edges

    @staticmethod
    def _slice(lines: list[str], start: int, end: int) -> str:
        return "\n".join(lines[max(start - 1, 0) : end])

    @staticmethod
    def _class_header(lines: list[str], definition: _Definition, covered: set[int]) -> str:
        """Class signature, docstring and class-level attributes, minus its methods."""
        kept: list[str] = []
        for number in range(definition.start_line, definition.end_line + 1):
            if number in covered:
                continue
            kept.append(lines[number - 1])
        return "\n".join(kept).rstrip()

    @staticmethod
    def _module_preamble(lines: list[str], covered: set[int]) -> str:
        kept = [line for number, line in enumerate(lines, start=1) if number not in covered]
        return "\n".join(kept).rstrip()

    def _fallback_chunks(self, path: str, source: str, repo_sha: str) -> list[CodeChunk]:
        """A file that will not parse still deserves to be retrievable."""
        content = source[:MAX_CHUNK_CHARS]
        line_count = source.count("\n") + 1
        return [
            CodeChunk(
                chunk_id=chunk_identifier(repo_sha, path, "<unparsed>", 1, line_count),
                repo_sha=repo_sha,
                path=path,
                language=self.language,
                symbol="<unparsed>",
                symbol_type=SymbolType.MODULE,
                start_line=1,
                end_line=line_count,
                content=content,
                imports=[],
                calls=[],
                is_test=looks_like_test(path),
                token_estimate=max(1, len(content) // 4),
            )
        ]


class TextParser:
    """Fallback parser for non-code files worth retrieving (docs, config).

    It produces one chunk per file so that README/pyproject content can still be
    surfaced as repository conventions and matched lexically.
    """

    language: str = "text"
    extensions: tuple[str, ...] = (
        ".md",
        ".rst",
        ".txt",
        ".toml",
        ".cfg",
        ".ini",
        ".yaml",
        ".yml",
        ".json",
    )

    def parse(self, path: str, source: str) -> tuple[list[SymbolRef], list[ImportEdge], FileRecord]:
        record = FileRecord(
            path=path,
            language="text",
            size_bytes=len(source.encode("utf-8")),
            sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            line_count=source.count("\n") + 1,
            is_test=False,
        )
        return [], [], record

    def chunk(self, path: str, source: str, repo_sha: str) -> list[CodeChunk]:
        content = source[:MAX_CHUNK_CHARS]
        if len(content.strip()) < MIN_CHUNK_CHARS:
            return []
        line_count = source.count("\n") + 1
        return [
            CodeChunk(
                chunk_id=chunk_identifier(repo_sha, path, "<file>", 1, line_count),
                repo_sha=repo_sha,
                path=path,
                language="text",
                symbol="<file>",
                symbol_type=SymbolType.MODULE,
                start_line=1,
                end_line=line_count,
                content=content,
                token_estimate=max(1, len(content) // 4),
            )
        ]
