"""Embedding providers.

The default provider is local, deterministic and free: a hashed bag-of-features
embedding over code-aware tokens. It is genuinely weaker than a trained model,
and this module says so rather than pretending otherwise -- but it needs no API
key, produces stable vectors across runs (which matters for reproducible
benchmarks) and, combined with the lexical and graph signals in the retriever,
is good enough to locate a symbol named in an issue.

Set ``PATCHPILOT_EMBEDDING_PROVIDER=openai`` with a base URL and key to use any
OpenAI-compatible embedding endpoint, including a local one such as Ollama or
LM Studio.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

import httpx
from patchpilot_core.config import Settings
from patchpilot_core.errors import ConfigurationError, ModelAdapterError

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\sA-Za-z0-9_]")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_PYTHON_STOPWORDS = frozenset(
    {
        "self",
        "def",
        "return",
        "import",
        "from",
        "the",
        "a",
        "an",
        "is",
        "if",
        "else",
        "for",
        "in",
        "and",
        "or",
        "not",
        "none",
        "true",
        "false",
        "as",
    }
)


def code_tokens(text: str) -> list[str]:
    """Tokenise source or prose into identifier-aware terms.

    ``parse_config_file`` yields ``parse_config_file``, ``parse``, ``config``,
    ``file`` so that an issue saying "config parsing" matches the symbol.
    """
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        lowered = raw.lower()
        if len(lowered) < 2 or lowered in _PYTHON_STOPWORDS:
            continue
        tokens.append(lowered)
        parts = [part for part in _CAMEL_RE.sub(" ", raw).replace("_", " ").split() if part]
        if len(parts) > 1:
            tokens.extend(part.lower() for part in parts if len(part) > 1)
    return tokens


class HashEmbedder:
    """Deterministic local embeddings; implements ``EmbeddingProvider``."""

    name = "hash"

    def __init__(self, dimension: int = 256) -> None:
        if dimension < 16:
            raise ConfigurationError("embedding dimension must be at least 16")
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        counts = Counter(code_tokens(text))
        vector = [0.0] * self.dimension
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            # Sub-linear term weighting, as in classic tf-idf style scoring.
            vector[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class OpenAICompatEmbedder:
    """Any OpenAI-compatible ``/embeddings`` endpoint (OpenAI, Ollama, vLLM, ...)."""

    name = "openai"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None,
        dimension: int,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dimension = dimension
        self.timeout = timeout

    def _post(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = httpx.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": texts},
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise ModelAdapterError(
                f"embedding request to {self.base_url} failed: {exc}",
                remediation=(
                    "Check PATCHPILOT_EMBEDDING_BASE_URL / PATCHPILOT_EMBEDDING_API_KEY, "
                    "or set PATCHPILOT_EMBEDDING_PROVIDER=hash to run fully offline."
                ),
            ) from exc
        try:
            return [item["embedding"] for item in payload["data"]]
        except (KeyError, TypeError) as exc:
            raise ModelAdapterError(
                f"embedding endpoint returned an unexpected payload: {payload!r:.200}"
            ) from exc

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        batch_size = 64
        for start in range(0, len(texts), batch_size):
            vectors.extend(self._post(texts[start : start + batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._post([text])[0]


def build_embedder(settings: Settings) -> HashEmbedder | OpenAICompatEmbedder:
    """Factory driven purely by configuration; falls back to local on misconfig."""
    if settings.embedding_provider == "openai":
        base_url = settings.embedding_base_url or settings.openai_base_url
        api_key = settings.embedding_api_key or settings.openai_api_key
        if not base_url:
            raise ConfigurationError(
                "embedding_provider=openai requires PATCHPILOT_EMBEDDING_BASE_URL",
                remediation="Set the base URL, or use PATCHPILOT_EMBEDDING_PROVIDER=hash.",
            )
        return OpenAICompatEmbedder(
            settings.embedding_model,
            base_url=base_url,
            api_key=api_key,
            dimension=settings.embedding_dim,
        )
    return HashEmbedder(settings.embedding_dim)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
