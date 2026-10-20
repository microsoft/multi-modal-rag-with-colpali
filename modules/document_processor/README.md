# Document Processor Service

This service converts PDFs into page images and ColPali embeddings for visual document search. It stores images in Azure Blob Storage and indexes embeddings with metadata in Qdrant.

## Dependency compatibility checks

Use Python 3.12 and the committed lockfile:

```shell
uv sync --locked --no-editable
uv run --locked python -m unittest discover -s tests -v
```

These offline-service tests cover PDF rendering, PNG/JPEG ingestion, transparent
image conversion, DPI downsampling, embedding image payloads, invalid image
rejection, and MSAL assertion signing. They do not call Azure or the inference
service. Transitive security floors are recorded in
`tool.uv.constraint-dependencies`.

## Architecture Overview

Service Bus-driven pipeline for processing documents:

1. **Service Bus Consumer** - Listens for document events with retries
2. **PDF Processing** - Converts PDFs into individual page images
3. **ColPali Integration** - Generates visual embeddings for each page
4. **Image Storage** - Uploads page images to Azure Blob Storage
5. **Vector Indexing** - Stores embeddings in Qdrant with metadata and image URLs
6. **Document Lifecycle** - Handles creation and deletion events

## Concurrency Model

The processor uses a **rolling pipeline**: each page moves through independently, and each client uses a semaphore to limit concurrent operations:

```
┌─────────────────────────────────────────────────────────────────┐
│  Service Bus Message Flow                                       │
│  DOCUMENT_MAX_CONCURRENT = 3 (messages pulled at a time)        │
├─────────────────────────────────────────────────────────────────┤
│  Doc 1: Page1 ──→ Page2 ──→ Page3 ──→ ...                       │
│  Doc 2: Page1 ──→ Page2 ──→ Page3 ──→ ...                       │
│  Doc 3: Page1 ──→ Page2 ──→ Page3 ──→ ...                       │
├─────────────────────────────────────────────────────────────────┤
│  Rolling Pipeline (each page flows independently):              │
│                                                                 │
│  Page 1: ColPali ──────→ Blob ──────→ Qdrant                    │
│  Page 2:    ColPali ──────→ Blob ──────→ Qdrant                 │
│  Page 3:       ColPali ──────→ Blob ──────→ Qdrant              │
│  Page 4:          ColPali ──────→ Blob ──────→ Qdrant           │
│  ...                                                            │
├─────────────────────────────────────────────────────────────────┤
│  Each client controls its own concurrency (singleton handlers): │
│  • COLPALI_MAX_CONCURRENT = 5 (GPU embedding requests)          │
│  • COLPALI_BATCH_SIZE = 1 (pages per request)                   │
│  • BLOB_MAX_CONCURRENT = 250 (blob uploads)                     │
│  • QDRANT_MAX_CONCURRENT = 5 (index writes)                     │
│  • QDRANT_BATCH_SIZE = 20 (points per upsert)                   │
└─────────────────────────────────────────────────────────────────┘
```

### Why Rolling Pipeline?

Pages move to the next stage without waiting for an entire batch to finish. Each stage limits its own concurrency, keeping requests flowing without overwhelming downstream services.

### Concurrency Settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `DOCUMENT_MAX_CONCURRENT` | 3 | Documents processed simultaneously (controlled by receiver) |
| `COLPALI_MAX_CONCURRENT` | 5 | Concurrent GPU embedding requests |
| `COLPALI_BATCH_SIZE` | 1 | Pages per ColPali request |
| `BLOB_MAX_CONCURRENT` | 250 | Azure Blob parallel operations |
| `QDRANT_MAX_CONCURRENT` | 5 | Parallel Qdrant batch writes |
| `QDRANT_BATCH_SIZE` | 20 | Points per Qdrant upsert |

**Note:** Document concurrency is controlled by the Service Bus receiver (not internal semaphore), ensuring messages remain safe in the queue until pulled.

## Qdrant Vector Schema

Uses the **mean_pooling_with_hierarchical_quantized_prefetch_only** index setup:

| Vector | Size | HNSW | Quantization | Storage | Purpose |
|--------|------|------|--------------|---------|--------|
| `hierarchical_pooled` | 128 | Disabled (m=0) | None | On disk | Exact reranking for accuracy |
| `mean_pooled_columns` | 128 | Enabled | Binary (RAM) | Quantized only | Fast column-wise prefetch |
| `mean_pooled_rows` | 128 | Enabled | Binary (RAM) | Quantized only | Fast row-wise prefetch |
