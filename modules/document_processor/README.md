# Document Processor Service

This module provides a containerized document processing pipeline that converts PDF documents into ColPali embeddings for visual document search. Page images are stored in Azure Blob Storage, while vector embeddings with metadata are indexed in Qdrant for efficient similarity search.

## Architecture Overview

Service Bus-driven pipeline for processing documents:

1. **Service Bus Consumer** - Listens for document events with retries
2. **PDF Processing** - Converts PDFs into individual page images
3. **ColPali Integration** - Generates visual embeddings for each page
4. **Image Storage** - Uploads page images to Azure Blob Storage
5. **Vector Indexing** - Stores embeddings in Qdrant with metadata and image URLs
6. **Document Lifecycle** - Handles creation and deletion events

## Concurrency Model

The processor uses a **rolling pipeline** architecture where each page flows through independently, with each client managing its own concurrency via semaphores:

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

Unlike batch-based processing that waits at synchronization points, the rolling pipeline:
- **Maximizes throughput**: As soon as one ColPali request completes, the next starts immediately
- **Eliminates bottlenecks**: No waiting for entire batches to complete
- **Natural backpressure**: Each semaphore independently throttles its stage
- **Better GPU utilization**: ColPali requests flow continuously without gaps

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

Uses the **mean_pooling_with_hierarchical_quantized_prefetch_only** index setup (optimal based on benchmarking):

| Vector | Size | HNSW | Quantization | Storage | Purpose |
|--------|------|------|--------------|---------|--------|
| `hierarchical_pooled` | 128 | Disabled (m=0) | None | On disk | Exact reranking for accuracy |
| `mean_pooled_columns` | 128 | Enabled | Binary (RAM) | Quantized only | Fast column-wise prefetch |
| `mean_pooled_rows` | 128 | Enabled | Binary (RAM) | Quantized only | Fast row-wise prefetch |
