# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Async HTTP client for the vLLM `/pooling` endpoint.

The shim talks to vLLM over loopback HTTP (sidecar container in the same pod).
Images are transported via a shared tmpfs volume (`emptyDir { medium: Memory }`
mounted at /shm in both containers) — the shim writes each image to a UUID
filename and sends `file:///shm/<uuid>.png` to vLLM. Files remain until every
upstream read finishes; interrupted transports leave cleanup to the age-based
janitor. This avoids base64 encode/decode overhead and HTTP body bloat.

The endpoint contract is:
  POST {VLLM_BASE_URL}/pooling
  Body (text):       {"model": "...", "input": [[token_id, ...], ...],
                       "add_special_tokens": false}
  Body (image):      {"model": "...", "messages": [...], ...}
                     One retrieval-prompt conversation per image.
  Response:          {"data": [{"data": [[...embedding rows...]], ...}, ...]}

Per-token embeddings are returned as float lists, already L2-normalized by
vLLM (matches the current HF transformers behaviour).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    TypeVar,
)

import aiohttp
import torch
from PIL import Image

logger = logging.getLogger(__name__)
T = TypeVar("T")


def token_limit_detail(body: str) -> dict[str, str | int] | None:
    """Recognize vLLM's token overflow errors without classifying arbitrary 400s."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return None
    match = re.match(
        r"The decoder prompt \(length (?P<input_tokens>[1-9][0-9]*)\) is longer than the "
        r"maximum model length of (?P<max_tokens>[1-9][0-9]*)\.(?:\s|$)",
        message,
    )
    if match is None:
        match = re.match(
            r"This model's maximum context length is (?P<max_tokens>[1-9][0-9]*) "
            r"tokens\. However, you requested (?P<output_tokens>0|[1-9][0-9]*) "
            r"output tokens and your prompt contains (?P<lower_bound>(?:at least )?)"
            r"(?P<input_tokens>[1-9][0-9]*) input tokens, for a total of "
            r"(?P=lower_bound)(?P<total_tokens>[1-9][0-9]*) tokens\.(?:\s|$)",
            message,
        )
    if match is None:
        return None
    actual, maximum = int(match["input_tokens"]), int(match["max_tokens"])
    if actual <= maximum:
        return None
    if "total_tokens" in match.groupdict() and int(
        match["total_tokens"]
    ) != actual + int(match["output_tokens"]):
        return None
    return {
        "code": "embedding_token_limit_exceeded",
        "input_tokens": actual,
        "max_tokens": maximum,
    }


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
            "MODEL_ID", "TomoroAI/tomoro-ai-colqwen3-embed-4b-awq"
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
        self._detached_tasks: set[asyncio.Task] = set()
        self._closing = False

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
                    self._session = aiohttp.ClientSession(
                        timeout=timeout,
                        connector=aiohttp.TCPConnector(keepalive_timeout=4),
                    )
        return self._session

    async def close(self) -> None:
        self._closing = True
        if self._detached_tasks:
            _, pending = await asyncio.wait(
                self._detached_tasks, timeout=self.request_timeout_seconds + 5
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
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

    async def embed_token_ids(
        self, token_ids: List[List[int]], priority: Optional[int] = None
    ) -> List[torch.Tensor]:
        """Pool unpadded, reference-processor token sequences through vLLM.

        Args:
            token_ids: Non-empty sequences, including active augmentation tokens.
            priority: vLLM scheduling priority (lower = higher priority).
                Defaults to ``VLLM_TEXT_PRIORITY`` env (default 0). Only
                honoured when vLLM is started with
                ``--scheduling-policy priority``.

        Returns:
            List of variable-length tensors, one per input, each of shape
            (seq_len_i, embed_dim) on CPU. vLLM does not pad across the
            batch — each input keeps its natural sequence length.
        """
        if not token_ids or any(not ids for ids in token_ids):
            raise ValueError("token_ids must contain non-empty sequences")

        body = {
            "model": self.model_id,
            "input": token_ids,
            "add_special_tokens": False,
            "priority": priority if priority is not None else self.text_priority,
        }
        data = await self._post_pooling(body)
        return _decode_pooling_response(data)

    def _image_request(self, url: str) -> Dict[str, Any]:
        return {
            "model": self.model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": url}},
                        {
                            "type": "text",
                            "text": "Describe the image.<|im_end|><|endoftext|>",
                        },
                    ],
                }
            ],
            # Preserve the retrieval suffix, not the generic chat ending.
            "continue_final_message": True,
            "add_generation_prompt": False,
            "add_special_tokens": False,
        }

    async def embed_images(
        self, images: List[Image.Image], priority: Optional[int] = None
    ) -> List[torch.Tensor]:
        """Pool images through vLLM via tmpfs file transport.

        Each image is written to /shm/<uuid>.png, vLLM reads it via
        file:// URL, then the file is deleted after upstream reads finish.
        Interrupted transports retain files for the age-based janitor.

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

        async def embed_staged(image_urls):
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
                body = self._image_request(url)
                body["priority"] = effective_priority
                return await self._post_pooling(body)

            # A rejected image must not unlink files still used by sibling calls.
            results = await asyncio.gather(
                *(_embed_one(url) for url in image_urls), return_exceptions=True
            )
            # An HTTP rejection must not hide a sibling whose upstream read
            # may still be active after a disconnected transport.
            for result in results:
                if isinstance(
                    result, (asyncio.CancelledError, TimeoutError, aiohttp.ClientError)
                ):
                    raise result
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return results

        results = await self._run_with_staged_images(images, embed_staged)
        # Each per-image response has exactly one entry; flatten to a single
        # list whose order matches the input images.
        flattened = [entry for resp in results for entry in resp]
        return _decode_pooling_response(flattened)

    async def tokenize_chat_image(self, image: Image.Image) -> List[int]:
        """Return the exact token-id sequence vLLM uses for a single-image
        chat request, matching the structure :meth:`embed_images` sends.

        The caller checks this against the installed retrieval processor's
        prompt and patch grid before accepting any output representation.
        """

        async def tokenize_staged(image_urls):
            body = self._image_request(image_urls[0])
            return await self._post_json("/tokenize", body)

        payload = await self._run_with_staged_images([image], tokenize_staged)
        tokens = payload.get("tokens")
        if not isinstance(tokens, list):
            raise VLLMClientError(
                f"vLLM /tokenize returned unexpected payload: {payload!r}"
            )
        return [int(t) for t in tokens]

    async def _run_with_staged_images(
        self, images: List[Image.Image], operation: Callable[[List[str]], Awaitable[T]]
    ) -> T:
        if self._closing:
            raise RuntimeError("Inference client is closing")
        # The detached operation owns its bitmaps even if the caller closes theirs.
        owned_images = []
        try:
            for image in images:
                owned_images.append(image.copy())
        except BaseException:
            for image in owned_images:
                image.close()
            raise

        async def run():
            try:
                async with self._stage_images(owned_images) as urls:
                    return await operation(urls)
            finally:
                for image in owned_images:
                    image.close()

        task = asyncio.create_task(run())
        self._detached_tasks.add(task)
        caller_cancelled = False

        def finished(done):
            self._detached_tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None and caller_cancelled:
                    logger.warning(
                        "Detached image request failed: %s", type(error).__name__
                    )

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            caller_cancelled = True
            raise

    @asynccontextmanager
    async def _stage_images(
        self, images: List[Image.Image]
    ) -> AsyncIterator[List[str]]:
        """Write images to tmpfs, yield file:// URLs, delete on exit.

        Cancellation or transport failure can leave upstream reads in flight.
        In that case retain files for the age-based janitor instead of unlinking.
        """
        self.image_shm_dir.mkdir(parents=True, exist_ok=True)
        staged_paths: List[Path] = []
        retain = False
        dispatched = False
        try:
            for img in images:
                path = self.image_shm_dir / f"{uuid.uuid4().hex}.png"
                # PIL save is sync; offload to default executor so we don't
                # block the event loop on disk (tmpfs, but still a syscall).
                staged_paths.append(path)
                saving = asyncio.create_task(
                    asyncio.to_thread(_save_image_png, img, path)
                )
                cancelled = False
                while not saving.done():
                    try:
                        await asyncio.shield(saving)
                    except asyncio.CancelledError:
                        cancelled = True
                saving.result()
                if cancelled:
                    raise asyncio.CancelledError
            dispatched = True
            yield [f"file://{p}" for p in staged_paths]
        except (asyncio.CancelledError, asyncio.TimeoutError, aiohttp.ClientError):
            retain = dispatched
            if retain:
                logger.warning(
                    "Retaining staged images after interrupted upstream operation for janitor cleanup"
                )
            raise
        finally:
            for path in [] if retain else staged_paths:
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
        with img.convert("RGB") as converted:
            converted.save(path, format="PNG")
    else:
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
        try:
            sequence = torch.tensor(rows, dtype=torch.float32)
        except (TypeError, ValueError) as exc:
            raise VLLMClientError(
                "vLLM /pooling returned a malformed embedding matrix"
            ) from exc
        if (
            sequence.ndim != 2
            or sequence.shape[1] == 0
            or not torch.isfinite(sequence).all()
            or (sequence.abs().sum(dim=1) == 0).any()
        ):
            raise VLLMClientError(
                "vLLM /pooling returned invalid vectors or zero-padding rows"
            )
        sequences.append(sequence)
    return sequences


# Process-global singleton — uvicorn worker processes each get their own.
_client_singleton: Optional[VLLMEmbeddingClient] = None


def get_client() -> VLLMEmbeddingClient:
    """Return the per-process VLLMEmbeddingClient singleton."""
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = VLLMEmbeddingClient()
    return _client_singleton
