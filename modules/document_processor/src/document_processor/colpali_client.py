# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Pooled inference transport with bounded, lossless batch recovery."""

import asyncio
import base64
import io
import logging
import math
import os

import aiohttp
from PIL import Image

from .models import DocumentPage, EmbedHealthResponse, EmbedRequest, PoolingType
from .page_processing import run_cpu

logger = logging.getLogger(__name__)
VECTOR_FIELDS = {
    "hierarchical_pooled_embeddings": "hierarchical_pooled",
    "mean_row_pooled_embeddings": "mean_row_pooled",
    "mean_column_pooled_embeddings": "mean_column_pooled",
}


class EmbeddingTokenLimitError(RuntimeError):
    def __init__(self, input_tokens: int, max_tokens: int):
        self.input_tokens = input_tokens
        self.max_tokens = max_tokens
        super().__init__(
            f"Embedding requires {input_tokens} tokens; limit is {max_tokens}"
        )


class ColPaliClient:
    def __init__(self, require_endpoint: bool = True, pooling_types=None):
        endpoint = os.getenv("COLPALI_INFERENCE_ENDPOINT")
        if require_endpoint and not endpoint:
            raise ValueError("COLPALI_INFERENCE_ENDPOINT is required")
        self.endpoint_url = endpoint.rstrip("/") if endpoint else None
        self.request_timeout = float(os.getenv("COLPALI_REQUEST_TIMEOUT", "120"))
        self.batch_size = int(os.getenv("COLPALI_BATCH_SIZE", "1"))
        self.max_concurrent = int(os.getenv("COLPALI_MAX_CONCURRENT", "5"))
        self.embedding_dpi = int(os.getenv("COLPALI_EMBEDDING_DPI", "150"))
        self.max_image_pixels = int(os.getenv("COLPALI_MAX_IMAGE_PIXELS", "4194304"))
        self.dimension = int(os.getenv("COLPALI_EMBEDDING_DIMENSION", "320"))
        if (
            min(
                self.batch_size,
                self.max_concurrent,
                self.embedding_dpi,
                self.dimension,
                self.max_image_pixels,
            )
            <= 0
        ):
            raise ValueError(
                "Embedding dimensions, DPI, batch size and concurrency must be positive"
            )
        if not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            raise ValueError("COLPALI_REQUEST_TIMEOUT must be finite and positive")
        self.pooling_types = pooling_types or [
            PoolingType.MEAN_POOLING,
            PoolingType.HIERARCHICAL_POOLING,
        ]
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                connector=aiohttp.TCPConnector(limit=max(32, self.max_concurrent * 4)),
            )
        return self._session

    async def close(self):
        if self._session is not None:
            await self._session.close()

    async def health_check(self) -> EmbedHealthResponse:
        session = await self._get_session()
        async with session.get(f"{self.endpoint_url}/health") as response:
            response.raise_for_status()
            return EmbedHealthResponse(**await response.json())

    def _prepare_payload(self, pages: list[DocumentPage]) -> dict:
        images = []
        for page in pages:
            image = page.image_content
            scale = min(1.0, self.embedding_dpi / page.effective_dpi)
            converted = image.convert("RGB")
            try:
                if scale < 1:
                    resized = converted.resize(
                        (
                            max(1, int(image.width * scale)),
                            max(1, int(image.height * scale)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                    converted.close()
                    converted = resized
                with io.BytesIO() as buffer:
                    converted.save(buffer, format="JPEG", quality=85)
                    images.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
            finally:
                converted.close()
        return EmbedRequest(images=images, pooling_type=self.pooling_types).model_dump(
            mode="json"
        )

    def _validate_response(self, result, count):
        if not isinstance(result, dict):
            raise ValueError("Embedding response must be an object")
        required = set()
        if PoolingType.HIERARCHICAL_POOLING in self.pooling_types:
            required.add("hierarchical_pooled_embeddings")
        if PoolingType.MEAN_POOLING in self.pooling_types:
            required.update(
                ("mean_row_pooled_embeddings", "mean_column_pooled_embeddings")
            )
        if PoolingType.NONE in self.pooling_types:
            required.add("embeddings")
        for key in required:
            values = result.get(key)
            if not isinstance(values, list) or len(values) != count:
                raise ValueError(f"Incomplete embedding response: {key}")
            for rows in values:
                if not isinstance(rows, list) or not rows:
                    raise ValueError(f"Empty embedding response: {key}")
                for row in rows:
                    if (
                        not isinstance(row, list)
                        or len(row) != self.dimension
                        or any(
                            isinstance(v, bool)
                            or not isinstance(v, (int, float))
                            or not math.isfinite(v)
                            for v in row
                        )
                    ):
                        raise ValueError(f"Invalid embedding dimensions/values: {key}")
        return {key: result[key] for key in required}

    async def _request(self, pages):
        if not self.endpoint_url:
            raise RuntimeError("Inference endpoint is not configured")
        async with self.semaphore:
            payload = await run_cpu(self._prepare_payload, pages)
            session = await self._get_session()
            async with session.post(
                f"{self.endpoint_url}/embeddings", json=payload
            ) as response:
                if response.status == 422:
                    body = await response.json(content_type=None)
                    detail = body.get("detail") if isinstance(body, dict) else None
                    if (
                        isinstance(detail, dict)
                        and detail.get("code") == "embedding_token_limit_exceeded"
                    ):
                        actual, maximum = (
                            detail.get("input_tokens"),
                            detail.get("max_tokens"),
                        )
                        if (
                            type(actual) is int
                            and type(maximum) is int
                            and actual > maximum > 0
                        ):
                            raise EmbeddingTokenLimitError(actual, maximum)
                response.raise_for_status()
                return self._validate_response(await response.json(), len(pages))

    @staticmethod
    def _merge(first, second):
        if first.keys() != second.keys():
            raise ValueError("Split embedding responses have different vector fields")
        return {key: first[key] + second[key] for key in first}

    async def _generate(self, pages, reductions=0):
        for attempt in range(3):
            try:
                return await self._request(pages)
            except EmbeddingTokenLimitError as error:
                if len(pages) > 1:
                    result = await self._generate(pages[:1])
                    for page in pages[1:]:
                        result = self._merge(result, await self._generate([page]))
                    return result
                if reductions >= 3:
                    raise
                page = pages[0]
                scale = min(
                    0.8, math.sqrt(error.max_tokens / error.input_tokens) * 0.95
                )
                embedding_scale = min(1.0, self.embedding_dpi / page.effective_dpi)
                embedding_pixels = max(
                    1, int(page.image_content.width * embedding_scale)
                ) * max(1, int(page.image_content.height * embedding_scale))
                scale *= min(1.0, math.sqrt(self.max_image_pixels / embedding_pixels))
                size = tuple(max(1, int(d * scale)) for d in page.image_content.size)
                if size == page.image_content.size:
                    raise
                logger.warning(
                    "Reducing embedding image for page %s after token limit",
                    page.page_number,
                )
                image = await run_cpu(
                    page.image_content.resize, size, Image.Resampling.LANCZOS
                )
                reduced = page.model_copy(update={"image_content": image})
                try:
                    return await self._generate([reduced], reductions + 1)
                finally:
                    image.close()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                if (
                    isinstance(error, aiohttp.ClientResponseError)
                    and error.status in (500, 502)
                    and len(pages) > 1
                ):
                    middle = len(pages) // 2
                    await asyncio.sleep(0.5)
                    return self._merge(
                        await self._generate(pages[:middle]),
                        await self._generate(pages[middle:]),
                    )
                if attempt == 2:
                    raise
                logger.warning("Embedding request failed; retrying: %s", error)
                await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError("Embedding retry loop exhausted")

    async def generate_embeddings(self, document_pages):
        single = isinstance(document_pages, DocumentPage)
        pages = [document_pages] if single else document_pages
        if not pages:
            raise ValueError("At least one page is required")
        result = await self._generate(pages)
        return {key: value[0] for key, value in result.items()} if single else result
