# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import ClientConnectionError, web
from PIL import Image

from colpali_inference.vllm_client import (
    VLLMClientError,
    VLLMEmbeddingClient,
    VLLMUpstreamError,
    _decode_pooling_response,
    token_limit_detail,
)


class VLLMClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_callers_cannot_exceed_image_capacity(self):
        started, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handle(request):
            body = await request.json()
            path = Path(
                body["messages"][0]["content"][0]["image_url"]["url"].removeprefix(
                    "file://"
                )
            )
            requests.append(path)
            if len(requests) == 1:
                started.set()
                await release.wait()
                with Image.open(path) as staged:
                    self.assertEqual(staged.size, (16, 16))
                return web.json_response({"error": "temporary failure"}, status=500)
            return web.json_response({"data": [{"data": [[1.0, 2.0]]}]})

        app = web.Application()
        app.router.add_post("/pooling", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"LIMIT_CONCURRENCY": "1"}),
            Image.new("RGB", (16, 16)) as image,
        ):
            client = VLLMEmbeddingClient(
                base_url=f"http://127.0.0.1:{runner.addresses[0][1]}",
                image_shm_dir=directory,
            )
            tasks = []
            try:
                first = asyncio.create_task(client.embed_images([image]))
                tasks.append(first)
                await asyncio.wait_for(started.wait(), timeout=5)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                # Admission must precede even the bitmap copy, not just dispatch.
                with patch.object(
                    image, "copy", side_effect=AssertionError("capacity exhausted")
                ):
                    for _ in range(5):
                        waiting = asyncio.create_task(client.embed_images([image]))
                        tasks.append(waiting)
                        await asyncio.sleep(0)
                        waiting.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await waiting
                self.assertEqual(len(requests), 1)
                self.assertEqual(len(list(Path(directory).glob("*.png"))), 1)
                next_request = asyncio.create_task(client.embed_images([image]))
                tasks.append(next_request)
                release.set()
                result = await asyncio.wait_for(next_request, timeout=5)
                self.assertEqual(result[0].tolist(), [[1.0, 2.0]])
                self.assertEqual(len(requests), 2)
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await client.close()

    async def test_transport_failure_retains_files_even_when_sibling_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            Image.new("RGB", (16, 16)) as image,
        ):
            client = VLLMEmbeddingClient(image_shm_dir=directory)
            client._post_pooling = AsyncMock(
                side_effect=[
                    VLLMUpstreamError(500),
                    ClientConnectionError("disconnected"),
                ],
            )
            with self.assertRaises(ClientConnectionError):
                await client.embed_images([image, image])
            await client.close()
            self.assertEqual(len(list(Path(directory).glob("*.png"))), 2)

    async def test_cancelled_caller_does_not_unlink_inflight_images(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            Image.new("RGB", (16, 16)) as image,
        ):
            client = VLLMEmbeddingClient(image_shm_dir=directory)
            started, release = asyncio.Event(), asyncio.Event()
            paths = []

            async def upstream(body):
                path = Path(
                    body["messages"][0]["content"][0]["image_url"]["url"].removeprefix(
                        "file://"
                    )
                )
                paths.append(path)
                started.set()
                await release.wait()
                self.assertTrue(path.exists())
                return [{"data": [[1.0, 2.0]]}]

            client._post_pooling = upstream
            task = asyncio.create_task(client.embed_images([image]))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(paths[0].exists())
            self.assertEqual(len(client._detached_tasks), 1)
            release.set()
            await client.close()
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_failed_batch_waits_for_sibling_upstream_reads(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            Image.new("RGB", (16, 16)) as image,
        ):
            client = VLLMEmbeddingClient(image_shm_dir=directory)
            started, release = asyncio.Event(), asyncio.Event()
            calls = 0

            async def upstream(body):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise VLLMUpstreamError(500)
                path = Path(
                    body["messages"][0]["content"][0]["image_url"]["url"].removeprefix(
                        "file://"
                    )
                )
                started.set()
                await release.wait()
                self.assertTrue(path.exists())
                return [{"data": [[1.0, 2.0]]}]

            client._post_pooling = upstream
            task = asyncio.create_task(client.embed_images([image, image]))
            await asyncio.wait_for(started.wait(), timeout=5)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(VLLMUpstreamError):
                await task
            await client.close()
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_partial_stage_failure_cleans_every_file(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            Image.new("RGB", (16, 16)) as image,
        ):
            with patch.dict("os.environ", {"LIMIT_CONCURRENCY": "1"}):
                client = VLLMEmbeddingClient(image_shm_dir=directory)
            calls = 0

            def save(image, path):
                nonlocal calls
                path.write_bytes(b"partial")
                calls += 1
                if calls == 2:
                    raise OSError("disk full")

            with patch(
                "colpali_inference.vllm_client._save_image_png", side_effect=save
            ):
                with self.assertRaises(OSError):
                    await client.embed_images([image, image])
            client._post_pooling = AsyncMock(return_value=[{"data": [[1.0, 2.0]]}])
            result = await asyncio.wait_for(client.embed_images([image]), timeout=5)
            self.assertEqual(result[0].tolist(), [[1.0, 2.0]])
            await client.close()
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_image_tokenization_and_pooling_use_identical_reference_prompt(self):
        requests = []

        async def handle(request):
            body = await request.json()
            requests.append((request.path, body))
            content = body["messages"][0]["content"]
            path = content[0]["image_url"]["url"].removeprefix("file://")
            with Image.open(path) as staged:
                self.assertEqual(staged.size, (16, 32))
            self.assertEqual(
                content[1],
                {
                    "type": "text",
                    "text": "Describe the image.<|im_end|><|endoftext|>",
                },
            )
            self.assertIs(body["continue_final_message"], True)
            self.assertIs(body["add_generation_prompt"], False)
            self.assertIs(body["add_special_tokens"], False)
            if request.path == "/tokenize":
                return web.json_response({"tokens": [1, 2]})
            self.assertEqual(body["priority"], 7)
            return web.json_response({"data": [{"data": [[1.0, 0.0], [0.0, 1.0]]}]})

        app = web.Application()
        app.router.add_post("/tokenize", handle)
        app.router.add_post("/pooling", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            Image.new("RGB", (16, 32)) as image,
        ):
            client = VLLMEmbeddingClient(
                base_url=f"http://127.0.0.1:{runner.addresses[0][1]}",
                image_shm_dir=tmp_dir,
            )
            self.addAsyncCleanup(client.close)
            self.assertEqual(await client.tokenize_chat_image(image), [1, 2])
            self.assertEqual(
                (await client.embed_images([image], priority=7))[0].shape, (2, 2)
            )
            self.assertEqual([path for path, _ in requests], ["/tokenize", "/pooling"])
            self.assertEqual(list(Path(tmp_dir).iterdir()), [])
            tokenize_body, pooling_body = requests[0][1], requests[1][1]
            pooling_body.pop("priority")
            for body in (tokenize_body, pooling_body):
                body["messages"][0]["content"][0]["image_url"]["url"] = "<staged image>"
            self.assertEqual(tokenize_body, pooling_body)

    async def test_token_ids_are_forwarded_without_special_tokens(self):
        for priority in (None, 0, 7):
            with self.subTest(priority=priority):
                client = VLLMEmbeddingClient()
                client.text_priority = 3
                client._post_pooling = AsyncMock(
                    return_value=[
                        {"data": [[1.0, 2.0]]},
                        {"data": [[3.0, 4.0], [5.0, 6.0]]},
                    ]
                )
                token_ids = [[151643], [5, 151643]]
                result = await client.embed_token_ids(token_ids, priority=priority)
                client._post_pooling.assert_awaited_once_with(
                    {
                        "model": client.model_id,
                        "input": token_ids,
                        "add_special_tokens": False,
                        "priority": 3 if priority is None else priority,
                    }
                )
                self.assertEqual([len(matrix) for matrix in result], [1, 2])

    async def test_empty_token_sequences_are_rejected_before_dispatch(self):
        for ids in ([], [[]], [[1], []]):
            with self.subTest(ids=ids):
                client = VLLMEmbeddingClient()
                client._post_pooling = AsyncMock()
                with self.assertRaisesRegex(ValueError, "non-empty"):
                    await client.embed_token_ids(ids)
                client._post_pooling.assert_not_awaited()


class PoolingResponseTests(unittest.TestCase):
    def test_token_limit_errors_are_structured_but_other_400s_are_not(self):
        messages = [
            "The decoder prompt (length 3000) is longer than the maximum model length of 2048.",
            "This model's maximum context length is 2048 tokens. However, you requested 0 output tokens and your prompt contains at least 3000 input tokens, for a total of at least 3000 tokens.",
        ]
        for message in messages:
            self.assertEqual(
                token_limit_detail(json.dumps({"error": {"message": message}})),
                {
                    "code": "embedding_token_limit_exceeded",
                    "input_tokens": 3000,
                    "max_tokens": 2048,
                },
            )
        for body in ("not json", "[]", "{}", '{"error":{"message":"bad image"}}'):
            self.assertIsNone(token_limit_detail(body))

    def test_invalid_or_zero_padding_vectors_are_rejected(self):
        for rows in (
            [],
            [[0.0, 0.0]],
            [[float("nan"), 1.0]],
            [[float("inf"), 1.0]],
            [[float("-inf"), 1.0]],
            [1.0, 2.0],
            [[1.0, 2.0], [3.0]],
            [["invalid", 1.0]],
            {"unexpected": [1.0]},
            [[], []],
        ):
            with self.subTest(rows=rows), self.assertRaises(VLLMClientError):
                _decode_pooling_response([{"data": rows}])

    def test_valid_vectors_preserve_order_values_and_zero_components(self):
        rows = [[1.0, 0.0, -2.0], [0.0, 3.0, 4.0]]
        output = _decode_pooling_response([{"data": rows}, {"data": [rows[1]]}])
        self.assertEqual([matrix.tolist() for matrix in output], [rows, [rows[1]]])
