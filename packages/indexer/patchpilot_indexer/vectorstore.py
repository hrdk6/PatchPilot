"""Vector storage.

Two implementations of the ``VectorStore`` protocol:

* :class:`QdrantVectorStore` -- the production path, started by Docker Compose.
* :class:`LocalVectorStore` -- an exact brute-force index persisted as JSON.

The local store exists so that ``pytest`` and a bare ``uvicorn`` work with no
services running at all. It is exact (no ANN approximation) and therefore a
useful correctness oracle for the Qdrant path, but it is O(n) per query and is
not intended for large repositories.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from patchpilot_core.config import Settings
from patchpilot_core.errors import RetrievalError

from .embeddings import cosine_similarity


class LocalVectorStore:
    """Exact in-process vector index with optional JSON persistence."""

    name = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self._dimension: int | None = None
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- persistence
    def _file(self, namespace: str) -> Path | None:
        if self.root is None:
            return None
        safe = namespace.replace("/", "_").replace(":", "_")
        return self.root / f"{safe}.json"

    def _load(self, namespace: str) -> dict[str, Any]:
        with self._lock:
            if namespace in self._data:
                return self._data[namespace]
            file = self._file(namespace)
            if file is not None and file.exists():
                try:
                    self._data[namespace] = json.loads(file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    self._data[namespace] = {}
            else:
                self._data[namespace] = {}
            return self._data[namespace]

    def _flush(self, namespace: str) -> None:
        file = self._file(namespace)
        if file is None:
            return
        file.write_text(
            json.dumps(self._data.get(namespace, {}), separators=(",", ":")),
            encoding="utf-8",
        )

    # ----------------------------------------------------------------- protocol
    def ensure_collection(self, dimension: int) -> None:
        self._dimension = dimension

    def upsert(
        self,
        namespace: str,
        chunk_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, object]],
    ) -> None:
        if not (len(chunk_ids) == len(vectors) == len(payloads)):
            raise RetrievalError("upsert received mismatched ids/vectors/payloads")
        with self._lock:
            bucket = self._load(namespace)
            for chunk_id, vector, payload in zip(chunk_ids, vectors, payloads, strict=True):
                bucket[chunk_id] = {"vector": vector, "payload": payload}
            self._flush(namespace)

    def search(self, namespace: str, vector: list[float], limit: int) -> list[tuple[str, float]]:
        bucket = self._load(namespace)
        scored = [
            (chunk_id, cosine_similarity(vector, entry["vector"]))
            for chunk_id, entry in bucket.items()
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    def delete_namespace(self, namespace: str) -> None:
        with self._lock:
            self._data.pop(namespace, None)
            file = self._file(namespace)
            if file is not None and file.exists():
                file.unlink()

    def count(self, namespace: str) -> int:
        return len(self._load(namespace))


class QdrantVectorStore:
    """Qdrant-backed store. One collection, one payload field per namespace."""

    name = "qdrant"

    def __init__(
        self,
        url: str,
        collection: str,
        *,
        api_key: str | None = None,
        timeout: int = 30,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RetrievalError("qdrant-client is not installed") from exc
        self.collection = collection
        self._models = __import__("qdrant_client.models", fromlist=["models"])
        try:
            self.client = QdrantClient(url=url, api_key=api_key, timeout=timeout)
        except Exception as exc:
            raise RetrievalError(
                f"could not connect to Qdrant at {url}: {exc}",
                remediation=(
                    "Start it with `docker compose up -d qdrant`, or unset "
                    "PATCHPILOT_QDRANT_URL to use the local vector store."
                ),
            ) from exc

    @staticmethod
    def _point_id(namespace: str, chunk_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"patchpilot://{namespace}/{chunk_id}"))

    def ensure_collection(self, dimension: int) -> None:
        models = self._models
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
            )
            self.client.create_payload_index(
                collection_name=self.collection,
                field_name="namespace",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    def upsert(
        self,
        namespace: str,
        chunk_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, object]],
    ) -> None:
        models = self._models
        points = [
            models.PointStruct(
                id=self._point_id(namespace, chunk_id),
                vector=vector,
                payload={**payload, "namespace": namespace, "chunk_id": chunk_id},
            )
            for chunk_id, vector, payload in zip(chunk_ids, vectors, payloads, strict=True)
        ]
        for start in range(0, len(points), 256):
            self.client.upsert(
                collection_name=self.collection, points=points[start : start + 256], wait=True
            )

    def search(self, namespace: str, vector: list[float], limit: int) -> list[tuple[str, float]]:
        models = self._models
        response = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=limit,
            with_payload=True,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace))
                ]
            ),
        )
        results: list[tuple[str, float]] = []
        for point in response.points:
            payload = point.payload or {}
            chunk_id = str(payload.get("chunk_id") or point.id)
            results.append((chunk_id, float(point.score)))
        return results

    def delete_namespace(self, namespace: str) -> None:
        models = self._models
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="namespace", match=models.MatchValue(value=namespace)
                        )
                    ]
                )
            ),
            wait=True,
        )


def build_vector_store(settings: Settings) -> LocalVectorStore | QdrantVectorStore:
    """Use Qdrant when configured; otherwise fall back to the local store."""
    if settings.qdrant_url:
        return QdrantVectorStore(
            settings.qdrant_url,
            settings.qdrant_collection,
            api_key=settings.qdrant_api_key,
        )
    return LocalVectorStore(settings.vector_store_root)
