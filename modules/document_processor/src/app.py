"""
Document Processor Container App
Adapted from Azure Functions indexer to handle Event Grid webhook notifications
Processes documents using ColPali and indexes in QDRANT
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Union

from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob.aio import BlobServiceClient
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel

from colpali_client import ColPaliClient
from document_processor import DocumentProcessor
from search_indexer import SearchIndexer

# Global set to keep track of running background tasks
background_processing_tasks = set()


# Pydantic models for API documentation and validation
class HealthResponse(BaseModel):
    status: str
    service: str


class EventProcessingResponse(BaseModel):
    status: str
    eventCount: int
    processedCount: int


class EventGridValidationResponse(BaseModel):
    validationResponse: str


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for FastAPI application startup and shutdown events
    """
    # Startup
    global background_processing_tasks
    background_processing_tasks = set()
    logger.info(
        "Document processor startup complete - background task management initialized"
    )

    yield

    # Shutdown
    if background_processing_tasks:
        logger.info(
            f"Waiting for {len(background_processing_tasks)} background tasks to complete..."
        )
        await asyncio.gather(*background_processing_tasks, return_exceptions=True)
        background_processing_tasks.clear()
    logger.info("Document processor shutdown complete")


app = FastAPI(
    title="Document Processor",
    description="Container App for processing documents with ColPali via Event Grid webhooks",
    version="0.1.0",
    lifespan=lifespan,
)


# Configuration from environment variables
STORAGE_ACCOUNT_NAME = os.getenv("DATA_STORAGE_ACCOUNT_NAME")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")

# Initialize shared Azure credential (cached and reused)
if AZURE_CLIENT_ID:
    credential = ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)
else:
    credential = DefaultAzureCredential()

# Initialize processors
doc_processor = DocumentProcessor()
colpali_client = ColPaliClient(credential=credential)
search_indexer = SearchIndexer(credential=credential)

# Initialize Azure Blob Service Client only if storage account is configured
blob_service_client = None
if STORAGE_ACCOUNT_NAME:
    blob_service_client = BlobServiceClient(
        account_url=f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint for container readiness"""
    # Clean up any completed tasks
    completed_tasks = [task for task in background_processing_tasks if task.done()]
    for task in completed_tasks:
        background_processing_tasks.remove(task)

    active_tasks = len(background_processing_tasks)
    status = (
        "healthy" if active_tasks < 50 else "busy"
    )  # Mark as busy if too many background tasks

    return HealthResponse(
        status=f"{status}-tasks:{active_tasks}", service="document-processor"
    )


@app.post("/api/webhook")
async def handle_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> Union[EventGridValidationResponse, EventProcessingResponse]:
    """
    Handle Event Grid webhook notifications

    Implements Event Grid validation handshake as per Azure requirements:
    - Must return HTTP 200 OK status code
    - Must return JSON with "validationResponse" containing the validation code
    - Must complete within 30 seconds

    Then processes blob created events for document processing.
    """
    try:
        # Parse the request body to get events
        events = await request.json()
        logger.info(f"Received {len(events)} events")

        # Handle Event Grid subscription validation
        for event in events:
            if (
                event.get("eventType")
                == "Microsoft.EventGrid.SubscriptionValidationEvent"
            ):
                # Extract validation code from the event data
                validation_code = event.get("data", {}).get("validationCode")
                logger.info(
                    f"Validating Event Grid subscription with code: {validation_code}"
                )

                # Return validation response as per Event Grid requirements
                # Must return HTTP 200 with JSON containing validationResponse
                return EventGridValidationResponse(validationResponse=validation_code)

        # Handle actual blob events
        logger.info(f"Processing {len(events)} blob events")

        # Process events asynchronously using FastAPI BackgroundTasks
        accepted_count = 0

        for event in events:
            if event.get("eventType") == "Microsoft.Storage.BlobCreated":
                # Add background task for blob processing
                background_tasks.add_task(process_blob_event_background, event)
                accepted_count += 1
            elif event.get("eventType") == "Microsoft.Storage.BlobDeleted":
                # Add background task for blob deletion
                background_tasks.add_task(process_blob_deleted_event_background, event)
                accepted_count += 1

        # Return immediately to avoid Event Grid 30-second timeout
        # Processing will continue in the background via FastAPI's task management
        logger.info(f"Accepted {accepted_count} events for background processing")
        return EventProcessingResponse(
            status="accepted", eventCount=len(events), processedCount=accepted_count
        )

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


async def process_blob_event_background(event: Dict[str, Any]) -> None:
    """
    Background task wrapper for blob processing
    """
    try:
        blob_url = event.get("data", {}).get("url", "unknown")
        logger.info(f"Starting background blob processing for: {blob_url}")

        # Process the blob event directly in this background task
        await process_blob_event_with_cleanup(event)

        logger.info(f"Completed background blob processing for: {blob_url}")
    except Exception as e:
        logger.error(f"Failed to process blob in background: {str(e)}", exc_info=True)


async def process_blob_deleted_event_background(event: Dict[str, Any]) -> None:
    """
    Background task wrapper for blob deletion
    """
    try:
        blob_url = event.get("data", {}).get("url", "unknown")
        logger.info(f"Starting background blob deletion for: {blob_url}")

        # Process the blob deletion event directly in this background task
        await process_blob_deleted_event_with_cleanup(event)

        logger.info(f"Completed background blob deletion for: {blob_url}")
    except Exception as e:
        logger.error(f"Failed to delete blob in background: {str(e)}", exc_info=True)


async def process_blob_event_with_cleanup(event: Dict[str, Any]) -> None:
    """
    Wrapper that handles cleanup of completed tasks
    """
    blob_url = event.get("data", {}).get("url", "unknown")
    start_time = time.time()

    try:
        logger.info(f"Starting background processing for: {blob_url}")
        result = await process_blob_event_async(event)

        end_time = time.time()
        processing_time = end_time - start_time
        logger.info(
            f"Background blob processing completed successfully for {blob_url}: {result} (took {processing_time:.2f}s)"
        )

    except Exception as e:
        end_time = time.time()
        processing_time = end_time - start_time
        logger.error(
            f"Background blob processing failed for {blob_url} after {processing_time:.2f}s: {str(e)}",
            exc_info=True,
        )

    finally:
        # Clean up completed tasks
        current_task = asyncio.current_task()
        if current_task in background_processing_tasks:
            background_processing_tasks.remove(current_task)
            logger.debug(
                f"Removed completed task for {blob_url} (remaining: {len(background_processing_tasks)})"
            )


async def process_blob_deleted_event_with_cleanup(event: Dict[str, Any]) -> None:
    """
    Wrapper that handles cleanup of completed tasks
    """
    blob_url = event.get("data", {}).get("url", "unknown")
    start_time = time.time()

    try:
        logger.info(f"Starting background deletion for: {blob_url}")
        result = await process_blob_deleted_event_async(event)

        end_time = time.time()
        processing_time = end_time - start_time
        logger.info(
            f"Background blob deletion completed successfully for {blob_url}: {result} (took {processing_time:.2f}s)"
        )

    except Exception as e:
        end_time = time.time()
        processing_time = end_time - start_time
        logger.error(
            f"Background blob deletion failed for {blob_url} after {processing_time:.2f}s: {str(e)}",
            exc_info=True,
        )

    finally:
        # Clean up completed tasks
        current_task = asyncio.current_task()
        if current_task in background_processing_tasks:
            background_processing_tasks.remove(current_task)
            logger.debug(
                f"Removed completed deletion task for {blob_url} (remaining: {len(background_processing_tasks)})"
            )


def parse_blob_url(blob_url: str) -> tuple[str, str]:
    """
    Parse Azure Blob Storage URL to extract container name and blob name.

    URL format: https://{account}.blob.core.windows.net/{container}/{blob-path}

    Args:
        blob_url: Full blob URL from event data

    Returns:
        Tuple of (container_name, blob_name) where blob_name includes full path

    Example:
        parse_blob_url("https://storage.blob.core.windows.net/documents/reports/2024/file.pdf")
        Returns: ("documents", "reports/2024/file.pdf")
    """
    url_parts = blob_url.split("/")
    # URL structure: ['https:', '', 'account.blob.core.windows.net', 'container', 'path', 'to', 'blob']
    # Container is at index 3, blob name is everything from index 4 onwards
    container_name = url_parts[3]
    blob_name = "/".join(url_parts[4:])
    return container_name, blob_name


async def process_blob_event_async(event: Dict[str, Any]) -> bool:
    """
    Process a blob created event using the existing document processing logic
    Returns True if successful, False otherwise
    """
    start_time = time.time()

    try:
        # Extract blob information from event
        blob_url = event["data"]["url"]
        # Parse container and blob name from URL (supports subdirectories)
        container_name, blob_name = parse_blob_url(blob_url)

        logger.info(
            f"Document processor triggered for blob: {container_name}/{blob_name}"
        )

        # Skip if not in documents container
        if container_name != "documents":
            logger.info(f"Skipping blob in container: {container_name}")
            return False

        # Determine file type from blob name
        file_extension = os.path.splitext(blob_name)[1].lower()

        # Only process PDF files
        if file_extension != ".pdf":
            logger.info(f"Skipping non-PDF file: {blob_name} ({file_extension})")
            return False

        # Download blob content (requires storage account configuration)
        if not blob_service_client:
            logger.error("Storage account not configured - cannot process blob events")
            return False

        blob_client = blob_service_client.get_blob_client(
            container=container_name, blob=blob_name
        )

        download_stream = await blob_client.download_blob()
        blob_content = await download_stream.readall()
        blob_size = len(blob_content)

        logger.info(f"Downloaded blob: {blob_name}, size: {blob_size} bytes")

        # Now we can directly await the async processing since FastAPI supports async
        success = await process_document_async(
            blob_content, blob_name, file_extension, start_time
        )

        return success

    except Exception as e:
        end_time = time.time()
        processing_time = end_time - start_time
        logger.error(
            f"Error processing blob event after {processing_time:.2f} seconds: {str(e)}"
        )
        return False


async def process_blob_deleted_event_async(event: Dict[str, Any]) -> bool:
    """
    Process a blob deleted event by removing the document from the search index
    Returns True if successful, False otherwise
    """
    start_time = time.time()

    try:
        # Extract blob information from event
        blob_url = event["data"]["url"]
        # Parse container and blob name from URL (supports subdirectories)
        container_name, blob_name = parse_blob_url(blob_url)

        logger.info(
            f"Document deletion triggered for blob: {container_name}/{blob_name}"
        )

        # Skip if not in documents container
        if container_name != "documents":
            logger.info(f"Skipping blob in container: {container_name}")
            return False

        # Determine file type from blob name
        file_extension = os.path.splitext(blob_name)[1].lower()

        # Only process PDF files
        if file_extension != ".pdf":
            logger.info(f"Skipping non-PDF file: {blob_name} ({file_extension})")
            return False

        # Delete all pages for this document from the index
        delete_success = await search_indexer.delete_document_pages(blob_name)

        end_time = time.time()
        processing_time = end_time - start_time

        if delete_success:
            logger.info(
                f"Successfully deleted document pages for {blob_name} in {processing_time:.2f} seconds"
            )
            return True
        else:
            logger.error(
                f"Failed to delete document pages for {blob_name} after {processing_time:.2f} seconds"
            )
            return False

    except Exception as e:
        end_time = time.time()
        processing_time = end_time - start_time
        logger.error(
            f"Error processing blob deletion event after {processing_time:.2f} seconds: {str(e)}"
        )
        return False


async def process_document_async(
    blob_content: bytes, blob_name: str, file_extension: str, start_time: float
) -> bool:
    """
    Async document processing logic adapted from the original function app
    """
    try:
        logger.info(f"Processing PDF file: {blob_name}")

        # Delete existing pages for this document to handle re-indexing scenarios
        # This ensures that if a document is re-uploaded with fewer pages,
        # orphaned pages from the previous version are removed
        delete_success = await search_indexer.delete_document_pages(blob_name)
        if delete_success:
            logger.info(f"Cleared existing pages for document: {blob_name}")
        else:
            logger.warning(
                f"Could not clear existing pages for document: {blob_name}, continuing anyway"
            )

        # Process document into page chunks
        document_pages = doc_processor.process_document(
            content=blob_content, filename=blob_name, file_type=file_extension
        )

        logger.info(f"PDF split into {len(document_pages)} pages")

        # Free the large blob content from memory immediately
        del blob_content

        # Process pages in small batches to prevent memory exhaustion
        # Batch size scales with GPU concurrency (2x for efficient memory usage)
        max_concurrent_requests = int(os.getenv("COLPALI_MAX_CONCURRENT_REQUESTS", "3"))
        batch_size = max_concurrent_requests * 2

        async def process_page_batch(batch_pages: List[tuple]):
            """Process a batch of pages concurrently but with limited memory footprint."""

            async def process_single_page(page_index: int, page_chunk: Dict[str, Any]):
                """Process a single page and return the result with timing info."""
                page_start_time = time.time()
                try:
                    logger.info(f"Starting processing for page {page_index + 1}")

                    # Generate embeddings using ColPali online endpoint (async)
                    embeddings = await colpali_client.generate_embeddings(page_chunk)

                    # Clear page image from memory immediately after processing
                    if "page_image" in page_chunk:
                        page_chunk["page_image"].close()
                        del page_chunk["page_image"]

                    page_end_time = time.time()
                    page_processing_time = page_end_time - page_start_time

                    if embeddings:
                        # ColQwen2 returns structured embeddings with original and pooled versions
                        original_embeddings = embeddings.get("original_embeddings", [])
                        pooled_embeddings = embeddings.get("pooled_embeddings", [])
                        flattened_embeddings = {}

                        if original_embeddings and len(original_embeddings) > 0:
                            # Flatten batches: concatenate all patches from all batches
                            all_patches = []
                            for batch in original_embeddings:
                                if isinstance(batch, list):
                                    all_patches.extend(batch)
                            flattened_embeddings["original_embeddings"] = all_patches
                            num_patches = len(all_patches)
                            patch_dim = (
                                len(all_patches[0]) if len(all_patches) > 0 else 0
                            )
                        else:
                            num_patches = 0
                            patch_dim = 0

                        if pooled_embeddings and len(pooled_embeddings) > 0:
                            # Flatten batches: concatenate all patches from all batches
                            all_pooled_patches = []
                            for batch in pooled_embeddings:
                                if isinstance(batch, list):
                                    all_pooled_patches.extend(batch)
                            flattened_embeddings["pooled_embeddings"] = (
                                all_pooled_patches
                            )
                            pooled_patches = len(all_pooled_patches)
                            pooled_dim = (
                                len(all_pooled_patches[0])
                                if len(all_pooled_patches) > 0
                                else 0
                            )
                        else:
                            pooled_patches = 0
                            pooled_dim = 0

                        result = {
                            "page_id": page_chunk["page_number"],
                            "page_content": page_chunk,
                            "embeddings": flattened_embeddings,
                            "source_file": blob_name,
                            "num_patches": num_patches,
                            "patch_dimensions": patch_dim,
                            "pooled_patches": pooled_patches,
                            "pooled_dimensions": pooled_dim,
                        }

                        # Index this page immediately to avoid large batch timeouts
                        try:
                            index_success = await search_indexer.index_embeddings(
                                [result]
                            )
                            if index_success:
                                logger.info(
                                    f"Successfully generated and indexed embeddings for page {page_index + 1}: original ({num_patches} × {patch_dim}), pooled ({pooled_patches} × {pooled_dim}) in {page_processing_time:.2f}s"
                                )
                            else:
                                logger.warning(
                                    f"Generated embeddings for page {page_index + 1} ({num_patches} patches) but QDRANT indexing failed - check QDRANT connection and collection setup"
                                )
                        except Exception as index_error:
                            logger.error(
                                f"Failed to index page {page_index + 1}: {str(index_error)}"
                            )

                        return result
                    else:
                        logger.warning(
                            f"No embeddings generated for page {page_index + 1} (took {page_processing_time:.2f}s)"
                        )
                        return None

                except Exception as e:
                    page_end_time = time.time()
                    page_processing_time = page_end_time - page_start_time
                    logger.error(
                        f"Failed to generate embeddings for page {page_index + 1} after {page_processing_time:.2f}s: {str(e)}"
                    )
                    return None

            # Process all pages in this batch concurrently
            batch_tasks = [
                process_single_page(i, page_chunk) for i, page_chunk in batch_pages
            ]
            return await asyncio.gather(*batch_tasks, return_exceptions=True)

        # Process all pages in batches to prevent memory exhaustion
        embedding_start_time = time.time()
        logger.info(
            f"Starting batched processing of {len(document_pages)} pages (batch size: {batch_size})..."
        )

        embeddings_results: List[Dict[str, Any]] = []

        # Split pages into batches
        for batch_start in range(0, len(document_pages), batch_size):
            batch_end = min(batch_start + batch_size, len(document_pages))
            batch_pages = [
                (i, document_pages[i]) for i in range(batch_start, batch_end)
            ]

            logger.info(
                f"Processing batch {batch_start // batch_size + 1}: pages {batch_start + 1}-{batch_end}"
            )

            batch_results = await process_page_batch(batch_pages)

            # Filter out None results and exceptions from this batch
            for result in batch_results:
                if (
                    result is not None
                    and not isinstance(result, Exception)
                    and isinstance(result, dict)
                ):
                    embeddings_results.append(result)

        embedding_end_time = time.time()
        embedding_processing_time = embedding_end_time - embedding_start_time

        logger.info(
            f"Parallel processing completed in {embedding_processing_time:.2f}s"
        )
        logger.info(
            f"Generated embeddings for {len(embeddings_results)} out of {len(document_pages)} pages"
        )

        # Individual indexing now happens per page - no batch indexing needed
        logger.info("All pages processed with individual indexing")

        # Calculate processing time and log summary for monitoring
        end_time = time.time()
        processing_time = end_time - start_time
        total_patches = sum(result["num_patches"] for result in embeddings_results)
        avg_patch_dim = (
            embeddings_results[0]["patch_dimensions"] if embeddings_results else 0
        )

        logger.info(f"Document processing completed for: {blob_name}")
        logger.info(f"Processing time: {processing_time:.2f} seconds")
        logger.info(
            f"Summary: {len(embeddings_results)} pages processed, {total_patches} total patches, {avg_patch_dim} dimensions per patch"
        )

        return len(embeddings_results) > 0

    except Exception as e:
        end_time = time.time()
        processing_time = end_time - start_time
        logger.error(
            f"Error processing document {blob_name} after {processing_time:.2f} seconds: {str(e)}"
        )
        return False


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Document Processor Container App")

    # Run FastAPI app with Uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
