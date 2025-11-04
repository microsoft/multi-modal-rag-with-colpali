# Document Processor Container App

A FastAPI-based container app that processes PDF documents via Event Grid webhooks and creates ColPali embeddings for visual document search.

## What it does

1. **Receives webhooks**: Listens for Event Grid notifications when PDFs are uploaded to or deleted from Azure Storage
2. **Converts to images**: Splits the PDF into page images (configurable DPI and page limits)
3. **Creates embeddings**: Sends images to ColPali endpoint to generate visual embeddings
4. **Stores in search**: Indexes the embeddings in QDRANT vector database for retrieval
5. **Handles deletions**: Removes document pages from the index when files are deleted from storage

## Architecture

This app runs as a Container App and processes documents asynchronously using Event Grid webhooks, providing better scalability and performance compared to the previous Azure Functions approach.

## Key components

- `app.py` - Main FastAPI application with webhook endpoints
- `document_processor.py` - Converts PDFs to images using PyMuPDF
- `colpali_client.py` - Sends images to ColPali ML endpoint
- `search_indexer.py` - Async QDRANT indexer with optimized pooling strategies and non-blocking I/O operations

## Search Service Configuration

The search indexer uses QDRANT vector database:

### Environment Variables

```bash
# Service endpoints
QDRANT_ENDPOINT=https://your-qdrant-endpoint:6333

# Other processing settings
STORAGE_ACCOUNT_NAME=your_storage_account
COLPALI_ENDPOINT=https://your-colpali-endpoint/score
```

## Deployment

The container app is deployed automatically when you run the infrastructure deployment. To deploy just the document processor:

```bash
# Windows
scripts/windows/deploy_document_processor.ps1

# Linux
scripts/linux/deploy_document_processor.sh
```
