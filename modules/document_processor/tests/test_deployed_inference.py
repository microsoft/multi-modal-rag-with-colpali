# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import json
import os
import time
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from conftest import source_url


@pytest.mark.skipif(
    not os.getenv("TEST_INFERENCE_URL"),
    reason="Requires an explicitly selected inference endpoint",
)
@pytest.mark.parametrize("stack", [{"COLPALI_STORAGE_DPI": "500"}], indirect=True)
async def test_real_inference_supports_incremental_indexing_and_image_retrieval(
    stack, record_property
):
    endpoint = os.environ["TEST_INFERENCE_URL"].rstrip("/")
    if urlsplit(endpoint).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(
            "TEST_INFERENCE_URL must use an explicit loopback port-forward"
        )
    timings = {}
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=180)
    ) as session:

        async def forward(payload):
            async with session.post(f"{endpoint}/embeddings", json=payload) as response:
                return web.Response(
                    status=response.status,
                    body=await response.read(),
                    content_type=response.content_type,
                )

        stack.inference.reply = forward
        event = stack.add_pdf("runtime check.pdf", "First page", "Second page")
        start = time.perf_counter()
        assert (await stack.deliver(event)).settlements == [("complete", {})]
        timings["initial_two_pages_seconds"] = time.perf_counter() - start
        before = await stack.points()
        assert len(before) == 2
        for point in before:
            assert set(point.vector) == {
                "hierarchical_pooled",
                "mean_row_pooled",
                "mean_column_pooled",
            }
            assert all(
                len(row) == 320 for rows in point.vector.values() for row in rows
            )

        start = time.perf_counter()
        chunks = await stack.retriever.search_documents(
            "First and second page", top_k=5
        )
        timings["first_query_seconds"] = time.perf_counter() - start
        assert {(chunk.document_id, chunk.page_number) for chunk in chunks} == {
            (source_url("runtime check.pdf"), 1),
            (source_url("runtime check.pdf"), 2),
        }
        for chunk in chunks:
            stored = stack.blobs.blobs[
                (
                    "documents-page-images",
                    f"runtime check.pdf/page_{chunk.page_number}.jpg",
                )
            ]
            assert base64.b64decode(chunk.page_image_base64) == stored.content

        requests = len(stack.inference.image_batches)
        start = time.perf_counter()
        assert (await stack.deliver(event)).settlements == [("complete", {})]
        timings["unchanged_redelivery_seconds"] = time.perf_counter() - start
        assert len(stack.inference.image_batches) == requests
        assert [(p.id, p.vector) for p in await stack.points()] == [
            (p.id, p.vector) for p in before
        ]

        changed = stack.add_pdf(
            "runtime check.pdf", "First page", "Updated second page"
        )
        start = time.perf_counter()
        assert (await stack.deliver(changed)).settlements == [("complete", {})]
        timings["one_changed_page_seconds"] = time.perf_counter() - start
        assert len(stack.inference.image_batches) == requests + 1
        after = await stack.points()
        assert [point.id for point in after] == [point.id for point in before]
        assert after[0].vector == before[0].vector
        assert "Updated second page" in after[1].payload["text_content"]
    record_property("functional_timings_seconds", json.dumps(timings, sort_keys=True))
