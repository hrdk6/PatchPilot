"""Structural repository indexing and explainable hybrid retrieval."""

from __future__ import annotations

from .embeddings import HashEmbedder, OpenAICompatEmbedder, build_embedder, code_tokens
from .gitignore import GitignoreMatcher
from .graph import DependencyGraph, module_name_for_path, resolve_import_edges
from .indexer import IndexCache, RepositoryIndex, RepositoryIndexer, embedding_text
from .python_parser import PythonParser, TextParser
from .registry import parser_for, register_parser, registered_languages
from .retrieval import HybridRetriever
from .scanner import RepositoryScanner, ScanResult, detect_language, read_convention_files
from .vectorstore import LocalVectorStore, QdrantVectorStore, build_vector_store

__all__ = [
    "DependencyGraph",
    "GitignoreMatcher",
    "HashEmbedder",
    "HybridRetriever",
    "IndexCache",
    "LocalVectorStore",
    "OpenAICompatEmbedder",
    "PythonParser",
    "QdrantVectorStore",
    "RepositoryIndex",
    "RepositoryIndexer",
    "RepositoryScanner",
    "ScanResult",
    "TextParser",
    "build_embedder",
    "build_vector_store",
    "code_tokens",
    "detect_language",
    "embedding_text",
    "module_name_for_path",
    "parser_for",
    "read_convention_files",
    "register_parser",
    "registered_languages",
    "resolve_import_edges",
]
