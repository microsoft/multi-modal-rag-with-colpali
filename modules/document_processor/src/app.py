"""
Document Processor Container App
Adapted from Azure Functions indexer to handle Event Grid webhook notifications
Processes documents using ColPali and indexes in QDRANT
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List

from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from colpali_client import ColPaliClient

# Import existing processors from the indexer module
from document_processor import DocumentProcessor
from search_indexer import SearchIndexer


# Pydantic models for API documentation and validation
class HealthResponse(BaseModel):
    status: str
    service: str
    components: Dict[str, str]


class WebhookResponse(BaseModel):
    status: str
    eventCount: int
    processedCount: int


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Document Processor",
    description="Container App for processing documents with ColPali via Event Grid webhooks",
    version="0.1.0",
)

# Configuration from environment variables
STORAGE_ACCOUNT_NAME = os.getenv("STORAGE_ACCOUNT_NAME")

# Initialize shared Azure credential (cached and reused)
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
    return HealthResponse(
        status="healthy",
        service="document-processor",
        components={
            "doc_processor": "ready",
            "colpali_client": "ready",
            "search_indexer": "ready",
            "storage_client": "ready" if blob_service_client else "not configured",
        },
    )


@app.post("/api/webhook")
async def handle_webhook(request: Request):
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
                return JSONResponse(
                    content={"validationResponse": validation_code}, status_code=200
                )

        # Handle actual blob events
        logger.info(f"Processing {len(events)} blob events")

        processed_count = 0
        for event in events:
            if event.get("eventType") == "Microsoft.Storage.BlobCreated":
                if await process_blob_event_async(event):
                    processed_count += 1
            elif event.get("eventType") == "Microsoft.Storage.BlobDeleted":
                if await process_blob_deleted_event_async(event):
                    processed_count += 1

        return WebhookResponse(
            status="processed", eventCount=len(events), processedCount=processed_count
        )

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


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
        delete_success = search_indexer.delete_document_pages(blob_name)

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
        delete_success = search_indexer.delete_document_pages(blob_name)
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

        # Process all pages in parallel with ColPali (same logic as function app)
        async def process_page(page_index: int, page_chunk: Dict[str, Any]):
            """Process a single page and return the result with timing info."""
            page_start_time = time.time()
            try:
                logger.info(f"Starting processing for page {page_index + 1}")

                # Generate embeddings using ColPali online endpoint (async)
                embeddings = await colpali_client.generate_embeddings(page_chunk)
                page_end_time = time.time()
                page_processing_time = page_end_time - page_start_time

                if embeddings:
                    # ColQwen2 returns structured embeddings with original and pooled versions
                    # Format: [batch_size, num_patches, embedding_dim]
                    original_embeddings = embeddings.get("original_embeddings", [])
                    pooled_embeddings = embeddings.get("pooled_embeddings", [])

                    # Handle batched embeddings: API returns [batch1, batch2, ...] where each batch is [patches...]
                    # For single page processing, we flatten all batches into a single list of patches
                    flattened_embeddings = {}

                    if original_embeddings and len(original_embeddings) > 0:
                        # Flatten batches: concatenate all patches from all batches
                        all_patches = []
                        for batch in original_embeddings:
                            if isinstance(batch, list):
                                all_patches.extend(batch)

                        flattened_embeddings["original_embeddings"] = all_patches
                        num_patches = len(all_patches)
                        patch_dim = len(all_patches[0]) if len(all_patches) > 0 else 0
                    else:
                        num_patches = 0
                        patch_dim = 0

                    if pooled_embeddings and len(pooled_embeddings) > 0:
                        # Flatten batches: concatenate all patches from all batches
                        all_pooled_patches = []
                        for batch in pooled_embeddings:
                            if isinstance(batch, list):
                                all_pooled_patches.extend(batch)

                        flattened_embeddings["pooled_embeddings"] = all_pooled_patches
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
                        "embeddings": flattened_embeddings,  # Pass flattened patches to indexer
                        "source_file": blob_name,
                        "num_patches": num_patches,
                        "patch_dimensions": patch_dim,
                        "pooled_patches": pooled_patches,
                        "pooled_dimensions": pooled_dim,
                    }

                    # Index this page immediately to avoid large batch timeouts
                    try:
                        index_success = await search_indexer.index_embeddings([result])
                        if index_success:
                            logger.info(
                                f"Successfully generated and indexed embeddings for page {page_index + 1}: original ({num_patches} × {patch_dim}), pooled ({pooled_patches} × {pooled_dim}) in {page_processing_time:.2f}s"
                            )
                        else:
                            logger.warning(
                                f"Generated embeddings but failed to index page {page_index + 1}"
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

        # Process all pages concurrently
        embedding_start_time = time.time()
        logger.info(f"Starting parallel processing of {len(document_pages)} pages...")

        page_tasks = [
            process_page(i, page_chunk) for i, page_chunk in enumerate(document_pages)
        ]
        page_results = await asyncio.gather(*page_tasks, return_exceptions=True)

        embedding_end_time = time.time()
        embedding_processing_time = embedding_end_time - embedding_start_time

        # Filter out None results and exceptions
        embeddings_results: List[Dict[str, Any]] = []
        for result in page_results:
            if (
                result is not None
                and not isinstance(result, Exception)
                and isinstance(result, dict)
            ):
                embeddings_results.append(result)

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
