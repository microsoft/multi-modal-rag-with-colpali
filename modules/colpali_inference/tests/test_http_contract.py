# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import uvicorn

from colpali_inference.vllm_client import VLLMUpstreamError


class EmbeddingHttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch("colpali_inference.setup_logging.configure_telemetry"):
            from colpali_inference import app

        self.engine = SimpleNamespace(generate_embeddings=AsyncMock())
        engine_patch = patch.object(app, "inference_service", self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        self.url = f"http://127.0.0.1:{listener.getsockname()[1]}/embeddings"
        server = uvicorn.Server(
            uvicorn.Config(
                app.app,
                lifespan="off",
                log_level="error",
                access_log=False,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))

        async def shutdown():
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=5)

        self.addAsyncCleanup(shutdown)
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                    self.fail("Inference HTTP server stopped before startup")
                await asyncio.sleep(0.01)
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        self.addAsyncCleanup(self.http.close)

    async def test_only_prompt_overflow_gets_the_token_recovery_contract(self):
        for message in (
            "The decoder prompt (length 3000) is longer than the maximum model length of 2048.",
            "This model's maximum context length is 2048 tokens. However, you requested 0 output tokens and your prompt contains at least 3000 input tokens, for a total of at least 3000 tokens.",
            "The supplied image could not be decoded",
        ):
            with self.subTest(message=message):
                self.engine.generate_embeddings.side_effect = VLLMUpstreamError(
                    400, json.dumps({"error": {"message": message}})
                )
                async with self.http.post(
                    self.url, json={"texts": ["report"]}
                ) as response:
                    body = await response.json()
                    if "3000" in message:
                        self.assertEqual(response.status, 422)
                        self.assertEqual(
                            body,
                            {
                                "detail": {
                                    "code": "embedding_token_limit_exceeded",
                                    "input_tokens": 3000,
                                    "max_tokens": 2048,
                                }
                            },
                        )
                    else:
                        self.assertEqual(response.status, 502)
                        self.assertNotIn("embedding_token_limit_exceeded", str(body))

    async def test_overload_and_outage_remain_retryable_with_server_backoff(self):
        for upstream, expected in ((429, 429), (503, 503), (500, 502)):
            with self.subTest(upstream=upstream):
                self.engine.generate_embeddings.side_effect = VLLMUpstreamError(
                    upstream, "temporarily unavailable", retry_after="7"
                )
                async with self.http.post(
                    self.url, json={"texts": ["report"]}
                ) as response:
                    self.assertEqual(response.status, expected)
                    self.assertEqual(
                        response.headers.get("Retry-After"),
                        "7" if upstream in (429, 503) else None,
                    )
