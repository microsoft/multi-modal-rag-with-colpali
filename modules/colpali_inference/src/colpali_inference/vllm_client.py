# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Async HTTP client for the vLLM `/pooling` endpoint.

The shim talks to vLLM over loopback HTTP (sidecar container in the same pod).
Images are transported via a shared tmpfs volume (`emptyDir { medium: Memory }`
mounted at /shm in both containers) — the shim writes each image to a UUID
filename, sends `file:///shm/<uuid>.png` to vLLM, then deletes the file in a
finally block. This avoids base64 encode/decode overhead and HTTP body bloat
on every request.

The endpoint contract is:
  POST {VLLM_BASE_URL}/pooling
  Body (text):       {"model": "...", "input": ["text1", "text2", ...]}
  Body (image):      {"model": "...", "input": [{"content": [{"type":
                       "image_url", "image_url": {"url": "file:///shm/..."}}]},
                       ...]}
  Response:          {"data": [{"data": [[...embedding rows...]], ...}, ...]}

Per-token embeddings are returned as float lists, already L2-normalized by
vLLM (matches the current HF transformers behaviour).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional

import aiohttp
import torch
from PIL import Image

logger = logging.getLogger(__name__)


class VLLMClientError(RuntimeError):
    """Raised when the vLLM /pooling call fails for shim-side reasons
    (bad response shape, etc.) — distinct from upstream HTTP errors."""


class VLLMUpstreamError(RuntimeError):
    """Raised when vLLM returns a non-2xx HTTP status.

    Carries the upstream status code and a body snippet so the FastAPI
    layer can map it back to the caller (429 → 429 with Retry-After,
    503 → 503, anything else → 502 Bad Gateway). The shim does not retry;
    retries are the caller's responsibility.
    """

    def __init__(
        self,
        status_code: int,
        body: str = "",
        retry_after: Optional[str] = None,
    ) -> None:
        super().__init__(f"vLLM returned {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after


class VLLMEmbeddingClient:
    """Async client for vLLM's /pooling endpoint.

    Singleton per shim worker process. Holds a reusable `aiohttp.ClientSession`
    so we don't pay TCP/TLS setup per request.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model_id: Optional[str] = None,
        image_shm_dir: Optional[str] = None,
        request_timeout_seconds: float = 300.0,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("VLLM_BASE_URL", "http://localhost:8001")
        ).rstrip("/")
        self.model_id = model_id or os.getenv(
            "MODEL_ID", "TomoroAI/tomoro-colqwen3-embed-4b"
        )
        self.image_shm_dir = Path(image_shm_dir or os.getenv("IMAGE_SHM_DIR", "/shm"))
        self.request_timeout_seconds = request_timeout_seconds
        # Default scheduling priorities sent on every /pooling call.
        # Lower = higher priority in vLLM. Text (search) preempts images
        # (index). Only honoured when vLLM is started with
        # --scheduling-policy priority; ignored otherwise.
        self.text_priority = int(os.getenv("VLLM_TEXT_PRIORITY", "0"))
        self.image_priority = int(os.getenv("VLLM_IMAGE_PRIORITY", "10"))
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        logger.info(
            "VLLMEmbeddingClient configured: base_url=%s model_id=%s "
            "image_shm_dir=%s text_priority=%d image_priority=%d",
            self.base_url,
            self.model_id,
            self.image_shm_dir,
            self.text_priority,
            self.image_priority,
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared ClientSession, creating it lazily on first use.

        Must be created from inside the running event loop (per-worker
        uvicorn process), not at import time.
        """
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)
                    self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def health(self) -> bool:
        """True iff vLLM's /health returns 200."""
        try:
            session = await self._get_session()
            async with session.get(f"{self.base_url}/health") as resp:
                return resp.status == 200
        except Exception as e:  # noqa: BLE001 — health check should never raise
            logger.debug("vLLM health check failed: %s", e)
            return False

    async def wait_until_ready(
        self,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        """Block until vLLM /health returns 200 or timeout expires."""
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        attempt = 0
        while True:
            if await self.health():
                logger.info("vLLM sidecar is ready (after %d polls)", attempt)
                return
            attempt += 1
            if asyncio.get_event_loop().time() >= deadline:
                raise VLLMClientError(
                    f"vLLM sidecar at {self.base_url} did not become ready within "
                    f"{timeout_seconds}s"
                )
            await asyncio.sleep(poll_interval_seconds)

    async def embed_texts(
        self, texts: List[str], priority: Optional[int] = None
    ) -> List[torch.Tensor]:
        """Pool texts through vLLM.

        Args:
            texts: Non-empty list of input strings.
            priority: vLLM scheduling priority (lower = higher priority).
                Defaults to ``VLLM_TEXT_PRIORITY`` env (default 0). Only
                honoured when vLLM is started with
                ``--scheduling-policy priority``.

        Returns:
            List of variable-length tensors, one per input, each of shape
            (seq_len_i, embed_dim) on CPU. vLLM does not pad across the
            batch — each input keeps its natural sequence length.
        """
        if not texts:
            raise ValueError("texts must be non-empty")

        body = {
            "model": self.model_id,
            "input": list(texts),
            "priority": priority if priority is not None else self.text_priority,
        }
        data = await self._post_pooling(body)
        return _decode_pooling_response(data)

    async def embed_images(
        self, images: List[Image.Image], priority: Optional[int] = None
    ) -> List[torch.Tensor]:
        """Pool images through vLLM via tmpfs file transport.

        Each image is written to /shm/<uuid>.png, vLLM reads it via
        file:// URL, then the file is deleted in a finally block.

        Args:
            images: Non-empty list of PIL images.
            priority: vLLM scheduling priority (lower = higher priority).
                Defaults to ``VLLM_IMAGE_PRIORITY`` env (default 10).
                Indexing/image traffic is deliberately deprioritised so
                user-facing text searches preempt it on the GPU.

        Returns:
            List of variable-length tensors, one per image, each of shape
            (seq_len_i, embed_dim) on CPU.
        """
        if not images:
            raise ValueError("images must be non-empty")

        async with self._stage_images(images) as image_urls:
            # vLLM's /pooling endpoint accepts either PoolingCompletionRequest
            # (input: str | list[str] | list[int] | list[list[int]]) or
            # PoolingChatRequest (messages: [...]). Multimodal content blocks
            # only validate against the chat variant, and that variant pools a
            # single conversation per request — so we fan out N parallel
            # requests, one per image, and stitch the responses back together.
            effective_priority = (
                priority if priority is not None else self.image_priority
            )

            async def _embed_one(url: str) -> List[Any]:
                body = {
                    "model": self.model_id,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": url}}
                            ],
                        }
                    ],
                    "priority": effective_priority,
                }
                return await self._post_pooling(body)

            results = await asyncio.gather(*(_embed_one(url) for url in image_urls))
        # Each per-image response has exactly one entry; flatten to a single
        # list whose order matches the input images.
        flattened = [entry for resp in results for entry in resp]
        return _decode_pooling_response(flattened)

    async def tokenize_chat_image(self, image: Image.Image) -> List[int]:
        """Return the exact token-id sequence vLLM uses for a single-image
        chat request, matching the structure :meth:`embed_images` sends.

        This is the authoritative tokenization for that input — using the
        local HF processor in the shim is not reliable for multimodal models
        because vLLM applies the model's chat template (different prompt
        structure than colpali_engine's image-only template). Mean pooling
        in inference.py needs exact alignment between the embedding length
        returned by /pooling and the input_ids it ran against, which only
        the /tokenize endpoint can guarantee.
        """
        async with self._stage_images([image]) as image_urls:
            url = image_urls[0]
            body = {
                "model": self.model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": url}}],
                    }
                ],
                "add_generation_prompt": False,
                "add_special_tokens": False,
            }
            payload = await self._post_json("/tokenize", body)
        tokens = payload.get("tokens")
        if not isinstance(tokens, list):
            raise VLLMClientError(
                f"vLLM /tokenize returned unexpected payload: {payload!r}"
            )
        return [int(t) for t in tokens]

    @asynccontextmanager
    async def _stage_images(
        self, images: List[Image.Image]
    ) -> AsyncIterator[List[str]]:
        """Write images to tmpfs, yield file:// URLs, delete on exit.

        Files are deleted whether the request succeeds, fails, or is cancelled.
        """
        self.image_shm_dir.mkdir(parents=True, exist_ok=True)
        staged_paths: List[Path] = []
        try:
            for img in images:
                path = self.image_shm_dir / f"{uuid.uuid4().hex}.png"
                # PIL save is sync; offload to default executor so we don't
                # block the event loop on disk (tmpfs, but still a syscall).
                await asyncio.to_thread(_save_image_png, img, path)
                staged_paths.append(path)
            yield [f"file://{p}" for p in staged_paths]
        finally:
            for path in staged_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to delete staged image %s: %s", path, e)

    async def _post_pooling(self, body: dict) -> List[Any]:
        """POST to /pooling. Single shot — no retries.

        Any non-2xx response is raised as :class:`VLLMUpstreamError` carrying
        the upstream status code so the FastAPI layer can map 429/503 back
        to the caller. Network errors and timeouts bubble up unchanged.
        Retries are the caller's responsibility, not the shim's.
        """
        payload = await self._post_json("/pooling", body)
        if not isinstance(payload, dict) or "data" not in payload:
            raise VLLMClientError(
                f"vLLM /pooling returned unexpected payload shape: {type(payload)}"
            )
        return payload["data"]

    async def _post_json(self, path: str, body: dict) -> Any:
        """POST JSON to vLLM at the given path; raise on non-2xx; return parsed JSON.

        Used by both /pooling and /tokenize. Same retry / error-mapping
        contract: caller decides whether to back off on 429/503.
        """
        url = f"{self.base_url}{path}"
        session = await self._get_session()
        async with session.post(url, json=body) as resp:
            if resp.status >= 400:
                text = await resp.text()
                retry_after = resp.headers.get("Retry-After")
                if resp.status in (429, 503):
                    logger.warning(
                        "vLLM %s rejected with %s (retry-after=%s): %s",
                        path,
                        resp.status,
                        retry_after,
                        text[:200],
                    )
                else:
                    logger.error("vLLM %s failed %s: %s", path, resp.status, text[:500])
                raise VLLMUpstreamError(
                    status_code=resp.status,
                    body=text,
                    retry_after=retry_after,
                )
            return await resp.json()


def _save_image_png(img: Image.Image, path: Path) -> None:
    """Save a PIL image as PNG to the given path. Runs in a worker thread."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(path, format="PNG")


def _decode_pooling_response(data: List[Any]) -> List[torch.Tensor]:
    """Convert vLLM /pooling response data to a list of per-input tensors.

    vLLM returns one entry per input. Each entry's `.data` is a list of
    embedding rows (already L2-normalized by vLLM). Sequence lengths differ
    per input — we keep them as a list of variable-length tensors rather
    than padding to a dense batch, so they align naturally with the
    shim's per-image tokenization output.
    """
    sequences: List[torch.Tensor] = []
    for entry in data:
        if not isinstance(entry, dict) or "data" not in entry:
            raise VLLMClientError(
                f"vLLM /pooling entry missing 'data' field: {entry!r}"
            )
        rows = entry["data"]
        if not rows:
            raise VLLMClientError("vLLM /pooling entry has empty embedding list")
        sequences.append(torch.tensor(rows, dtype=torch.float32))
    return sequences


# Process-global singleton — uvicorn worker processes each get their own.
_client_singleton: Optional[VLLMEmbeddingClient] = None


def get_client() -> VLLMEmbeddingClient:
    """Return the per-process VLLMEmbeddingClient singleton."""
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = VLLMEmbeddingClient()
    return _client_singleton
