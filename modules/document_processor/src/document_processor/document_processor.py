# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Blob events -> incremental page rendering -> embeddings -> durable Qdrant pages."""

import asyncio
import hashlib
import io
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from importlib.metadata import version
from urllib.parse import quote

import fitz
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient
from PIL import UnidentifiedImageError
from tenacity import retry, stop_after_attempt, wait_fixed

from .colpali_client import VECTOR_FIELDS, ColPaliClient, EmbeddingTokenLimitError
from .models import (
    DocumentMetadata,
    DocumentPage,
    DocumentSource,
    ProcessedPage,
    ProcessingResult,
    ServiceBusEvent,
    compute_file_hash,
    compute_page_hash,
)
from .page_processing import PDF_EXECUTOR, PageRenderer, crop_whitespace, run_cpu
from .qdrant_index import QdrantIndex
from .service_bus import ServiceBusConsumer, bounded

logger = logging.getLogger(__name__)


class InvalidDocumentError(ValueError):
    pass


class DocumentProcessor:
    def __init__(
        self,
        require_service_bus=True,
        pooling_types=None,
        delete_existing_pages=True,
        shutdown_event=None,
    ):
        self.supported_formats = {".pdf", ".jpg", ".jpeg", ".png"}
        self.service_bus_namespace = os.getenv("SERVICE_BUS_NAMESPACE_NAME")
        self.queue_name = os.getenv("SERVICE_BUS_QUEUE_NAME", "document-processing")
        self.data_storage_account = os.getenv("DATA_STORAGE_ACCOUNT_NAME")
        if require_service_bus and not (
            self.service_bus_namespace and self.data_storage_account
        ):
            raise ValueError(
                "SERVICE_BUS_NAMESPACE_NAME and DATA_STORAGE_ACCOUNT_NAME are required"
            )
        self.credential = DefaultAzureCredential()
        self.shutdown_event = shutdown_event or asyncio.Event()
        self.delete_existing_pages = delete_existing_pages
        self.service_bus_client = None
        self._blob_service_client = None
        self._blob_client_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._index_initialized = False
        self._document_locks = {}
        self._container_lock = asyncio.Lock()
        self._containers = set()
        self.qdrant_index = QdrantIndex(require_endpoint=require_service_bus)
        self.colpali_client = ColPaliClient(
            require_endpoint=require_service_bus, pooling_types=pooling_types
        )
        self.pdf_image_dpi = int(
            os.getenv("COLPALI_STORAGE_DPI", os.getenv("COLPALI_IMAGE_DPI", "500"))
        )
        self.renderer = PageRenderer(
            self.pdf_image_dpi,
            int(os.getenv("PDF_MAX_RENDER_PIXELS", "75000000")),
            crop=os.getenv("COLPALI_CROP_WHITESPACE", "true").lower() == "true",
        )
        self.buffer_pages = int(os.getenv("RENDER_BUFFER_MAX_PAGES", "4"))
        blob_concurrency = int(os.getenv("BLOB_MAX_CONCURRENT", "250"))
        if min(self.buffer_pages, blob_concurrency) <= 0:
            raise ValueError("Render buffer and blob concurrency must be positive")
        self._blob_semaphore = asyncio.Semaphore(blob_concurrency)
        configuration = {
            "contract": 2,
            "page_reuse": 2,
            "revision": os.getenv("INDEXING_REVISION", "1"),
            "model": os.getenv("MODEL_ID", "TomoroAI/tomoro-ai-colqwen3-embed-4b-awq"),
            "dimension": self.qdrant_index.dimension,
            "storage_dpi": self.pdf_image_dpi,
            "embedding_dpi": self.colpali_client.embedding_dpi,
            "inference_max_pixels": int(
                os.getenv("COLPALI_MAX_IMAGE_PIXELS", "4194304")
            ),
            "max_pixels": self.renderer.max_pixels,
            "crop": self.renderer.crop,
            "pooling": [p.value for p in self.colpali_client.pooling_types],
            "render_versions": {
                name: version(name) for name in ("pymupdf", "pymupdf4llm", "pillow")
            },
        }
        self.fingerprint = hashlib.sha256(
            json.dumps(configuration, sort_keys=True).encode()
        ).hexdigest()

    @asynccontextmanager
    async def _document_lock(self, document_id):
        lock, users = self._document_locks.get(document_id, (asyncio.Lock(), 0))
        self._document_locks[document_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._document_locks[document_id]
            if users == 1:
                del self._document_locks[document_id]
            else:
                self._document_locks[document_id] = (lock, users - 1)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        errors = []
        for resource in (
            self.colpali_client,
            self.qdrant_index,
            self._blob_service_client,
            self.service_bus_client,
            self.credential,
        ):
            if resource is not None:
                try:
                    await bounded(resource.close(), 30)
                except Exception as error:
                    logger.exception("Failed to close processor resource")
                    errors.append(error)
        if errors:
            raise RuntimeError("Processor cleanup failed") from errors[0]

    async def _initialize_index(self):
        async with self._init_lock:
            if not self._index_initialized:
                if not await self.qdrant_index.initialize():
                    raise RuntimeError("Qdrant initialization failed")
                self._index_initialized = True

    async def _get_blob_service_client(self):
        async with self._blob_client_lock:
            if self._blob_service_client is None:
                if not self.data_storage_account:
                    raise ValueError("DATA_STORAGE_ACCOUNT_NAME is required")
                self._blob_service_client = BlobServiceClient(
                    account_url=f"https://{self.data_storage_account}.blob.core.windows.net",
                    credential=self.credential,
                )
        return self._blob_service_client

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(0.5), reraise=True)
    async def _download_blob_with_retry(self, blob_client):
        stream = await blob_client.download_blob()
        return await stream.readall()

    @staticmethod
    def _encode_image(image):
        with io.BytesIO() as buffer:
            image.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()

    async def upload_page_image(
        self,
        page_image,
        page_images_container,
        source_blob_name,
        page_number,
        source_metadata=None,
    ):
        client = await self._get_blob_service_client()
        async with self._container_lock:
            if page_images_container not in self._containers:
                try:
                    await client.get_container_client(
                        page_images_container
                    ).create_container()
                except ResourceExistsError:
                    pass
                self._containers.add(page_images_container)
        name = f"{source_blob_name}/page_{page_number}.jpg"
        async with self._blob_semaphore:
            content = await run_cpu(self._encode_image, page_image)
            blob = client.get_blob_client(container=page_images_container, blob=name)
            await self._upload_with_retry(blob, content)
        return f"https://{self.data_storage_account}.blob.core.windows.net/{page_images_container}/{quote(name, safe='/')}"

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(0.5), reraise=True)
    async def _upload_with_retry(self, blob, content):
        await blob.upload_blob(
            content,
            overwrite=True,
            content_settings=ContentSettings(
                content_type="image/jpeg", cache_control="no-cache"
            ),
        )

    async def initialize_service_bus(self):
        await self._initialize_index()
        self.service_bus_client = ServiceBusClient(
            fully_qualified_namespace=f"{self.service_bus_namespace}.servicebus.windows.net",
            credential=self.credential,
        )

    async def start_message_consumption(self):
        if self.service_bus_client is None:
            await self.initialize_service_bus()
        await ServiceBusConsumer(
            self.service_bus_client,
            self.queue_name,
            self._process_service_bus_message,
            self.shutdown_event,
        ).listen()

    async def _process_service_bus_message(self, message):
        try:
            body = json.loads(str(message))
            events = body if isinstance(body, list) else [body]
            if not events or any(not isinstance(event, dict) for event in events):
                raise ValueError("Expected a non-empty Event Grid event or array")
            parsed = [ServiceBusEvent.from_message_body(event) for event in events]
        except (ValueError, TypeError, KeyError) as error:
            logger.warning("Invalid event message: %s", error)
            return ProcessingResult(
                success=False,
                document_id="",
                permanent_failure=True,
                error_message=str(error),
            )
        failures = []
        for event in parsed:
            result = await self._process_event(event)
            if not result.success:
                failures.append(result)
        if failures:
            # A transient failure must not cause the whole Event Grid array to be
            # discarded because another event is permanently invalid.
            return next(
                (
                    failure
                    for failure in failures
                    if not failure.permanent_failure and not failure.dead_letter_reason
                ),
                failures[0],
            )
        return ProcessingResult(success=True, document_id="")

    async def _process_event(self, event):
        try:
            source = DocumentSource.from_url(event.data.blob_url)
        except ValueError as error:
            return ProcessingResult(
                success=False,
                document_id="",
                error_message=str(error),
                permanent_failure=True,
            )
        if source.file_extension not in self.supported_formats:
            logger.info(
                "Ignoring unsupported document extension: %s", source.file_extension
            )
            return ProcessingResult(
                success=True,
                document_id=source.document_id,
                skip_reason="unsupported_type",
            )
        if event.event_type == "Microsoft.Storage.BlobDeleted":
            return await self._handle_blob_deleted(source)
        if event.event_type != "Microsoft.Storage.BlobCreated":
            logger.info("Ignoring event type: %s", event.event_type)
            return ProcessingResult(
                success=True,
                document_id=source.document_id,
                skip_reason="unsupported_event",
            )
        try:
            client = await self._get_blob_service_client()
            blob = client.get_blob_client(container=source.container, blob=source.path)
            properties = await blob.get_blob_properties()
            content = await self._download_blob_with_retry(blob)

            async def verify_version():
                if (await blob.get_blob_properties()).etag != properties.etag:
                    raise RuntimeError("Source blob changed during indexing; retrying")

            return await self.process_document_complete(
                content,
                source.path,
                source.file_extension,
                source.blob_url,
                blob_metadata=properties.metadata,
                force_reindex=event.force_reindex,
                verify_version=verify_version,
            )
        except Exception as error:
            logger.exception("Blob event failed")
            return ProcessingResult(
                success=False, document_id=source.document_id, error_message=str(error)
            )

    async def _handle_blob_deleted(self, source):
        try:
            async with self._document_lock(source.document_id):
                await self._initialize_index()
                await self.qdrant_index.delete_document_pages(source.document_id)
                client = await self._get_blob_service_client()
                container = client.get_container_client(
                    f"{source.container}-page-images"
                )
                try:
                    async for blob in container.list_blobs(
                        name_starts_with=f"{source.path}/"
                    ):
                        await container.delete_blob(blob.name)
                except ResourceNotFoundError:
                    pass
            return ProcessingResult(success=True, document_id=source.document_id)
        except Exception as error:
            logger.exception("Document deletion failed")
            return ProcessingResult(
                success=False, document_id=source.document_id, error_message=str(error)
            )

    async def process_document_complete(
        self,
        blob_content,
        blob_name,
        file_extension,
        blob_url=None,
        page_images_container=None,
        blob_metadata=None,
        force_reindex=False,
        verify_version=None,
    ):
        start = time.monotonic()
        document_id = blob_url or blob_name
        try:
            if not blob_url:
                if not self.data_storage_account:
                    raise InvalidDocumentError(
                        "A blob URL or storage account is required"
                    )
                blob_url = f"https://{self.data_storage_account}.blob.core.windows.net/documents/{quote(blob_name, safe='/')}"
            source = DocumentSource.from_url(blob_url)
            document_id = source.document_id
            if source.file_extension not in self.supported_formats:
                raise InvalidDocumentError(
                    f"Unsupported format: {source.file_extension}"
                )
            if file_extension.lower() != source.file_extension:
                raise InvalidDocumentError("File extension does not match source URL")
            if not isinstance(blob_content, bytes) or not blob_content:
                raise InvalidDocumentError("Document must contain non-empty file bytes")
            async with self._document_lock(document_id):
                result = await self._process_document(
                    source,
                    blob_content,
                    blob_metadata or {},
                    page_images_container or f"{source.container}-page-images",
                    force_reindex,
                    verify_version,
                )
            result.processing_time_seconds = time.monotonic() - start
            logger.info(
                "Document complete: pages=%s indexed=%s skipped=%s duration=%.2fs",
                result.page_count,
                result.indexed_count,
                result.skipped_pages,
                result.processing_time_seconds,
            )
            return result
        except Exception as error:
            logger.exception("Document processing failed")
            return ProcessingResult(
                success=False,
                document_id=document_id,
                error_message=str(error),
                permanent_failure=isinstance(
                    error,
                    (InvalidDocumentError, UnidentifiedImageError, fitz.FileDataError),
                ),
                dead_letter_reason="EmbeddingTokenLimitExceeded"
                if isinstance(error, EmbeddingTokenLimitError)
                else None,
                processing_time_seconds=time.monotonic() - start,
            )

    async def _process_document(
        self, source, content, blob_metadata, image_container, force, verify_version
    ):
        await self._initialize_index()
        file_hash = await run_cpu(compute_file_hash, content)
        state = await self.qdrant_index.get_document_state(source.document_id)
        matching = (
            {
                number: payload
                for number, payload in state.items()
                if payload.get("processing_fingerprint") == self.fingerprint
            }
            if not force
            else {}
        )
        if matching and len(matching) == len(state):
            count = next(iter(matching.values())).get("page_count")
            if (
                isinstance(count, int)
                and count > 0
                and set(matching) == set(range(1, count + 1))
                and all(
                    p.get("file_hash") == file_hash and p.get("page_count") == count
                    for p in matching.values()
                )
            ):
                metadata = DocumentMetadata(
                    document=next(iter(matching.values()))
                    .get("metadata", {})
                    .get("document", {}),
                    blob=blob_metadata,
                )
                if verify_version:
                    await verify_version()
                await self.qdrant_index.complete_document(
                    source.document_id,
                    file_hash,
                    self.fingerprint,
                    count,
                    metadata,
                )
                return ProcessingResult(
                    success=True,
                    document_id=source.document_id,
                    indexed_count=count,
                    page_count=count,
                    skipped_pages=count,
                    skip_reason="file_unchanged",
                )
        image_page = None
        if source.file_extension == ".pdf":
            try:
                count, pdf_metadata = await run_cpu(
                    self.renderer.pdf_info, content, executor=PDF_EXECUTOR
                )
            except ValueError as error:
                raise InvalidDocumentError(str(error)) from error
        else:
            image_page = await run_cpu(
                self.renderer.image_page, content, source.file_name
            )
            count, pdf_metadata = 1, {}
        if count <= 0:
            raise InvalidDocumentError("Document has no pages")
        metadata = DocumentMetadata(
            document={
                **pdf_metadata,
                "filename": source.file_name,
                "file_extension": source.file_extension,
            },
            blob=blob_metadata,
        )
        resume = {
            number
            for number, payload in matching.items()
            if payload.get("indexed_with_file_hash") == file_hash
            and 1 <= number <= count
            and payload.get("source", {}).get("page_image_url")
        }
        completed = set(resume)
        skipped = set(resume)
        queue = asyncio.Queue(maxsize=self.buffer_pages)
        sentinel = object()
        workers = self.colpali_client.max_concurrent

        async def produce():
            generator = (
                self.renderer.iter_pdf(content, source.file_name, resume)
                if image_page is None
                else iter([image_page])
            )
            current = None
            try:
                while True:
                    current = await run_cpu(
                        next, generator, sentinel, executor=PDF_EXECUTOR
                    )
                    if current is sentinel:
                        break
                    if current.page_number in resume:
                        current.image_content.close()
                    else:
                        await queue.put(current)
                    current = None
                for _ in range(workers):
                    await queue.put(sentinel)
            finally:
                if isinstance(current, DocumentPage):
                    current.image_content.close()
                if hasattr(generator, "close"):
                    await run_cpu(generator.close, executor=PDF_EXECUTOR)

        async def process_batch(batch):
            changed = []
            hashes = {}
            for page in batch:
                page_hash = await run_cpu(compute_page_hash, page.image_content)
                hashes[page.page_number] = page_hash
                previous = matching.get(page.page_number, {})
                if (
                    previous.get("page_hash") == page_hash
                    and previous.get("effective_dpi") == page.effective_dpi
                    and previous.get("source", {}).get("page_image_url")
                ):
                    if previous.get("text_content") != page.text_content:
                        await self.qdrant_index.update_page_text(
                            source.document_id, page.page_number, page.text_content
                        )
                    completed.add(page.page_number)
                    skipped.add(page.page_number)
                else:
                    if self.renderer.crop:
                        cropped = await run_cpu(crop_whitespace, page.image_content)
                        if cropped is not page.image_content:
                            page.image_content.close()
                            page.image_content = cropped
                    changed.append(page)
            if not changed:
                return
            async with asyncio.TaskGroup() as group:
                embeddings = group.create_task(
                    self.colpali_client.generate_embeddings(changed)
                )
                uploads = [
                    group.create_task(
                        self.upload_page_image(
                            page.image_content,
                            image_container,
                            source.path,
                            page.page_number,
                        )
                    )
                    for page in changed
                ]
            result = embeddings.result()
            for key in VECTOR_FIELDS:
                if not isinstance(result.get(key), list) or len(result[key]) != len(
                    changed
                ):
                    raise RuntimeError(f"Incomplete embedding batch: {key}")
            points = [
                ProcessedPage(
                    source=source,
                    metadata=metadata,
                    page_number=page.page_number,
                    text_content=page.text_content,
                    page_image_url=uploads[index].result(),
                    embeddings={
                        target: result[key][index]
                        for key, target in VECTOR_FIELDS.items()
                    },
                    indexed_with_file_hash=file_hash,
                    page_hash=hashes[page.page_number],
                    effective_dpi=page.effective_dpi,
                    processing_fingerprint=self.fingerprint,
                    page_count=count,
                )
                for index, page in enumerate(changed)
            ]
            if await self.qdrant_index.index_embeddings(points) != len(points):
                raise RuntimeError("Incomplete Qdrant write")
            completed.update(page.page_number for page in changed)

        async def consume():
            while True:
                page = await queue.get()
                if page is sentinel:
                    return
                batch = [page]
                finished = False
                try:
                    while len(batch) < self.colpali_client.batch_size:
                        try:
                            page = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if page is sentinel:
                            finished = True
                            break
                        batch.append(page)
                    await process_batch(batch)
                finally:
                    for page in batch:
                        page.image_content.close()
                if finished:
                    return

        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(produce())
                for _ in range(workers):
                    group.create_task(consume())
        except ExceptionGroup as errors:

            def leaf(error):
                return (
                    leaf(error.exceptions[0])
                    if isinstance(error, ExceptionGroup)
                    else error
                )

            raise leaf(errors) from errors
        finally:
            while not queue.empty():
                page = queue.get_nowait()
                if page is not sentinel:
                    page.image_content.close()
            if image_page is not None:
                image_page.image_content.close()
        if completed != set(range(1, count + 1)):
            raise RuntimeError(f"Incomplete page coverage: {len(completed)}/{count}")
        if verify_version:
            await verify_version()
        if self.delete_existing_pages:
            if not await self.qdrant_index.delete_orphan_pages(
                source.document_id, count
            ):
                raise RuntimeError("Orphan cleanup failed")
        await self.qdrant_index.complete_document(
            source.document_id,
            file_hash,
            self.fingerprint,
            count,
            metadata,
        )
        return ProcessingResult(
            success=True,
            document_id=source.document_id,
            indexed_count=count,
            page_count=count,
            skipped_pages=len(skipped),
            skip_reason="all_pages_unchanged"
            if len(skipped) == count
            else "partial_pages_unchanged"
            if skipped
            else None,
        )
