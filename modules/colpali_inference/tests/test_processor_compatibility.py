# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import base64
import os
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import torch
from PIL import Image
from transformers import AutoProcessor

from colpali_inference.inference import ColPaliInference
from colpali_inference.models import EmbedRequest

PROCESSOR_PATH = os.getenv("COLPALI_TEST_PROCESSOR_PATH", "")


@unittest.skipUnless(
    PROCESSOR_PATH,
    "Set COLPALI_TEST_PROCESSOR_PATH to a trusted local processor snapshot",
)
class ProcessorCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_requests_preserve_reference_processor_token_sequences(self):
        processor = AutoProcessor.from_pretrained(
            PROCESSOR_PATH, local_files_only=True, trust_remote_code=True
        )
        texts = ["Revenue", "What is the total revenue in this document?"]
        reference = processor.process_texts(texts)
        expected_queries = [
            ids[mask.bool()].tolist()
            for ids, mask in zip(reference["input_ids"], reference["attention_mask"])
        ]
        self.assertNotEqual(len(expected_queries[0]), len(expected_queries[1]))
        for ids in expected_queries:
            self.assertEqual(ids[-10:], [processor.tokenizer.pad_token_id] * 10)

        with Image.new("RGB", (224, 168), "white") as image:
            image_batch = processor.process_images([image])
            image_ids = image_batch["input_ids"][0][
                image_batch["attention_mask"][0].bool()
            ].tolist()
            stream = BytesIO()
            image.save(stream, format="PNG")

        image_vectors = torch.arange(len(image_ids) * 3, dtype=torch.float32).view(
            -1, 3
        )
        query_vectors = [
            torch.arange(len(ids) * 3, dtype=torch.float32).view(-1, 3)
            for ids in expected_queries
        ]

        async def embed_queries(token_ids, *, priority=None):
            self.assertEqual(token_ids, expected_queries)
            await asyncio.sleep(0)
            return query_vectors

        client = SimpleNamespace(
            tokenize_chat_image=AsyncMock(return_value=image_ids),
            embed_images=AsyncMock(return_value=[image_vectors]),
            embed_token_ids=AsyncMock(side_effect=embed_queries),
        )
        inference = ColPaliInference(vllm_client=client)
        inference.processor = processor
        inference.is_initialized = True
        text_request = EmbedRequest(texts=texts)
        image_request = EmbedRequest(
            images=[base64.b64encode(stream.getvalue()).decode()],
            pooling_type=["none"],
        )
        # Unequal query lengths enable padding; image validation disables it.
        # Exercise both on the same real fast-tokenizer backend.
        for _ in range(12):
            query_response, image_response = await asyncio.gather(
                inference.generate_embeddings(text_request),
                inference.generate_embeddings(image_request),
            )
            self.assertEqual(
                query_response.embeddings, [rows.tolist() for rows in query_vectors]
            )
            self.assertEqual(image_response.embeddings, [image_vectors.tolist()])
