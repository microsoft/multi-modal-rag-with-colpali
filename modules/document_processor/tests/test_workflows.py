# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import base64
import hashlib
import io
import os
import random
from uuid import uuid4

import fitz
import pytest
from aiohttp import web
from conftest import make_pdf, source_url
from PIL import Image
from qdrant_client import models

from document_processor.models import DocumentPage, PoolingType


@pytest.mark.parametrize(
    "stack",
    [
        {
            "COLPALI_STORAGE_DPI": None,
            "COLPALI_IMAGE_DPI": None,
            "COLPALI_EMBEDDING_DPI": None,
        }
    ],
    indirect=True,
)
async def test_default_a4_ingestion_keeps_500_dpi_citations_and_embeds_at_150_dpi(
    stack,
):
    with fitz.open() as document:
        document.new_page(width=595, height=842).insert_text((40, 60), "A4 reference")
        stack.blobs.put("a4.pdf", document.tobytes())
    assert (await stack.deliver(stack.event("a4.pdf"))).settlements == [
        ("complete", {})
    ]
    assert stack.inference.image_batches == [[(1239, 1754)]]
    chunks = await stack.retriever.search_documents("A4 reference")
    assert len(chunks) == 1 and "A4 reference" in chunks[0].text_content
    with Image.open(io.BytesIO(base64.b64decode(chunks[0].page_image_base64))) as image:
        assert image.size == (4132, 5848)


@pytest.mark.parametrize("optimized", [False, True])
async def test_small_top_k_still_reranks_the_full_200_candidate_pool(stack, optimized):
    if optimized and not os.getenv("TEST_QDRANT_URL"):
        pytest.skip("Real Qdrant is required to exercise index optimization")
    await stack.processor.qdrant_index.initialize()
    client = stack.processor.qdrant_index.qdrant_client
    rng = random.Random(0)
    query = (
        [rng.uniform(-1, 1) for _ in range(320)] if optimized else [1.0] + [0.0] * 319
    )

    async def query_embedding(_):
        return web.json_response({"embeddings": [[query]]})

    stack.inference.reply = query_embedding
    points = []
    for index in range(201):
        mean = [[1.0, index / 200] + [0.0] * 318]
        hierarchical = [[0.0, 1.0] + [0.0] * 318]
        if index == 199:
            hierarchical = [[1.0, 0.1] + [0.0] * 318]
        elif index == 200:
            hierarchical = [[1.0, 0.0] + [0.0] * 318]
        if optimized:
            # Binary-identical sparse vectors cannot establish ANN recall.
            # The unoptimized case above tests the exact candidate boundary.
            mean = [
                query
                if index == 199
                else [-v for v in query]
                if index == 200
                else [rng.uniform(-1, 1) for _ in range(320)]
            ]
            hierarchical = [list(query) if index == 199 else [-v for v in query]]
            if index == 199:
                hierarchical[0][0] += 0.1
        points.append(
            models.PointStruct(
                id=str(uuid4()),
                vector={
                    "mean_row_pooled": mean,
                    "mean_column_pooled": mean,
                    "hierarchical_pooled": hierarchical,
                },
                payload={
                    "document_id": source_url(f"candidate-{index}.pdf"),
                    "page_number": 1,
                    "metadata": {"document": {"filename": f"candidate-{index}.pdf"}},
                },
            )
        )
    await client.upsert(stack.processor.qdrant_index.collection_name, points, wait=True)
    if optimized:
        await client.update_collection(
            stack.processor.qdrant_index.collection_name,
            optimizers_config=models.OptimizersConfigDiff(indexing_threshold=1),
            vectors_config={
                name: models.VectorParamsDiff(
                    hnsw_config=models.HnswConfigDiff(full_scan_threshold=10)
                )
                for name in ("mean_row_pooled", "mean_column_pooled")
            },
        )
        async with asyncio.timeout(60):
            while True:
                info = await client.get_collection(
                    stack.processor.qdrant_index.collection_name
                )
                if info.indexed_vectors_count >= len(points):
                    break
                await asyncio.sleep(0.25)
    for options, count in (({}, 5), ({"top_k": None}, 5), ({"top_k": 1}, 1)):
        chunks = await stack.retriever.search_documents_batch(["reference"], **options)
        assert len(chunks) == count
        assert chunks[0].source_file == "candidate-199.pdf"
        if not optimized:
            assert all(chunk.source_file != "candidate-200.pdf" for chunk in chunks)


async def test_indexed_documents_are_searchable_with_distinct_citations_and_images(
    stack,
):
    paths = ["first folder/report.pdf", "second/report.pdf"]
    events = [
        stack.add_pdf(
            path, f"Report {index}", "Second page", metadata={"category": str(index)}
        )
        for index, path in enumerate(paths)
    ]
    events[0]["data"]["url"] += "?sig=temporary-access-token"
    message = await stack.deliver(events)
    assert message.settlements == [("complete", {})]

    chunks = await stack.retriever.search_documents_batch(
        ["report", "second page"], top_k=10
    )
    assert {(chunk.document_id, chunk.page_number) for chunk in chunks} == {
        (source_url(path), page) for path in paths for page in (1, 2)
    }
    assert len(chunks) == 4
    for chunk in chunks:
        path = paths[0] if chunk.document_id == source_url(paths[0]) else paths[1]
        image = stack.blobs.blobs[
            ("documents-page-images", f"{path}/page_{chunk.page_number}.jpg")
        ]
        assert base64.b64decode(chunk.page_image_base64) == image.content
        assert (
            chunk.page_image_url
            == source_url(path).replace("/documents/", "/documents-page-images/")
            + f"/page_{chunk.page_number}.jpg"
        )
        assert chunk.source_file == "report.pdf"
        assert (
            "Report" if chunk.page_number == 1 else "Second page"
        ) in chunk.text_content
        assert "sig=" not in chunk.document_id
    assert all(
        len(point.vector["hierarchical_pooled"][0]) == 320
        for point in await stack.points()
    )


async def test_unchanged_redelivery_updates_metadata_without_buying_more_embeddings(
    stack,
):
    event = stack.add_pdf(
        "report.pdf", "First page", "Second page", metadata={"category": "old"}
    )
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    before = await stack.points()
    requests, uploads = len(stack.inference.image_batches), len(stack.blobs.uploads)
    stack.blobs.blobs[("documents", "report.pdf")].metadata = {"category": "new"}

    assert (await stack.deliver(event)).settlements == [("complete", {})]
    after = await stack.points()
    assert len(stack.inference.image_batches) == requests
    assert len(stack.blobs.uploads) == uploads
    assert [
        (point.id, point.vector, point.payload["indexed_at"]) for point in after
    ] == [(point.id, point.vector, point.payload["indexed_at"]) for point in before]
    results = await stack.retriever.search_documents("report")
    assert len(results) == 2
    assert all(
        chunk.metadata["metadata"]["blob"] == {"category": "new"} for chunk in results
    )


@pytest.mark.parametrize("fail_text_write", [False, True])
async def test_ocr_correction_updates_text_without_reembedding_unchanged_pixels(
    stack, monkeypatch, fail_text_write
):
    def unavailable_markdown(*args, **kwargs):
        raise RuntimeError("Markdown unavailable; use raw OCR text")

    monkeypatch.setattr(
        "document_processor.page_processing.pymupdf4llm.to_markdown",
        unavailable_markdown,
    )

    def scanned_pdf(ocr_text):
        with fitz.open() as document:
            page = document.new_page(width=144, height=144)
            page.insert_text((10, 20), "Visible heading")
            page.insert_text((10, 50), ocr_text, render_mode=3)
            return document.tobytes()

    event = stack.event("scan.pdf")
    stack.blobs.put("scan.pdf", scanned_pdf("incorrect OCR"))
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    before = (await stack.points())[0]
    requests, uploads = len(stack.inference.image_batches), len(stack.blobs.uploads)

    stack.blobs.put("scan.pdf", scanned_pdf("corrected OCR"))
    if fail_text_write:
        client = stack.processor.qdrant_index.qdrant_client
        set_payload = client.set_payload

        async def unavailable(**kwargs):
            if set(kwargs["payload"]) == {"text_content"}:
                raise OSError("Text payload write unavailable")
            return await set_payload(**kwargs)

        monkeypatch.setattr(client, "set_payload", unavailable)
        assert (await stack.deliver(event)).settlements == [("abandon", {})]
        assert (await stack.points())[0].payload["file_hash"] == before.payload[
            "file_hash"
        ]
        monkeypatch.setattr(client, "set_payload", set_payload)
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    chunks = await stack.retriever.search_documents("corrected OCR")
    assert len(chunks) == 1
    assert "corrected OCR" in chunks[0].text_content
    assert "incorrect OCR" not in chunks[0].text_content
    after = (await stack.points())[0]
    assert after.payload["file_hash"] != before.payload["file_hash"]
    assert after.payload["page_hash"] == before.payload["page_hash"]
    assert after.vector == before.vector
    assert len(stack.inference.image_batches) == requests
    assert len(stack.blobs.uploads) == uploads


async def test_force_reindex_replaces_existing_pages_without_duplicates(stack):
    event = stack.add_pdf("report.pdf", "First page", "Second page")
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    ids = {point.id for point in await stack.points()}
    requests = len(stack.inference.image_batches)
    event["force_reindex"] = True
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    assert len(stack.inference.image_batches) == requests + 2
    assert {point.id for point in await stack.points()} == ids
    assert len(await stack.retriever.search_documents("report")) == 2


async def test_dpi_only_image_update_replaces_vectors_and_replay_reuses_them(stack):
    async def resolution_embedding(payload):
        if payload.get("images"):
            width = stack.inference.image_batches[-1][0][0]
            vector = [1.0, width / 1000] + [0.0] * 318
            return web.json_response(
                {
                    key: [[vector]]
                    for key in (
                        "hierarchical_pooled_embeddings",
                        "mean_row_pooled_embeddings",
                        "mean_column_pooled_embeddings",
                    )
                }
            )

    stack.inference.reply = resolution_embedding
    event = stack.event("scan.png")
    points = []
    with Image.new("RGB", (1000, 1000), "navy") as image:
        for dpi in (300, 150):
            with io.BytesIO() as buffer:
                image.save(buffer, format="PNG", dpi=(dpi, dpi))
                stack.blobs.put("scan.png", buffer.getvalue())
            assert (await stack.deliver(event)).settlements == [("complete", {})]
            points.append((await stack.points())[0])
    before, after = points
    assert stack.inference.image_batches == [[(500, 500)], [(1000, 1000)]]
    assert before.id == after.id
    assert before.payload["page_hash"] == after.payload["page_hash"]
    assert before.vector != after.vector
    assert before.payload["effective_dpi"] == 300
    assert after.payload["effective_dpi"] == 150
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    assert len(stack.inference.image_batches) == 2
    chunks = await stack.retriever.search_documents("scan")
    assert len(chunks) == 1
    with Image.open(io.BytesIO(base64.b64decode(chunks[0].page_image_base64))) as image:
        assert image.size == (1000, 1000)


@pytest.mark.parametrize(
    "pooling,fields",
    [
        ([PoolingType.NONE], {"embeddings"}),
        (
            [PoolingType.MEAN_POOLING],
            {"mean_row_pooled_embeddings", "mean_column_pooled_embeddings"},
        ),
        ([PoolingType.HIERARCHICAL_POOLING], {"hierarchical_pooled_embeddings"}),
        (
            list(PoolingType),
            {
                "embeddings",
                "mean_row_pooled_embeddings",
                "mean_column_pooled_embeddings",
                "hierarchical_pooled_embeddings",
            },
        ),
    ],
)
async def test_requested_pooling_representations_succeed_without_retries(
    stack, pooling, fields
):
    stack.processor.colpali_client.pooling_types = pooling
    with Image.new("RGB", (16, 16), "red") as image:
        page = DocumentPage(filename="page.png", image_content=image)
        result = await stack.processor.colpali_client.generate_embeddings([page, page])
    assert set(result) == fields
    assert all(len(batch) == 2 and len(batch[0][0]) == 320 for batch in result.values())
    assert len(stack.inference.image_batches) == 1


async def test_temporary_inference_failure_redelivers_and_resumes_durable_pages(stack):
    event = stack.add_pdf("report.pdf", "First page", "Second page", "Third page")

    async def fail_after_first_page(_):
        if len(stack.inference.image_batches) > 1:
            return web.json_response(
                {"detail": "GPU temporarily unavailable"}, status=503
            )

    stack.inference.reply = fail_after_first_page
    assert (await stack.deliver(event)).settlements == [("abandon", {})]
    partial = await stack.points()
    assert len(partial) == 1 and partial[0].payload["file_hash"] is None
    request_count = len(stack.inference.image_batches)

    stack.inference.reply = None
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    points = await stack.points()
    assert len(stack.inference.image_batches) == request_count + 2
    assert points[0].payload["indexed_at"] == partial[0].payload["indexed_at"]
    expected_hash = hashlib.sha256(
        stack.blobs.blobs[("documents", "report.pdf")].content
    ).hexdigest()
    assert {point.payload["file_hash"] for point in points} == {expected_hash}
    assert {
        chunk.page_number for chunk in await stack.retriever.search_documents("report")
    } == {1, 2, 3}


@pytest.mark.parametrize(
    "failure",
    ["missing_field", "missing_page", "wrong_dimension", "nonfinite", "invalid_json"],
)
async def test_bad_inference_responses_never_publish_or_acknowledge_a_document(
    stack, failure
):
    event = stack.add_pdf("report.pdf", "Must not be published")

    async def malformed(_):
        if failure == "invalid_json":
            return web.Response(text="not json", content_type="application/json")
        rows = [[1.0] + [0.0] * 319]
        if failure == "wrong_dimension":
            rows = [[1.0, 2.0]]
        elif failure == "nonfinite":
            rows[0][0] = float("nan")
        body = {
            "hierarchical_pooled_embeddings": [rows],
            "mean_row_pooled_embeddings": [rows],
            "mean_column_pooled_embeddings": []
            if failure == "missing_page"
            else [rows],
        }
        if failure == "missing_field":
            del body["mean_column_pooled_embeddings"]
        return web.json_response(body)

    stack.inference.reply = malformed
    assert (await stack.deliver(event)).settlements == [("abandon", {})]
    assert await stack.points() == []
    assert len(stack.inference.image_batches) == 3


@pytest.mark.parametrize("exhausted", [False, True], ids=["recovers", "dead_letters"])
async def test_token_limit_recovery_preserves_readable_storage_images(stack, exhausted):
    event = stack.add_pdf("report.pdf", "Original resolution must survive")

    async def enforce_token_limit(payload):
        if payload.get("images") and (
            exhausted or stack.inference.image_batches[-1][0][0] > 200
        ):
            return web.json_response(
                {
                    "detail": {
                        "code": "embedding_token_limit_exceeded",
                        "input_tokens": 3000,
                        "max_tokens": 2048,
                    }
                },
                status=422,
            )

    stack.inference.reply = enforce_token_limit
    message = await stack.deliver(event)
    sizes = [batch[0] for batch in stack.inference.image_batches]
    assert sizes[0] == (300, 300)
    assert all(next_size[0] < size[0] for size, next_size in zip(sizes, sizes[1:]))
    assert len(sizes) <= 4
    stored = stack.blobs.blobs[("documents-page-images", "report.pdf/page_1.jpg")]
    with Image.open(io.BytesIO(stored.content)) as image:
        assert image.size == (600, 600)
    if exhausted:
        assert len(sizes) == 4
        assert len(message.settlements) == 1
        operation, details = message.settlements[0]
        assert (
            operation == "dead_letter"
            and details["reason"] == "EmbeddingTokenLimitExceeded"
        )
        assert await stack.points() == []
    else:
        assert sizes[-1][0] <= 200
        assert message.settlements == [("complete", {})]
        assert len(await stack.retriever.search_documents("original resolution")) == 1


@pytest.mark.parametrize("stack", [{"COLPALI_STORAGE_DPI": "150"}], indirect=True)
async def test_a1_token_recovery_reduces_below_inference_cap_without_shrinking_citation(
    stack,
):
    with fitz.open() as document:
        document.new_page(width=1684, height=2384).insert_text((40, 60), "A1 drawing")
        stack.blobs.put("drawing.pdf", document.tobytes())

    async def enforce_capped_token_grid(payload):
        if not payload.get("images"):
            return None
        width, height = stack.inference.image_batches[-1][0]
        tokens = min(width * height, 4194304) // 1024 + 11
        if tokens > 3072:
            return web.json_response(
                {
                    "detail": {
                        "code": "embedding_token_limit_exceeded",
                        "input_tokens": tokens,
                        "max_tokens": 3072,
                    }
                },
                status=422,
            )

    stack.inference.reply = enforce_capped_token_grid
    assert (await stack.deliver(stack.event("drawing.pdf"))).settlements == [
        ("complete", {})
    ]
    sizes = [batch[0] for batch in stack.inference.image_batches]
    assert sizes[0][0] * sizes[0][1] > 17000000
    assert len(sizes) == 2
    assert sizes[-1][0] * sizes[-1][1] < 3072 * 1024
    chunks = await stack.retriever.search_documents("A1 drawing")
    assert len(chunks) == 1
    with Image.open(io.BytesIO(base64.b64decode(chunks[0].page_image_base64))) as image:
        assert image.size == sizes[0]


async def test_source_replacement_mid_processing_cannot_be_marked_complete(stack):
    event = stack.add_pdf("report.pdf", "Old version")

    async def replace_source(_):
        stack.blobs.put("report.pdf", make_pdf("Latest version"))
        stack.inference.reply = None

    stack.inference.reply = replace_source
    assert (await stack.deliver(event)).settlements == [("abandon", {})]
    assert all(point.payload["file_hash"] is None for point in await stack.points())
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    chunks = await stack.retriever.search_documents("version")
    assert len(chunks) == 1 and "Latest version" in chunks[0].text_content
    assert (
        chunks[0].metadata["file_hash"]
        == hashlib.sha256(
            stack.blobs.blobs[("documents", "report.pdf")].content
        ).hexdigest()
    )


async def test_mixed_event_failures_do_not_discard_retryable_work(stack):
    stack.blobs.put("broken.pdf", b"not a PDF")
    valid = stack.add_pdf("valid.pdf", "Recoverable document")

    async def offline(_):
        return web.json_response({"detail": "unavailable"}, status=503)

    stack.inference.reply = offline
    events = [stack.event("broken.pdf"), valid]
    assert (await stack.deliver(events)).settlements == [("abandon", {})]
    stack.inference.reply = None
    message = await stack.deliver(events)
    assert len(message.settlements) == 1
    operation, details = message.settlements[0]
    assert operation == "dead_letter" and details["reason"] == "InvalidDocument"
    results = await stack.retriever.search_documents("recoverable")
    assert len(results) == 1 and results[0].source_file == "valid.pdf"


async def test_image_storage_outage_cannot_complete_a_document_and_replay_recovers(
    stack,
):
    event = stack.add_pdf("report.pdf", "First page", "Second page")
    stack.blobs.failed_uploads.add("report.pdf/page_2.jpg")
    assert (await stack.deliver(event)).settlements == [("abandon", {})]
    partial = await stack.points()
    assert len(partial) == 1 and partial[0].payload["file_hash"] is None
    stack.blobs.failed_uploads.clear()
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    results = await stack.retriever.search_documents("report")
    assert len(results) == 2 and all(chunk.page_image_base64 for chunk in results)
    assert (await stack.points())[0].payload["indexed_at"] == partial[0].payload[
        "indexed_at"
    ]


async def test_replayed_deletion_removes_only_the_requested_document_and_images(stack):
    first = stack.add_pdf("folder/report.pdf", "Delete me", "Also delete me")
    second = stack.add_pdf("folder/report.pdf-copy.pdf", "Keep me")
    assert (await stack.deliver([first, second])).settlements == [("complete", {})]
    deletion = stack.event("folder/report.pdf", delete=True)
    for _ in range(2):
        assert (await stack.deliver(deletion)).settlements == [("complete", {})]
    results = await stack.retriever.search_documents("report")
    assert len(results) == 1 and results[0].source_file == "report.pdf-copy.pdf"
    assert results[0].page_image_base64
    images = {
        name
        for container, name in stack.blobs.blobs
        if container == "documents-page-images"
    }
    assert images == {"folder/report.pdf-copy.pdf/page_1.jpg"}


@pytest.mark.parametrize("status", [500, 502])
async def test_http_batch_recovery_does_not_drop_duplicate_or_reorder_pages(
    stack, status
):
    async def reject_batches(payload):
        if len(payload["images"]) > 1:
            return web.json_response(
                {"detail": "batch exhausted GPU memory"}, status=status
            )
        with Image.open(io.BytesIO(base64.b64decode(payload["images"][0]))) as image:
            color = image.getpixel((0, 0))
        channel = max(range(3), key=lambda index: color[index])
        vector = [0.0] * 320
        vector[channel] = 1.0
        return web.json_response(
            {
                "hierarchical_pooled_embeddings": [[vector]],
                "mean_row_pooled_embeddings": [[vector]],
                "mean_column_pooled_embeddings": [[vector]],
            }
        )

    stack.inference.reply = reject_batches
    pages = [
        DocumentPage(
            filename="report.pdf",
            page_number=index + 1,
            image_content=Image.new("RGB", (32, 32), color),
        )
        for index, color in enumerate(("red", "green", "blue"))
    ]
    try:
        output = await stack.processor.colpali_client.generate_embeddings(pages)
        for field in (
            "hierarchical_pooled_embeddings",
            "mean_row_pooled_embeddings",
            "mean_column_pooled_embeddings",
        ):
            assert len(output[field]) == 3
            assert [rows[0].index(1.0) for rows in output[field]] == [0, 1, 2]
    finally:
        for page in pages:
            page.image_content.close()


async def test_render_limit_and_markdown_failure_still_produce_searchable_text(
    stack, monkeypatch
):
    stack.processor.renderer.max_pixels = 10000

    def broken_markdown(*args, **kwargs):
        raise RuntimeError("Markdown extraction failed")

    monkeypatch.setattr(
        "document_processor.page_processing.pymupdf4llm.to_markdown", broken_markdown
    )
    event = stack.add_pdf("report.pdf", "Readable fallback")
    assert (await stack.deliver(event)).settlements == [("complete", {})]
    results = await stack.retriever.search_documents("readable fallback")
    assert len(results) == 1 and "Readable fallback" in results[0].text_content
    with Image.open(
        io.BytesIO(base64.b64decode(results[0].page_image_base64))
    ) as image:
        assert 0 < image.width * image.height <= 10000


@pytest.mark.parametrize(
    "mode", ["L", "RGBA", "P"], ids=["grayscale", "transparent", "palette"]
)
async def test_image_formats_keep_content_when_cropped_and_downscaled(stack, mode):
    stack.processor.renderer.max_pixels = 250000
    stack.processor.renderer.crop = True
    stack.processor.colpali_client.embedding_dpi = 75
    with Image.new(
        "RGB" if mode == "P" else mode,
        (1000, 1000),
        (255, 255, 255, 0) if mode == "RGBA" else "white",
    ) as image:
        image.paste("black", (400, 400, 600, 600))
        stream = io.BytesIO()
        if mode == "P":
            with image.convert("P", palette=Image.Palette.ADAPTIVE) as palette:
                palette.save(stream, format="PNG", dpi=(300, 300))
        else:
            image.save(stream, format="PNG", dpi=(300, 300))
    stack.blobs.put("scan.png", stream.getvalue())

    assert (await stack.deliver(stack.event("scan.png"))).settlements == [
        ("complete", {})
    ]
    results = await stack.retriever.search_documents("scan")
    assert len(results) == 1
    with Image.open(
        io.BytesIO(base64.b64decode(results[0].page_image_base64))
    ) as stored:
        assert stored.mode == "RGB"
        assert 100 <= stored.width <= 140 and 100 <= stored.height <= 140
        assert max(stored.getpixel((stored.width // 2, stored.height // 2))) < 10
        assert min(stored.getpixel((0, 0))) > 245
        assert stack.inference.image_batches == [
            [(stored.width // 2, stored.height // 2)]
        ]
