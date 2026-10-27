# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import io
import json
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
from uuid import uuid4

import fitz
import pytest
from agent.document_retriever import DocumentRetriever
from aiohttp import web
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from PIL import Image
from qdrant_client import AsyncQdrantClient

from document_processor.document_processor import DocumentProcessor
from document_processor.service_bus import ServiceBusConsumer


def make_pdf(*texts):
    with fitz.open() as document:
        for text in texts:
            document.new_page(width=144, height=144).insert_text((20, 40), text)
        return document.tobytes()


def source_url(path):
    return f"https://example.blob.core.windows.net/documents/{quote(path, safe='/')}"


@dataclass
class StoredBlob:
    content: bytes
    metadata: dict
    etag: int


class MemoryBlobService:
    """Azure SDK boundary fake shared by the real writer and image reader."""

    def __init__(self):
        self.blobs = {}
        self.containers = {"documents"}
        self.uploads = []
        self.downloads = []
        self.failed_uploads = set()

    def put(self, path, content, metadata=None, container="documents"):
        key = (container, path)
        previous = self.blobs.get(key)
        self.blobs[key] = StoredBlob(
            content, metadata or {}, previous.etag + 1 if previous else 1
        )

    def get_blob_client(self, container, blob):
        key = (container, blob)

        async def properties():
            stored = self.blobs[key]
            return SimpleNamespace(metadata=dict(stored.metadata), etag=stored.etag)

        async def download():
            async def readall():
                self.downloads.append(key)
                if key not in self.blobs:
                    raise ResourceNotFoundError("Blob not found")
                return self.blobs[key].content

            return SimpleNamespace(readall=readall)

        async def upload(content, *, overwrite, content_settings):
            if blob in self.failed_uploads:
                raise OSError("Storage temporarily unavailable")
            assert overwrite and content_settings.content_type == "image/jpeg"
            self.put(blob, content, container=container)
            self.uploads.append(key)

        return SimpleNamespace(
            get_blob_properties=properties, download_blob=download, upload_blob=upload
        )

    def get_container_client(self, container):
        async def create():
            if container in self.containers:
                raise ResourceExistsError("Container already exists")
            self.containers.add(container)

        async def list_blobs(name_starts_with):
            for stored_container, name in list(self.blobs):
                if stored_container == container and name.startswith(name_starts_with):
                    yield SimpleNamespace(name=name)

        async def delete(name):
            del self.blobs[(container, name)]

        return SimpleNamespace(
            create_container=create, list_blobs=list_blobs, delete_blob=delete
        )

    async def close(self):
        pass


class InferenceEndpoint:
    """HTTP contract double for the GPU service; never replaces the HTTP client."""

    def __init__(self):
        self.image_batches = []
        self.reply = None

    async def handle(self, request):
        payload = await request.json()
        if payload.get("images"):
            sizes = []
            for encoded in payload["images"]:
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                    image.load()
                    sizes.append(image.size)
            self.image_batches.append(sizes)
        if self.reply is not None:
            response = await self.reply(payload)
            if response is not None:
                return response
        vectors = [
            [[1.0] + [0.0] * 319]
            for _ in payload.get("images", payload.get("texts", []))
        ]
        if payload.get("texts"):
            return web.json_response({"embeddings": vectors})
        response = {}
        if "none" in payload.get("pooling_type", []):
            response["embeddings"] = vectors
        if "hierarchical_pooling" in payload.get("pooling_type", []):
            response["hierarchical_pooled_embeddings"] = vectors
        if "mean_pooling" in payload.get("pooling_type", []):
            response["mean_row_pooled_embeddings"] = vectors
            response["mean_column_pooled_embeddings"] = vectors
        return web.json_response(response)


@dataclass
class QueueMessage:
    body: str
    delivery_count: int = 1
    auto_renew_error: Exception | None = None
    settlements: list = field(default_factory=list)

    def __str__(self):
        return self.body


class QueueReceiver:
    async def complete_message(self, message):
        message.settlements.append(("complete", {}))

    async def abandon_message(self, message):
        message.settlements.append(("abandon", {}))

    async def dead_letter_message(self, message, **details):
        message.settlements.append(("dead_letter", details))


class DocumentStack:
    def __init__(self, processor, retriever, blobs, inference):
        self.processor = processor
        self.retriever = retriever
        self.blobs = blobs
        self.inference = inference
        self.consumer = ServiceBusConsumer(
            None,
            "documents",
            processor._process_service_bus_message,
            processor.shutdown_event,
        )

    def add_pdf(self, path, *texts, metadata=None):
        self.blobs.put(path, make_pdf(*texts), metadata)
        return self.event(path)

    @staticmethod
    def event(path, *, force=False, delete=False):
        return {
            "eventType": "Microsoft.Storage.BlobDeleted"
            if delete
            else "Microsoft.Storage.BlobCreated",
            "data": {"url": source_url(path)},
            "force_reindex": force,
        }

    async def deliver(self, event):
        message = QueueMessage(json.dumps(event))
        await self.consumer.process_message(QueueReceiver(), message)
        return message

    async def points(self):
        points, offset = await self.processor.qdrant_index.qdrant_client.scroll(
            collection_name=self.processor.qdrant_index.collection_name,
            with_payload=True,
            with_vectors=True,
            limit=100,
        )
        assert offset is None
        return sorted(
            points,
            key=lambda point: (
                point.payload["document_id"],
                point.payload["page_number"],
            ),
        )


@pytest.fixture
async def stack(monkeypatch, request):
    qdrant_url = os.getenv("TEST_QDRANT_URL")
    if qdrant_url and urlsplit(qdrant_url).hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        raise ValueError("TEST_QDRANT_URL must target an isolated local test server")
    inference = InferenceEndpoint()
    app = web.Application()
    app.router.add_post("/embeddings", inference.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    processor = retriever = None
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        endpoint = f"http://127.0.0.1:{runner.addresses[0][1]}"
        settings = {
            "COLPALI_INFERENCE_ENDPOINT": endpoint,
            "COLPALI_EMBEDDING_DIMENSION": "320",
            "COLPALI_MAX_CONCURRENT": "1",
            "COLPALI_BATCH_SIZE": "1",
            "COLPALI_STORAGE_DPI": "300",
            "COLPALI_EMBEDDING_DPI": "150",
            "COLPALI_CROP_WHITESPACE": "false",
            "QDRANT_COLLECTION_NAME": f"workflow-documents-{uuid4().hex}",
            "DATA_STORAGE_ACCOUNT_NAME": "example",
            "AZURE_TOKEN_CREDENTIALS": "EnvironmentCredential",
        }
        settings.update(getattr(request, "param", {}))
        for name, value in settings.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        monkeypatch.delenv("QDRANT_ENDPOINT", raising=False)
        blobs = MemoryBlobService()
        monkeypatch.setattr(
            "document_processor.document_processor.BlobServiceClient", lambda **_: blobs
        )
        monkeypatch.setattr(
            "agent.document_retriever.BlobServiceClient", lambda **_: blobs
        )
        processor = DocumentProcessor(require_service_bus=False)
        processor.qdrant_index.qdrant_client = (
            AsyncQdrantClient(url=qdrant_url, api_key=os.getenv("TEST_QDRANT_API_KEY"))
            if qdrant_url
            else AsyncQdrantClient(location=":memory:")
        )
        monkeypatch.setattr(
            "agent.document_retriever.AsyncQdrantClient",
            lambda **_: processor.qdrant_index.qdrant_client,
        )
        retriever = DocumentRetriever(fetch_images=True)
        yield DocumentStack(processor, retriever, blobs, inference)
    finally:
        if retriever is not None:
            await retriever.credential.close()
        if processor is not None:
            if qdrant_url:
                client = processor.qdrant_index.qdrant_client
                collection = processor.qdrant_index.collection_name
                if await client.collection_exists(collection):
                    await client.delete_collection(collection)
            await processor.close()
        await runner.cleanup()
