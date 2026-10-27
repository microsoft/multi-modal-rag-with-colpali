# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Qdrant storage for the version-two, ColQwen-compatible indexing contract."""

import asyncio
import logging
import math
import os
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from tenacity import AsyncRetrying, stop_after_attempt, wait_fixed

from .models import DocumentMetadata, ProcessedPage, generate_page_id

logger = logging.getLogger(__name__)
VECTOR_NAMES = ("hierarchical_pooled", "mean_row_pooled", "mean_column_pooled")


class IncompatibleCollectionError(ValueError):
    """An existing collection needs an explicit rebuild, not an in-place overwrite."""


class QdrantIndex:
    def __init__(self, credential=None, require_endpoint: bool = True):
        self.collection_name = os.getenv("QDRANT_COLLECTION_NAME", "colpali-documents")
        self.dimension = int(os.getenv("COLPALI_EMBEDDING_DIMENSION", "320"))
        self.upsert_batch_size = int(os.getenv("QDRANT_BATCH_SIZE", "20"))
        concurrency = int(os.getenv("QDRANT_MAX_CONCURRENT", "5"))
        if min(self.dimension, self.upsert_batch_size, concurrency) <= 0:
            raise ValueError(
                "Qdrant dimensions, batch size and concurrency must be positive"
            )
        self._upsert_semaphore = asyncio.Semaphore(concurrency)
        endpoint = os.getenv("QDRANT_ENDPOINT")
        if require_endpoint and not endpoint:
            raise ValueError("QDRANT_ENDPOINT is required")
        self.qdrant_client = (
            AsyncQdrantClient(
                url=endpoint,
                api_key=os.getenv("QDRANT_API_KEY"),
                timeout=float(os.getenv("QDRANT_REQUEST_TIMEOUT", "120")),
            )
            if endpoint
            else None
        )

    def _client(self) -> AsyncQdrantClient:
        if self.qdrant_client is None:
            raise RuntimeError("Qdrant client is not configured")
        return self.qdrant_client

    def _get_default_vectors_config(self) -> dict[str, models.VectorParams]:
        config = {}
        for name in VECTOR_NAMES:
            config[name] = models.VectorParams(
                size=self.dimension,
                distance=models.Distance.COSINE,
                memory=models.Memory.COLD,
                multivector_config=models.MultiVectorConfig(
                    comparator=models.MultiVectorComparator.MAX_SIM
                ),
                hnsw_config=models.HnswConfigDiff(m=0, memory=models.Memory.CACHED)
                if name == "hierarchical_pooled"
                else models.HnswConfigDiff(
                    m=16, ef_construct=100, memory=models.Memory.PINNED
                ),
                quantization_config=(
                    models.BinaryQuantization(
                        binary=models.BinaryQuantizationConfig(
                            memory=models.Memory.PINNED
                        )
                    )
                    if name != "hierarchical_pooled"
                    else None
                ),
            )
        return config

    async def initialize(self) -> bool:
        client = self._client()
        if not await client.collection_exists(self.collection_name):
            try:
                await client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=self._get_default_vectors_config(),
                    payload=models.PayloadStorageParams(memory=models.Memory.COLD),
                    id_tracker=models.IdTrackerParams(memory=models.Memory.PINNED),
                )
            except Exception:
                # Another replica may have created it; still validate its schema.
                if not await client.collection_exists(self.collection_name):
                    raise
        info = await client.get_collection(self.collection_name)
        vectors = info.config.params.vectors
        if not isinstance(vectors, dict) or set(vectors) != set(VECTOR_NAMES):
            raise IncompatibleCollectionError(
                "Incompatible vector names. Reindex into a new collection and switch "
                "QDRANT_COLLECTION_NAME in both the processor and agent."
            )
        for name, vector in vectors.items():
            if (
                vector.size != self.dimension
                or vector.distance != models.Distance.COSINE
                or vector.multivector_config is None
                or vector.multivector_config.comparator
                != models.MultiVectorComparator.MAX_SIM
            ):
                raise IncompatibleCollectionError(
                    f"Incompatible vector schema for {name}"
                )
        updates = {}
        for name, vector in vectors.items():
            update = models.VectorParamsDiff()
            if vector.memory != models.Memory.COLD:
                update.memory = models.Memory.COLD
            graph_memory = (
                models.Memory.CACHED
                if name == "hierarchical_pooled"
                else models.Memory.PINNED
            )
            if vector.hnsw_config is None or vector.hnsw_config.memory != graph_memory:
                update.hnsw_config = models.HnswConfigDiff(memory=graph_memory)
            quantization = vector.quantization_config or info.config.quantization_config
            if (
                name != "hierarchical_pooled"
                and isinstance(quantization, models.BinaryQuantization)
                and quantization.binary.memory != models.Memory.PINNED
            ):
                # Preserve the encoding and query encoding of existing binary copies.
                update.quantization_config = models.BinaryQuantization(
                    binary=quantization.binary.model_copy(
                        update={"memory": models.Memory.PINNED}
                    )
                )
            if update.model_dump(exclude_none=True):
                updates[name] = update
        params = info.config.params
        collection_updates = models.CollectionParamsDiff()
        # Omitted collection placement defaults to cold payloads and pinned IDs.
        payload_memory = params.payload.memory if params.payload is not None else None
        if payload_memory not in (None, models.Memory.COLD) or (
            payload_memory is None and params.on_disk_payload is False
        ):
            collection_updates.payload = models.PayloadStorageParams(
                memory=models.Memory.COLD
            )
        if params.id_tracker is not None and params.id_tracker.memory not in (
            None,
            models.Memory.PINNED,
        ):
            collection_updates.id_tracker = models.IdTrackerParams(
                memory=models.Memory.PINNED
            )
        if updates or collection_updates.model_dump(exclude_none=True):
            if not await client.update_collection(
                collection_name=self.collection_name,
                vectors_config=updates or None,
                collection_params=collection_updates,
            ):
                raise RuntimeError("Qdrant refused memory placement reconciliation")
        for field in ("document_id", "processing_fingerprint"):
            existing_index = info.payload_schema.get(field)
            schema = (
                existing_index.params
                if existing_index is not None
                and isinstance(existing_index.params, models.KeywordIndexParams)
                else models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD)
            )
            await client.create_payload_index(
                collection_name=self.collection_name,
                field_name=field,
                field_schema=schema.model_copy(update={"memory": models.Memory.PINNED}),
                wait=True,
            )
        return True

    @staticmethod
    def _filter(document_id: str) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=document_id)
                )
            ]
        )

    def _build_payload(self, page: ProcessedPage) -> dict[str, Any]:
        return {
            "document_id": page.document_id,
            "page_id": page.page_id,
            "page_number": page.page_number,
            "page_count": page.page_count,
            "text_content": page.text_content,
            "indexed_at": page.indexed_at.isoformat(),
            "file_hash": page.file_hash,
            "indexed_with_file_hash": page.indexed_with_file_hash,
            "page_hash": page.page_hash,
            "effective_dpi": page.effective_dpi,
            "processing_fingerprint": page.processing_fingerprint,
            "source": {
                **page.source.model_dump(),
                "page_image_url": page.page_image_url,
            },
            "metadata": page.metadata.model_dump(mode="json"),
        }

    def _validate_vectors(self, vectors: dict) -> None:
        if set(vectors) != set(VECTOR_NAMES):
            raise ValueError("Embedding response must contain every named vector")
        for name, rows in vectors.items():
            if not rows or any(
                len(row) != self.dimension
                or any(
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    for v in row
                )
                for row in rows
            ):
                raise ValueError(f"Invalid vector shape or values for {name}")

    async def _retry(self, operation):
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_fixed(0.5),
            reraise=True,
        ):
            with attempt:
                return await operation()

    async def index_embeddings(self, processed_pages: list[ProcessedPage]) -> int:
        client = self._client()
        points = []
        for page in processed_pages:
            self._validate_vectors(page.embeddings)
            points.append(
                models.PointStruct(
                    id=page.page_id,
                    vector=page.embeddings,
                    payload=self._build_payload(page),
                )
            )

        async def upsert(batch):
            async with self._upsert_semaphore:
                await self._retry(
                    lambda: client.upsert(
                        collection_name=self.collection_name,
                        points=batch,
                        wait=True,
                    )
                )

        # Bound both queued work and active requests.
        for start in range(0, len(points), self.upsert_batch_size):
            await upsert(points[start : start + self.upsert_batch_size])
        return len(points)

    async def get_document_state(self, document_id: str) -> dict[int, dict[str, Any]]:
        state = {}
        offset = None
        while True:
            points, offset = await self._client().scroll(
                collection_name=self.collection_name,
                scroll_filter=self._filter(document_id),
                offset=offset,
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                number = payload.get("page_number")
                if isinstance(number, int) and number > 0:
                    if str(point.id) != generate_page_id(
                        document_id, number
                    ) or not isinstance(payload.get("source"), dict):
                        raise IncompatibleCollectionError(
                            "Legacy page identity or payload; rebuild this collection"
                        )
                    if number in state:
                        raise IncompatibleCollectionError(
                            "Duplicate page identities; rebuild this collection"
                        )
                    state[number] = payload
            if offset is None:
                return state

    async def update_page_text(
        self, document_id: str, page_number: int, text: str | None
    ) -> None:
        await self._retry(
            lambda: self._client().set_payload(
                collection_name=self.collection_name,
                payload={"text_content": text},
                points=[generate_page_id(document_id, page_number)],
                wait=True,
            )
        )

    async def complete_document(
        self,
        document_id: str,
        file_hash: str,
        fingerprint: str,
        page_count: int,
        metadata: DocumentMetadata,
    ) -> None:
        await self._retry(
            lambda: self._client().set_payload(
                collection_name=self.collection_name,
                payload={
                    "file_hash": file_hash,
                    "indexed_with_file_hash": file_hash,
                    "processing_fingerprint": fingerprint,
                    "page_count": page_count,
                    "metadata": metadata.model_dump(mode="json"),
                },
                points=self._filter(document_id),
                wait=True,
            )
        )

    async def delete_document_pages(self, document_id: str) -> bool:
        await self._retry(
            lambda: self._client().delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=self._filter(document_id)),
                wait=True,
            )
        )
        return True

    async def delete_orphan_pages(
        self, document_id: str, max_valid_page_number: int
    ) -> bool:
        selector = self._filter(document_id)
        selector.must.append(
            models.FieldCondition(
                key="page_number", range=models.Range(gt=max_valid_page_number)
            )
        )
        await self._retry(
            lambda: self._client().delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=selector),
                wait=True,
            )
        )
        return True

    async def close(self):
        if self.qdrant_client is not None:
            await self.qdrant_client.close()
