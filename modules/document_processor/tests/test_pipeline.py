# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fitz
import pytest
from PIL import Image
from qdrant_client import AsyncQdrantClient, models

from document_processor.colpali_client import VECTOR_FIELDS
from document_processor.document_processor import DocumentProcessor
from document_processor.models import (
    DocumentPage,
    DocumentSource,
    ProcessedPage,
    generate_page_id,
)
from document_processor.qdrant_index import IncompatibleCollectionError

URL = "https://example.blob.core.windows.net/documents/folder/report.pdf"


def pdf(*texts):
    with fitz.open() as document:
        for text in texts:
            document.new_page(width=144, height=144).insert_text((20, 40), text)
        return document.tobytes()


@pytest.fixture
async def processor(monkeypatch):
    monkeypatch.setenv("COLPALI_EMBEDDING_DIMENSION", "3")
    monkeypatch.setenv("COLPALI_MAX_CONCURRENT", "1")
    monkeypatch.setenv("COLPALI_BATCH_SIZE", "1")
    instance = DocumentProcessor(require_service_bus=False)
    instance.qdrant_index.qdrant_client = AsyncQdrantClient(location=":memory:")

    async def embed(pages):
        return {key: [[[1.0, 0.5, 0.2]] for _ in pages] for key in VECTOR_FIELDS}

    instance.colpali_client.generate_embeddings = AsyncMock(side_effect=embed)
    instance.upload_page_image = AsyncMock(return_value="https://example/page.jpg")
    yield instance
    await instance.close()


async def process(processor, content, **kwargs):
    return await processor.process_document_complete(
        content,
        "folder/report.pdf",
        ".pdf",
        URL,
        **kwargs,
    )


async def test_new_contract_and_unchanged_metadata_refresh(processor):
    content = pdf("one", "two")
    result = await process(processor, content, blob_metadata={"category": "first"})
    assert result.success and result.indexed_count == result.page_count == 2
    source = DocumentSource.from_url(URL)
    points = await processor.qdrant_index.qdrant_client.retrieve(
        processor.qdrant_index.collection_name,
        [generate_page_id(URL, 1)],
        with_vectors=True,
    )
    assert points[0].payload["source"] == {
        **source.model_dump(),
        "page_image_url": "https://example/page.jpg",
    }
    assert points[0].payload["metadata"]["document"]["filename"] == "report.pdf"
    assert set(points[0].vector) == {
        "hierarchical_pooled",
        "mean_row_pooled",
        "mean_column_pooled",
    }
    assert points[0].payload["file_hash"]
    processor.renderer.pdf_info = Mock(
        side_effect=AssertionError("should not render unchanged file")
    )
    processor.colpali_client.generate_embeddings.reset_mock()
    result = await process(processor, content, blob_metadata={"category": "updated"})
    assert result.success and result.skip_reason == "file_unchanged"
    processor.colpali_client.generate_embeddings.assert_not_awaited()
    state = await processor.qdrant_index.get_document_state(URL)
    assert all(p["metadata"]["blob"] == {"category": "updated"} for p in state.values())


async def test_changed_pages_only_and_no_reupsert_of_unchanged(processor):
    assert (await process(processor, pdf("one", "two"))).success
    processor.colpali_client.generate_embeddings.reset_mock()
    processor.upload_page_image.reset_mock()
    result = await process(processor, pdf("one", "new second page"))
    assert result.success and result.skipped_pages == 1
    assert processor.colpali_client.generate_embeddings.await_count == 1
    assert processor.upload_page_image.await_count == 1
    assert (
        processor.colpali_client.generate_embeddings.call_args.args[0][0].page_number
        == 2
    )


async def test_force_and_configuration_changes_reembed(processor):
    content = pdf("one", "two")
    assert (await process(processor, content)).success
    result = await process(processor, content, force_reindex=True)
    assert result.success and result.skipped_pages == 0
    processor.fingerprint = "changed-render-or-model-configuration"
    result = await process(processor, content)
    assert result.success and result.skipped_pages == 0


async def test_partial_failure_resumes_without_rendering_written_pages(processor):
    content = pdf("one", "two", "three")
    original = processor.colpali_client.generate_embeddings.side_effect

    async def fail_second(pages):
        if pages[0].page_number == 2:
            raise RuntimeError("inference unavailable")
        return await original(pages)

    processor.colpali_client.generate_embeddings.side_effect = fail_second
    failed = await process(processor, content)
    assert not failed.success
    state = await processor.qdrant_index.get_document_state(URL)
    assert set(state) == {1}
    assert state[1]["file_hash"] is None
    renderer = processor.renderer.iter_pdf
    seen_skips = []

    def observe(content, name, skips):
        seen_skips.append(skips)
        return renderer(content, name, skips)

    processor.renderer.iter_pdf = observe
    processor.colpali_client.generate_embeddings.side_effect = original
    result = await process(processor, content)
    assert result.success and result.skipped_pages == 1
    assert seen_skips == [{1}]
    state = await processor.qdrant_index.get_document_state(URL)
    assert len({p["file_hash"] for p in state.values()}) == 1
    assert all(p["file_hash"] for p in state.values())


async def test_missing_page_is_not_a_complete_file_skip(processor):
    content = pdf("one", "two")
    assert (await process(processor, content)).success
    await processor.qdrant_index.qdrant_client.delete(
        processor.qdrant_index.collection_name,
        [generate_page_id(URL, 2)],
    )
    processor.colpali_client.generate_embeddings.reset_mock()
    result = await process(processor, content)
    assert result.success and result.skip_reason != "file_unchanged"
    assert processor.colpali_client.generate_embeddings.await_count == 1


async def test_hash_stamp_failure_is_retryable(processor):
    processor.qdrant_index.complete_document = AsyncMock(
        side_effect=RuntimeError("stamp unavailable")
    )
    assert not (await process(processor, pdf("one"))).success
    state = await processor.qdrant_index.get_document_state(URL)
    assert state[1]["file_hash"] is None


async def test_orphans_removed_only_after_successful_replacement(processor):
    assert (await process(processor, pdf("one", "two", "three"))).success
    processor.colpali_client.generate_embeddings = AsyncMock(
        side_effect=RuntimeError("offline")
    )
    assert not (await process(processor, pdf("changed"))).success
    assert len(await processor.qdrant_index.get_document_state(URL)) == 3


async def test_successful_shrink_removes_orphans(processor):
    assert (await process(processor, pdf("one", "two"))).success
    assert (await process(processor, pdf("one"))).success
    assert set(await processor.qdrant_index.get_document_state(URL)) == {1}


async def test_source_change_prevents_completion(processor):
    processor.qdrant_index.complete_document = AsyncMock()
    result = await process(
        processor,
        pdf("one"),
        verify_version=AsyncMock(side_effect=RuntimeError("source changed")),
    )
    assert not result.success
    processor.qdrant_index.complete_document.assert_not_awaited()


@pytest.mark.parametrize("content", [b"", b"not a pdf"])
async def test_invalid_documents_are_not_successful(processor, content):
    result = await process(processor, content)
    assert not result.success and result.permanent_failure


async def test_legacy_collection_rejected_without_deleting_data(processor):
    client = processor.qdrant_index.qdrant_client
    await client.create_collection(
        processor.qdrant_index.collection_name,
        vectors_config={
            "mean_pooled_rows": models.VectorParams(
                size=128, distance=models.Distance.COSINE
            )
        },
    )
    await client.upsert(
        processor.qdrant_index.collection_name,
        [
            models.PointStruct(
                id=1,
                vector={"mean_pooled_rows": [1.0] * 128},
                payload={"text": "Keep existing data"},
            )
        ],
    )
    before = await client.retrieve(
        processor.qdrant_index.collection_name, [1], with_vectors=True
    )
    with pytest.raises(IncompatibleCollectionError):
        await processor.qdrant_index.initialize()
    assert (
        await client.retrieve(
            processor.qdrant_index.collection_name, [1], with_vectors=True
        )
        == before
    )


async def test_render_backpressure_and_cancellation_cleanup(processor):
    images = []
    reached_embedding = asyncio.Event()
    processor.renderer.pdf_info = lambda _: (100, {})

    def pages(*_):
        for number in range(1, 101):
            image = Image.new("RGB", (16, 16), "white")
            images.append(image)
            yield DocumentPage(
                filename="report.pdf", page_number=number, image_content=image
            )

    async def blocked(_):
        reached_embedding.set()
        await asyncio.Event().wait()

    processor.renderer.iter_pdf = pages
    processor.colpali_client.generate_embeddings = blocked
    processor.qdrant_index.complete_document = AsyncMock()
    task = asyncio.create_task(process(processor, b"source bytes"))
    await asyncio.wait_for(reached_embedding.wait(), timeout=5)
    await asyncio.sleep(0.05)
    assert (
        len(images) <= processor.buffer_pages + processor.colpali_client.batch_size + 1
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    for image in images:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))
    processor.qdrant_index.complete_document.assert_not_awaited()
    assert not processor._document_locks


async def test_encrypted_pdf_is_a_permanent_failure(processor):
    with fitz.open() as document:
        document.new_page()
        content = document.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="owner",
            user_pw="reader",
        )
    result = await process(processor, content)
    assert not result.success and result.permanent_failure
    processor.colpali_client.generate_embeddings.assert_not_awaited()


async def test_qdrant_retries_only_failed_batch_and_persists_every_page(
    processor, monkeypatch
):
    index = processor.qdrant_index
    source = DocumentSource.from_url(URL)
    pages = [
        ProcessedPage(
            source=source,
            page_number=n,
            processing_fingerprint="test",
            page_count=3,
            embeddings={name: [[1.0, 0.5, 0.2]] for name in VECTOR_FIELDS.values()},
        )
        for n in range(1, 4)
    ]
    await index.initialize()
    original_upsert = index.qdrant_client.upsert
    calls = []

    async def intermittent_failure(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise OSError("Temporary storage failure")
        return await original_upsert(**kwargs)

    monkeypatch.setattr(index.qdrant_client, "upsert", intermittent_failure)
    index.upsert_batch_size = 2
    assert await index.index_embeddings(pages) == 3
    assert [len(call["points"]) for call in calls] == [2, 1, 1]
    assert all(call["wait"] is True for call in calls)
    stored = await index.qdrant_client.retrieve(
        index.collection_name, [page.page_id for page in pages], with_vectors=True
    )
    assert {point.payload["page_number"] for point in stored} == {1, 2, 3}
    assert all(set(point.vector) == set(VECTOR_FIELDS.values()) for point in stored)


async def test_existing_vector_storage_is_reconciled_without_recreation(processor):
    index = processor.qdrant_index
    vectors = index._get_default_vectors_config()
    for vector in vectors.values():
        vector.on_disk = False
    original = index.qdrant_client
    client = SimpleNamespace(
        collection_exists=AsyncMock(return_value=True),
        get_collection=AsyncMock(
            return_value=SimpleNamespace(
                config=SimpleNamespace(params=SimpleNamespace(vectors=vectors))
            )
        ),
        update_collection=AsyncMock(return_value=True),
        create_collection=AsyncMock(),
        create_payload_index=AsyncMock(),
    )
    index.qdrant_client = client
    try:
        assert await index.initialize()
        client.create_collection.assert_not_awaited()
        updates = client.update_collection.call_args.kwargs["vectors_config"]
        assert set(updates) == set(vectors)
        assert all(vector.on_disk for vector in updates.values())
    finally:
        index.qdrant_client = original
