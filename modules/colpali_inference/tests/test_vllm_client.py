# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from PIL import Image

from colpali_inference.vllm_client import (
    VLLMClientError,
    VLLMEmbeddingClient,
    _decode_pooling_response,
)


class VLLMClientTests(unittest.IsolatedAsyncioTestCase):
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
